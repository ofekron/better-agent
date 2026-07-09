"""Detached runner for Sourcegraph's Amp CLI.

Per turn: spawns `amp -x --stream-json` (fresh thread) or
`amp threads continue <threadId> -x --stream-json` (resume), feeds the
prompt via stdin, parses Amp's Claude-Code-compatible stream-json from
stdout, normalizes each event to a Claude-shaped agent_message, and
appends to `<run_dir>/session_events.jsonl` — which the AmpProvider
tails for the render tree (gemini-family path). Writes `state.json`
(session_id = Amp thread id, e.g. "T-<uuid>") as soon as the thread id
is known, and `complete.json` on exit.

Amp's execute mode exits after the turn completes, so each Better Agent
turn is exactly one CLI invocation; multi-turn conversations resume via
`amp threads continue <threadId>`. Fork is real: `amp threads fork
<threadId>` prints a new thread id which this runner then continues.

Event shapes verified against a real `amp -x --stream-json` run
(2026-07-09, amp 0.0.1765051277):
  {"type":"system","subtype":"init","session_id":"T-...","tools":[...]}
  {"type":"user","message":{"role":"user","content":[...]},"session_id":...}
  {"type":"assistant","message":{...}} / {"type":"result","subtype":...}
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
from runs_dir import atomic_write_json

logger = logging.getLogger(__name__)

# Deterministic UUID namespace: the same (thread_id, event ordinal) pair
# always yields the same render uuid, so a re-run of the same normalization
# collides in the render tree's uuid dedup instead of duplicating.
_AMP_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.runner_amp.events")

# Amp thread ids as printed by `amp threads new/fork` and carried in the
# stream-json `session_id` field.
_THREAD_ID_RE = re.compile(r"\bT-[0-9a-fA-F][0-9a-fA-F-]{10,}\b")

# `amp -m/--mode` values (agent modes: model + system prompt + tool
# selection). "sonnet" maps to `--use-sonnet`; "auto" / "" means Amp's
# default (Opus, mode "smart").
AMP_EXECUTION_MODES = ("smart", "rush", "free")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _event_uuid(session_id: Optional[str], event_index: int) -> str:
    """Deterministic render uuid for the Nth emitted event of a thread.
    Amp stream-json events carry no per-event id, so the ordinal within
    the run is the stable handle."""
    if session_id:
        return str(uuid.uuid5(_AMP_UUID_NAMESPACE, f"{session_id}|{event_index}"))
    return _new_uuid()


def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
    atomic_write_json(run_dir / "state.json", state)


def model_argv(model: str) -> list[str]:
    """Map a Better Agent model selector to amp CLI flags.

    Amp auto-selects the underlying LLM; the only user-facing knobs are
    the agent mode (`-m smart|rush|free`) and the Sonnet toggle
    (`--use-sonnet`). Raises on unknown selectors — the provider
    validates upstream, so an unknown value here is a wiring bug and
    must fail closed rather than silently run the wrong model."""
    value = str(model or "").strip()
    if value in ("", "auto"):
        return []
    if value in AMP_EXECUTION_MODES:
        return ["-m", value]
    if value == "sonnet":
        return ["--use-sonnet"]
    raise ValueError(f"unknown amp model selector: {value!r}")


def permission_argv(permission: Optional[dict]) -> list[str]:
    """Fail closed: `--dangerously-allow-all` (disables every command
    confirmation) is added ONLY when the resolved permission explicitly
    asks for full permission. Anything else — including the empty dict
    the permission resolver currently returns for the amp kind — runs
    without the flag, so confirmation-gated commands are refused by the
    CLI instead of silently executed."""
    mode = (permission or {}).get("mode") if isinstance(permission, dict) else None
    if mode in ("dangerously-allow-all", "bypassPermissions", "yolo"):
        return ["--dangerously-allow-all"]
    return []


def parse_fork_thread_id(output: str) -> Optional[str]:
    """Extract the new thread id from `amp threads fork` output."""
    match = _THREAD_ID_RE.search(output or "")
    return match.group(0) if match else None


def build_amp_argv(
    amp_bin: str,
    *,
    resume_thread_id: Optional[str],
    model: str,
    permission: Optional[dict],
) -> list[str]:
    """argv for one execute-mode turn. The prompt goes via stdin (never
    argv): no shell is involved and huge prompts can't hit arg limits.
    Global flags (verified accepted on both invocation shapes): -m /
    --use-sonnet / --dangerously-allow-all / --stream-json."""
    argv: list[str] = [amp_bin]
    if resume_thread_id:
        argv += ["threads", "continue", resume_thread_id]
    argv += model_argv(model)
    argv += permission_argv(permission)
    argv += ["-x", "--stream-json"]
    return argv


def _message_model(message: dict, fallback: str) -> str:
    model = message.get("model")
    return model if isinstance(model, str) and model else fallback


def _has_tool_result(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def normalize_amp_event(
    event: dict[str, Any],
    *,
    session_id: Optional[str],
    parent_uuid: str,
    model: str,
    event_index: int,
) -> Optional[dict[str, Any]]:
    """Map one Amp stream-json event to a Claude-shaped agent_message
    payload, or None when the event is bookkeeping (system init, result
    — both handled by the run loop) or the user prompt echo (already in
    the render tree from the Better Agent side).

    Amp's stream is Claude-Code-compatible, so `message` passes through
    nearly verbatim; only the render-tree envelope (uuid / parentUuid /
    timestamp) is added."""
    etype = event.get("type")
    uid = _event_uuid(session_id, event_index)
    ts = datetime.now().isoformat()

    if etype in ("system", "result"):
        return None

    if etype == "assistant":
        message = event.get("message")
        if not isinstance(message, dict) or not message.get("content"):
            return None
        out_message = dict(message)
        out_message["model"] = _message_model(message, model or "amp")
        return {
            "type": "assistant",
            "message": out_message,
            "uuid": uid,
            "parentUuid": parent_uuid,
            "timestamp": ts,
            "parent_tool_use_id": event.get("parent_tool_use_id"),
        }

    if etype == "user":
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        # Prompt echo (plain user text) is skipped; tool_result carriers
        # are rendered so tool cards resolve.
        if not _has_tool_result(content):
            return None
        return {
            "type": "user",
            "message": dict(message),
            "uuid": uid,
            "parentUuid": parent_uuid,
            "timestamp": ts,
            "parent_tool_use_id": event.get("parent_tool_use_id"),
        }

    # Unknown event type: surface as a diagnostic instead of silently
    # dropping (same invariant as runner_gemini's unknown path).
    return {
        "type": "unknown_event",
        "raw_type": etype,
        "raw": event,
        "uuid": uid,
        "parentUuid": parent_uuid,
        "timestamp": ts,
    }


def result_error(event: dict[str, Any]) -> Optional[str]:
    """Extract the terminal error from an Amp `result` event, or None on
    success. Verified shapes: subtype "success" with `result` text;
    is_error=true with an `error` string (e.g. the 402 credits error)."""
    if not event.get("is_error") and event.get("subtype") == "success":
        return None
    err = event.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip()
    if isinstance(err, dict):
        msg = err.get("message") or err.get("error")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    result_text = event.get("result")
    if isinstance(result_text, str) and result_text.strip():
        return result_text.strip()
    return f"amp turn failed (subtype={event.get('subtype')!r})"


def result_token_usage(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage = event.get("usage")
    out: dict[str, Any] = dict(usage) if isinstance(usage, dict) else {}
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        out["total_cost_usd"] = cost
    duration = event.get("duration_ms")
    if isinstance(duration, (int, float)):
        out["duration_ms"] = duration
    return out or None


def auth_failure_from_output(stdout: str, stderr: str) -> Optional[str]:
    combined = f"{stdout}\n{stderr}".lower()
    if "api key is not configured" in combined or (
        "amp login" in combined and "api key" in combined
    ):
        return (
            "Amp CLI is not authenticated. Run `amp login` or set "
            "AMP_API_KEY, then retry."
        )
    return None


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_amp fatal: %s", error)
    atomic_write_json(
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


async def _fork_thread(amp_bin: str, thread_id: str, cwd: str) -> tuple[Optional[str], str]:
    """Run `amp threads fork <id>`; returns (new_thread_id, raw_output)."""
    proc = await asyncio.create_subprocess_exec(
        amp_bin, "threads", "fork", thread_id,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    output = stdout_bytes.decode(errors="replace") + "\n" + stderr_bytes.decode(errors="replace")
    if proc.returncode != 0:
        return None, output
    return parse_fork_thread_id(output), output


async def _run(run_dir: Path, inputs: dict[str, Any]) -> int:
    amp_bin = resolve_cli_binary("amp")
    if not amp_bin:
        _fail(run_dir, "amp CLI not found on PATH")
        return 1

    prompt = _prepend_capability_context(str(inputs.get("prompt") or ""), inputs)
    model = str(inputs.get("model") or "").strip()
    cwd = str(inputs.get("cwd") or os.getcwd())
    session_id = str(inputs.get("session_id") or "").strip()
    permission = inputs.get("permission") if isinstance(inputs.get("permission"), dict) else {}
    fork = bool(inputs.get("fork"))
    if not prompt:
        _fail(run_dir, "missing required field: prompt")
        return 1
    if fork and not session_id:
        _fail(run_dir, "fork requested without a source thread id")
        return 1

    resume_target = session_id or None
    if fork and session_id:
        new_thread_id, fork_output = await _fork_thread(amp_bin, session_id, cwd)
        if not new_thread_id:
            _fail(
                run_dir,
                "amp threads fork failed for "
                f"{session_id}: {fork_output.strip()[:500]}",
            )
            return 1
        resume_target = new_thread_id

    try:
        argv = build_amp_argv(
            amp_bin,
            resume_thread_id=resume_target,
            model=model,
            permission=permission,
        )
    except ValueError as exc:
        _fail(run_dir, str(exc))
        return 1

    events_path = run_dir / "session_events.jsonl"
    state: dict[str, Any] = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": resume_target,
        "jsonl_path": str(events_path),
        "complete": False,
    }
    # Resume/fork: the thread id is known up front — persist immediately
    # so the provider bootstrap can attach before the first event lands.
    # Fresh threads persist on the init event instead.
    if resume_target:
        _write_state(run_dir, state)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=os.environ.copy(),
        limit=16 * 1024 * 1024,
    )
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()

    cancel_path = run_dir / "cancel"
    cancelled = False

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

    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                return
            stderr_chunks.append(chunk)

    cancel_task = asyncio.create_task(_watch_cancel())
    stderr_task = asyncio.create_task(_drain_stderr())

    discovered_sid: Optional[str] = resume_target
    parent_uuid = discovered_sid or "root"
    event_index = 0
    result_seen = False
    success = False
    error: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    unknown_types_seen: set[str] = set()
    stdout_tail: list[str] = []

    try:
        with events_path.open("a", encoding="utf-8") as events_file:
            async for raw_line in proc.stdout:
                if cancelled:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stdout_tail.append(line[:500])
                    continue
                if not isinstance(event, dict):
                    continue

                etype = event.get("type")
                sid = event.get("session_id")
                if isinstance(sid, str) and sid and sid != discovered_sid:
                    discovered_sid = sid
                    parent_uuid = sid
                    state["session_id"] = sid
                    _write_state(run_dir, state)

                if etype == "result":
                    result_seen = True
                    error = result_error(event)
                    success = error is None
                    token_usage = result_token_usage(event)
                    continue

                normalized = normalize_amp_event(
                    event,
                    session_id=discovered_sid,
                    parent_uuid=parent_uuid,
                    model=model,
                    event_index=event_index,
                )
                event_index += 1
                if normalized is None:
                    continue
                if normalized.get("type") == "unknown_event":
                    raw_type = str(normalized.get("raw_type"))
                    if raw_type not in unknown_types_seen:
                        unknown_types_seen.add(raw_type)
                        logger.warning(
                            "runner_amp: unknown stream-json event type %r — "
                            "surfacing as diagnostic", raw_type,
                        )
                events_file.write(
                    json.dumps({"type": "agent_message", "data": normalized}) + "\n"
                )
                events_file.flush()
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass

    await proc.wait()
    try:
        await asyncio.wait_for(stderr_task, timeout=2.0)
    except asyncio.TimeoutError:
        stderr_task.cancel()

    stderr = b"".join(stderr_chunks).decode(errors="replace").strip()
    if stderr:
        (run_dir / "amp_stderr.log").write_text(stderr, encoding="utf-8")

    stdout_diag = "\n".join(stdout_tail)
    auth_error = auth_failure_from_output(stdout_diag, stderr)
    if cancelled:
        success = False
        error = "cancelled"
    elif auth_error:
        success = False
        error = auth_error
    elif not result_seen and not error:
        success = False
        error = (
            stderr.splitlines()[-1].strip()
            if stderr else
            f"amp CLI exited (code {proc.returncode}) without a result event"
        )
    elif success and proc.returncode != 0:
        success = False
        error = stderr or f"amp CLI exited with code {proc.returncode}"

    # Surface the terminal error as the run's final answer so the message
    # content isn't left empty (parity with runner_gemini's error event).
    if error and not cancelled:
        try:
            error_event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Error: {error}"}],
                    "model": model or "amp",
                },
                "uuid": _new_uuid(),
                "parentUuid": parent_uuid,
                "timestamp": datetime.now().isoformat(),
                "isApiErrorMessage": True,
            }
            with events_path.open("a", encoding="utf-8") as events_file:
                events_file.write(
                    json.dumps({"type": "agent_message", "data": error_event}) + "\n"
                )
        except OSError:
            logger.exception("failed to append error event")

    state["complete"] = True
    state["finished_at"] = datetime.now().isoformat()
    if discovered_sid:
        state["session_id"] = discovered_sid
    _write_state(run_dir, state)
    atomic_write_json(
        run_dir / "complete.json",
        {
            "success": success,
            "session_id": discovered_sid,
            "error": error,
            "token_usage": token_usage,
            "finished_at": datetime.now().isoformat(),
        },
    )
    return 0 if success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_amp %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
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
        logger.exception("runner_amp top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
