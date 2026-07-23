"""Gemini CLI runner — detached per-run executable.

Spawned by `GeminiProvider.start_run` as a subprocess with
`start_new_session=True`. Handles one Gemini CLI run via `gemini -p -o
stream-json`. Parses stream-json events from stdout, normalizes them to
Claude jsonl format, and writes to `session_events.jsonl` so the backend
can tail it.

Life of a run:
  1. Backend creates run dir, writes input.json.
  2. Backend spawns `python runner_gemini.py --run-dir <path>` detached.
  3. This script reads input.json, spawns `gemini -p <prompt> -o stream-json`.
  4. On init event: captures session_id, writes state.json.
  5. Each event is normalized to Claude jsonl shape and appended to
     session_events.jsonl.
  6. On result event: writes complete.json and exits.

Cancel sentinel: backend writes `run_dir/cancel`, runner terminates the
gemini subprocess.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from capability_contexts import prepend_capability_context
from continuation import normalize_context_overflow_error
from runner_guard import (
    GHOST_RETRY_BACKOFF_S,
    GHOST_RETRY_MAX,
    apply_ghost_completion_guard,
    should_retry_ghost,
)
from builtin_mcp_config import native_mcp_runtime_env, with_builtin_mcp_servers
from runs_dir import atomic_write_json
from env_compat import dual_env_many, get_env
from provider_run_config import symlink_home_overlay, write_skill_tree
from runtime_skills import has_runtime_skills, materialize_runtime_skills
from proc_control import process_control as _process_control
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES

logger = logging.getLogger(__name__)


def _gemini_terminal_error(raw_event: dict) -> Optional[str]:
    err = _extract_error_message(raw_event.get("error"))
    if err:
        return normalize_context_overflow_error(err) or err
    if raw_event.get("status") != "success" and raw_event.get("stopReason") == "max_tokens":
        return "context_window_exceeded"
    return None


def _materialize_gemini_run_home(
    run_dir: Path,
    provider_run_config: dict,
    *,
    cwd: str,
    bare_config: bool = False,
) -> Optional[dict[str, str]]:
    mcp_servers = provider_run_config.get("mcp_servers") or {}
    skills = provider_run_config.get("skills") or {}
    has_ext_skills = has_runtime_skills(cwd, bare_config=bare_config)
    if not mcp_servers and not skills and not has_ext_skills:
        return None

    real_home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()).expanduser()
    overlay_home = run_dir / "gemini-home"
    symlink_home_overlay(real_home, overlay_home, skip={"settings.json", ".gemini", ".agents"})
    symlink_home_overlay(real_home / ".gemini", overlay_home / ".gemini", skip={"settings.json", "skills"})
    symlink_home_overlay(real_home / ".agents", overlay_home / ".agents", skip={"skills"})

    ext_count = materialize_runtime_skills(
        overlay_home / ".gemini" / "skills", cwd, bare_config=bare_config
    )
    materialize_runtime_skills(
        overlay_home / ".agents" / "skills", cwd, bare_config=bare_config
    )

    settings = _load_json_object(real_home / ".gemini" / "settings.json")
    if mcp_servers:
        settings["mcpServers"] = mcp_servers
    if skills or ext_count:
        settings["skills"] = {"enabled": True}

    if settings:
        (overlay_home / "settings.json").write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        gemini_dir = overlay_home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        (gemini_dir / "settings.json").write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if skills:
        write_skill_tree(overlay_home / ".gemini" / "skills", skills)
        write_skill_tree(overlay_home / ".agents" / "skills", skills)

    return {"GEMINI_CLI_HOME": str(overlay_home)}


def _load_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _with_communicate_mcp(inputs: dict, provider_run_config: dict) -> dict:
    import installation_profile
    if not installation_profile.integrations_enabled():
        return provider_run_config
    sender_session_id = str(
        inputs.get("mssg_sender_session_id") or inputs.get("app_session_id") or ""
    ).strip()
    backend_url = str(
        inputs.get("backend_url")
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    internal_token = str(inputs.get("internal_token") or "").strip()
    if not sender_session_id or not backend_url or not internal_token:
        return provider_run_config
    disabled_tools = [
        str(item).strip()
        for item in (inputs.get("disabled_builtin_tools") or [])
        if str(item or "").strip() in {
            "ask",
            "create_session",
            "create_sub_session",
            "delegate_task",
            "mssg",
        }
    ]

    config = {
        **provider_run_config,
        "mcp_servers": dict(provider_run_config.get("mcp_servers") or {}),
    }
    script = Path(__file__).with_name("communicate_mcp.py")
    if getattr(sys, "frozen", False):
        command = sys.executable
        args = ["--communicate-mcp"]
    else:
        command = sys.executable
        args = [str(script)]
    config["mcp_servers"]["communicate"] = {
        "command": command,
        "args": args,
        "env": dual_env_many({
            "BETTER_CLAUDE_BACKEND_URL": backend_url,
            "BETTER_CLAUDE_INTERNAL_TOKEN": internal_token,
            "BETTER_CLAUDE_MSSG_SENDER_SESSION_ID": sender_session_id,
            # ask(run_mode='fork') + create_worker need these to build the
            # delegation / worker-creation payload. The manager session id is
            # already passed as MSSG_SENDER_SESSION_ID and doubles as the
            # fork caller's app_session_id.
            "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
            "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
            "BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS": ",".join(sorted(set(disabled_tools))),
        }),
    }
    return config


def _resolve_gemini_cli() -> Optional[str]:
    """Find the gemini CLI binary."""
    from cli_paths import resolve_cli_binary

    return resolve_cli_binary("gemini")


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ============================================================================
# Tool name mapping — Gemini → Claude
# ============================================================================
# Mapping gemini's tool names to claude's so the existing
# ToolCall.tsx icon/diff/expanding code paths render gemini tool_uses
# identically to claude's. Anything not in this table passes through
# verbatim (rendered as a generic tool card). When the gemini CLI adds
# a new built-in, extend the map — and prefer claude's canonical name
# so one branch of frontend rendering covers both providers.
_TOOL_NAME_MAP = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "read_many_files": "Read",
    "write_file": "Write",
    "replace": "Edit",
    "grep_search": "Grep",
    "glob_search": "Glob",
    "list_directory": "LS",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "invoke_agent": "Task",
    "activate_skill": "Skill",
    "update_topic": "TodoWrite",
}


# Mapping gemini tool-input keys to claude's canonical input schema.
# Per-tool because each tool has a different key namespace. INVARIANT:
# only translates KEYS — values pass through. Keys not listed for a
# tool are forwarded verbatim. Lets the frontend's claude-shaped
# renderers (BashToolCall reads `command`, EditToolCall reads
# `file_path`/`old_string`/`new_string`, etc.) light up for gemini.
_TOOL_INPUT_KEY_MAP = {
    "Bash":      {"shell_command": "command", "cmd": "command"},
    "Read":      {"path": "file_path"},
    "Write":     {"path": "file_path", "contents": "content"},
    "Edit":      {"path": "file_path", "old": "old_string", "new": "new_string"},
    "Grep":      {"pattern": "pattern", "dir_path": "path"},
    "Glob":      {"pattern": "pattern"},
    "LS":        {"dir_path": "path", "directory": "path"},
    "WebFetch":  {"url": "url"},
    "WebSearch": {"query": "query"},
}


def _map_tool(raw_name: str, raw_input: dict) -> tuple[str, dict]:
    """Return (claude_tool_name, claude_input_dict) for a gemini tool_use.
    Unmapped tool names and unmapped input keys pass through verbatim
    so a new gemini tool still renders as a card with raw fields."""
    claude_name = _TOOL_NAME_MAP.get(raw_name, raw_name)
    if not isinstance(raw_input, dict):
        return claude_name, {"value": raw_input}
    key_map = _TOOL_INPUT_KEY_MAP.get(claude_name, {})
    mapped = {key_map.get(k, k): v for k, v in raw_input.items()}
    return claude_name, mapped


# ============================================================================
# Event normalization — Gemini stream-json → Claude jsonl shape
# ============================================================================
def _normalize_init(raw: dict) -> Optional[dict]:
    """Init events don't become jsonl lines — handled separately."""
    return None


def _normalize_message(raw: dict, parent_uuid: str) -> Optional[dict]:
    return _normalize_message_with_uuid(raw, parent_uuid, _new_uuid())


def _normalize_message_with_uuid(raw: dict, parent_uuid: str, msg_uuid: str) -> Optional[dict]:
    role = raw.get("role")
    content = raw.get("content", "")

    if role == "assistant":
        # Strip [Thought: true] marker if present (gemini-cli 0.42+)
        if isinstance(content, str):
            content = content.replace("[Thought: true]", "").strip()

        # Resolve the actual model used: gemini emits it on `init`, not
        # per-message; `init.model` is captured into module-level
        # `_resolved_model` so each message carries the right id (e.g.
        # `gemini-3.1-pro-preview`) rather than a generic "gemini".
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": content}],
                "model": raw.get("model") or _resolved_model or "gemini",
            },
            "uuid": msg_uuid,
            "parentUuid": parent_uuid,
            "timestamp": raw.get("timestamp", datetime.now().isoformat()),
        }
    # Skip user echo messages
    return None


# Resolved at first `init` event; threads the real model id (e.g.
# `gemini-3.1-pro-preview`) into every subsequent normalized assistant
# message so `message.model` matches claude's per-message attribution.
_resolved_model: Optional[str] = None


def _normalize_tool_use(raw: dict, parent_uuid: str) -> dict:
    name, mapped_input = _map_tool(
        raw.get("tool_name", "unknown"),
        raw.get("parameters") or {},
    )
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": raw.get("tool_id", _new_uuid()),
                "name": name,
                "input": mapped_input,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


def _normalize_tool_result(raw: dict, parent_uuid: str) -> dict:
    return _normalize_tool_result_with_uuid(raw, parent_uuid, _new_uuid())


def _normalize_tool_result_with_uuid(raw: dict, parent_uuid: str, msg_uuid: str) -> dict:
    output = raw.get("output", "")
    error = raw.get("error")
    if error:
        output = f"Error: {error.get('message', str(error)) if isinstance(error, dict) else error}"
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": raw.get("tool_id", ""),
                "content": output or "",
            }],
        },
        "uuid": msg_uuid,
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


def _normalize_error(raw: dict, parent_uuid: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": f"Error: {raw.get('message', 'unknown error')}",
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
        "isStreamError": True,
    }


def _normalize_unknown(raw: dict, parent_uuid: str) -> dict:
    """Surface a stream-json event whose `type` we don't know HOW to
    interpret. We still emit it — wrapped as an `agent_message` with
    an `unknown_event` data type — so the frontend renders a
    diagnostic card instead of pretending the event never happened.
    INVARIANT: every byte gemini emits is either normalized to a
    structured shape OR surfaced verbatim through this path. No silent
    drops. Same contract on the claude side is enforced by the
    frontend's DiagnosticEvent fallback."""
    return {
        "type": "unknown_event",
        "raw_type": raw.get("type"),
        "raw": raw,
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


# -- Gemini native event types (entity_id-based) --

def _normalize_assistant_text(raw: dict, parent_uuid: str) -> Optional[dict]:
    """Gemini assistant_text: thinking/reasoning text."""
    text = raw.get("text", "")
    if not text:
        return None
    # Strip [Thought: true] marker if present (gemini-cli 0.42+)
    text = text.replace("[Thought: true]", "").strip()
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": _resolved_model or "gemini",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


def _normalize_tool_code(raw: dict, parent_uuid: str) -> dict:
    """Gemini tool_code: tool invocation (maps to standard tool_use)."""
    name, mapped_input = _map_tool(
        raw.get("tool_name", "unknown"),
        raw.get("tool_input") or {},
    )
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": raw.get("tool_id", _new_uuid()),
                "name": name,
                "input": mapped_input,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


def _normalize_tool_output_event(raw: dict, parent_uuid: str) -> dict:
    """Gemini tool_output: tool result (maps to standard tool_result)."""
    output = raw.get("output", "")
    error = raw.get("error")
    if error:
        output = f"Error: {error.get('message', str(error)) if isinstance(error, dict) else error}"
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": raw.get("tool_id", ""),
                "content": output or "",
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


_NORMALIZERS = {
    "init": _normalize_init,
    "message": _normalize_message,
    "tool_use": _normalize_tool_use,
    "tool_result": _normalize_tool_result,
    "error": _normalize_error,
    "assistant_text": _normalize_assistant_text,
    "tool_code": _normalize_tool_code,
    "tool_output": _normalize_tool_output_event,
}


def _normalize_event(raw: dict, parent_uuid: str) -> Optional[dict]:
    event_type = raw.get("type")
    normalizer = _NORMALIZERS.get(event_type)
    if normalizer is None:
        # Surface unknown event types as a diagnostic event the
        # frontend can render — never silently drop. Also logs the
        # type once via the caller's _log_unknown_event helper so a
        # new gemini-cli release surfaces in CI.
        return _normalize_unknown(raw, parent_uuid)
    if event_type == "init":
        return _normalize_init(raw)
    return normalizer(raw, parent_uuid)


_unknown_event_types_seen: set[str] = set()


def _log_unknown_event(log: logging.Logger, etype: str) -> None:
    """Log each unknown stream-json event type once. Cheaper than
    flooding logs on every line, loud enough that a new event type
    surfaces in CI."""
    if etype in _unknown_event_types_seen:
        return
    _unknown_event_types_seen.add(etype)
    log.warning("runner_gemini: unknown stream-json event type %r — dropping silently. Add a normalizer if it carries data.", etype)


def _extract_error_message(err: Any) -> Optional[str]:
    """Unified error extractor for both 'result' and 'error' events."""
    if not err:
        return None
    if isinstance(err, dict):
        return err.get("message") or err.get("error") or str(err)
    return str(err)


import re


_STACK_FRAME_RE = re.compile(r"^\s+at\s+")
_NAMED_ERROR_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*Error:")


def _extract_stderr_error(stderr_text: str) -> Optional[str]:
    for raw_line in stderr_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _NAMED_ERROR_RE.search(line):
            return normalize_context_overflow_error(line) or line

    for raw_line in reversed(stderr_text.splitlines()):
        line = raw_line.strip()
        if not line or _STACK_FRAME_RE.match(line):
            continue
        if line in {"}", "]", "["}:
            continue
        return normalize_context_overflow_error(line) or line
    return None

_NETWORK_ERROR_PATTERN = re.compile(
    r"(?:"
    r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|"
    r"ENOTFOUND|EAI_NONAME|getaddrinfo|could not resolve|"
    r"socket hang up|network error|"
    r"connect ETIMEDOUT|connect ECONNREFUSED|"
    r"TLS handshake|SSL handshake|"
    r"HTTP 50[23]|HTTP 429|"
    r"rate.?limit|overloaded|temporarily unavailable|"
    r"service unavailable|bad gateway"
    r")",
    re.IGNORECASE,
)


def _is_network_error_message(msg: str) -> bool:
    """Check if an error message indicates a transient network failure."""
    return bool(_NETWORK_ERROR_PATTERN.search(msg))


def _sum_usage(a: Optional[dict], b: Optional[dict]) -> dict:
    out: dict[str, int] = {}
    for d in ((a or {}), (b or {})):
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)):
                out[k] = int(out.get(k, 0)) + int(v)
    return out


# ============================================================================
# Image attachments
# ============================================================================
def _materialize_attachments(run_dir: Path, images: list) -> list[Path]:
    """Decode base64 image attachments to disk under run_dir/attachments.

    Returns the absolute file paths. The gemini CLI's headless path
    (`runNonInteractive` → `handleAtCommand`) resolves `@path` references
    via read_many_files, which emits inlineData parts for image mime
    types — that is the only supported way to attach images to a gemini
    `-p` invocation.
    """
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        ext = img["media_type"].split("/")[-1].replace("jpeg", "jpg")
        fpath = att_dir / f"attachment_{i}.{ext}"
        fpath.write_bytes(base64.b64decode(img["data"]))
        paths.append(fpath)
    return paths


def _apply_image_attachments(
    run_dir: Path, prompt: Optional[str], images: list
) -> tuple[Optional[str], Optional[Path]]:
    """Fold image attachments into the gemini prompt.

    Materializes images to disk and appends a `@path` reference for each
    so the CLI's headless `handleAtCommand` inlines them as image parts.
    Returns (prompt_with_refs, attachment_dir). attachment_dir is None
    when there are no images; callers add it via `--include-directories`
    so the absolute `@path` resolves inside a trusted workspace dir.
    """
    if not images:
        return prompt, None
    paths = _materialize_attachments(run_dir, images)
    at_refs = "\n".join(f"@{p}" for p in paths)
    new_prompt = f"{prompt}\n\n{at_refs}" if prompt else at_refs
    return new_prompt, paths[0].parent


def _prepend_capability_context(prompt: str, inputs: dict) -> str:
    return prepend_capability_context(prompt, inputs)


# ============================================================================
# Main async runner
# ============================================================================
async def _run(run_dir: Path, inputs: dict) -> int:
    global _resolved_model
    log = logging.getLogger("runner_gemini")

    prompt = inputs.get("prompt")
    images = inputs.get("images") or []
    files = inputs.get("files") or []
    cwd = inputs.get("cwd")
    if not cwd:
        _fail(run_dir, "missing required field: cwd")
        return 1
    if not prompt and not images and not files:
        _fail(run_dir, "missing required field: prompt")
        return 1
    prompt = _prepend_capability_context(prompt or "", inputs)

    # Inject file contents into the prompt for non-image attachments.
    if files:
        import base64 as _b64
        file_sections: list[str] = []
        for f in files:
            try:
                raw = _b64.b64decode(f.get("data", ""))
                name = f.get("name", "unknown")
            except Exception:
                log.warning("Skipping malformed file attachment: %s", f.get("name", "?"))
                continue
            try:
                text = raw.decode("utf-8")
                file_sections.append(
                    f"<file name=\"{name}\">\n{text}\n</file>"
                )
            except UnicodeDecodeError:
                file_sections.append(
                    f"<file name=\"{name}\">[binary file, {f.get('size', len(raw))} bytes]</file>"
                )
        file_preamble = "\n\n".join(file_sections)
        prompt = f"{file_preamble}\n\n{prompt}" if prompt else file_preamble

    # Image attachments: materialize to disk and reference via `@path`.
    prompt, attachment_dir = _apply_image_attachments(run_dir, prompt, images)

    model = inputs.get("model")
    session_id = inputs.get("session_id")
    provider_run_config = with_builtin_mcp_servers(
        inputs,
        _with_communicate_mcp(
            inputs,
            inputs.get("provider_run_config") or {},
        ),
    )
    run_env = os.environ.copy()
    run_env.update(native_mcp_runtime_env(inputs))
    scoped_env = _materialize_gemini_run_home(
        run_dir,
        provider_run_config,
        cwd=cwd,
        bare_config=bool(inputs.get("bare_config")),
    )
    if scoped_env:
        run_env.update(scoped_env)

    gemini_bin = _resolve_gemini_cli()
    if not gemini_bin:
        _fail(run_dir, "gemini CLI not found on PATH")
        return 1

    _permission = inputs.get("permission") or {}
    # Gemini has NO headless interactive-approval channel: in `-p` mode the
    # CLI throws ("requires user confirmation, which is not supported in
    # non-interactive mode") before emitting any answerable event. So unlike
    # Claude/Codex there is no tool_approval round-trip here — only the
    # headless-safe modes (yolo / auto_edit / plan) are exposed (see
    # permission.GEMINI_APPROVAL_MODES). "default" is intentionally absent.
    _approval_mode = (
        _permission.get("mode") if isinstance(_permission, dict) else None
    ) or "yolo"
    cmd: list[str] = [gemini_bin, "--approval-mode", _approval_mode, "--skip-trust", "-p", "-", "-o", "stream-json"]
    cmd += ["--include-directories", "/"]
    if attachment_dir:
        cmd += ["--include-directories", str(attachment_dir)]
    if model:
        cmd += ["-m", model]
    if session_id:
        cmd += ["-r", session_id]

    state: dict = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": None,
        "jsonl_path": None,
        "complete": False,
    }
    state_path = run_dir / "state.json"

    events_path = run_dir / "session_events.jsonl"

    # Network retry: infinite retry on transient failures.
    _retry_backoff = 2.0
    _ghost_attempts = 0
    _accumulated_usage: dict = {}
    _cancel_path = run_dir / "cancel"

    async def _retry_sleep(seconds: float) -> None:
        """Sleep with cancel sentinel check every 0.5s."""
        import time as _time
        deadline = _time.monotonic() + seconds
        while _time.monotonic() < deadline:
            if _cancel_path.exists():
                raise asyncio.CancelledError()
            await asyncio.sleep(min(0.5, deadline - _time.monotonic()))

    while True:
        # Per-attempt state — reset on each retry
        discovered_sid: Optional[str] = None
        parent_uuid = _new_uuid()
        total_usage: dict = {}
        success = False
        error: Optional[str] = None
        cancelled = False
        result_seen = False
        assistant_seen = False

        current_content: dict[str, str] = {}
        current_uuids: dict[str, str] = {}

        _resolved_model = None

        state["session_id"] = None
        state["jsonl_path"] = None
        state["complete"] = False
        try:
            if events_path.exists():
                events_path.unlink()
        except OSError:
            pass
        # Also clean up raw tool events and stderr from previous attempt
        try:
            _raw_tool_events = run_dir / "gemini_tool_events_raw.jsonl"
            if _raw_tool_events.exists():
                _raw_tool_events.unlink()
        except OSError:
            pass
        try:
            _stderr_log = run_dir / "gemini_stderr.log"
            if _stderr_log.exists():
                _stderr_log.unlink()
        except OSError:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=run_env,
                **_process_control().detach_spawn_kwargs(),
                limit=SUBPROCESS_LINE_LIMIT_BYTES,
            )

            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

            cancel_seen = asyncio.Event()
            cancel_path = run_dir / "cancel"

            async def _drain_stderr() -> None:
                try:
                    with (run_dir / "gemini_stderr.log").open("ab") as f:
                        while True:
                            chunk = await proc.stderr.read(8192)
                            if not chunk:
                                return
                            f.write(chunk)
                            f.flush()
                except Exception:
                    log.exception("gemini stderr drain failed")

            stderr_task = asyncio.create_task(_drain_stderr())

            async def _cancel_watcher() -> None:
                import signal
                nonlocal cancelled
                while not cancel_seen.is_set():
                    if cancel_path.exists():
                        cancelled = True
                        log.info("cancel sentinel seen, terminating gemini tree")
                        _pc = _process_control()
                        _pc.signal_stop(proc.pid)
                        for _ in range(30):
                            if proc.returncode is not None:
                                break
                            await asyncio.sleep(0.1)
                        if proc.returncode is None:
                            _pc.force_kill(proc.pid)
                        cancel_seen.set()
                        return
                    try:
                        await asyncio.wait_for(cancel_seen.wait(), timeout=0.15)
                    except asyncio.TimeoutError:
                        pass

            cancel_task = asyncio.create_task(_cancel_watcher())

            try:
                with events_path.open("a", encoding="utf-8") as events_file:
                    async for raw_line in proc.stdout:
                        if cancelled:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            raw_event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        etype = raw_event.get("type")

                        if etype in ("tool_result", "tool_output"):
                            try:
                                with (run_dir / "gemini_tool_events_raw.jsonl").open("a", encoding="utf-8") as _f:
                                    _f.write(json.dumps(raw_event) + "\n")
                            except Exception:
                                pass

                        if etype == "init":
                            sid = raw_event.get("session_id")
                            if sid:
                                discovered_sid = sid
                                state["session_id"] = sid
                                state["jsonl_path"] = str(events_path)
                                atomic_write_json(state_path, state)
                            init_model = raw_event.get("model")
                            if init_model:
                                _resolved_model = init_model
                            continue

                        if etype == "result":
                            result_seen = True
                            success = raw_event.get("status") == "success"
                            stats = raw_event.get("stats") or {}
                            if stats:
                                total_usage = {
                                    "input_tokens": stats.get("input_tokens", 0),
                                    "output_tokens": stats.get("output_tokens", 0),
                                    "cache_read_input_tokens": stats.get("cached", 0),
                                    "total_tokens": stats.get("total_tokens", 0),
                                    "duration_ms": stats.get("duration_ms"),
                                }
                            err = _gemini_terminal_error(raw_event)
                            if err:
                                error = err
                            elif success:
                                error = None
                            break

                        if etype == "error":
                            new_err = _extract_error_message(raw_event)
                            if new_err and not error:
                                error = normalize_context_overflow_error(new_err) or new_err

                        normalized = None
                        if etype == "message" and raw_event.get("role") == "assistant":
                            role = "assistant"
                            if role not in current_uuids:
                                current_uuids[role] = _new_uuid()
                                current_content[role] = ""

                            current_content[role] += raw_event.get("content", "")
                            if current_content[role].strip():
                                assistant_seen = True

                            mod_event = dict(raw_event)
                            mod_event["content"] = current_content[role]
                            normalized = _normalize_message_with_uuid(mod_event, parent_uuid, current_uuids[role])

                        elif etype == "assistant_text":
                            role = "assistant_text"
                            if role not in current_uuids:
                                current_uuids[role] = _new_uuid()
                                current_content[role] = ""

                            current_content[role] += raw_event.get("text", "")
                            clean_text = current_content[role].replace("[Thought: true]", "").strip()
                            normalized = {
                                "type": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "thinking", "thinking": clean_text}],
                                    "model": _resolved_model or "gemini",
                                },
                                "uuid": current_uuids[role],
                                "parentUuid": parent_uuid,
                                "timestamp": raw_event.get("timestamp", datetime.now().isoformat()),
                            }

                        elif etype == "tool_result":
                            role = "tool_result_" + str(raw_event.get("tool_id", "default"))
                            if role not in current_uuids:
                                current_uuids[role] = _new_uuid()
                                current_content[role] = ""

                            current_content[role] += raw_event.get("output", "")

                            mod_event = dict(raw_event)
                            mod_event["output"] = current_content[role]
                            normalized = _normalize_tool_result_with_uuid(mod_event, parent_uuid, current_uuids[role])

                        elif etype == "tool_output":
                            role = "tool_output_" + str(raw_event.get("tool_id", "default"))
                            if role not in current_uuids:
                                current_uuids[role] = _new_uuid()
                                current_content[role] = ""

                            current_content[role] += raw_event.get("output", "")
                            normalized = _normalize_tool_result_with_uuid(
                                {"output": current_content[role], "tool_id": raw_event.get("tool_id", "")},
                                parent_uuid, current_uuids[role],
                            )

                        else:
                            normalized = _normalize_event(raw_event, parent_uuid)
                            if normalized is None and etype not in _NORMALIZERS:
                                _log_unknown_event(log, etype or "(missing)")

                        if normalized is not None:
                            events_file.write(json.dumps(normalized) + "\n")
                            events_file.flush()
                            new_uuid = normalized.get("uuid")
                            if new_uuid and etype not in ("message", "assistant_text", "tool_result", "tool_output"):
                                parent_uuid = new_uuid

            finally:
                cancel_seen.set()
                if not cancel_task.done():
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass

            await proc.wait()

            try:
                await asyncio.wait_for(stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stderr_task.cancel()

            if proc.returncode != 0 and not error and not cancelled:
                try:
                    stderr_log = run_dir / "gemini_stderr.log"
                    if stderr_log.exists():
                        error = _extract_stderr_error(stderr_log.read_text(encoding="utf-8"))
                    if not error:
                        error = f"Gemini CLI exited with code {proc.returncode}"
                except Exception as e:
                    log.error(f"failed to extract error from stderr: {e}")
                    error = f"Gemini CLI exited with code {proc.returncode}"

            if not result_seen and not error and not cancelled:
                tail_msg: Optional[str] = None
                try:
                    if events_path.exists():
                        raw_lines = events_path.read_text(encoding="utf-8").splitlines()
                        for raw in reversed(raw_lines):
                            if not raw.strip():
                                continue
                            try:
                                ev = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            msg = ev.get("message") or {}
                            content = msg.get("content")
                            if isinstance(content, list):
                                for part in reversed(content):
                                    if isinstance(part, dict):
                                        txt = part.get("text") or part.get("error")
                                        if isinstance(txt, str) and txt.strip():
                                            tail_msg = txt.strip()
                                            break
                            elif isinstance(content, str) and content.strip():
                                tail_msg = content.strip()
                            if tail_msg:
                                break
                except Exception:
                    log.exception("failed to scan session_events.jsonl for tail diagnostic")
                base = "Gemini CLI exited without emitting a result event"
                error = f"{base}: {tail_msg}" if tail_msg else base

        except asyncio.CancelledError:
            error = "cancelled"
        except Exception as e:
            log.exception("Gemini runner failed")
            error = f"{type(e).__name__}: {e}"

        # Gemini CLI can report status: "success" while the actual content
        # is an API error (e.g. quota exhaustion with no 4xx code). Scan the
        # accumulated assistant content and flip success when this happens.
        # Runs per-attempt so a retry starts from a clean classification.
        if success and not error and not cancelled:
            all_text = " ".join(current_content.get(k, "") for k in ("assistant", "assistant_text"))
            if re.search(r"API Error:", all_text):
                error = normalize_context_overflow_error(all_text.strip()) or all_text.strip()
                success = False

        # Ghost-completion guard (parity with Claude + Codex runners): a
        # zero-usage success with no assistant output for a non-empty prompt
        # is a provider ghost completion, not a real success. Applied inside
        # the loop so a prompt_not_executed result can be retried — the
        # provider intermittently swallows an empty/failed upstream response
        # as a successful zero-usage turn, and a fresh attempt usually
        # succeeds.
        success, error = apply_ghost_completion_guard(
            success=success,
            cancelled=cancelled,
            error=error,
            prompt=prompt,
            assistant_seen=assistant_seen,
            total_usage=total_usage,
            result_seen=result_seen,
        )

        # Network retry check: if the error looks transient, retry
        if error and not cancelled and _is_network_error_message(error):
            # Accumulate usage from failed attempt before resetting
            if total_usage:
                _accumulated_usage = _sum_usage(_accumulated_usage, total_usage)
            log.warning(
                "gemini network error, retry %.1fs: %s", _retry_backoff, error,
            )
            await _retry_sleep(_retry_backoff)
            _retry_backoff = min(_retry_backoff * 2, 60.0)
            continue

        # Ghost-completion retry (bounded): prompt_not_executed is
        # transient — retry a few times before failing the turn.
        if should_retry_ghost(error, cancelled=cancelled, attempts=_ghost_attempts):
            _ghost_attempts += 1
            log.warning(
                "gemini ghost completion (prompt_not_executed); "
                "retry %d/%d after %.1fs",
                _ghost_attempts, GHOST_RETRY_MAX, GHOST_RETRY_BACKOFF_S,
            )
            await _retry_sleep(GHOST_RETRY_BACKOFF_S)
            continue

        # Accumulate usage across all attempts
        total_usage = _sum_usage(_accumulated_usage, total_usage)
        _retry_backoff = 2.0
        break

    if cancelled and not error:
        error = "cancelled"

    final_success = success and not cancelled and not error

    # Emit the error as a regular assistant text event (NOT via
    # _normalize_error, which sets isStreamError=True and gets
    # skipped by extract_output_text). The error IS the run's final
    # answer — it must survive content derivation so msg.content is
    # populated rather than left empty.
    if error and not final_success:
        try:
            error_event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Error: {error}"}],
                    "model": _resolved_model or "gemini",
                },
                "uuid": _new_uuid(),
                "parentUuid": parent_uuid,
                "timestamp": datetime.now().isoformat(),
                "isApiErrorMessage": True,
            }
            with events_path.open("a", encoding="utf-8") as ef:
                ef.write(json.dumps(error_event) + "\n")
                ef.flush()
        except Exception:
            log.exception("failed to emit error event to session_events.jsonl")

    complete = {
        "success": final_success,
        "session_id": discovered_sid,
        "error": error,
        "token_usage": total_usage or None,
        "finished_at": datetime.now().isoformat(),
    }
    try:
        (run_dir / "complete.json").write_text(json.dumps(complete, indent=2), encoding="utf-8")
    except Exception:
        log.exception("failed to write complete.json")

    state["complete"] = True
    state["finished_at"] = complete["finished_at"]
    if discovered_sid and not state.get("session_id"):
        state["session_id"] = discovered_sid
    try:
        atomic_write_json(state_path, state)
    except Exception:
        log.exception("failed to finalize state.json")

    return 0 if final_success else 1


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_gemini fatal: %s", error)
    try:
        (run_dir / "complete.json").write_text(json.dumps({
            "success": False,
            "session_id": None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("failed to write error complete.json")


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_gemini %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("runner_gemini").info("starting for run_dir=%s", run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as e:
        _fail(run_dir, f"failed to read input.json: {e}")
        return 1

    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as e:
        logger.exception("runner_gemini top-level failure")
        _fail(run_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
