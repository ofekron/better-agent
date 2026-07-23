"""Detached runner for Moonshot AI's Kimi CLI.

Per turn: spawns `kimi --print --output-format stream-json --session
<sid>` with the prompt piped over stdin, parses Kimi's stream-json
stdout (one kosong `Message` JSON object per line, per kimi-cli 0.75
`ui/print/visualize.py::JsonPrinter`), normalizes each message to Claude
jsonl shape, and appends to `<run_dir>/session_events.jsonl` — which the
KimiProvider tails for the render tree. Writes `state.json` immediately
(the session id is pre-generated: `--session` with an unknown id CREATES
that session, per kimi-cli `cli/__init__.py`), and `complete.json` on
exit.

Kimi stream-json line shapes (source-derived, not guessed):
  assistant: {"role": "assistant", "content": <str | [parts]>,
              "tool_calls": [{"type": "function", "id": ...,
                              "function": {"name": ..., "arguments": <json str>}}]}
  tool:      {"role": "tool", "content": <str | [parts]>, "tool_call_id": ...}
Content parts: {"type": "text", "text"} / {"type": "think", "think"} /
image_url / audio_url / video_url. A single TextPart serializes as a
plain string. Errors (auth, LLM provider) print as plain non-JSON text
on stdout with a non-zero exit code.

Print mode implicitly runs `--yolo` (kimi-cli auto-approves everything
non-interactively), so there is no approval round-trip. The runner only
streams: no render-tree mutation, no secret material in argv or logs.
"""

from __future__ import annotations

import argparse
import asyncio
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
from runner_errors import classify
from runs_dir import atomic_write_json
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES

logger = logging.getLogger(__name__)

# Deterministic UUID namespace so replaying the same stdout stream (crash
# recovery re-reads session_events.jsonl, not stdout) keeps stable render
# uuids for the same (session, event ordinal).
_KIMI_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.runner_kimi.events")

# kimi-cli built-in tool names → Claude canonical names so the existing
# frontend tool renderers (icons, diffs, expansion) light up for Kimi.
# Anything not listed passes through verbatim as a generic tool card.
_TOOL_NAME_MAP = {
    "Shell": "Bash",
    "ReadFile": "Read",
    "WriteFile": "Write",
    "StrReplaceFile": "Edit",
    "FetchURL": "WebFetch",
    "SearchWeb": "WebSearch",
    "SetTodoList": "TodoWrite",
}

# Per-tool input KEY translation to Claude's canonical schema. Values pass
# through; unlisted keys are forwarded verbatim.
_TOOL_INPUT_KEY_MAP = {
    "Read": {"path": "file_path"},
    "Write": {"path": "file_path"},
    "Edit": {"path": "file_path"},
    "Glob": {"directory": "path"},
}


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _event_uuid(session_id: Optional[str], event_key: str) -> str:
    if session_id and event_key:
        return str(uuid.uuid5(_KIMI_UUID_NAMESPACE, f"{session_id}|{event_key}"))
    return _new_uuid()


def _map_tool(raw_name: str, raw_input: Any) -> tuple[str, dict]:
    claude_name = _TOOL_NAME_MAP.get(raw_name, raw_name)
    if not isinstance(raw_input, dict):
        return claude_name, {"input": raw_input}
    key_map = _TOOL_INPUT_KEY_MAP.get(claude_name, {})
    return claude_name, {key_map.get(k, k): v for k, v in raw_input.items()}


def _iter_parts(content: Any) -> list[dict[str, Any]]:
    """Kimi content is a plain string (single TextPart) or a list of typed
    part dicts. Return a uniform part list."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return []


def _flatten_text(content: Any) -> str:
    return "\n".join(
        str(p.get("text") or "")
        for p in _iter_parts(content)
        if p.get("type") == "text" and p.get("text")
    )


def _agent_message(
    *,
    role: str,
    content: list[dict[str, Any]],
    parent_uuid: str,
    model: str,
    uuid_str: str,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        message["model"] = model
    return {
        "type": "agent_message",
        "data": {
            "type": role,
            "message": message,
            "uuid": uuid_str,
            "parentUuid": parent_uuid,
            "timestamp": datetime.now().isoformat(),
            "parent_tool_use_id": None,
        },
    }


def normalize_kimi_message(
    msg: dict[str, Any],
    *,
    session_id: Optional[str],
    parent_uuid: str,
    model: str,
    event_key: str,
) -> list[dict[str, Any]]:
    """Map one Kimi stream-json line (a kosong Message dump) to zero or
    more Claude-shaped agent_message events."""
    role = msg.get("role")

    if role == "assistant":
        blocks: list[dict[str, Any]] = []
        for part in _iter_parts(msg.get("content")):
            ptype = part.get("type")
            if ptype == "text" and part.get("text"):
                blocks.append({"type": "text", "text": str(part["text"])})
            elif ptype == "think" and part.get("think"):
                blocks.append({"type": "thinking", "thinking": str(part["think"])})
            # image_url / audio_url / video_url parts carry no renderable
            # text for the assistant transcript — skip.
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            raw_args = fn.get("arguments")
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {"input": raw_args}
            if not isinstance(parsed, dict):
                parsed = {"input": parsed}
            name, mapped_input = _map_tool(str(fn.get("name") or "tool"), parsed)
            blocks.append({
                "type": "tool_use",
                "id": str(tc.get("id") or _new_uuid()),
                "name": name,
                "input": mapped_input,
            })
        if not blocks:
            return []
        return [_agent_message(
            role="assistant",
            content=blocks,
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=_event_uuid(session_id, event_key),
        )]

    if role == "tool":
        text = _flatten_text(msg.get("content"))
        # tool_result_to_message wraps errors as `<system>ERROR: ...</system>`
        # (kimi-cli soul/message.py).
        is_error = text.startswith("<system>ERROR:")
        return [_agent_message(
            role="user",
            content=[{
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or ""),
                "content": text,
                "is_error": is_error,
            }],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=_event_uuid(session_id, event_key),
        )]

    # user/system echoes carry no new render content in print mode.
    return []


def build_kimi_argv(
    kimi_bin: str,
    *,
    model: str,
    session_id: str,
    cwd: str,
) -> list[str]:
    """argv for one print-mode turn. The prompt is piped via stdin (never
    argv — keeps it out of `ps` and away from arg-length limits). No
    credential material ever appears here."""
    argv = [
        kimi_bin,
        "--print",
        "--output-format", "stream-json",
        "--session", session_id,
        "--work-dir", cwd,
    ]
    if model:
        argv += ["--model", model]
    return argv


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_kimi fatal: %s", error)
    _write_json(
        run_dir / "complete.json",
        {
            "success": False,
            "session_id": None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        },
    )


def _prepend_capability_context(prompt: str, inputs: dict[str, Any]) -> str:
    return prepend_capability_context(prompt, inputs)


async def _run(run_dir: Path, inputs: dict[str, Any]) -> int:
    kimi_bin = resolve_cli_binary("kimi")
    if not kimi_bin:
        _fail(run_dir, "kimi CLI not found on PATH")
        return 1

    prompt = _prepend_capability_context(str(inputs.get("prompt") or ""), inputs)
    model = str(inputs.get("model") or "").strip()
    cwd = str(inputs.get("cwd") or os.getcwd())
    if not prompt:
        _fail(run_dir, "missing required field: prompt")
        return 1

    # `--session <id>` resumes an existing session or creates one under
    # that id, so a fresh run pre-generates its sid — no discovery needed.
    session_id = str(inputs.get("session_id") or "").strip() or str(uuid.uuid4())

    argv = build_kimi_argv(kimi_bin, model=model, session_id=session_id, cwd=cwd)

    state: dict[str, Any] = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": session_id,
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "complete": False,
    }
    _write_json(run_dir / "state.json", state)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=os.environ.copy(),
        limit=SUBPROCESS_LINE_LIMIT_BYTES,
    )

    assert proc.stdin is not None
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()

    cancel_path = run_dir / "cancel"
    events_path = run_dir / "session_events.jsonl"
    cancelled = False
    parent_uuid = session_id
    event_index = 0
    assistant_seen = False
    plain_output: list[str] = []

    async def _watch_cancel() -> None:
        nonlocal cancelled
        while proc.returncode is None:
            if cancel_path.exists():
                cancelled = True
                proc.terminate()
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
                return
            await asyncio.sleep(0.15)

    async def _drain_stderr() -> None:
        try:
            with (run_dir / "kimi_stderr.log").open("ab") as fh:
                while True:
                    chunk = await proc.stderr.read(8192)
                    if not chunk:
                        return
                    fh.write(chunk)
                    fh.flush()
        except Exception:
            logger.exception("kimi stderr drain failed")

    cancel_task = asyncio.create_task(_watch_cancel())
    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        with events_path.open("a", encoding="utf-8") as events_file:
            async for raw_line in proc.stdout:
                if cancelled:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON stdout is kimi's plain-text error surface
                    # (auth/provider failures print via rich, rc != 0).
                    plain_output.append(line)
                    continue
                if not isinstance(msg, dict):
                    plain_output.append(line)
                    continue
                normalized = normalize_kimi_message(
                    msg,
                    session_id=session_id,
                    parent_uuid=parent_uuid,
                    model=model or "kimi",
                    event_key=str(event_index),
                )
                event_index += 1
                for event in normalized:
                    events_file.write(json.dumps(event) + "\n")
                    events_file.flush()
                    parent_uuid = event["data"]["uuid"]
                    if event["data"]["type"] == "assistant":
                        assistant_seen = True
        await proc.wait()
    finally:
        for task in (cancel_task, stderr_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    success = proc.returncode == 0 and not cancelled
    error: Optional[str] = None
    if cancelled:
        error = "cancelled"
    elif not success:
        stderr_tail = ""
        try:
            stderr_log = run_dir / "kimi_stderr.log"
            if stderr_log.is_file():
                stderr_tail = stderr_log.read_text(encoding="utf-8").strip()[-2000:]
        except OSError:
            pass
        plain_text = "\n".join(plain_output).strip()
        hit = classify("kimi", plain_text, stderr_tail)
        error = (
            (hit.message if hit else None)
            or plain_text
            or stderr_tail
            or f"kimi CLI exited with code {proc.returncode}"
        )
    elif not assistant_seen:
        # rc == 0 with zero assistant output for a non-empty prompt is a
        # ghost completion, not a real success — fail the turn honestly.
        error = "kimi CLI exited without emitting any assistant output"
        success = False

    # Surface the error as the turn's final assistant text so the render
    # tree shows the failure instead of an empty message.
    if error and not cancelled:
        try:
            error_event = _agent_message(
                role="assistant",
                content=[{"type": "text", "text": f"Error: {error}"}],
                parent_uuid=parent_uuid,
                model=model or "kimi",
                uuid_str=_new_uuid(),
            )
            error_event["data"]["isApiErrorMessage"] = True
            with events_path.open("a", encoding="utf-8") as ef:
                ef.write(json.dumps(error_event) + "\n")
        except Exception:
            logger.exception("failed to emit error event to session_events.jsonl")

    state["complete"] = True
    state["finished_at"] = datetime.now().isoformat()
    _write_json(run_dir / "state.json", state)
    _write_json(
        run_dir / "complete.json",
        {
            "success": success,
            "session_id": session_id,
            "error": error,
            # kimi's stream-json surface exposes no token accounting.
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        },
    )
    return 0 if success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_kimi %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
        from runner_operation_host import hydrate_runner_inputs
        inputs = hydrate_runner_inputs(inputs, run_dir)
    except Exception as exc:
        _fail(run_dir, f"failed to read input.json: {exc}")
        return 1
    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as exc:
        logger.exception("runner_kimi top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
