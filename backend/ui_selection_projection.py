"""Projects session-deletion facts onto the ui_selection open-tab list.

`ui_selection` owns the durable open-session-tab list; session deletion is
owned by `session_manager` / `virtual_session_store`. Rather than having the
delete paths mutate the tab list directly, they publish/report the deletion
fact and this projection prunes the tab list it owns, then pushes the new
snapshot so every connected client's tab strip converges.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import ui_selection
from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)

_SUBSCRIBER_NAME = "ui_selection_tab_cleanup"

_broadcast_global: Callable[[str, dict], Awaitable[None]] | None = None


async def close_tabs_for_deleted(session_ids: list[str]) -> None:
    """Prune deleted sessions from the open tab list and broadcast the
    resulting snapshot. No-op when none of them had a tab open."""
    snapshot = await asyncio.to_thread(ui_selection.close_session_tabs, session_ids)
    if snapshot is None or _broadcast_global is None:
        return
    await _broadcast_global("ui_selection_changed", snapshot)


def bind(broadcast_global: Callable[[str, dict], Awaitable[None]]) -> None:
    """Subscribe the tab projection to `session.deleted`. Idempotent."""
    global _broadcast_global
    _broadcast_global = broadcast_global

    async def _handler(event: BusEvent) -> None:
        payload = event.payload or {}
        deleted = payload.get("deleted_sids")
        if not isinstance(deleted, list) or not deleted:
            deleted = [event.sid] if event.sid else []
        try:
            await close_tabs_for_deleted(deleted)
        except Exception:
            logger.exception("ui_selection tab cleanup failed for %s", event.sid)

    bus.unsubscribe(_SUBSCRIBER_NAME)
    bus.subscribe("session.deleted", _handler, priority=50, name=_SUBSCRIBER_NAME)


def unbind() -> None:
    global _broadcast_global
    bus.unsubscribe(_SUBSCRIBER_NAME)
    _broadcast_global = None
