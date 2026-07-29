"""Owner of the extension frontend-UI projection.

Single responsibility: decide what the UI catalog looks like right now,
and tell connected clients when — and only when — it actually changed.

Extension mutations (install, uninstall, enable, permission grant,
per-module toggle) announce themselves as the `extension.mutated` fact.
This manager is the sole subscriber that cares about their UI
consequence. It recomputes the frontend entrypoint projection on its own
thread (reading the extension store is blocking disk work), diffs it
against the last published snapshot, and broadcasts
`extension.ui.frontend_modules` carrying the new projection plus the set
of extension ids whose UI actually moved.

Two properties follow, and both exist to keep extension UI stable:

- A mutation with no UI consequence — a permission grant, a daemon
  restart, a config edit — produces no frame at all, so no client
  re-renders anything.
- A mutation with a UI consequence names the affected extensions, so a
  client can leave every other extension's mounted UI untouched.

Facts in, facts out. This manager never mutates the extension store; it
only projects it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from event_bus import BusEvent, bus
from thread_loop_host import ThreadLoopHost

logger = logging.getLogger(__name__)

# Fact published by whoever mutated an extension. Payload carries the
# mutated `extension_id` when it is known; the manager treats it as a
# hint for logging only — the projection diff is the authority on what
# changed.
EXTENSION_MUTATED = "extension.mutated"

# WS topic the frontend UI slots subscribe to.
UI_MODULES_CHANGED = "extension.ui.frontend_modules"

_SUBSCRIPTION_NAME = "extension_ui_manager.on_extension_mutated"


class ExtensionUIManager(ThreadLoopHost):
    """Projects extension state into UI-catalog facts, off the main loop."""

    def __init__(self) -> None:
        super().__init__(
            name="extension-ui-manager",
            executor_workers=2,
            io_thread_name_prefix="extension-ui-io",
        )
        self._published: dict[str, str] = {}
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._reconcile_lock: Optional[asyncio.Lock] = None

    # -- wiring ---------------------------------------------------------

    def bind(self, main_loop: asyncio.AbstractEventLoop) -> None:
        """Subscribe to extension facts. Idempotent.

        `main_loop` is captured because WS fan-out is owned by the
        coordinator on the main loop; the manager schedules onto it
        rather than touching client sockets from its own thread.
        """
        self._main_loop = main_loop
        bus.unsubscribe(_SUBSCRIPTION_NAME)
        bus.subscribe(
            EXTENSION_MUTATED,
            self._on_extension_mutated,
            name=_SUBSCRIPTION_NAME,
            loop=self.loop,
        )

    def on_stopping(self) -> None:
        bus.unsubscribe(_SUBSCRIPTION_NAME)

    # -- fact handling --------------------------------------------------

    async def _on_extension_mutated(self, event: BusEvent) -> None:
        """Runs on the manager loop; recomputes and publishes a delta."""
        if self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()
        async with self._reconcile_lock:
            try:
                await self._reconcile()
            except Exception:
                logger.exception(
                    "extension UI reconcile failed for %s",
                    (event.payload or {}).get("extension_id") or "<unknown>",
                )
                # Fail visible, not silent: tell clients to re-pull so a
                # broken catalog surfaces through the REST error path
                # instead of leaving stale UI mounted forever.
                self._broadcast({})

    async def _reconcile(self) -> None:
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, _read_entrypoints)
        snapshot = _snapshot(entries)
        changed = _changed_ids(self._published, snapshot)
        if not changed:
            return
        self._published = snapshot
        self._broadcast(
            {"entrypoints": entries, "changed_extension_ids": sorted(changed)}
        )

    # -- publication ----------------------------------------------------

    def _broadcast(self, payload: dict[str, Any]) -> None:
        """Hand a UI frame to the coordinator on the main loop."""
        loop = self._main_loop
        if loop is None or loop.is_closed():
            logger.warning("extension UI manager not bound; dropping UI frame")
            return
        from orchestrator import get_active_coordinator

        coordinator = get_active_coordinator()
        if coordinator is None:
            return
        try:
            coordinator.schedule_global(UI_MODULES_CHANGED, payload, loop=loop)
        except RuntimeError:
            # Shutdown closed global broadcasting; nothing left to tell.
            pass


def _read_entrypoints() -> list[dict[str, Any]]:
    import extension_store

    return extension_store.frontend_entrypoints()


def _snapshot(entries: list[dict[str, Any]]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for entry in entries:
        extension_id = str(entry.get("extension_id") or "")
        if not extension_id:
            continue
        snapshot[extension_id] = json.dumps(
            entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    return snapshot


def _changed_ids(before: dict[str, str], after: dict[str, str]) -> set[str]:
    changed = {eid for eid, blob in after.items() if before.get(eid) != blob}
    changed.update(eid for eid in before if eid not in after)
    return changed


manager = ExtensionUIManager()
