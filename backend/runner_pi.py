"""Detached runner for the pi coding agent CLI (badlogic/pi-mono).

Per turn: spawns `pi --mode json -p` (prompt piped on stdin), reads pi's
LF-delimited JSON event stream from stdout, normalizes each event to
Claude jsonl shape, and appends to `<run_dir>/session_events.jsonl` —
which PiProvider tails for the render tree (gemini recovery family).

Session isolation: every run passes `--session-dir <run_dir>/pi-sessions`
so pi's durable session file lands inside the run dir. Continuation
resolves the prior session file by session id across run dirs and passes
it as `--session <path>` (same session id, appends to that file) or
`--fork <path>` (new session id copied into this run's session dir).

pi's print mode exits 0 even when the model turn errored; the terminal
assistant message carries `stopReason: "error" | "aborted"` plus
`errorMessage`, which this runner converts into the run error.

Cancel sentinel: backend writes `run_dir/cancel`, runner terminates the
pi subprocess tree.
"""

from __future__ import annotations

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
from cli_paths import resolve_cli_binary
from proc_control import process_control as _process_control
from runner_errors import resume_session_mismatch, stderr_error
from runs_dir import atomic_write_json, runs_root as _runs_root

logger = logging.getLogger(__name__)

# Deterministic UUID namespace: the same pi tool result normalized twice
# (message_start + message_end both carry the full ToolResultMessage)
# collides in the render tree's uuid dedup instead of duplicating.
_PI_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.runner_pi.events")

# Directory inside each run dir where pi persists its session jsonl.
PI_SESSION_DIR_NAME = "pi-sessions"


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _tool_result_uuid(session_id: Optional[str], tool_call_id: str) -> str:
    if session_id and tool_call_id:
        return str(uuid.uuid5(_PI_UUID_NAMESPACE, f"{session_id}|toolResult|{tool_call_id}"))
    return _new_uuid()


# ============================================================================
# Tool name / input-key mapping — pi → Claude canonical
# ============================================================================
# pi's built-in tools (pi-coding-agent 0.73.1: dist/core/tools/*.js). Mapping
# to Claude's canonical names lets the frontend's existing renderers light up.
# Unmapped names pass through verbatim (generic tool card).
_TOOL_NAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "grep": "Grep",
    "find": "Glob",
    "ls": "LS",
}

# Per-tool input key translation, keys not listed pass through verbatim.
_TOOL_INPUT_KEY_MAP = {
    "Read":  {"path": "file_path"},
    "Edit":  {"path": "file_path", "oldText": "old_string", "newText": "new_string"},
    "Write": {"path": "file_path"},
    "Grep":  {"path": "path"},
    "Glob":  {"path": "path"},
    "LS":    {"path": "path"},
}


def _map_tool(raw_name: str, raw_input: Any) -> tuple[str, dict]:
    claude_name = _TOOL_NAME_MAP.get(raw_name, raw_name)
    if not isinstance(raw_input, dict):
        return claude_name, {"value": raw_input}
    key_map = _TOOL_INPUT_KEY_MAP.get(claude_name, {})
    return claude_name, {key_map.get(k, k): v for k, v in raw_input.items()}


# ============================================================================
# Event normalization — pi AgentSessionEvent → Claude jsonl shape
# ============================================================================
def _flatten_content_text(content: Any) -> str:
    """Flatten a pi content-block list (text/image blocks) to a string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            parts.append(f"[image {block.get('mimeType', '')}]")
    return "\n".join(p for p in parts if p)


def _pi_timestamp(message: dict) -> str:
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return datetime.fromtimestamp(ts / 1000.0).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now().isoformat()


def _assistant_content_blocks(message: dict) -> list[dict]:
    """Map pi AssistantMessage content blocks to Claude content blocks."""
    blocks: list[dict] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif btype == "thinking":
            blocks.append({"type": "thinking", "thinking": str(block.get("thinking") or "")})
        elif btype == "toolCall":
            name, mapped_input = _map_tool(
                str(block.get("name") or "unknown"),
                block.get("arguments") or {},
            )
            blocks.append({
                "type": "tool_use",
                "id": str(block.get("id") or _new_uuid()),
                "name": name,
                "input": mapped_input,
            })
    return blocks


def normalize_assistant_message(
    message: dict,
    *,
    parent_uuid: str,
    msg_uuid: str,
    fallback_model: str,
) -> Optional[dict]:
    """pi AssistantMessage → Claude-shaped assistant event."""
    blocks = _assistant_content_blocks(message)
    if not blocks:
        return None
    model = str(message.get("model") or "") or fallback_model
    provider = str(message.get("provider") or "")
    if provider and "/" not in model:
        model = f"{provider}/{model}"
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": blocks,
            "model": model,
        },
        "uuid": msg_uuid,
        "parentUuid": parent_uuid,
        "timestamp": _pi_timestamp(message),
    }


def normalize_tool_result_message(
    message: dict,
    *,
    parent_uuid: str,
    session_id: Optional[str],
) -> Optional[dict]:
    """pi ToolResultMessage → Claude-shaped user/tool_result event.
    Uses a deterministic uuid derived from the toolCallId so the
    message_start/message_end double emission collapses in dedup."""
    tool_call_id = str(message.get("toolCallId") or "")
    if not tool_call_id:
        return None
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": _flatten_content_text(message.get("content")),
                "is_error": bool(message.get("isError", False)),
            }],
        },
        "uuid": _tool_result_uuid(session_id, tool_call_id),
        "parentUuid": parent_uuid,
        "timestamp": _pi_timestamp(message),
    }


def normalize_unknown_event(raw: dict, parent_uuid: str) -> dict:
    """Surface an unrecognized pi event as a diagnostic row rather than
    silently dropping it (same contract as runner_gemini)."""
    return {
        "type": "unknown_event",
        "raw_type": raw.get("type"),
        "raw": raw,
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


# Event types that are pure bookkeeping — consumed or intentionally skipped,
# never surfaced as diagnostics. tool_execution_* are skipped because the
# toolCall lives in the assistant message content and the result arrives as
# a ToolResultMessage via message_start/message_end.
_BOOKKEEPING_EVENT_TYPES = {
    "session",
    "agent_start", "agent_end",
    "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
    "queue_update",
    "compaction_start", "compaction_end",
    "auto_retry_start", "auto_retry_end",
}

# Message roles handled by the streaming loop; other roles (custom,
# bashExecution, branchSummary, compactionSummary) carry no render-tree
# content for Better Agent and are skipped.
_SKIPPED_MESSAGE_ROLES = {"user", "custom", "bashExecution", "branchSummary", "compactionSummary"}

_TERMINAL_STOP_REASONS = {"error", "aborted"}


def error_from_assistant_message(message: dict) -> Optional[str]:
    """Terminal error carried on a pi assistant message, if any. pi's json
    print mode exits 0 on model errors — this is the only error signal."""
    stop_reason = message.get("stopReason")
    if stop_reason not in _TERMINAL_STOP_REASONS:
        return None
    return str(message.get("errorMessage") or f"Request {stop_reason}")


def _usage_from_message(message: dict) -> dict:
    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    out = {
        "input_tokens": int(usage.get("input") or 0),
        "output_tokens": int(usage.get("output") or 0),
        "cache_read_input_tokens": int(usage.get("cacheRead") or 0),
        "total_tokens": int(usage.get("totalTokens") or 0),
    }
    return out if any(out.values()) else {}


def _sum_usage(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = int(out.get(k, 0)) + int(v)
    return out


# ============================================================================
# Session file discovery — resume/fork target resolution
# ============================================================================
def session_dir_for_run(run_dir: Path) -> Path:
    return run_dir / PI_SESSION_DIR_NAME


def find_session_file_for_sid(session_id: str) -> Optional[Path]:
    """Locate the pi session jsonl for a session id across run dirs.
    pi names session files `<timestamp>_<sessionId>.jsonl` under
    `<run_dir>/pi-sessions/--<cwd-slug>--/`. Newest match wins."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    root = _runs_root()
    if not root.is_dir():
        return None
    matches: list[Path] = []
    for run_dir in root.iterdir():
        sess_root = run_dir / PI_SESSION_DIR_NAME
        if not sess_root.is_dir():
            continue
        matches.extend(sess_root.glob(f"**/*_{sid}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


# ============================================================================
# Attachments
# ============================================================================
def _materialize_attachments(run_dir: Path, images: list) -> list[Path]:
    """Decode base64 image attachments to disk; pi inlines them via `@path`
    positional arguments (its `[@files...]` prompt-inclusion path)."""
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        ext = str(img.get("media_type") or "image/png").split("/")[-1].replace("jpeg", "jpg")
        fpath = att_dir / f"attachment_{i}.{ext}"
        fpath.write_bytes(base64.b64decode(img["data"]))
        paths.append(fpath)
    return paths


def _inline_file_attachments(prompt: str, files: list, log: logging.Logger) -> str:
    sections: list[str] = []
    for f in files:
        try:
            raw = base64.b64decode(f.get("data", ""))
            name = f.get("name", "unknown")
        except Exception:
            log.warning("Skipping malformed file attachment: %s", f.get("name", "?"))
            continue
        try:
            sections.append(f"<file name=\"{name}\">\n{raw.decode('utf-8')}\n</file>")
        except UnicodeDecodeError:
            sections.append(
                f"<file name=\"{name}\">[binary file, {f.get('size', len(raw))} bytes]</file>"
            )
    if not sections:
        return prompt
    preamble = "\n\n".join(sections)
    return f"{preamble}\n\n{prompt}" if prompt else preamble


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_pi fatal: %s", error)
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


# ============================================================================
# Main async runner
# ============================================================================
async def _run(run_dir: Path, inputs: dict) -> int:
    log = logging.getLogger("runner_pi")

    pi_bin = resolve_cli_binary("pi")
    if not pi_bin:
        _fail(run_dir, "pi CLI not found on PATH")
        return 1

    cwd = inputs.get("cwd")
    if not cwd:
        _fail(run_dir, "missing required field: cwd")
        return 1
    prompt = str(inputs.get("prompt") or "")
    images = inputs.get("images") or []
    files = inputs.get("files") or []
    if not prompt and not images and not files:
        _fail(run_dir, "missing required field: prompt")
        return 1

    prompt = prepend_capability_context(prompt, inputs)
    prompt = _inline_file_attachments(prompt, files, log)

    model = str(inputs.get("model") or "").strip()
    reasoning_effort = str(inputs.get("reasoning_effort") or "").strip()
    session_id = str(inputs.get("session_id") or "").strip()
    fork = bool(inputs.get("fork"))
    permission = inputs.get("permission") or {}
    permission_mode = permission.get("mode") if isinstance(permission, dict) else None

    session_dir = session_dir_for_run(run_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    argv: list[str] = [pi_bin, "--mode", "json", "-p", "--session-dir", str(session_dir)]
    if model:
        argv += ["--model", model]
    if reasoning_effort:
        argv += ["--thinking", reasoning_effort]
    if permission_mode == "plan":
        # Read-only turn: pi disables every tool; the model can only answer.
        argv += ["--no-tools"]
    if session_id:
        prior = find_session_file_for_sid(session_id)
        if prior is None:
            _fail(
                run_dir,
                f"pi session file for session id {session_id!r} not found under "
                f"{_runs_root()} — the originating run dir may have been pruned. "
                f"Start a fresh session.",
            )
            return 1
        argv += (["--fork", str(prior)] if fork else ["--session", str(prior)])
    elif fork:
        _fail(run_dir, "fork requested without a source session_id")
        return 1

    attachment_paths = _materialize_attachments(run_dir, images) if images else []
    argv += [f"@{p}" for p in attachment_paths]

    state: dict[str, Any] = {
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
    cancel_path = run_dir / "cancel"

    discovered_sid: Optional[str] = None
    parent_uuid = _new_uuid()
    total_usage: dict = {}
    error: Optional[str] = None
    cancelled = False
    assistant_seen = False
    session_lost = False
    # uuid of the in-flight assistant message (message_start → message_end);
    # streaming updates rewrite the same uuid so the render tree replaces
    # in place while events.jsonl appends per delta (gemini-family semantics).
    current_assistant_uuid: Optional[str] = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=os.environ.copy(),
            **_process_control().detach_spawn_kwargs(),
            limit=16 * 1024 * 1024,
        )
    except FileNotFoundError:
        _fail(run_dir, "pi CLI not found on PATH")
        return 1

    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()

    cancel_seen = asyncio.Event()

    async def _drain_stderr() -> None:
        try:
            with (run_dir / "pi_stderr.log").open("ab") as f:
                while True:
                    chunk = await proc.stderr.read(8192)
                    if not chunk:
                        return
                    f.write(chunk)
                    f.flush()
        except Exception:
            log.exception("pi stderr drain failed")

    stderr_task = asyncio.create_task(_drain_stderr())

    async def _cancel_watcher() -> None:
        nonlocal cancelled
        while not cancel_seen.is_set():
            if cancel_path.exists():
                cancelled = True
                log.info("cancel sentinel seen, terminating pi tree")
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
    unknown_types_seen: set[str] = set()

    def _write_event(events_file, normalized: Optional[dict], *, advance_parent: bool) -> None:
        nonlocal parent_uuid
        if normalized is None:
            return
        events_file.write(json.dumps(normalized) + "\n")
        events_file.flush()
        if advance_parent and normalized.get("uuid"):
            parent_uuid = normalized["uuid"]

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
                if not isinstance(raw_event, dict):
                    continue

                etype = raw_event.get("type")

                if etype == "session":
                    sid = raw_event.get("id")
                    if sid:
                        discovered_sid = str(sid)
                        if session_id and not fork:
                            mismatch = resume_session_mismatch("pi", session_id, discovered_sid)
                            if mismatch:
                                error = mismatch.message
                                session_lost = True
                                break
                        state["session_id"] = discovered_sid
                        state["jsonl_path"] = str(events_path)
                        atomic_write_json(state_path, state)
                    continue

                if etype in ("message_start", "message_update", "message_end"):
                    message = raw_event.get("message")
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")

                    if role == "assistant":
                        if etype == "message_start" or current_assistant_uuid is None:
                            current_assistant_uuid = _new_uuid()
                        normalized = normalize_assistant_message(
                            message,
                            parent_uuid=parent_uuid,
                            msg_uuid=current_assistant_uuid,
                            fallback_model=model or "pi",
                        )
                        if normalized is not None and any(
                            b.get("type") == "text" and b.get("text", "").strip()
                            for b in normalized["message"]["content"]
                        ):
                            assistant_seen = True
                        _write_event(
                            events_file, normalized,
                            advance_parent=(etype == "message_end"),
                        )
                        if etype == "message_end":
                            current_assistant_uuid = None
                            usage = _usage_from_message(message)
                            if usage:
                                total_usage = _sum_usage(total_usage, usage)
                            terminal = error_from_assistant_message(message)
                            if terminal:
                                error = terminal
                        continue

                    if role == "toolResult":
                        if etype != "message_end":
                            continue
                        normalized = normalize_tool_result_message(
                            message,
                            parent_uuid=parent_uuid,
                            session_id=discovered_sid,
                        )
                        _write_event(events_file, normalized, advance_parent=True)
                        continue

                    if role in _SKIPPED_MESSAGE_ROLES:
                        continue
                    # Unknown message role — surface, never drop.
                    _write_event(
                        events_file,
                        normalize_unknown_event(raw_event, parent_uuid),
                        advance_parent=False,
                    )
                    continue

                if etype in _BOOKKEEPING_EVENT_TYPES:
                    continue

                if etype not in unknown_types_seen:
                    unknown_types_seen.add(str(etype))
                    log.warning("runner_pi: unknown pi event type %r — surfacing as diagnostic", etype)
                _write_event(
                    events_file,
                    normalize_unknown_event(raw_event, parent_uuid),
                    advance_parent=False,
                )
    except Exception as exc:
        log.exception("pi runner stream loop failed")
        error = error or f"{type(exc).__name__}: {exc}"
    finally:
        cancel_seen.set()
        if not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass

    # Session-loss guard tripped mid-stream: fail closed — kill the CLI
    # instead of letting the wrong session's turn run out.
    if session_lost and proc.returncode is None:
        _process_control().force_kill(proc.pid)

    await proc.wait()
    try:
        await asyncio.wait_for(stderr_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        stderr_task.cancel()

    if proc.returncode != 0 and not error and not cancelled:
        stderr_text = ""
        stderr_log = run_dir / "pi_stderr.log"
        if stderr_log.exists():
            try:
                stderr_text = stderr_log.read_text(encoding="utf-8")
            except OSError:
                pass
        error = (
            stderr_error("pi", stderr_text)
            or f"pi CLI exited with code {proc.returncode}"
        )

    if cancelled and not error:
        error = "cancelled"

    if not error and not cancelled and not assistant_seen:
        error = "pi CLI exited without emitting any assistant output"

    final_success = not cancelled and not error

    # The error IS the run's final answer — emit it as a regular assistant
    # text event so content derivation populates msg.content (gemini parity).
    if error and not final_success:
        try:
            error_event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Error: {error}"}],
                    "model": model or "pi",
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

    finished_at = datetime.now().isoformat()
    try:
        (run_dir / "complete.json").write_text(json.dumps({
            "success": final_success,
            "session_id": discovered_sid or (session_id or None),
            "error": error,
            "token_usage": total_usage or None,
            "finished_at": finished_at,
        }, indent=2), encoding="utf-8")
    except Exception:
        log.exception("failed to write complete.json")

    state["complete"] = True
    state["finished_at"] = finished_at
    if discovered_sid and not state.get("session_id"):
        state["session_id"] = discovered_sid
    try:
        atomic_write_json(state_path, state)
    except Exception:
        log.exception("failed to finalize state.json")

    return 0 if final_success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_pi %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("runner_pi").info("starting for run_dir=%s", run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as exc:
        _fail(run_dir, f"failed to read input.json: {exc}")
        return 1

    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as exc:
        logger.exception("runner_pi top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
