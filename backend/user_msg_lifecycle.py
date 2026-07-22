"""User message lifecycle events on the bus.

Five states per user prompt:

  queued    — POST/WS accepted; we know `kind` (send / queued_behind / interrupt)
  sent      — provider.start_run returned; runner is alive
  received  — tailer saw the matching `type=user` line in the agent's jsonl
  done      — natural completion per orchestration mode
              (native/manager: 1 turn; supervisor: terminal verdict)
  failed    — error before or during the turn

`lifecycle_msg_id` is a uuid generated at the WS boundary and threaded
through the whole pipeline. It correlates the five events across all
WS subscribers and through events.jsonl restore.

Interrupt cross-refs: a `queued` with `kind="interrupt"` carries
`interrupts_msg_id`; the matching `done` for the interrupted prompt
carries `interrupted_by_msg_id`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from event_bus import BusEvent, bus
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)


_USER_MSG_QUEUED = "user_message_queued"
_USER_MSG_SENT = "user_message_sent"
_USER_MSG_RECEIVED = "user_message_received"
_USER_MSG_DONE = "user_message_done"
_USER_MSG_FAILED = "user_message_failed"


def new_lifecycle_msg_id() -> str:
    """Generate a fresh correlation id for one user-message lifecycle."""
    return str(uuid.uuid4())


def queued_payload(
    *,
    lifecycle_msg_id: str,
    content: str,
    kind: str,
    queue_position: int,
    client_id: Optional[str] = None,
    interrupts_msg_id: Optional[str] = None,
    images_count: int = 0,
    orchestration_mode: Optional[str] = None,
) -> dict:
    payload = {
        "lifecycle_msg_id": lifecycle_msg_id,
        "kind": kind,
        "queue_position": queue_position,
        "content_preview": content[:200],
        "content_length": len(content),
        "images_count": images_count,
        "orchestration_mode": orchestration_mode,
    }
    if client_id:
        payload["client_id"] = client_id
    if kind == "interrupt" and interrupts_msg_id:
        payload["interrupts_msg_id"] = interrupts_msg_id
    return payload


def terminal_event_for_lifecycle(
    app_session_id: str, lifecycle_msg_id: str
) -> Optional[dict]:
    """Scan the session root's events.jsonl for the done/failed event of one
    lifecycle_msg_id. Used to re-attach an `ask` caller after a backend
    restart: recovery does not reliably re-emit user_message_done/failed, so
    the durable events.jsonl is the authoritative completion signal. Returns
    the matched event dict (with `type` + `data`) or None.
    """
    import json as _json

    from pathlib import Path
    import session_store

    if not _root_id_for(app_session_id):
        return None
    path = (
        Path(session_store.session_file_path(app_session_id)).parent
        / app_session_id
        / "events.jsonl"
    )
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if not line.strip() or lifecycle_msg_id not in line:
            continue
        try:
            evt = _json.loads(line)
        except (ValueError, TypeError):
            continue
        if evt.get("type") in (_USER_MSG_DONE, _USER_MSG_FAILED) and (
            (evt.get("data") or {}).get("lifecycle_msg_id") == lifecycle_msg_id
        ):
            return evt
    return None


async def terminal_event_for_lifecycle_async(
    app_session_id: str, lifecycle_msg_id: str
) -> Optional[dict]:
    return await asyncio.to_thread(
        terminal_event_for_lifecycle,
        app_session_id,
        lifecycle_msg_id,
    )


def _root_id_for(app_session_id: str) -> Optional[str]:
    """Resolve the session's root_id for bus event routing."""
    return session_manager._root_id_for(app_session_id)


async def _publish_lifecycle(
    event_type: str,
    app_session_id: str,
    lifecycle_msg_id: str,
    payload: dict,
    *,
    run_id: Optional[str] = None,
) -> bool:
    """Single source for the user_message_* BusEvent shape.

    INVARIANT: every user_message_* emit MUST go through here AND
    through `TurnManager._publish_user_lifecycle` so the BusEvent
    envelope (type/root_id/sid/msg_id/payload) stays uniform and a
    single sole-emitter point exists for audit.

    Routes through the active coordinator's `TurnManager`. The
    fallback to a direct `bus.publish` exists only for the early-boot
    window before `Coordinator.__init__` has set the active
    coordinator (importer code running at module load) — in normal
    operation every emit goes through TurnManager.
    """
    try:
        from orchestrator import get_active_coordinator
        coord = get_active_coordinator()
    except Exception:
        coord = None
    if coord is not None:
        return await coord.user_prompt_manager._publish_user_lifecycle(
            event_type,
            app_session_id=app_session_id,
            lifecycle_msg_id=lifecycle_msg_id,
            payload=payload,
            run_id=run_id,
        )
    # Pre-coordinator fallback. Should never happen at runtime — the
    # coordinator is constructed at app startup before any prompt
    # flows. Kept as a belt-and-suspenders so a misordered import in
    # a test fixture doesn't silently lose an event.
    logger.warning(
        "user_message lifecycle publish before active coordinator — "
        "type=%s sid=%s (early-boot fallback path)",
        event_type, app_session_id,
    )
    root_id = _root_id_for(app_session_id)
    if root_id is None:
        return False
    await bus.publish(BusEvent(
        type=event_type,
        root_id=root_id,
        sid=app_session_id,
        msg_id=lifecycle_msg_id,
        run_id=run_id,
        payload=payload,
    ))
    return True


async def emit_queued(
    *,
    app_session_id: str,
    lifecycle_msg_id: str,
    content: str,
    kind: str,                       # "send" | "queued_behind" | "interrupt"
    queue_position: int,
    client_id: Optional[str] = None,
    interrupts_msg_id: Optional[str] = None,
    images_count: int = 0,
    orchestration_mode: Optional[str] = None,
) -> None:
    payload = queued_payload(
        lifecycle_msg_id=lifecycle_msg_id,
        content=content,
        kind=kind,
        queue_position=queue_position,
        client_id=client_id,
        interrupts_msg_id=interrupts_msg_id,
        images_count=images_count,
        orchestration_mode=orchestration_mode,
    )
    if not await _publish_lifecycle(
        _USER_MSG_QUEUED, app_session_id, lifecycle_msg_id, payload,
    ):
        logger.error("lifecycle queued: no root for app_session=%s", app_session_id)


async def emit_sent(
    *,
    app_session_id: str,
    lifecycle_msg_id: str,
    run_id: str,
    agent_sid: Optional[str] = None,
) -> None:
    """`agent_sid` is the underlying agent CLI's session id (provider-
    agnostic — claude_sid for ClaudeProvider, the Gemini session id for
    GeminiProvider, etc.)."""
    await _publish_lifecycle(
        _USER_MSG_SENT, app_session_id, lifecycle_msg_id,
        {"lifecycle_msg_id": lifecycle_msg_id, "agent_sid": agent_sid},
        run_id=run_id,
    )


async def emit_received(
    *,
    app_session_id: str,
    lifecycle_msg_id: str,
    agent_user_uuid: str,
    agent_sid: Optional[str] = None,
) -> None:
    """`agent_user_uuid` is the uuid the underlying agent CLI stamped on
    the user-role line in its own jsonl. Used to correlate the lifecycle
    msg with the agent's record of having seen the prompt."""
    await _publish_lifecycle(
        _USER_MSG_RECEIVED, app_session_id, lifecycle_msg_id,
        {
            "lifecycle_msg_id": lifecycle_msg_id,
            "agent_user_uuid": agent_user_uuid,
            "agent_sid": agent_sid,
        },
    )


async def emit_done(
    *,
    app_session_id: str,
    lifecycle_msg_id: str,
    success: bool,
    cancelled: bool = False,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    token_usage_total: Optional[dict] = None,
    sub_turns: Optional[list[dict]] = None,
    interrupted_by_msg_id: Optional[str] = None,
) -> None:
    payload = {
        "lifecycle_msg_id": lifecycle_msg_id,
        "success": success,
        "cancelled": cancelled,
        "error": error,
        "duration_ms": duration_ms,
        "token_usage_total": token_usage_total,
        "sub_turns": sub_turns or [],
    }
    if interrupted_by_msg_id:
        payload["interrupted_by_msg_id"] = interrupted_by_msg_id
    await _publish_lifecycle(
        _USER_MSG_DONE, app_session_id, lifecycle_msg_id, payload,
    )


async def emit_failed(
    *,
    app_session_id: str,
    lifecycle_msg_id: str,
    reason: str,
    error: Optional[str] = None,
) -> None:
    await _publish_lifecycle(
        _USER_MSG_FAILED, app_session_id, lifecycle_msg_id,
        {
            "lifecycle_msg_id": lifecycle_msg_id,
            "reason": reason,
            "error": error,
        },
    )
