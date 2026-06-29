"""Detached runner for the Copilot CLI.

Per turn: spawns `copilot -p <prompt> --allow-all-tools`, discovers the
session-scoped event log Copilot writes at
`<config_dir>/session-state/<sessionId>.jsonl`, tails it, normalizes each
Copilot event to Claude jsonl shape, and appends to
`<run_dir>/session_events.jsonl` — which the CopilotProvider tails for
the render tree. Writes `state.json` (session_id + jsonl_path) as soon as
the session is discovered, and `complete.json` on exit.

Copilot's non-interactive `-p` mode exits after the turn completes, so
each Better Agent turn is exactly one CLI invocation; multi-turn
conversations resume via `--resume <sessionId>`.
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
from runs_dir import atomic_write_json

logger = logging.getLogger(__name__)

# Deterministic UUID namespace so the live tail and the post-exit final
# flush of the same Copilot event collide in the render tree's uuid dedup
# instead of duplicating.
_COPILOT_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.runner_copilot.events")

_TAIL_POLL_INTERVAL = 0.1

# Copilot event types that carry renderable content. Everything else
# (session.*, assistant.turn_*, session.truncation) is bookkeeping.
_RENDERS = {
    "user.message",
    "assistant.message",
    "tool.execution_start",
    "tool.execution_complete",
}


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
    _write_json(run_dir / "state.json", state)


def _event_uuid(session_id: Optional[str], copilot_event_id: Optional[str]) -> str:
    """Deterministic render uuid for a Copilot event. Falls back to a
    random uuid when we have no stable handle (synthetic events)."""
    if session_id and copilot_event_id:
        return str(uuid.uuid5(_COPILOT_UUID_NAMESPACE, f"{session_id}|{copilot_event_id}"))
    return _new_uuid()


def _agent_message(
    *,
    role: str,
    content: list[dict[str, Any]],
    parent_uuid: str,
    model: str,
    uuid_str: str,
    timestamp: Optional[str] = None,
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
            "timestamp": timestamp or datetime.now().isoformat(),
            "parent_tool_use_id": None,
        },
    }


def _stringify_result(result: Any) -> str:
    """Flatten a Copilot tool result payload to a tool_result content
    string. `result` is usually `{content: ...}` but defend against
    alternative shapes (raw string, list, nested object)."""
    if isinstance(result, dict):
        if "content" in result:
            return _stringify_result(result["content"])
        # Some tools return {output: ...} or a structured payload.
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)
    if isinstance(result, list):
        parts = [_stringify_result(p) for p in result]
        return "\n".join(p for p in parts if p)
    if result is None:
        return ""
    return str(result)


def normalize_copilot_event(
    event: dict[str, Any],
    *,
    session_id: Optional[str],
    parent_uuid: str,
    model: str,
) -> Optional[dict[str, Any]]:
    """Map one Copilot session-state event to a Claude-shaped
    agent_message event, or None if the event carries no renderable
    content (turn/session bookkeeping)."""
    etype = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    copilot_id = event.get("id")
    ts = event.get("timestamp")
    uid = _event_uuid(session_id, copilot_id)

    if etype == "user.message":
        text = str(data.get("content") or "")
        if not text:
            return None
        return _agent_message(
            role="user",
            content=[{"type": "text", "text": text}],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=uid,
            timestamp=ts,
        )

    if etype == "assistant.message":
        text = str(data.get("content") or "")
        if not text:
            return None
        return _agent_message(
            role="assistant",
            content=[{"type": "text", "text": text}],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=uid,
            timestamp=ts,
        )

    if etype == "tool.execution_start":
        tool_id = str(data.get("toolCallId") or _new_uuid())
        name = str(data.get("toolName") or "tool")
        arguments = data.get("arguments")
        input_data = arguments if isinstance(arguments, dict) else {"input": arguments}
        return _agent_message(
            role="assistant",
            content=[{
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": input_data,
            }],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=uid,
            timestamp=ts,
        )

    if etype == "tool.execution_complete":
        tool_id = str(data.get("toolCallId") or _new_uuid())
        content = _stringify_result(data.get("result"))
        return _agent_message(
            role="user",
            content=[{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": content,
                "is_error": not bool(data.get("success", True)),
            }],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=uid,
            timestamp=ts,
        )

    return None


def _resolve_config_dir(inputs: dict[str, Any]) -> Path:
    raw = str(inputs.get("config_dir") or "").strip()
    return Path(raw if raw else "~/.copilot").expanduser()


# Copilot has used two session-state layouts:
#   legacy:  <config_dir>/session-state/<sessionId>.jsonl
#   current: <config_dir>/session-state/<sessionId>/events.jsonl   (0.0.39x+)
# Both hold the same type-tagged event stream, so every helper below
# works in terms of "the session's events.jsonl path" regardless of layout.
def _session_event_file(config_dir: Path, session_id: str) -> Path:
    """Return the existing events path for a session id, preferring the
    current dir layout; falls back to the new-layout path when neither
    exists (caller writes nothing there — Copilot does)."""
    new_layout = config_dir / "session-state" / session_id / "events.jsonl"
    if new_layout.is_file():
        return new_layout
    legacy = config_dir / "session-state" / f"{session_id}.jsonl"
    if legacy.is_file():
        return legacy
    return new_layout


def _iter_session_event_files(session_state_dir: Path) -> list[Path]:
    """All session event files currently on disk (both layouts)."""
    if not session_state_dir.is_dir():
        return []
    out: list[Path] = []
    for child in session_state_dir.iterdir():
        if child.is_file() and child.suffix == ".jsonl":
            out.append(child)
        elif child.is_dir():
            ev = child / "events.jsonl"
            if ev.is_file():
                out.append(ev)
    return out


def _read_session_id_from_file(path: Path) -> Optional[str]:
    """Read the sessionId out of the first session.start event."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "session.start":
                    data = evt.get("data") if isinstance(evt.get("data"), dict) else {}
                    sid = data.get("sessionId")
                    if isinstance(sid, str) and sid:
                        return sid
    except OSError:
        return None
    return None


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _snapshot_session_files(session_state_dir: Path) -> set[Path]:
    return set(_iter_session_event_files(session_state_dir))


def _discover_fresh_session_file(
    session_state_dir: Path,
    baseline: set[Path],
) -> Optional[Path]:
    """First session events file that appeared since baseline and carries
    a session.start (so we can read its sessionId)."""
    for candidate in _iter_session_event_files(session_state_dir):
        if candidate in baseline:
            continue
        if _read_session_id_from_file(candidate):
            return candidate
    return None


def _auth_failure_from_output(stdout: str, stderr: str) -> Optional[str]:
    combined = f"{stdout}\n{stderr}"
    if "not authenticated" in combined.lower() or "login" in combined.lower() and "gh auth" in combined.lower():
        return (
            "Copilot CLI is not authenticated. Run `gh auth login` (or "
            "`copilot` interactively) to sign in, then retry."
        )
    return None


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_copilot fatal: %s", error)
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
    copilot_bin = resolve_cli_binary("copilot")
    if not copilot_bin:
        _fail(run_dir, "copilot CLI not found on PATH")
        return 1

    prompt = _prepend_capability_context(str(inputs.get("prompt") or ""), inputs)
    model = str(inputs.get("model") or "").strip()
    cwd = str(inputs.get("cwd") or os.getcwd())
    session_id = str(inputs.get("session_id") or "").strip()
    config_dir = _resolve_config_dir(inputs)
    session_state_dir = config_dir / "session-state"
    if not prompt:
        _fail(run_dir, "missing required field: prompt")
        return 1

    # Resume: the session jsonl already exists; tail from its current end
    # so we only render this turn's new events. Fresh: snapshot existing
    # files so we can identify the new one Copilot creates.
    resume_path: Optional[Path] = None
    pre_query_line_count = 0
    baseline_files: set[Path] = set()
    if session_id and _session_event_file(config_dir, session_id).is_file():
        resume_path = _session_event_file(config_dir, session_id)
        pre_query_line_count = _count_lines(resume_path)
    else:
        baseline_files = _snapshot_session_files(session_state_dir)

    run_env = os.environ.copy()
    argv: list[str] = [copilot_bin]
    if model:
        argv += ["--model", model]
    if session_id and resume_path is not None:
        argv += ["--resume", session_id]
    # Allow the tools Copilot needs to act on the workspace autonomously
    # (required for non-interactive -p mode) and scope file access to cwd.
    argv += ["--allow-all-tools", "--add-dir", cwd, "--config-dir", str(config_dir)]
    argv += ["-p", prompt]

    state: dict[str, Any] = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": session_id or None,
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "pre_query_line_count": pre_query_line_count,
        "complete": False,
    }
    # Persist early when resuming so the provider can bootstrap before the
    # first event lands. Fresh runs re-write state.json once the session
    # file is discovered.
    if session_id:
        _write_state(run_dir, state)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=run_env,
    )

    cancel_path = run_dir / "cancel"
    events_path = run_dir / "session_events.jsonl"
    cancelled = False
    # Shared tail cursor between the streaming watcher and the final flush
    # so every source line is emitted exactly once.
    next_line: dict[str, int] = {"n": pre_query_line_count}
    discovered: dict[str, Any] = {"path": resume_path, "session_id": session_id or None}

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

    def _emit_new(source_path: Path) -> None:
        """Normalize and append any source lines past the cursor."""
        sid = discovered.get("session_id")
        parent = sid or "root"
        total = _count_lines(source_path)
        if total <= next_line["n"]:
            return
        try:
            with source_path.open("r", encoding="utf-8") as fh:
                for _ in range(next_line["n"]):
                    fh.readline()
                new_lines = fh.readlines()
        except OSError:
            return
        appended = False
        with events_path.open("a", encoding="utf-8") as out:
            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                normalized = normalize_copilot_event(
                    evt, session_id=sid, parent_uuid=parent, model=model or "copilot",
                )
                if normalized is None:
                    continue
                out.write(json.dumps(normalized) + "\n")
                appended = True
        if appended:
            next_line["n"] = total
        else:
            # Still advance past lines we read but skipped (bookkeeping).
            next_line["n"] = total

    async def _watch_discovery() -> None:
        while proc.returncode is None:
            path = discovered.get("path")
            if path is None:
                path = _discover_fresh_session_file(session_state_dir, baseline_files)
                if path is not None:
                    discovered["path"] = path
            if path is not None and not discovered.get("session_id"):
                sid = _read_session_id_from_file(path)
                if sid:
                    discovered["session_id"] = sid
                    state["session_id"] = sid
                    _write_state(run_dir, state)
            await asyncio.sleep(_TAIL_POLL_INTERVAL)

    async def _watch_stream() -> None:
        while proc.returncode is None:
            path = discovered.get("path")
            if path is not None:
                _emit_new(path)
            await asyncio.sleep(_TAIL_POLL_INTERVAL)

    cancel_task = asyncio.create_task(_watch_cancel())
    discovery_task = asyncio.create_task(_watch_discovery())
    stream_task = asyncio.create_task(_watch_stream())
    try:
        stdout_bytes, stderr_bytes = await proc.communicate()
    finally:
        for task in (cancel_task, discovery_task, stream_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()
    if stderr:
        (run_dir / "copilot_stderr.log").write_text(stderr, encoding="utf-8")

    # Final discovery + flush for events written after the last stream poll.
    final_path = discovered.get("path")
    if final_path is None and session_id:
        final_path = _session_event_file(config_dir, session_id)
    if final_path is not None and final_path.is_file():
        if not discovered.get("session_id"):
            sid = _read_session_id_from_file(final_path)
            if sid:
                discovered["session_id"] = sid
                state["session_id"] = sid
        _emit_new(final_path)

    auth_error = _auth_failure_from_output(stdout, stderr)
    success = proc.returncode == 0 and not cancelled and auth_error is None
    error = None if success else (
        "cancelled"
        if cancelled else
        auth_error or stderr or f"copilot CLI exited with code {proc.returncode}"
    )

    state["complete"] = True
    state["finished_at"] = datetime.now().isoformat()
    _write_state(run_dir, state)
    _write_json(
        run_dir / "complete.json",
        {
            "success": success,
            "session_id": discovered.get("session_id") or session_id or None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        },
    )
    return 0 if success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_copilot %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
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
        logger.exception("runner_copilot top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
