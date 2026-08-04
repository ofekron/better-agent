from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import session_status
import user_input_store
import working_mode
from event_bus import BusEvent, bus, register_event_schema
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

STATUS_INPUT_KINDS = frozenset({
    "running_changed",
    "monitoring_changed",
    "unread_changed",
    "seen_advanced",
    "error_changed",
    "marker_set",
    "marker_cleared",
    "todos_updated",
    "tasks_updated",
    "user_input_changed",
    "selectors_set",
    "archived_set",
    "deleted",
    "created",
    "forked",
})

register_event_schema("session.status_projected", 1)


def current(
    sid: str,
    projected_state_snapshot: Callable[[], tuple[set[str], dict[str, str]]],
) -> dict | None:
    session = session_manager.get_lite(sid)
    if session is None:
        return None
    _, monitoring_by_sid = projected_state_snapshot()
    return _project_session(
        session,
        monitoring_by_sid,
        session_manager.unread_counts_snapshot(),
        user_input_store.pending_counts_by_session(),
    )


def _project_session(
    session: dict,
    monitoring_by_sid: dict[str, str],
    unread_by_sid: dict[str, int],
    pending_input_by_sid: dict[str, int],
) -> dict:
    sid = session["id"]
    status = session_status.compute(
        session,
        monitoring_by_sid,
        unread_by_sid,
        pending_input_by_sid,
    )
    cwd = str(session.get("cwd") or "")
    visible = bool(cwd) and not working_mode.should_hide_from_sidebar(session)
    return {
        "session_id": sid,
        "project_key": (
            {"cwd": cwd, "node_id": session.get("node_id") or "primary"}
            if visible
            else None
        ),
        "running": status.busy,
        "unread": status.unread,
        "waiting_for_user": status.waiting_for_user,
        "errored": status.errored,
        **session_status.project_status(status),
    }


def all_current(
    projected_state_snapshot: Callable[[], tuple[set[str], dict[str, str]]],
) -> list[dict]:
    _, monitoring_by_sid = projected_state_snapshot()
    unread_by_sid = session_manager.unread_counts_snapshot()
    pending_input_by_sid = user_input_store.pending_counts_by_session()
    return [
        _project_session(
            session,
            monitoring_by_sid,
            unread_by_sid,
            pending_input_by_sid,
        )
        for session in session_manager.list()
        if session.get("id")
    ]


async def publish_all_current(
    projected_state_snapshot: Callable[[], tuple[set[str], dict[str, str]]],
) -> None:
    projections = await asyncio.to_thread(all_current, projected_state_snapshot)
    for projection in projections:
        sid = projection["session_id"]
        await bus.publish(BusEvent(
            type="session.status_projected",
            root_id=sid,
            sid=sid,
            payload=projection,
            persist=False,
        ))


def bind(
    broadcast_global: Callable[[str, dict], Awaitable[None]],
    projected_state_snapshot: Callable[[], tuple[set[str], dict[str, str]]],
) -> None:
    async def _handler(event: BusEvent) -> None:
        kind = event.type.removeprefix("session.")
        if kind not in STATUS_INPUT_KINDS:
            return
        try:
            if kind == "deleted":
                deleted_sids = event.payload.get("deleted_sids") or [event.sid]
                for deleted_sid in deleted_sids:
                    if not isinstance(deleted_sid, str) or not deleted_sid:
                        continue
                    await bus.publish(BusEvent(
                        type="session.status_projected",
                        root_id=event.root_id,
                        sid=deleted_sid,
                        payload={"session_id": deleted_sid, "deleted": True},
                        persist=False,
                    ))
                return
            projection = await asyncio.to_thread(
                current,
                event.sid,
                projected_state_snapshot,
            )
            if projection is not None:
                await bus.publish(BusEvent(
                    type="session.status_projected",
                    root_id=event.root_id,
                    sid=event.sid,
                    payload=projection,
                    persist=False,
                ))
                await broadcast_global("session_status_changed", {
                    "session_id": event.sid,
                    "status_key": projection["status_key"],
                    "status_rank": projection["status_rank"],
                })
        except Exception:
            logger.exception(
                "session status projection failed for %s",
                event.type,
            )

    bus.unsubscribe("session_status_projection")
    bus.subscribe(
        "session.*",
        _handler,
        priority=50,
        name="session_status_projection",
    )


def unbind() -> None:
    bus.unsubscribe("session_status_projection")
