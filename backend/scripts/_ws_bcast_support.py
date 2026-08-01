"""Shared fakes for SessionWSBroadcaster unit tests.

`SessionWSBroadcaster._dispatch` fans out via `coordinator.schedule_global`
and only fires when a loop is available (running, or bound through
`broadcaster.bind(loop)`). Tests therefore need (a) a coordinator that
captures `schedule_global` calls and (b) a driven loop around `on_change`.
This module owns both so the broadcaster test files share one definition.
"""

from __future__ import annotations

import asyncio
from typing import Any


class CapturingCoordinator:
    """Records every `schedule_global` fan-out as `{"type", "data"}`.

    `_dispatch` ignores the return value, so this is plain synchronous
    bookkeeping — no coroutine to await."""

    def __init__(self, captured: list[dict[str, Any]]) -> None:
        self.captured = captured

    def schedule_global(self, type_: str, data: dict, **_kwargs) -> None:
        self.captured.append({"type": type_, "data": data})


async def _drain() -> None:
    # Two zero-second sleeps let any coroutine the coordinator scheduled on
    # the bound loop run to completion before the loop closes.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def drive(broadcaster: Any, sid: str, change: dict) -> None:
    """Bind a fresh loop so `_dispatch`'s loop-based schedule path runs, fire
    one change, and drain any scheduled coroutines before closing the loop."""
    loop = asyncio.new_event_loop()
    broadcaster.bind(loop)
    asyncio.set_event_loop(loop)
    try:
        broadcaster.on_change(sid, change)
        loop.run_until_complete(_drain())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
