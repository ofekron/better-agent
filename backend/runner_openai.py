"""runner_openai — BA-owned agent loop over an OpenAI Chat Completions endpoint.

Unlike the claude/gemini/codex runners (which spawn an external CLI that owns
the tool/MCP/approval loop), this runner IS the agent host: it makes HTTP
Chat Completions calls itself and executes tools in-process. There is no
external CLI subprocess.

It plugs into the SAME event/recovery/render-tree funnel as gemini: it writes
only `run_dir/session_events.jsonl` (Claude-shaped lines), `state.json`, and
`complete.json`. The provider (provider_openai.py) tails session_events.jsonl
with GeminiJsonlTailer and feeds apply_event.

OpenAI is the clean path where BA owns the internals instead of adapting to
provider-native CLI quirks:
  - native and team/manager mode both run through this in-process loop.
  - in-process coding tools: Bash, Read, Write, Edit, Grep, Glob.
  - BA loopback tools: mssg, ask, delegate_task, create_session,
    create_sub_session, create_worker, ensure_named_worker, open_file_panel,
    request_user_input, start_file_discussion, and scoped capability load/release.
  - fork is a pure history copy to a fresh BA-owned OpenAI agent session.
  - steering is appended as the next user message before the next model round.
  - text, file, and image attachments are encoded directly for Chat
    Completions.
  - permission: honor the run's permission mode; gate risky tools behind the
    backend tool-approval round-trip (POST /api/internal/tool-approvals/request)
    unless the mode is bypass.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN,
    normalize_ask_mode,
)
from orchestration_tool_descriptions import (
    ASK_DESCRIPTION as _ASK_DESCRIPTION,
    CREATE_SESSION_DESCRIPTION as _CREATE_SESSION_DESCRIPTION,
    CREATE_SUB_SESSION_DESCRIPTION as _CREATE_SUB_SESSION_DESCRIPTION,
    CREATE_WORKER_DESCRIPTION as _CREATE_WORKER_DESCRIPTION,
    DELEGATE_TASK_DESCRIPTION as _DELEGATE_TASK_DESCRIPTION,
    ENSURE_NAMED_WORKER_DESCRIPTION as _ENSURE_NAMED_WORKER_DESCRIPTION,
    MSSG_DESCRIPTION as _MSSG_DESCRIPTION,
)
from orchestration_tool_schemas import (
    DELEGATE_TASK_INPUT_SCHEMA as _DELEGATE_TASK_INPUT_SCHEMA,
)
from capability_contexts import prepend_capability_context, render_capability_context
from tool_approval_client import describe_tool_call, request_tool_approval

logger = logging.getLogger("runner_openai")

_BASH_TIMEOUT_S = 120
_MAX_OUTPUT_CHARS = 40_000
# Safety bound on the agent tool loop. runner_openai IS the agent host (no
# external CLI like claude/codex/gemini to impose its own limits), so it needs
# an in-process runaway guard. High enough that agentic models (e.g. Sakana
# Fugu, a tool-heavy multi-agent system that routinely needs >40 tool rounds)
# finish naturally; overridable per-run via inputs["max_tool_loops"]. When the
# cap IS hit, the turn is reported as not-completed (see _run's for/else),
# never silently as a successful completion.
_MAX_TOOL_LOOPS = 1000
_SESSIONS_SUBDIR = "openai_sessions"
DELEGATE_HTTP_TIMEOUT_S = 24 * 60 * 60
_OPEN_FILE_PANEL_HTTP_TIMEOUT_S = 30.0
_TOOL_ENV_DENY_EXACT = {
    "better_claude_internal_token",
    "better_agent_internal_token",
}
_TOOL_ENV_DENY_PREFIXES = ("anthropic", "openai")


# --------------------------------------------------------------------------
# small utils
# --------------------------------------------------------------------------

def _now_iso() -> str:
    # localtime with offset, stable enough for event timestamps.
    import datetime
    return datetime.datetime.now().astimezone().isoformat()


def _new_uuid() -> str:
    return uuid.uuid4().hex


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _fail(run_dir: Path, error: str) -> None:
    """Write a failure complete.json so the provider's _watch_complete finalizes."""
    complete = {
        "success": False,
        "session_id": None,
        "error": error,
        "token_usage": None,
        "finished_at": _now_iso(),
    }
    try:
        (run_dir / "complete.json").write_text(
            json.dumps(complete, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("failed to write failure complete.json")


# --------------------------------------------------------------------------
# session message history (BA-owned conversation store)
# --------------------------------------------------------------------------

def _sessions_root() -> Path:
    from paths import ba_home
    root = ba_home() / _SESSIONS_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_path(agent_session_id: str) -> Path:
    # agent_session_id is untrusted-shaped; confine to a single filename.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", agent_session_id)
    return _sessions_root() / f"{safe}.json"


def _load_history(agent_session_id: Optional[str]) -> tuple[str, list[dict]]:
    """Return (session_id, messages). Fresh if no prior history."""
    if agent_session_id:
        loaded = _load_history_file(agent_session_id)
        if loaded is not None:
            return loaded
    sid = agent_session_id or _new_uuid()
    return sid, []


def _load_history_file(agent_session_id: str) -> Optional[tuple[str, list[dict]]]:
    p = _session_path(agent_session_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        sid = str(data.get("session_id") or agent_session_id)
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ValueError("messages is not a list")
        return sid, list(messages)
    except Exception:
        logger.exception("corrupt openai history %s; starting fresh", p)
        return None


def _load_history_for_run(resume_sid: Optional[str], *, fork: bool) -> tuple[str, list[dict]]:
    """Load run history.

    Normal resume continues the same BA-owned OpenAI agent session. A fork
    copies the parent history into a fresh session id so the branch is isolated
    from the parent's durable context without any provider-native hack.
    """
    if fork:
        child_sid = _new_uuid()
        if not resume_sid:
            return child_sid, []
        loaded = _load_history_file(str(resume_sid))
        if loaded is None:
            raise FileNotFoundError(f"cannot fork missing openai history: {resume_sid}")
        _parent_sid, parent_messages = loaded
        return child_sid, copy.deepcopy(parent_messages)
    return _load_history(resume_sid)


def _save_history(agent_session_id: str, messages: list[dict]) -> None:
    _atomic_write_json(
        _session_path(agent_session_id),
        {"session_id": agent_session_id, "messages": messages},
    )


# --------------------------------------------------------------------------
# prompt / attachment shaping
# --------------------------------------------------------------------------


def _prepend_capability_context(prompt: str, inputs: dict) -> str:
    return prepend_capability_context(prompt, inputs)


def _prepend_file_attachments(prompt: str, files: list) -> str:
    if not files:
        return prompt
    sections: list[str] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "unknown")
        try:
            raw = base64.b64decode(str(f.get("data") or ""))
        except Exception:
            logger.warning("Skipping malformed file attachment: %s", name)
            continue
        try:
            text = raw.decode("utf-8")
            sections.append(f"<file name=\"{name}\">\n{text}\n</file>")
        except UnicodeDecodeError:
            size = f.get("size") if isinstance(f.get("size"), int) else len(raw)
            sections.append(f"<file name=\"{name}\">[binary file, {size} bytes]</file>")
    if not sections:
        return prompt
    preamble = "\n\n".join(sections)
    return f"{preamble}\n\n{prompt}" if prompt else preamble


def _image_part(img: dict) -> Optional[dict]:
    if not isinstance(img, dict):
        return None
    data = img.get("data")
    if not isinstance(data, str) or not data:
        return None
    media_type = str(img.get("media_type") or img.get("mime_type") or "image/png")
    if not media_type.startswith("image/"):
        media_type = "image/png"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{data}"},
    }


def _build_user_content(prompt: str, images: list) -> str | list[dict]:
    image_parts = [p for p in (_image_part(img) for img in (images or [])) if p]
    if not image_parts:
        return prompt
    parts: list[dict] = []
    if prompt:
        parts.append({"type": "text", "text": prompt})
    parts.extend(image_parts)
    return parts


def _steer_prompt(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "User steering update. Adjust the current turn accordingly."
    return f"User steering update for this in-flight turn:\n\n{raw}"


def _drain_steer_messages(run_dir: Path, offset: int, messages: list[dict]) -> tuple[int, int]:
    """Append newly-written steer.jsonl payloads to Chat Completions history.

    provider_openai.steer_run appends one JSON object per user steer. Chat
    Completions cannot interrupt an active response stream, but BA owns the
    loop, so the next model round can include every steer that arrived since
    the last drain. Returns (new_byte_offset, appended_count).
    """
    inbox = run_dir / "steer.jsonl"
    if not inbox.exists():
        return offset, 0
    appended = 0
    try:
        with inbox.open("r", encoding="utf-8") as f:
            f.seek(offset)
            for raw in f:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                prompt = _steer_prompt(str(payload.get("prompt") or ""))
                images = payload.get("images") if isinstance(payload.get("images"), list) else []
                messages.append({"role": "user", "content": _build_user_content(prompt, images)})
                appended += 1
            return f.tell(), appended
    except OSError:
        logger.exception("failed to drain openai steer inbox %s", inbox)
        return offset, appended


# --------------------------------------------------------------------------
# event emission (Claude-shaped lines -> session_events.jsonl)
# --------------------------------------------------------------------------

class EventEmitter:
    """Writes normalized Claude-jsonl lines to session_events.jsonl, one uuid
    per logical block, rewriting on each delta. parentUuid advances only when a
    new logical block starts."""

    def __init__(self, events_path: Path) -> None:
        self._fp = events_path.open("a", encoding="utf-8")
        self._parent: Optional[str] = None
        # active accumulation state, keyed by block kind
        self._text_uuid: Optional[str] = None
        self._text_buf: list[str] = []
        self._thinking_uuid: Optional[str] = None
        self._thinking_buf: list[str] = []
        self._tool_calls: dict[int, dict] = {}  # index -> {uuid, id, name, args}
        self._model: str = ""

    def set_model(self, model: str) -> None:
        self._model = model or ""

    def _write(self, event: dict, *, uuid_str: str) -> None:
        event["uuid"] = uuid_str
        event["parentUuid"] = self._parent
        event["timestamp"] = _now_iso()
        self._fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fp.flush()

    def _assistant(self, content: list[dict], uuid_str: str, **extra: Any) -> None:
        msg = {"role": "assistant", "content": content, "model": self._model}
        if extra:
            msg.update(extra)
        self._write({"type": "assistant", "message": msg}, uuid_str=uuid_str)

    def _user(self, content: list[dict], uuid_str: str) -> None:
        msg = {"role": "user", "content": content}
        self._write({"type": "user", "message": msg}, uuid_str=uuid_str)

    # --- streaming deltas ---
    def feed_text_delta(self, chunk: str) -> None:
        if self._text_uuid is None:
            self._text_uuid = _new_uuid()
            self._text_buf = []
        self._text_buf.append(chunk)
        self._assistant(
            [{"type": "text", "text": "".join(self._text_buf)}],
            uuid_str=self._text_uuid,
        )

    def feed_thinking_delta(self, chunk: str) -> None:
        if self._thinking_uuid is None:
            self._thinking_uuid = _new_uuid()
            self._thinking_buf = []
        self._thinking_buf.append(chunk)
        self._assistant(
            [{"type": "thinking", "thinking": "".join(self._thinking_buf)}],
            uuid_str=self._thinking_uuid,
        )

    def feed_tool_call_delta(self, idx: int, tc_id: Optional[str],
                             name: Optional[str], args_delta: Optional[str]) -> None:
        tc = self._tool_calls.get(idx)
        if tc is None:
            tc = {"uuid": _new_uuid(), "id": tc_id or "", "name": name or "",
                  "args": ""}
            self._tool_calls[idx] = tc
        if tc_id:
            tc["id"] = tc_id
        if name:
            tc["name"] = tc["name"] or name
        if args_delta:
            tc["args"] += args_delta
        # emit current accumulated tool_use (id/name may still be partial until
        # the chunk that carries them, but the final delta is authoritative).
        try:
            parsed = json.loads(tc["args"]) if tc["args"] else {}
        except json.JSONDecodeError:
            parsed = {}
        self._assistant(
            [{"type": "tool_use", "id": tc["id"], "name": tc["name"],
              "input": parsed}],
            uuid_str=tc["uuid"],
        )

    def close_text(self) -> Optional[str]:
        """Finalize the text block, advance parent, return the text."""
        if self._text_uuid is not None:
            self._parent = self._text_uuid
            text = "".join(self._text_buf)
            self._text_uuid = None
            self._text_buf = []
            return text
        return None

    def close_thinking(self) -> None:
        if self._thinking_uuid is not None:
            self._parent = self._thinking_uuid
            self._thinking_uuid = None
            self._thinking_buf = []

    def finalize_tool_calls(self) -> list[dict]:
        """Close all open tool_use blocks (advance parent to the last), return
        the finalized tool calls [{id,name,arguments}]."""
        calls = []
        last_uuid: Optional[str] = None
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            last_uuid = tc["uuid"]
            calls.append({"id": tc["id"], "name": tc["name"],
                          "arguments": tc["args"]})
        if calls:
            self._parent = last_uuid
        self._tool_calls = {}
        return calls

    # --- non-streamed events ---
    def emit_tool_result(self, tool_use_id: str, content: str) -> str:
        uid = _new_uuid()
        self._user(
            [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
            uuid_str=uid,
        )
        self._parent = uid
        return uid

    def emit_error(self, error: str) -> None:
        uid = _new_uuid()
        self._assistant(
            [{"type": "text", "text": f"Error: {error}"}],
            uuid_str=uid,
            isApiErrorMessage=True,
        )
        self._parent = uid

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def _confined_path(cwd: Path, raw: str) -> Path:
    """Resolve raw against cwd and refuse escapes (.., absolute, symlink)."""
    cwd = cwd.resolve()
    candidate = (cwd / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    if candidate != cwd and cwd not in candidate.parents:
        raise PermissionError(f"path escapes cwd: {raw}")
    return candidate


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n...[truncated, {len(text)} total chars]"


def _tool_subprocess_env() -> dict[str, str]:
    """Environment visible to model-run shell commands.

    Provider credentials and BA loopback tokens authenticate the runner itself;
    tools must not inherit them. Keep this denylist intentionally broad for
    OpenAI/Anthropic-style env vars and exact for internal-token names so a
    Bash tool cannot read or exfiltrate provider/backend secrets.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        lowered = key.lower()
        if lowered in _TOOL_ENV_DENY_EXACT:
            continue
        if lowered.startswith(_TOOL_ENV_DENY_PREFIXES):
            continue
        env[key] = value
    return env


def _tool_bash(args: dict, cwd: Path) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "Error: empty command"
    timeout = min(int(args.get("timeout") or _BASH_TIMEOUT_S), _BASH_TIMEOUT_S)
    env = _tool_subprocess_env()
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    out = proc.stdout
    if proc.stderr:
        out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
    if proc.returncode != 0:
        out += f"\n[exit {proc.returncode}]"
    return _truncate(out) or "(no output)"


def _tool_read(args: dict, cwd: Path) -> str:
    raw = args.get("file_path") or args.get("path") or ""
    try:
        p = _confined_path(cwd, raw)
    except PermissionError as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: not found: {raw}"
    if p.is_dir():
        return f"Error: is a directory: {raw}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: {e}"
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or 2000)
    lines = text.splitlines()
    sel = lines[max(0, offset - 1): max(0, offset - 1) + limit]
    return _truncate("\n".join(sel))


def _tool_write(args: dict, cwd: Path) -> str:
    raw = args.get("file_path") or args.get("path") or ""
    data = args.get("content")
    if data is None:
        return "Error: missing content"
    try:
        p = _confined_path(cwd, raw)
    except PermissionError as e:
        return f"Error: {e}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")
    return f"wrote {len(data)} bytes to {raw}"


def _tool_edit(args: dict, cwd: Path) -> str:
    raw = args.get("file_path") or args.get("path") or ""
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or new is None:
        return "Error: old_string and new_string required"
    try:
        p = _confined_path(cwd, raw)
    except PermissionError as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: not found: {raw}"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "Error: old_string not found"
    if count > 1 and not args.get("replace_all"):
        return f"Error: old_string matches {count} times; set replace_all to replace all"
    new_text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"edited {raw} ({count} match{'es' if count != 1 else ''})"


def _tool_grep(args: dict, cwd: Path) -> str:
    pattern = args.get("pattern") or ""
    if not pattern:
        return "Error: missing pattern"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    root_raw = args.get("path") or "."
    try:
        root = _confined_path(cwd, root_raw)
    except PermissionError as e:
        return f"Error: {e}"
    max_files = 200
    hits = []
    base = root if root.is_dir() else root.parent
    for path in base.rglob("*"):
        if len(hits) >= max_files:
            break
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if regex.search(line):
                    rel = path.relative_to(base)
                    hits.append(f"{rel}:{i}:{line[:300]}")
                    if len(hits) >= max_files:
                        break
        except Exception:
            continue
    return _truncate("\n".join(hits)) if hits else "(no matches)"


def _tool_glob(args: dict, cwd: Path) -> str:
    pattern = args.get("pattern") or ""
    if not pattern:
        return "Error: missing pattern"
    root_raw = args.get("path") or "."
    try:
        root = _confined_path(cwd, root_raw)
    except PermissionError as e:
        return f"Error: {e}"
    base = root if root.is_dir() else root.parent
    # simple glob: ** is recursive
    matches = sorted(str(p.relative_to(base)) for p in base.glob(pattern))
    return _truncate("\n".join(matches[:500])) if matches else "(no matches)"


TOOL_HANDLERS = {
    "Bash": _tool_bash,
    "Read": _tool_read,
    "Write": _tool_write,
    "Edit": _tool_edit,
    "Grep": _tool_grep,
    "Glob": _tool_glob,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "Bash", "description": "Run a shell command in the project cwd.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "integer", "description": "Max seconds (<=120)."}},
            "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a text file relative to cwd.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "Write", "description": "Write text to a file (overwrites).",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "Edit", "description": "Replace old_string with new_string in a file.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
            "required": ["file_path", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "Grep", "description": "Regex search files under a path.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "Glob", "description": "Glob match files under a path.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
]


def _function_tool_schema(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": parameters,
    }}


_CREATE_WORKER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "worker_description": {"type": "string"},
        "justification": {"type": "string"},
        "orchestration_mode": {"type": "string", "enum": ["team", "native"]},
        "node_id": {"type": "string"},
    },
    "required": ["worker_description", "justification", "orchestration_mode"],
    "additionalProperties": False,
}

_ENSURE_NAMED_WORKER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "cwd": {"type": "string"},
        "orchestration_mode": {"type": "string", "enum": ["team", "native"]},
        "provision_prompt": {"type": "string"},
        "description": {"type": "string"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
        "node_id": {"type": "string"},
    },
    "required": ["name", "cwd", "orchestration_mode"],
    "additionalProperties": False,
}

_OPEN_FILE_PANEL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["panel", "inline"]},
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "selected_start": {"type": "integer"},
        "selected_end": {"type": "integer"},
    },
    "required": ["mode", "path"],
    "additionalProperties": False,
}

_OPEN_FILE_PANEL_DESCRIPTION = (
    "Show the user a specific location in a file — this is a communication "
    "tool, not a file opener. Use mode='inline' to embed a file view in this "
    "message or mode='panel' to open a persistent side panel."
)

_REQUEST_USER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "header": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["label"],
                        },
                    },
                },
                "required": ["id", "header", "question"],
            },
        },
        "timeout_seconds": {"type": "number"},
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_REQUEST_USER_INPUT_DESCRIPTION = (
    "Ask the user a bounded question and wait for their answer. Use this only "
    "when you cannot continue safely without user input."
)

_START_FILE_DISCUSSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "line": {"type": "integer"},
        "title": {"type": "string"},
    },
    "required": ["file_path", "line"],
    "additionalProperties": False,
}

_START_FILE_DISCUSSION_DESCRIPTION = (
    "Start an inline discussion attached to a specific line in file edit mode."
)

_MSSG_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {"type": "string"},
        "target_worker_id": {"type": "string"},
        "target_worker_pool": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["message"],
    "additionalProperties": False,
}

_CREATE_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "orchestration_mode": {"type": "string", "enum": ["native", "team"]},
        "node_id": {"type": "string"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_CREATE_SUB_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "node_id": {"type": "string"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
    },
    "required": [],
    "additionalProperties": False,
}

_ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {"type": "string"},
        "target_worker_id": {"type": "string"},
        "target_worker_pool": {"type": "string"},
        "message": {"type": "string"},
        "run_mode": {"type": "string", "enum": ["direct", "fork"]},
        "mode": {
            "type": "string",
            "enum": [
                ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN,
                ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
            ],
        },
        "worker_description": {"type": "string"},
        "worker_registry_cwd": {"type": "string"},
        "ephemeral": {"type": "boolean"},
    },
    "required": ["message"],
    "additionalProperties": False,
}

_DISABLEABLE_BUILTIN_TOOLS = frozenset({
    "ask",
    "create_session",
    "create_sub_session",
    "delegate_task",
    "ensure_named_worker",
    "mssg",
})

_ORCHESTRATION_TOOL_NAMES = frozenset({
    "mssg", "ask", "delegate_task", "create_session",
    "create_sub_session", "create_worker", "ensure_named_worker",
    "open_file_panel", "request_user_input", "start_file_discussion",
})

# Better Agent runtime-capability management. Available only when the backend
# loopback channel exists (non-bare, internal sessions); these tools let the
# model scope its own session. Core owns the active-capability write — the tools
# just POST the trigger. Dispatched in `_dispatch_tool`, no permission gate.
_CAPABILITY_TOOL_NAMES = frozenset({
    "list_capabilities", "load_capability", "release_capability",
})
_CAPABILITY_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_capabilities",
        "description": "List the scoped capabilities loadable in this session and which are active.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "load_capability",
        "description": ("Load a scoped capability into this session. Its MCP + skill become "
                        "available on the next turn. Pass the full capability id "
                        "(e.g. 'ofek.testape:testape')."),
        "parameters": {"type": "object", "properties": {
            "capability_id": {"type": "string"}}, "required": ["capability_id"]}}},
    {"type": "function", "function": {
        "name": "release_capability",
        "description": ("Release a previously loaded capability from this session. Pass the "
                        "full capability id (e.g. 'ofek.testape:testape')."),
        "parameters": {"type": "object", "properties": {
            "capability_id": {"type": "string"}}, "required": ["capability_id"]}}},
]


def _disabled_builtin_tools(inputs: dict) -> set[str]:
    raw = inputs.get("disabled_builtin_tools")
    if not isinstance(raw, list):
        return set()
    return {
        str(item).strip()
        for item in raw
        if str(item or "").strip() in _DISABLEABLE_BUILTIN_TOOLS
    }


def _disallowed_tool_names(inputs: dict) -> set[str]:
    raw = inputs.get("disallowed_tools")
    if not isinstance(raw, list):
        return set()
    return {str(item or "").strip().lower() for item in raw if str(item or "").strip()}


def _filtered_core_tool_schemas(inputs: dict) -> list[dict]:
    blocked = _disallowed_tool_names(inputs)
    if not blocked:
        return list(TOOL_SCHEMAS)
    out: list[dict] = []
    for schema in TOOL_SCHEMAS:
        name = str((schema.get("function") or {}).get("name") or "")
        if name.lower() not in blocked:
            out.append(schema)
    return out


def _tool_schemas_for_run(
    *,
    inputs: dict,
    capabilities_enabled: bool,
    loopback_enabled: bool,
    team_manager_enabled: bool,
    team_orchestration_enabled: bool,
    open_file_panel_enabled: bool,
    file_editing_mode: bool,
) -> list[dict]:
    """Build this turn's Chat Completions tool list.

    Core coding tools are always available unless explicitly disallowed. BA
    loopback/orchestration tools require the backend channel and can be disabled
    by the global built-in tool toggles.
    """
    schemas = _filtered_core_tool_schemas(inputs)
    disabled = _disabled_builtin_tools(inputs)
    if loopback_enabled:
        if team_manager_enabled and team_orchestration_enabled:
            schemas.append(_function_tool_schema(
                "create_worker", _CREATE_WORKER_DESCRIPTION, _CREATE_WORKER_INPUT_SCHEMA,
            ))
        if team_orchestration_enabled and "ensure_named_worker" not in disabled:
            schemas.append(_function_tool_schema(
                "ensure_named_worker",
                _ENSURE_NAMED_WORKER_DESCRIPTION,
                _ENSURE_NAMED_WORKER_INPUT_SCHEMA,
            ))
        mssg_sender_session_id = str(
            inputs.get("mssg_sender_session_id") or inputs.get("app_session_id") or ""
        ).strip()
        if mssg_sender_session_id:
            if "mssg" not in disabled:
                schemas.append(_function_tool_schema("mssg", _MSSG_DESCRIPTION, _MSSG_INPUT_SCHEMA))
            if "ask" not in disabled:
                schemas.append(_function_tool_schema("ask", _ASK_DESCRIPTION, _ASK_INPUT_SCHEMA))
        if "delegate_task" not in disabled:
            schemas.append(_function_tool_schema(
                "delegate_task", _DELEGATE_TASK_DESCRIPTION, _DELEGATE_TASK_INPUT_SCHEMA,
            ))
        if "create_session" not in disabled:
            schemas.append(_function_tool_schema(
                "create_session", _CREATE_SESSION_DESCRIPTION, _CREATE_SESSION_INPUT_SCHEMA,
            ))
        if "create_sub_session" not in disabled:
            schemas.append(_function_tool_schema(
                "create_sub_session", _CREATE_SUB_SESSION_DESCRIPTION, _CREATE_SUB_SESSION_INPUT_SCHEMA,
            ))
        if open_file_panel_enabled:
            schemas.append(_function_tool_schema(
                "open_file_panel", _OPEN_FILE_PANEL_DESCRIPTION, _OPEN_FILE_PANEL_INPUT_SCHEMA,
            ))
            schemas.append(_function_tool_schema(
                "request_user_input", _REQUEST_USER_INPUT_DESCRIPTION, _REQUEST_USER_INPUT_SCHEMA,
            ))
            if file_editing_mode:
                schemas.append(_function_tool_schema(
                    "start_file_discussion",
                    _START_FILE_DISCUSSION_DESCRIPTION,
                    _START_FILE_DISCUSSION_INPUT_SCHEMA,
                ))
    if capabilities_enabled:
        schemas += _CAPABILITY_TOOL_SCHEMAS
    return schemas


def _is_bypass(permission: Optional[dict]) -> bool:
    if not permission:
        return False
    # permission is CLI-vocab {axis: mode}; treat bypass/yolo as auto-allow.
    vals = " ".join(str(v).lower() for v in permission.values())
    return "bypass" in vals or "yolo" in vals


# --------------------------------------------------------------------------
# BA loopback/orchestration tools
# --------------------------------------------------------------------------

DynamicToolHandler = Callable[[dict], Awaitable[str]]


def _dynamic_tool_text_result(text: str, *, success: bool) -> str:
    prefix = "" if success else "Error: "
    return prefix + text if not text.startswith("Error:") else text


def _dynamic_tool_json_result(result: dict, *, success: bool) -> str:
    if success:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return _dynamic_tool_text_result(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        success=False,
    )


def _post_loopback_sync(
    payload: dict,
    *,
    backend_url: str,
    internal_token: str,
    url_path: str,
    timeout_s: float,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    deadline = time.monotonic() + timeout_s
    backoff = 1.0
    while True:
        req = urllib.request.Request(
            backend_url.rstrip("/") + url_path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": internal_token,
            },
        )
        try:
            remaining = max(1.0, deadline - time.monotonic())
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"loopback returned non-JSON: {e}; raw={raw[:200]!r}")
        except urllib.error.HTTPError:
            raise
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ConnectionError,
            TimeoutError,
        ) as e:
            if time.monotonic() >= deadline:
                raise
            reason = getattr(e, "reason", e)
            logger.warning(
                "loopback POST %s failed (%s); retrying in %.1fs",
                url_path,
                reason,
                backoff,
            )
            time.sleep(min(backoff, max(0.5, deadline - time.monotonic())))
            backoff = min(backoff * 2, 60.0)


def _args(params: dict) -> dict:
    return params.get("arguments") if isinstance(params.get("arguments"), dict) else {}


def _build_loopback_tool_handlers(inputs: dict, *, cwd: str, model: str) -> dict[str, DynamicToolHandler]:
    backend_url = inputs.get("backend_url") or ""
    internal_token = inputs.get("internal_token") or ""
    app_session_id = str(inputs.get("app_session_id") or "").strip()
    if not backend_url or not internal_token or not app_session_id:
        return {}
    handlers: dict[str, DynamicToolHandler] = {}
    disabled = _disabled_builtin_tools(inputs)
    mssg_sender_session_id = str(
        inputs.get("mssg_sender_session_id") or app_session_id or ""
    ).strip()

    async def create_worker(params: dict) -> str:
        args = _args(params)
        worker_description = str(args.get("worker_description") or "").strip()
        justification = str(args.get("justification") or "").strip()
        orchestration_mode = str(args.get("orchestration_mode") or "").strip()
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        if not worker_description or not justification or not orchestration_mode:
            return _dynamic_tool_text_result(
                "worker_description, justification and orchestration_mode are required",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "worker_description": worker_description,
                    "justification": justification,
                    "orchestration_mode": orchestration_mode,
                    "cwd": cwd,
                    "client_request_id": f"cw_{uuid.uuid4().hex[:10]}",
                    "node_id": node_id,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/create-worker",
                timeout_s=DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("create_worker dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_worker failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def ensure_named_worker(params: dict) -> str:
        args = _args(params)
        name = str(args.get("name") or "").strip()
        worker_cwd = str(args.get("cwd") or "").strip()
        orchestration_mode = str(args.get("orchestration_mode") or "").strip()
        if not name or not worker_cwd or not orchestration_mode:
            return _dynamic_tool_text_result(
                "name, cwd and orchestration_mode are required",
                success=False,
            )
        if orchestration_mode == "manager":
            orchestration_mode = "team"
        if orchestration_mode not in ("team", "native"):
            return _dynamic_tool_text_result(
                "orchestration_mode must be 'team' or 'native'",
                success=False,
            )
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        spec = {
            "role_key": name,
            "description": args.get("description") or f"worker:{name}",
            "orchestration_mode": orchestration_mode,
            "provision_prompt": args.get("provision_prompt"),
            "provider_id": args.get("provider_id"),
            "model": args.get("model"),
            "reasoning_effort": args.get("reasoning_effort"),
            "node_id": node_id,
            "tags": [name],
        }
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {"cwd": worker_cwd, "workers": [spec]},
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/workers/provision",
                timeout_s=DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("ensure_named_worker dynamic tool handler failed")
            return _dynamic_tool_text_result(f"ensure_named_worker failed: {e}", success=False)
        workers = (result or {}).get("workers") or []
        if not workers:
            return _dynamic_tool_text_result(
                "ensure_named_worker provision returned no worker",
                success=False,
            )
        worker = workers[0]
        return _dynamic_tool_json_result(
            {
                "agent_session_id": worker.get("agent_session_id"),
                "name": worker.get("name"),
                "created": bool(worker.get("created")),
                "orchestration_mode": worker.get("orchestration_mode"),
                "registry_cwd": worker.get("registry_cwd") or worker.get("cwd"),
            },
            success=True,
        )

    async def mssg(params: dict) -> str:
        args = _args(params)
        target_session_id = str(args.get("target_session_id") or "").strip()
        target_worker_id = str(args.get("target_worker_id") or "").strip()
        target_worker_pool = str(args.get("target_worker_pool") or "").strip()
        message = str(args.get("message") or "").strip()
        if (not target_session_id and not target_worker_id and not target_worker_pool) or not message:
            return _dynamic_tool_text_result("one target and message are required", success=False)
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": mssg_sender_session_id,
                    "target_session_id": target_session_id,
                    "target_worker_id": target_worker_id,
                    "target_worker_pool": target_worker_pool,
                    "message": message,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/mssg",
                timeout_s=30.0,
            )
        except Exception as e:
            logger.exception("mssg dynamic tool handler failed")
            return _dynamic_tool_text_result(f"mssg failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def ask(params: dict) -> str:
        args = _args(params)
        target_session_id = str(args.get("target_session_id") or "").strip()
        target_worker_id = str(args.get("target_worker_id") or "").strip()
        target_worker_pool = str(args.get("target_worker_pool") or "").strip()
        message = str(args.get("message") or "").strip()
        run_mode = str(args.get("run_mode") or "direct").strip() or "direct"
        try:
            mode = normalize_ask_mode(args.get("mode"))
        except ValueError as exc:
            return _dynamic_tool_text_result(str(exc), success=False)
        if (not target_session_id and not target_worker_id and not target_worker_pool) or not message:
            return _dynamic_tool_text_result("one target and message are required", success=False)
        if run_mode not in ("direct", "fork"):
            return _dynamic_tool_text_result("run_mode must be 'direct' or 'fork'", success=False)
        if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC and run_mode == "fork":
            return _dynamic_tool_text_result("async ask mode requires run_mode='direct'", success=False)
        ephemeral = bool(args.get("ephemeral"))
        if ephemeral and run_mode != "fork":
            return _dynamic_tool_text_result("ephemeral is only valid for run_mode='fork'", success=False)
        if run_mode == "fork":
            if not target_session_id:
                return _dynamic_tool_text_result("run_mode='fork' requires target_session_id", success=False)
            worker_registry_cwd = args.get("worker_registry_cwd")
            if worker_registry_cwd in ("", "null"):
                worker_registry_cwd = None
            payload = {
                "app_session_id": app_session_id,
                "instructions": message,
                "worker_session_id": target_session_id,
                "worker_description": str(args.get("worker_description") or "").strip(),
                "model": model,
                "cwd": cwd,
                "client_delegation_id": f"del_{uuid.uuid4().hex[:10]}",
                "run_mode": "fork",
                "worker_registry_cwd": worker_registry_cwd,
                "ephemeral": ephemeral,
            }
            url_path = "/api/internal/ask-fork"
        else:
            payload = {
                "sender_session_id": mssg_sender_session_id,
                "target_session_id": target_session_id,
                "target_worker_id": target_worker_id,
                "target_worker_pool": target_worker_pool,
                "message": message,
                "ask_id": f"ask_{uuid.uuid4().hex[:10]}",
                "mode": mode,
            }
            url_path = "/api/internal/ask"
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                payload,
                backend_url=backend_url,
                internal_token=internal_token,
                url_path=url_path,
                timeout_s=30.0 if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC else DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("ask dynamic tool handler failed")
            return _dynamic_tool_text_result(f"ask failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def delegate_task(params: dict) -> str:
        args = _args(params)
        task = str(args.get("task") or "").strip()
        if not task:
            return _dynamic_tool_text_result("task is required", success=False)
        target = args.get("target_session_id")
        if target in ("", "null"):
            target = None
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": app_session_id,
                    "task": task,
                    "target_session_id": target,
                    "cwd": cwd,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "sub_session": args.get("sub_session") is not False,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/delegate-task",
                timeout_s=DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("delegate_task dynamic tool handler failed")
            return _dynamic_tool_text_result(f"delegate_task failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def create_session(params: dict) -> str:
        args = _args(params)
        name = str(args.get("name") or "").strip()
        if not name:
            return _dynamic_tool_text_result("name is required", success=False)
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": app_session_id,
                    "name": name,
                    "cwd": cwd,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "orchestration_mode": args.get("orchestration_mode") or "native",
                    "node_id": node_id,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/create-session",
                timeout_s=30.0,
            )
        except Exception as e:
            logger.exception("create_session dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_session failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def create_sub_session(params: dict) -> str:
        args = _args(params)
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": app_session_id,
                    "description": str(args.get("description") or "").strip(),
                    "cwd": cwd,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "node_id": node_id,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/create-sub-session",
                timeout_s=30.0,
            )
        except Exception as e:
            logger.exception("create_sub_session dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_sub_session failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def open_file_panel(params: dict) -> str:
        args = _args(params)
        mode = str(args.get("mode") or "").strip()
        path = str(args.get("path") or "").strip()
        if mode not in ("panel", "inline") or not path:
            return _dynamic_tool_text_result("`mode` (panel|inline) and `path` are required", success=False)
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "mode": mode,
                    "path": path,
                    "start_line": args.get("start_line"),
                    "end_line": args.get("end_line"),
                    "selected_start": args.get("selected_start"),
                    "selected_end": args.get("selected_end"),
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/open-file-panel",
                timeout_s=_OPEN_FILE_PANEL_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("open_file_panel dynamic tool handler failed")
            return _dynamic_tool_text_result(f"open_file_panel failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def request_user_input(params: dict) -> str:
        args = _args(params)
        questions = args.get("questions")
        if not isinstance(questions, list) or not questions:
            return _dynamic_tool_text_result("`questions` must be a non-empty array", success=False)
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "questions": questions,
                    "timeout_seconds": args.get("timeout_seconds"),
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/user-input/request",
                timeout_s=DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("request_user_input dynamic tool handler failed")
            return _dynamic_tool_text_result(f"request_user_input failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    async def start_file_discussion(params: dict) -> str:
        args = _args(params)
        file_path = str(args.get("file_path") or "").strip()
        line = args.get("line")
        if not file_path or not isinstance(line, int) or line < 1:
            return _dynamic_tool_text_result("`file_path` and `line >= 1` are required", success=False)
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "file_path": file_path,
                    "line": line,
                    "title": args.get("title") or "",
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/file-editor/start-discussion",
                timeout_s=_OPEN_FILE_PANEL_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("start_file_discussion dynamic tool handler failed")
            return _dynamic_tool_text_result(f"start_file_discussion failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    try:
        import extension_store
        team_orchestration_ready = extension_store.is_extension_runtime_ready(
            extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
        )
    except Exception:
        team_orchestration_ready = False
    if (inputs.get("mode") or "native") == "manager" and team_orchestration_ready:
        handlers["create_worker"] = create_worker
    if team_orchestration_ready and "ensure_named_worker" not in disabled:
        handlers["ensure_named_worker"] = ensure_named_worker
    if mssg_sender_session_id:
        if "mssg" not in disabled:
            handlers["mssg"] = mssg
        if "ask" not in disabled:
            handlers["ask"] = ask
    if "delegate_task" not in disabled:
        handlers["delegate_task"] = delegate_task
    if "create_session" not in disabled:
        handlers["create_session"] = create_session
    if "create_sub_session" not in disabled:
        handlers["create_sub_session"] = create_sub_session
    if bool(inputs.get("open_file_panel_enabled")):
        handlers["open_file_panel"] = open_file_panel
        handlers["request_user_input"] = request_user_input
        if inputs.get("working_mode") == "file_editing":
            handlers["start_file_discussion"] = start_file_discussion
    return handlers


# --------------------------------------------------------------------------
# Chat Completions streaming
# --------------------------------------------------------------------------

async def _stream_chat(
    base_url: str, api_key: str, model: str, messages: list[dict],
    tools: list[dict], reasoning_effort: Optional[str] = None,
) -> AsyncIterator[dict]:
    """Yield parsed SSE chunk dicts from a streaming Chat Completions call."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning_effort and reasoning_effort != "none":
        payload["reasoning_effort"] = reasoning_effort
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # read=30min: per-chunk silence limit. Multi-agent endpoints (e.g. Fugu)
    # can go quiet for many minutes during internal deliberation between
    # stream chunks; 600s was too low and killed mid-stream turns.
    timeout = httpx.Timeout(connect=15.0, read=1800.0, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

async def _run(run_dir: Path, inputs: dict) -> int:
    cwd_str = inputs.get("cwd")
    if not cwd_str:
        _fail(run_dir, "missing cwd")
        return 1
    cwd = Path(cwd_str).expanduser().resolve()
    if not cwd.is_dir():
        _fail(run_dir, f"cwd does not exist: {cwd}")
        return 1

    api_key = os.environ.get("OPENAI_API_KEY") or ""
    base_url = os.environ.get("OPENAI_BASE_URL") or ""
    model = inputs.get("model") or ""
    if not api_key or not base_url or not model:
        _fail(run_dir, "OPENAI_API_KEY / OPENAI_BASE_URL / model not configured")
        return 1

    prompt = inputs.get("prompt") or ""
    app_session_id = inputs.get("app_session_id") or _new_uuid()
    permission = inputs.get("permission")
    bypass = _is_bypass(permission)
    backend_url = inputs.get("backend_url") or ""
    internal_token = inputs.get("internal_token") or ""
    # Interactive approval needs the backend HTTP channel. Without it the gate
    # can't surface a prompt — fail closed with a clear message rather than a
    # user-blaming "denied" (mirrors runner.py's interactive_permissions guard).
    interactive = bool(backend_url) and bool(internal_token) and bool(app_session_id)
    # Capability-management tools ride the same backend channel and are stripped
    # from bare (TestApe-isolated) sessions, matching runner.py / the stdio
    # capabilities MCP injected for the CLI providers.
    capabilities_enabled = interactive and not bool(inputs.get("bare_config"))
    # Feature flags mirror _build_loopback_tool_handlers' registration conditions
    # so the model only sees schemas for tools that actually have a handler
    # wired (ask/mssg/delegate/create_*/file-panel). Team-manager requires
    # manager mode; file_editing is the working_mode the file-panel edits in.
    mode = inputs.get("mode") or "native"
    loopback_enabled = capabilities_enabled
    team_manager_enabled = mode == "manager"
    try:
        import extension_store
        team_orchestration_enabled = extension_store.is_extension_runtime_ready(
            extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
        )
    except Exception:
        team_orchestration_enabled = False
    open_file_panel_enabled = bool(inputs.get("open_file_panel_enabled"))
    file_editing_mode = inputs.get("working_mode") == "file_editing"
    tool_schemas = _tool_schemas_for_run(
        inputs=inputs,
        capabilities_enabled=capabilities_enabled,
        loopback_enabled=loopback_enabled,
        team_manager_enabled=team_manager_enabled,
        team_orchestration_enabled=team_orchestration_enabled,
        open_file_panel_enabled=open_file_panel_enabled,
        file_editing_mode=file_editing_mode,
    )
    loopback_handlers = _build_loopback_tool_handlers(inputs, cwd=str(cwd), model=model)
    resume_sid = inputs.get("session_id")

    capability_context = render_capability_context(inputs.get("capability_contexts") or [])
    prompt = _prepend_file_attachments(prompt, inputs.get("files") or [])
    session_id, messages = _load_history_for_run(resume_sid, fork=bool(inputs.get("fork")))
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": _SYSTEM_PROMPT})
    transient_capability_message = None
    if capability_context:
        transient_capability_message = {"role": "system", "content": capability_context}
        messages.append(transient_capability_message)
    if prompt or inputs.get("images"):
        messages.append({"role": "user", "content": _build_user_content(prompt, inputs.get("images") or [])})

    events_path = run_dir / "session_events.jsonl"
    emitter = EventEmitter(events_path)
    emitter.set_model(model)

    # state.json — session_id known immediately; tailer starts on this.
    state = {
        "run_id": run_dir.name, "mode": inputs.get("mode") or "native",
        "runner_pid": os.getpid(), "app_session_id": app_session_id,
        "started_at": _now_iso(), "session_id": session_id,
        "jsonl_path": str(events_path), "complete": False,
    }
    _atomic_write_json(run_dir / "state.json", state)

    usage_acc = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "total_tokens": 0}
    error: Optional[str] = None
    try:
        max_loops = int(inputs.get("max_tool_loops") or _MAX_TOOL_LOOPS)
    except (TypeError, ValueError):
        max_loops = _MAX_TOOL_LOOPS
    reasoning_effort = inputs.get("reasoning_effort") or None
    steer_offset = 0

    try:
        for _ in range(max_loops):
            if (run_dir / "cancel").exists():
                error = "cancelled"
                break
            steer_offset, _ = _drain_steer_messages(run_dir, steer_offset, messages)
            finish_reason, tool_calls, asst_text, chunk_usage = await _one_round(
                base_url, api_key, model, messages, emitter, run_dir, tool_schemas,
                reasoning_effort=reasoning_effort,
            )
            if (run_dir / "cancel").exists():
                # `_one_round` breaks promptly on the cancel sentinel without
                # raising. Do not save a partial assistant chunk as a successful
                # turn; surface the cancellation and exit non-zero.
                error = "cancelled"
                break
            if chunk_usage:
                usage_acc["input_tokens"] += chunk_usage.get("prompt_tokens", 0)
                usage_acc["output_tokens"] += chunk_usage.get("completion_tokens", 0)
                usage_acc["total_tokens"] += chunk_usage.get("total_tokens", 0)
                pd = chunk_usage.get("prompt_tokens_details") or {}
                usage_acc["cache_read_input_tokens"] += pd.get("cached_tokens", 0) or 0

            # append the assistant turn to the OpenAI messages array. Persist the
            # reply text so resume on turn 2+ carries prior assistant answers.
            # content=None is valid ONLY alongside tool_calls; a round that
            # produced neither text nor tool_calls is dropped rather than
            # persisted as an invalid null-content message that 400s on resume.
            if tool_calls or asst_text is not None:
                asst_msg: dict = {"role": "assistant", "content": asst_text}
                if tool_calls:
                    asst_msg["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in tool_calls
                    ]
                messages.append(asst_msg)

            if not tool_calls or finish_reason == "stop":
                # A steer can arrive while the final response is streaming.
                # Chat Completions cannot alter already-emitted tokens, but if
                # we catch the steer before process exit we can run one more
                # model round with the steering prompt in context instead of
                # silently dropping it.
                steer_offset, steered = _drain_steer_messages(run_dir, steer_offset, messages)
                if steered:
                    continue
                break

            # execute tools
            for call in tool_calls:
                if (run_dir / "cancel").exists():
                    error = "cancelled"
                    break
                result = await _dispatch_tool(
                    call, cwd, app_session_id, run_dir, bypass,
                    interactive, backend_url, internal_token, emitter,
                    loopback_handlers,
                )
                messages.append({
                    "role": "tool", "tool_call_id": call["id"], "content": result,
                })
            if error:
                break
        else:
            # Loop exhausted the cap without the model emitting a terminal
            # response (finish_reason "stop" / no tool_calls) and without a
            # cancel — i.e. it was still mid-task when the budget ran out.
            # Surface this honestly as a not-completed turn instead of letting
            # error stay None and reporting a truncated turn as success.
            error = (
                f"max tool loops ({max_loops}) reached without the model "
                "ending the turn; raise max_tool_loops or let the model finish"
            )

        if transient_capability_message is not None:
            messages = [msg for msg in messages if msg is not transient_capability_message]
        _save_history(session_id, messages)
    except Exception as e:
        logger.exception("openai runner loop failed")
        error = f"{type(e).__name__}: {e}"

    if error:
        emitter.emit_error(error)
    emitter.close()

    complete = {
        "success": error is None,
        "session_id": session_id,
        "error": error,
        "token_usage": {**usage_acc, "duration_ms": None},
        "finished_at": _now_iso(),
    }
    _atomic_write_json(run_dir / "terminal.json", complete)
    _atomic_write_json(run_dir / "complete.json", complete)
    state["complete"] = True
    _atomic_write_json(run_dir / "state.json", state)
    return 0 if error is None else 1


async def _one_round(
    base_url: str, api_key: str, model: str, messages: list[dict],
    emitter: EventEmitter, run_dir: Path, tool_schemas: list[dict] = TOOL_SCHEMAS,
    reasoning_effort: Optional[str] = None,
) -> tuple[Optional[str], list[dict], Optional[str], Optional[dict]]:
    """Stream one assistant response. Finalize text/thinking/tool_calls.
    Returns (finish_reason, finalized_tool_calls, assistant_text, usage)."""
    finish_reason: Optional[str] = None
    usage: Optional[dict] = None
    async for chunk in _stream_chat(
        base_url, api_key, model, messages, tool_schemas,
        reasoning_effort=reasoning_effort,
    ):
        if (run_dir / "cancel").exists():
            break
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        fr = choice.get("finish_reason")
        if fr:
            finish_reason = fr
        delta = choice.get("delta") or {}
        if delta.get("content"):
            emitter.feed_text_delta(delta["content"])
        rc = delta.get("reasoning_content")
        if rc:
            emitter.feed_thinking_delta(rc)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            emitter.feed_tool_call_delta(
                idx, tc.get("id"), fn.get("name"), fn.get("arguments"),
            )

    emitter.close_thinking()
    text = emitter.close_text()
    tool_calls = emitter.finalize_tool_calls()
    return finish_reason, tool_calls, text, usage


async def _dispatch_tool(
    call: dict, cwd: Path, app_session_id: str, run_dir: Path,
    bypass: bool, interactive: bool, backend_url: str, internal_token: str,
    emitter: EventEmitter, loopback_handlers: dict[str, DynamicToolHandler],
) -> str:
    name = call["name"]
    try:
        args = json.loads(call.get("arguments") or "{}")
    except json.JSONDecodeError as e:
        emitter.emit_tool_result(call["id"], f"Error: bad arguments json: {e}")
        return f"Error: bad arguments json: {e}"

    # Capability management tools — no filesystem side effects, no permission
    # gate. They POST to the core capabilities endpoint over the loopback.
    if name in _CAPABILITY_TOOL_NAMES:
        return await _dispatch_capability_tool(
            name=name, args=args, backend_url=backend_url,
            internal_token=internal_token, app_session_id=app_session_id,
            interactive=interactive, emitter=emitter, tool_call_id=call["id"],
        )

    # Loopback/orchestration/file-panel tools (ask, mssg, delegate_task,
    # create_worker/create_session/create_sub_session, open_file_panel,
    # request_user_input, start_file_discussion). No filesystem side effects →
    # no permission gate; the handler owns auth via the backend loopback and
    # returns a self-describing result string.
    lb_handler = loopback_handlers.get(name)
    if lb_handler is not None:
        try:
            # Loopback handlers unwrap their inputs via `_args(params)`, i.e.
            # they read `params["arguments"]`. The provider gives us the bare
            # arguments dict, so wrap it to match that contract — otherwise
            # every loopback tool (ask/mssg/delegate_task/create_session/
            # create_sub_session/...) sees empty args and rejects valid calls.
            result = await lb_handler({"arguments": args})
        except Exception as e:
            logger.exception("loopback tool %s failed", name)
            result = f"Error: {type(e).__name__}: {e}"
        emitter.emit_tool_result(call["id"], result)
        return result

    # permission gate: non-bypass runs ask the backend before risky tools
    if not bypass and name in {"Bash", "Write", "Edit"}:
        if not interactive:
            # No approval channel — fail closed honestly, without blaming the
            # user. Bypass mode is the explicit opt-out for ungated execution.
            msg = "Error: approval required but backend approval channel unavailable"
            emitter.emit_tool_result(call["id"], msg)
            return msg
        verdict = await _request_approval(
            app_session_id=app_session_id, run_id=run_dir.name,
            tool_name=name, args=args,
            backend_url=backend_url, internal_token=internal_token,
            cancel_path=run_dir / "cancel",
        )
        if verdict == "cancelled":
            msg = "Error: tool use cancelled by user"
        elif verdict != "approved":
            msg = "Error: tool use denied by user"
        else:
            msg = None
        if msg is not None:
            emitter.emit_tool_result(call["id"], msg)
            return msg

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        emitter.emit_tool_result(call["id"], f"Error: unknown tool: {name}")
        return f"Error: unknown tool: {name}"
    try:
        result = await asyncio.to_thread(handler, args, cwd)
    except PermissionError as e:
        result = f"Error: {e}"
    except Exception as e:
        logger.exception("tool %s failed", name)
        result = f"Error: {type(e).__name__}: {e}"
    emitter.emit_tool_result(call["id"], result)
    return result


async def _wait_cancel(cancel_path: Path) -> bool:
    while True:
        if cancel_path.exists():
            return True
        await asyncio.sleep(0.5)


def _capabilities_endpoint_post(
    *, backend_url: str, internal_token: str, app_session_id: str, payload: dict,
) -> str:
    """POST a capability action (list/load/release) to the core endpoint and
    return a JSON-string result. Core owns the active-capability write; this is
    the authorized trigger. Errors are returned as text, never raised, so a
    failed call doesn't abort the turn."""
    url = backend_url.rstrip("/") + f"/api/internal/sessions/{app_session_id}/capabilities"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": internal_token,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
        if resp.status_code >= 400:
            return f"Error: HTTP {resp.status_code}: {resp.text[:500]}"
        return resp.text or "{}"
    except Exception as e:  # noqa: BLE001 — surface to the model
        return f"Error: {type(e).__name__}: {e}"


async def _dispatch_capability_tool(
    *, name: str, args: dict, backend_url: str, internal_token: str,
    app_session_id: str, interactive: bool, emitter: EventEmitter, tool_call_id: str,
) -> str:
    if not interactive:
        msg = "Error: capabilities require the backend channel (unavailable for this run)"
        emitter.emit_tool_result(tool_call_id, msg)
        return msg
    if name == "list_capabilities":
        payload = {"action": "list"}
    else:
        capability_id = str(args.get("capability_id") or "").strip()
        if not capability_id:
            msg = "Error: capability_id is required"
            emitter.emit_tool_result(tool_call_id, msg)
            return msg
        action = "load" if name == "load_capability" else "release"
        payload = {"action": action, "capability_id": capability_id}
    result = await asyncio.to_thread(
        _capabilities_endpoint_post,
        backend_url=backend_url,
        internal_token=internal_token,
        app_session_id=app_session_id,
        payload=payload,
    )
    emitter.emit_tool_result(tool_call_id, result)
    return result


async def _request_approval(
    *,
    app_session_id: str, run_id: str, tool_name: str, args: dict,
    backend_url: str, internal_token: str, cancel_path: Path,
) -> str:
    """Ask the backend for a human decision. Returns 'approved' | 'denied' |
    'cancelled'. The backend (not this client) is the fail-closed authority:
    any transport error or timeout resolves to 'denied'. A turn-cancel
    sentinel aborts the in-flight request as 'cancelled' instead of waiting
    for the backend timeout."""
    approval_task = asyncio.ensure_future(asyncio.to_thread(
        request_tool_approval,
        backend_url=backend_url,
        internal_token=internal_token,
        app_session_id=app_session_id,
        run_id=run_id,
        provider_kind="openai",
        tool_name=tool_name,
        # Same capped {"tool", "input"} shape as the Claude/Codex runners so
        # the one frontend card renders every argument (was a divergent,
        # un-truncated {"tool", "args"} the UI couldn't read).
        summary=describe_tool_call(tool_name, args),
    ))
    cancel_task = asyncio.ensure_future(_wait_cancel(cancel_path))
    done, pending = await asyncio.wait(
        {approval_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    if cancel_task in done:
        return "cancelled"
    try:
        approved = approval_task.result() if approval_task in done else False
    except Exception:
        # The client swallows errors itself, but defend the contract: any raise
        # out of the thread is a denial, never a turn-aborting exception.
        approved = False
    return "approved" if approved else "denied"


_SYSTEM_PROMPT = (
    "You are a software engineering agent running inside Better Agent's own "
    "agent loop over an OpenAI Chat Completions endpoint. You have tools: "
    "Bash, Read, Write, Edit, Grep, Glob. Work in the project cwd. Be concise. "
    "Use tools to inspect and edit files; do not guess at file contents."
)


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_openai %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        logger.exception("runner top-level failure")
        _fail(run_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    sys.exit(main(parser.parse_args().run_dir))
