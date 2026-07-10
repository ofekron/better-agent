"""Typed client boundary to the better-agent runtime.

Phase 1 of the runtime split: callers outside the runtime core — MCP
servers, extensions, provisioning, bridge tools — must not import
`main` or reach the coordinator object directly. They call the typed
methods on `runtime` below. The facade resolves the live runtime
through `orchestrator.get_active_coordinator()` (the single canonical
registration point) and fails closed with `RuntimeUnavailableError`
when no runtime is active in this process.

Later phases swap the resolver for an IPC transport without changing
callers. `backend/scripts/test_runtime_import_boundary.py` ratchets
the set of modules still allowed to import `main` directly.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

WsCallback = Callable[[dict], Awaitable[None]]


class RuntimeUnavailableError(RuntimeError):
    """No active better-agent runtime in this process; call refused."""


class RuntimeClient:
    """In-process typed facade over the runtime coordinator."""

    def _coordinator(self) -> Any:
        from orchestrator import get_active_coordinator

        coord = get_active_coordinator()
        if coord is None:
            raise RuntimeUnavailableError(
                "no active better-agent runtime in this process"
            )
        return coord

    # ── turn service ──────────────────────────────────────────────

    def submit_prompt(self, app_session_id: str, params: dict[str, Any]) -> str:
        return self._coordinator().submit_prompt(app_session_id, params)

    def register_ws(
        self,
        app_session_id: str,
        ws_callback: WsCallback,
        *,
        from_seq: int = 0,
    ) -> None:
        self._coordinator().register_ws(
            app_session_id, ws_callback, from_seq=from_seq
        )

    def unregister_ws(
        self, app_session_id: str, ws_callback: Optional[WsCallback] = None
    ) -> None:
        self._coordinator().unregister_ws(app_session_id, ws_callback)

    def in_flight_assistant_msg(self, app_session_id: str) -> Optional[dict]:
        return self._coordinator().turn_manager.get_in_flight_assistant_msg(
            app_session_id
        )

    def register_init_cancel_event(
        self, app_session_id: str, owner_tag: str, cancel_event: asyncio.Event
    ) -> None:
        self._coordinator().init_cancel_events[app_session_id] = (
            owner_tag,
            cancel_event,
        )

    def clear_init_cancel_event(self, app_session_id: str) -> None:
        self._coordinator().init_cancel_events.pop(app_session_id, None)

    async def init_target_agent_session(self, **kwargs: Any) -> Optional[str]:
        # Parameter contract is owned by
        # `Coordinator._init_target_agent_session`; forwarded verbatim.
        return await self._coordinator()._init_target_agent_session(**kwargs)

    # ── delegation service ────────────────────────────────────────

    async def run_delegation(self, **kwargs: Any) -> dict:
        # Parameter contract is owned by `Coordinator.run_delegation`;
        # forwarded verbatim so there is one typed signature source.
        return await self._coordinator().run_delegation(**kwargs)

    # ── projection service ────────────────────────────────────────

    async def dispatch_messages_delta(
        self,
        app_session_id: str,
        persist_to: str,
        msg: dict,
        *,
        omit_render_events: bool = False,
    ) -> None:
        await self._coordinator()._dispatch_messages_delta(
            app_session_id,
            persist_to,
            msg,
            omit_render_events=omit_render_events,
        )

    async def broadcast_global(self, event_type: str, data: dict) -> None:
        await self._coordinator().broadcast_global(event_type, data)


runtime = RuntimeClient()
