"""Cursor CLI runner — detached per-run executable.

Spawned by `CursorProvider.start_run` as a detached subprocess. Handles one
`cursor-agent` run via `cursor-agent --print --output-format stream-json
--stream-partial-output <prompt>`. Parses the CLI's stream-json events from
stdout, normalizes each to Claude jsonl shape, and appends to
`<run_dir>/session_events.jsonl` so the provider (GeminiProvider tailer
machinery) can tail it. Streaming only — no render-tree mutation.

Event shapes were verified against the installed CLI bundle
(~/.local/share/cursor-agent/versions/2025.11.25-d5b3271, the stream-json
emitter in 7434.index.js):
  {"type":"system","subtype":"init","session_id":...,"model":...,"cwd":...}
  {"type":"user","message":{"role":"user","content":[{"type":"text",...}]}}
  {"type":"assistant","message":{...content:[{type:"text",text:<delta>}]}}
  {"type":"thinking","subtype":"delta"|"completed","text":...}
  {"type":"tool_call","subtype":"started"|"completed","call_id":...,
   "tool_call":{"<case>ToolCall":{"args":{...},"result":{...}}}}
  {"type":"result","subtype":"success","is_error":bool,"result":...,
   "session_id":...}
With --stream-partial-output the CLI emits per-delta assistant/thinking
events (mutually exclusive with the accumulated stream-json flushes — the
emitter's else-if chain proves it), so this runner accumulates deltas into
one stable uuid per segment, mirroring runner_gemini.

The `session_id` on the init event is the chatId accepted by
`cursor-agent --resume <chatId>`; it is captured into state.json for
multi-turn resume.

Cancel sentinel: backend writes `run_dir/cancel`, runner terminates the
cursor-agent subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from capability_contexts import prepend_capability_context
from cli_paths import resolve_cli_binary
from proc_control import process_control as _process_control
from runs_dir import atomic_write_json

logger = logging.getLogger(__name__)

# Deterministic UUID namespace: the started/completed halves of one tool call
# and re-replays of the same stream collide in the render tree's uuid dedup
# instead of duplicating.
_CURSOR_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.runner_cursor.events")

# Better Agent permission mode → cursor-agent argv. Fail closed: only the
# explicit full-permission mode passes -f/--force ("Force allow commands
# unless explicitly denied"); every other/unknown value runs the CLI's
# default headless approval behavior.
CURSOR_PERMISSION_MODES = ("default", "force")


def permission_argv(permission: Any) -> list[str]:
    mode = permission.get("mode") if isinstance(permission, dict) else None
    if mode == "force":
        return ["-f"]
    return []


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _tool_call_uuid(session_id: Optional[str], call_id: Optional[str], phase: str) -> str:
    if session_id and call_id:
        return str(uuid.uuid5(_CURSOR_UUID_NAMESPACE, f"{session_id}|{call_id}|{phase}"))
    return _new_uuid()


# ---------------------------------------------------------------------------
# Tool mapping — cursor tool_call oneof case → Claude tool name + input keys.
# The emitted `tool_call` value is protobuf-es proto-JSON: one lowerCamel
# oneof key ("shellToolCall", "readToolCall", ...) wrapping {args, result}
# (verified from agent.v1.ToolCall's field list in the CLI bundle).
# Unmapped cases pass through with the raw case name so a new cursor tool
# still renders as a generic card. INVARIANT: only keys are translated —
# values pass through untouched.
# ---------------------------------------------------------------------------
_TOOL_CASE_MAP = {
    "shellToolCall": "Bash",
    "readToolCall": "Read",
    "writeToolCall": "Write",
    "editToolCall": "Edit",
    "grepToolCall": "Grep",
    "globToolCall": "Glob",
    "lsToolCall": "LS",
    "deleteToolCall": "Delete",
    "updateTodosToolCall": "TodoWrite",
    "readTodosToolCall": "TodoRead",
    "semSearchToolCall": "SemSearch",
    "webSearchToolCall": "WebSearch",
    "fetchToolCall": "WebFetch",
    "taskToolCall": "Task",
    "mcpToolCall": "MCP",
}

_TOOL_INPUT_KEY_MAP = {
    "Bash": {"command": "command", "workingDirectory": "cwd"},
    "Read": {"path": "file_path"},
    "Write": {"path": "file_path", "contents": "content", "fileText": "content"},
    "Edit": {"path": "file_path", "old": "old_string", "new": "new_string",
             "oldString": "old_string", "newString": "new_string"},
    "Grep": {"pattern": "pattern", "path": "path"},
    "Glob": {"globPattern": "pattern", "pattern": "pattern"},
    "LS": {"path": "path"},
    "WebSearch": {"query": "query"},
    "WebFetch": {"url": "url"},
}


def _tool_case(tool_call: Any) -> tuple[str, dict[str, Any]]:
    """Extract (oneof_case, payload) from a proto-JSON ToolCall object."""
    if isinstance(tool_call, dict):
        for key, value in tool_call.items():
            if key.endswith("ToolCall") and isinstance(value, dict):
                return key, value
    return "", {}


def _map_tool(case: str, args: Any) -> tuple[str, dict[str, Any]]:
    name = _TOOL_CASE_MAP.get(case, case or "tool")
    if not isinstance(args, dict):
        return name, ({"value": args} if args is not None else {})
    key_map = _TOOL_INPUT_KEY_MAP.get(name, {})
    return name, {key_map.get(k, k): v for k, v in args.items()}


def _stringify_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    if isinstance(result, list):
        parts = [_stringify_result(p) for p in result]
        return "\n".join(p for p in parts if p)
    if isinstance(result, dict):
        for key in ("stdout", "output", "content", "contents", "text", "message"):
            if isinstance(result.get(key), str):
                return result[key]
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


# Failure-ish oneof cases of agent.v1.ShellResult (and analogous result
# protos): anything not "success" is surfaced as an error tool_result.
_FAILURE_RESULT_CASES = (
    "failure", "timeout", "rejected", "spawnError", "permissionDenied", "error",
)


def _tool_result_payload(payload: dict[str, Any]) -> tuple[str, bool]:
    """(content, is_error) from a ToolCall oneof payload's `result` field."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return _stringify_result(result), False
    for case in _FAILURE_RESULT_CASES:
        if case in result:
            return _stringify_result(result[case]), True
    if "success" in result:
        return _stringify_result(result["success"]), False
    return _stringify_result(result), False


# ---------------------------------------------------------------------------
# Stream normalizer — one instance per CLI attempt. Feeds raw stream-json
# events in, yields Claude-shaped jsonl lines out. Pure state machine (no
# I/O) so tests can drive it directly.
# ---------------------------------------------------------------------------
class CursorStreamNormalizer:
    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.model: str = "cursor"
        self.result_seen = False
        self.success = False
        self.is_error = False
        self.result_text = ""
        self.error: Optional[str] = None
        self.assistant_seen = False
        self.duration_ms: Optional[int] = None
        self._parent_uuid = _new_uuid()
        # Per-segment accumulation (assistant text / thinking): same uuid is
        # re-emitted with grown content; downstream apply_event replaces on
        # uuid match. A tool_call closes the current segments.
        self._segment_uuid: dict[str, str] = {}
        self._segment_text: dict[str, str] = {}

    # -- helpers -----------------------------------------------------------
    def _timestamp(self, raw: dict) -> str:
        ts_ms = raw.get("timestamp_ms")
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            return datetime.fromtimestamp(ts_ms / 1000.0).isoformat()
        return datetime.now().isoformat()

    def _close_segments(self) -> None:
        self._segment_uuid.clear()
        self._segment_text.clear()

    def _segment(self, kind: str, delta: str) -> tuple[str, str]:
        if kind not in self._segment_uuid:
            self._segment_uuid[kind] = _new_uuid()
            self._segment_text[kind] = ""
        self._segment_text[kind] += delta
        return self._segment_uuid[kind], self._segment_text[kind]

    def _advance_parent(self, event: dict) -> None:
        new_uuid = event.get("uuid")
        if new_uuid:
            self._parent_uuid = new_uuid

    # -- event handlers ----------------------------------------------------
    def handle(self, raw: dict) -> list[dict]:
        etype = raw.get("type")
        if etype == "system":
            return self._handle_system(raw)
        if etype == "user":
            return []  # prompt echo — the render tree already has it
        if etype == "assistant":
            return self._handle_assistant(raw)
        if etype == "thinking":
            return self._handle_thinking(raw)
        if etype == "tool_call":
            return self._handle_tool_call(raw)
        if etype == "result":
            return self._handle_result(raw)
        return [self._unknown(raw)]

    def _handle_system(self, raw: dict) -> list[dict]:
        if raw.get("subtype") == "init":
            sid = raw.get("session_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            model = raw.get("model")
            if isinstance(model, str) and model:
                self.model = model
        return []

    def _extract_text(self, raw: dict) -> str:
        message = raw.get("message")
        if not isinstance(message, dict):
            return ""
        parts: list[str] = []
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        elif isinstance(content, str):
            parts.append(content)
        return "".join(parts)

    def _handle_assistant(self, raw: dict) -> list[dict]:
        delta = self._extract_text(raw)
        if not delta:
            return []
        seg_uuid, text = self._segment("assistant", delta)
        if text.strip():
            self.assistant_seen = True
        return [{
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": self.model,
            },
            "uuid": seg_uuid,
            "parentUuid": self._parent_uuid,
            "timestamp": self._timestamp(raw),
        }]

    def _handle_thinking(self, raw: dict) -> list[dict]:
        if raw.get("subtype") == "completed":
            self._segment_uuid.pop("thinking", None)
            self._segment_text.pop("thinking", None)
            return []
        delta = raw.get("text")
        if not isinstance(delta, str) or not delta:
            return []
        seg_uuid, text = self._segment("thinking", delta)
        return [{
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": text}],
                "model": self.model,
            },
            "uuid": seg_uuid,
            "parentUuid": self._parent_uuid,
            "timestamp": self._timestamp(raw),
        }]

    def _handle_tool_call(self, raw: dict) -> list[dict]:
        subtype = raw.get("subtype")
        call_id = str(raw.get("call_id") or "") or _new_uuid()
        case, payload = _tool_case(raw.get("tool_call"))
        self._close_segments()

        if subtype == "started":
            name, mapped_input = _map_tool(case, payload.get("args"))
            event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": mapped_input,
                    }],
                    "model": self.model,
                },
                "uuid": _tool_call_uuid(self.session_id, call_id, "started"),
                "parentUuid": self._parent_uuid,
                "timestamp": self._timestamp(raw),
            }
            self._advance_parent(event)
            return [event]

        if subtype == "completed":
            content, is_error = _tool_result_payload(payload)
            event = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": content,
                        "is_error": is_error,
                    }],
                },
                "uuid": _tool_call_uuid(self.session_id, call_id, "completed"),
                "parentUuid": self._parent_uuid,
                "timestamp": self._timestamp(raw),
            }
            self._advance_parent(event)
            return [event]

        return [self._unknown(raw)]

    def _handle_result(self, raw: dict) -> list[dict]:
        self.result_seen = True
        self.is_error = bool(raw.get("is_error"))
        self.success = raw.get("subtype") == "success" and not self.is_error
        self.result_text = str(raw.get("result") or "")
        duration = raw.get("duration_ms")
        if isinstance(duration, (int, float)):
            self.duration_ms = int(duration)
        if not self.success and not self.error:
            self.error = self.result_text or f"cursor-agent result subtype={raw.get('subtype')!r}"
        sid = raw.get("session_id")
        if isinstance(sid, str) and sid and not self.session_id:
            self.session_id = sid
        return []

    def _unknown(self, raw: dict) -> dict:
        # INVARIANT: every stream-json line is either normalized or surfaced
        # verbatim as a diagnostic — never silently dropped.
        return {
            "type": "unknown_event",
            "raw_type": raw.get("type"),
            "raw": raw,
            "uuid": _new_uuid(),
            "parentUuid": self._parent_uuid,
            "timestamp": self._timestamp(raw),
        }


# ---------------------------------------------------------------------------
# stderr / auth diagnostics
# ---------------------------------------------------------------------------
_AUTH_ERROR_RE = re.compile(r"authentication required|not authenticated|cursor-agent login", re.IGNORECASE)


def auth_failure_from_output(stdout: str, stderr: str) -> Optional[str]:
    if _AUTH_ERROR_RE.search(f"{stdout}\n{stderr}"):
        return (
            "Cursor CLI is not authenticated. Run `cursor-agent login` "
            "(or set CURSOR_API_KEY) and retry."
        )
    return None


def _extract_stderr_error(stderr_text: str) -> Optional[str]:
    for raw_line in stderr_text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("error:"):
            return line
    for raw_line in reversed(stderr_text.splitlines()):
        line = raw_line.strip()
        if line:
            return line
    return None


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_cursor fatal: %s", error)
    atomic_write_json(run_dir / "complete.json", {
        "success": False,
        "session_id": None,
        "error": error,
        "token_usage": None,
        "finished_at": datetime.now().isoformat(),
    })


def _prepend_capability_context(prompt: str, inputs: dict[str, Any]) -> str:
    return prepend_capability_context(prompt, inputs)


def _inject_file_attachments(prompt: str, files: list) -> str:
    """Inline text-file attachments into the prompt (cursor-agent headless
    accepts prompt text only)."""
    import base64
    sections: list[str] = []
    for f in files:
        try:
            raw = base64.b64decode(f.get("data", ""))
            name = f.get("name", "unknown")
        except Exception:
            logger.warning("skipping malformed file attachment: %s", f.get("name", "?"))
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


def build_argv(
    *,
    cursor_bin: str,
    prompt: str,
    model: str,
    session_id: str,
    permission: Any,
) -> list[str]:
    """Argv for one headless cursor-agent turn. Prompt is passed after `--`
    so prompt text can never be parsed as a flag (no argv injection)."""
    argv = [cursor_bin, "--print", "--output-format", "stream-json", "--stream-partial-output"]
    argv += permission_argv(permission)
    if model:
        argv += ["--model", model]
    if session_id:
        argv += ["--resume", session_id]
    argv += ["--", prompt]
    return argv


async def _run(run_dir: Path, inputs: dict[str, Any]) -> int:
    cursor_bin = resolve_cli_binary("cursor-agent")
    if not cursor_bin:
        _fail(run_dir, "cursor-agent CLI not found on PATH")
        return 1

    prompt = _prepend_capability_context(str(inputs.get("prompt") or ""), inputs)
    prompt = _inject_file_attachments(prompt, inputs.get("files") or [])
    model = str(inputs.get("model") or "").strip()
    cwd = str(inputs.get("cwd") or os.getcwd())
    session_id = str(inputs.get("session_id") or "").strip()
    if not prompt:
        _fail(run_dir, "missing required field: prompt")
        return 1

    argv = build_argv(
        cursor_bin=cursor_bin,
        prompt=prompt,
        model=model,
        session_id=session_id,
        permission=inputs.get("permission"),
    )

    state: dict[str, Any] = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": session_id or None,
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "complete": False,
    }
    state_path = run_dir / "state.json"
    events_path = run_dir / "session_events.jsonl"
    # Persist early when resuming so the provider bootstraps before the
    # first event lands; fresh runs write state on the init event.
    if session_id:
        atomic_write_json(state_path, state)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=os.environ.copy(),
        **_process_control().detach_spawn_kwargs(),
        limit=16 * 1024 * 1024,
    )

    normalizer = CursorStreamNormalizer()
    cancelled = False
    cancel_path = run_dir / "cancel"
    cancel_seen = asyncio.Event()

    async def _cancel_watcher() -> None:
        nonlocal cancelled
        while not cancel_seen.is_set():
            if cancel_path.exists():
                cancelled = True
                logger.info("cancel sentinel seen, terminating cursor-agent")
                pc = _process_control()
                pc.signal_stop(proc.pid)
                for _ in range(30):
                    if proc.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                if proc.returncode is None:
                    pc.force_kill(proc.pid)
                cancel_seen.set()
                return
            try:
                await asyncio.wait_for(cancel_seen.wait(), timeout=0.15)
            except asyncio.TimeoutError:
                pass

    async def _drain_stderr() -> None:
        try:
            with (run_dir / "cursor_stderr.log").open("ab") as fh:
                while True:
                    chunk = await proc.stderr.read(8192)
                    if not chunk:
                        return
                    fh.write(chunk)
                    fh.flush()
        except Exception:
            logger.exception("cursor stderr drain failed")

    cancel_task = asyncio.create_task(_cancel_watcher())
    stderr_task = asyncio.create_task(_drain_stderr())

    error: Optional[str] = None
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

                had_sid = bool(normalizer.session_id)
                for normalized in normalizer.handle(raw_event):
                    events_file.write(json.dumps(normalized) + "\n")
                    events_file.flush()
                if normalizer.session_id and not had_sid:
                    state["session_id"] = normalizer.session_id
                    atomic_write_json(state_path, state)
                if normalizer.result_seen:
                    break
    except asyncio.CancelledError:
        error = "cancelled"
    except Exception as exc:
        logger.exception("cursor runner stream loop failed")
        error = f"{type(exc).__name__}: {exc}"
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
    except (asyncio.TimeoutError, asyncio.CancelledError):
        stderr_task.cancel()

    stderr_text = ""
    stderr_log = run_dir / "cursor_stderr.log"
    if stderr_log.exists():
        try:
            stderr_text = stderr_log.read_text(encoding="utf-8")
        except OSError:
            pass

    if not error:
        error = normalizer.error
    auth_error = auth_failure_from_output("", stderr_text)
    if auth_error and not normalizer.success:
        error = auth_error
    if proc.returncode != 0 and not error and not cancelled:
        error = _extract_stderr_error(stderr_text) or (
            f"cursor-agent exited with code {proc.returncode}"
        )
    if not normalizer.result_seen and not error and not cancelled:
        error = "cursor-agent exited without emitting a result event"
    if cancelled:
        error = "cancelled"

    final_success = (
        normalizer.success and proc.returncode == 0 and not cancelled and not error
    )

    # A failed run's error IS the final answer — emit it as regular assistant
    # text so content derivation surfaces it (mirrors runner_gemini).
    if error and not final_success:
        try:
            with events_path.open("a", encoding="utf-8") as ef:
                ef.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"Error: {error}"}],
                        "model": normalizer.model,
                    },
                    "uuid": _new_uuid(),
                    "parentUuid": _new_uuid(),
                    "timestamp": datetime.now().isoformat(),
                    "isApiErrorMessage": True,
                }) + "\n")
        except Exception:
            logger.exception("failed to emit error event to session_events.jsonl")

    token_usage = (
        {"duration_ms": normalizer.duration_ms}
        if normalizer.duration_ms is not None else None
    )
    atomic_write_json(run_dir / "complete.json", {
        "success": final_success,
        "session_id": normalizer.session_id or session_id or None,
        "error": None if final_success else error,
        "token_usage": token_usage,
        "finished_at": datetime.now().isoformat(),
    })

    state["complete"] = True
    state["finished_at"] = datetime.now().isoformat()
    if normalizer.session_id:
        state["session_id"] = normalizer.session_id
    atomic_write_json(state_path, state)
    return 0 if final_success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_cursor %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        logger.exception("runner_cursor top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
