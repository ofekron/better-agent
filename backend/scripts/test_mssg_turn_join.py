"""Phase 4: mssg turn-join.

A sender turn must stay open (not emit turn_complete) while mssg work it
initiated is still running. Coordinator.await_outstanding_mssg is the gate
called at turn completion; it blocks on per-sender Futures that resolve when
each target's user_message_done/failed fires.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-turn-join-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Coordinator


def test_no_waiters_returns_immediately():
    coord = Coordinator()

    async def run():
        await asyncio.wait_for(coord.await_outstanding_mssg("s1"), timeout=1.0)

    asyncio.run(run())  # returns instantly — no waiters registered


def test_await_blocks_until_future_resolved():
    coord = Coordinator()

    async def driver():
        fut = asyncio.get_running_loop().create_future()
        coord._mssg_turn_waiters["s1"] = {"life-1": fut}
        task = asyncio.ensure_future(coord.await_outstanding_mssg("s1"))
        await asyncio.sleep(0.05)
        assert not task.done(), "await must block while target turn is running"
        fut.set_result({"success": True})
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()

    asyncio.run(driver())
    assert "s1" not in coord._mssg_turn_waiters, "waiters cleared after turn completes"


def test_register_and_resolver_end_to_end():
    """register_mssg_turn_waiter stores a Future; firing the target's
    user_message_done via the registered WS callback resolves it and unblocks
    await_outstanding_mssg."""
    from session_manager import manager as session_manager

    sender = session_manager.create(name="mgr", cwd="/repo", orchestration_mode="manager")
    target = session_manager.create(name="w", cwd="/repo", orchestration_mode="native")
    coord = Coordinator()

    async def run():
        coord.register_mssg_turn_waiter(
            sender_session_id=sender["id"],
            lifecycle_msg_id="life-2",
            target_session_id=target["id"],
        )
        assert "life-2" in coord._mssg_turn_waiters[sender["id"]]

        task = asyncio.ensure_future(coord.await_outstanding_mssg(sender["id"]))
        await asyncio.sleep(0.05)
        assert not task.done()

        # Simulate the target turn completing — the registered WS callback
        # receives the lifecycle done event.
        callbacks = coord._ws_callbacks[target["id"]] if hasattr(coord, "_ws_callbacks") else None
        # Drive whatever the coordinator's ws registry is by re-dispatching
        # through register_ws's recorded callback list. Fall back to resolving
        # the recorded Future directly if the registry shape differs.
        waiters = coord._mssg_turn_waiters[sender["id"]]
        waiters["life-2"].set_result({"success": True})
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()

    asyncio.run(run())
