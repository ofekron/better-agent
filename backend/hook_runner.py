from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any

import hook_store
from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)

_SUBSCRIBER_NAME = "configured_hook_runner"
_PRIORITY = 300
_HOOK_META_PREFIX = "hook."


def bind_configured_hooks() -> None:
    bus.unsubscribe(_SUBSCRIBER_NAME)
    bus.subscribe("*", _dispatch_matching_hooks, priority=_PRIORITY, name=_SUBSCRIBER_NAME)


async def _dispatch_matching_hooks(event: BusEvent) -> None:
    if event.type.startswith(_HOOK_META_PREFIX):
        return
    try:
        hooks = await asyncio.to_thread(hook_store.list_hooks)
    except Exception:
        logger.exception("hook runner: failed to load hooks")
        return
    for hook in hooks:
        if not hook.get("enabled", True):
            continue
        pattern = hook["pattern"]
        if not fnmatch.fnmatchcase(event.type, pattern):
            continue
        task = asyncio.create_task(
            _run_hook(hook, event),
            name=f"hook-{hook['id']}",
        )
        task.add_done_callback(lambda done, hook_id=hook["id"]: _log_task_failure(hook_id, done))


async def _run_hook(hook: dict[str, Any], event: BusEvent) -> None:
    envelope = _event_envelope(event, hook)
    await _publish_hook_meta("hook.started", event, hook, {})
    try:
        result = await _execute_hook_command(hook, envelope)
    except asyncio.TimeoutError:
        await _publish_hook_meta(
            "hook.failed",
            event,
            hook,
            {"error_class": "TimeoutError", "error_message": "hook timed out"},
        )
        return
    except Exception as exc:
        logger.exception("hook %s failed before process completion", hook["id"])
        await _publish_hook_meta(
            "hook.failed",
            event,
            hook,
            {"error_class": type(exc).__name__, "error_message": str(exc)},
        )
        return
    if result["returncode"] == 0:
        await _publish_hook_meta("hook.completed", event, hook, result)
        return
    await _publish_hook_meta(
        "hook.failed",
        event,
        hook,
        {
            **result,
            "error_class": "HookCommandFailed",
            "error_message": f"hook command exited {result['returncode']}",
        },
    )


async def _execute_hook_command(
    hook: dict[str, Any],
    envelope: dict[str, Any],
) -> dict[str, Any]:
    stdin = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    proc = await asyncio.create_subprocess_exec(
        *hook["command"],
        cwd=hook.get("cwd"),
        env=_build_env(hook),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin),
            timeout=float(hook["timeout_seconds"]),
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return {
        "returncode": int(proc.returncode or 0),
        "stdout": _decode_limited(stdout),
        "stderr": _decode_limited(stderr),
    }


def _build_env(hook: dict[str, Any]) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "BETTER_CLAUDE_HOOK_ID": hook["id"],
        "BETTER_CLAUDE_HOOK_NAME": hook["name"],
        "BETTER_CLAUDE_HOOK_PATTERN": hook["pattern"],
    }
    env.update(hook.get("env") or {})
    return env


def _event_envelope(event: BusEvent, hook: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook": {
            "id": hook["id"],
            "name": hook["name"],
            "pattern": hook["pattern"],
        },
        "event": {
            "type": event.type,
            "root_id": event.root_id,
            "sid": event.sid,
            "payload": event.payload,
            "msg_id": event.msg_id,
            "run_id": event.run_id,
            "ts": event.ts,
            "schema_version": event.schema_version,
            "seq": event.seq,
            "is_replay": event.is_replay,
        },
    }


async def _publish_hook_meta(
    event_type: str,
    source_event: BusEvent,
    hook: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    await bus.publish(BusEvent(
        type=event_type,
        root_id=source_event.root_id,
        sid=source_event.sid,
        payload={
            "hook_id": hook["id"],
            "hook_name": hook["name"],
            "hook_pattern": hook["pattern"],
            "source_event_type": source_event.type,
            "source_event_seq": source_event.seq,
            **payload,
        },
        persist=False,
    ))


def _decode_limited(data: bytes) -> str:
    if len(data) > hook_store.MAX_OUTPUT_BYTES:
        data = data[:hook_store.MAX_OUTPUT_BYTES]
    return data.decode("utf-8", errors="replace")


def _log_task_failure(hook_id: str, task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("hook %s task failed", hook_id)
