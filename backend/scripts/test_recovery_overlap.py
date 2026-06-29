"""Recovery-overlap regression tests at the prompt-processor level.

Locks:
  1. The processor barrier: a prompt submitted while an
     externally-registered run (a recovered live subprocess) is in
     active_run_ids does NOT reach handle_prompt until that run
     clears. Pre-fix, the processor started a second CLI subprocess
     concurrently with the recovered one — the interleaved-turns bug.
  2. Interrupt during a recovery overlap both fans the cancel out to
     the recovered run AND parks a pending cancel that displaces the
     queued prompt (deliberate dual effect — "displace what's next").
  3. A pending cancel left unconsumed by an item is cleared in the
     processor's finally and cannot abort the next prompt.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_overlap.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_recovl_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import Coordinator  # noqa: E402
from turn_manager import TurnManager  # noqa: E402
import startup_recovery_gate  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


class _UPM:
    @staticmethod
    def get_in_flight_lifecycle_msg_id(sid):
        return None

    @staticmethod
    def set_in_flight_lifecycle_msg_id(sid, mid):
        pass

    @staticmethod
    def clear_in_flight_lifecycle_msg_id(sid):
        pass


def _coord() -> Coordinator:
    startup_recovery_gate.reset_for_tests()
    c = Coordinator.__new__(Coordinator)
    c._prompt_queues = {}
    c._queued_ids = {}
    c._active_prompt_client_ids = {}
    c._prompt_client_id_by_item = {}
    c._processor_tasks = {}
    c._in_flight_prompts = {}
    c._cancelled_ids = {}
    c._session_cancelled = {}
    c.user_prompt_manager = _UPM()
    c.turn_manager = TurnManager(c)
    c.handled: list[dict] = []

    async def dispatch_raw(sid, event):
        pass

    c.dispatch_raw = dispatch_raw

    async def handle_prompt(**params):
        c.handled.append(params)

    c.handle_prompt = handle_prompt
    return c


def test_barrier_blocks_prompt_during_recovered_run() -> None:
    print("T1 prompt waits for recovered run to clear")
    c = _coord()
    sid = "sid-recov"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]

    async def _go() -> tuple[bool, bool]:
        c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
        await asyncio.sleep(1.2)
        blocked = len(c.handled) == 0
        # Recovered run finishes (what _finalize_when_done does).
        c.turn_manager.active_run_ids.pop(sid, None)
        for _ in range(40):
            if c.handled:
                break
            await asyncio.sleep(0.25)
        return blocked, len(c.handled) == 1

    blocked, ran = asyncio.run(_go())
    check("blocked while recovered run alive", blocked)
    check("ran after recovered run cleared", ran)


def test_startup_recovery_gate_blocks_pre_registration_window() -> None:
    print("T1b startup recovery gate blocks before active_run_ids exist")
    c = _coord()
    sid = "sid-startup-recovery"

    async def _go() -> tuple[bool, bool]:
        startup_recovery_gate.begin_recovery()
        c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
        await asyncio.sleep(0.8)
        blocked = len(c.handled) == 0
        startup_recovery_gate.mark_recovery_done()
        for _ in range(40):
            if c.handled:
                break
            await asyncio.sleep(0.25)
        return blocked, len(c.handled) == 1

    blocked, ran = asyncio.run(_go())
    startup_recovery_gate.reset_for_tests()
    check("blocked before recovery registration", blocked)
    check("ran after recovery scan/integration completed", ran)


def test_startup_recovery_failure_fails_closed() -> None:
    print("T1c startup recovery failure prevents prompt handling")
    c = _coord()
    sid = "sid-startup-failed"

    async def _go() -> bool:
        startup_recovery_gate.begin_recovery()
        c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
        await asyncio.sleep(0.2)
        startup_recovery_gate.mark_recovery_failed("boom")
        for _ in range(20):
            task = c._processor_tasks.get(sid)
            if task is None or task.done():
                break
            await asyncio.sleep(0.1)
        return len(c.handled) == 0

    blocked = asyncio.run(_go())
    startup_recovery_gate.reset_for_tests()
    check("did not handle prompt after recovery failure", blocked)


def test_startup_recovery_gate_has_bounded_default_wait() -> None:
    print("T1d startup recovery gate default wait is bounded")

    async def _go() -> tuple[bool, bool]:
        startup_recovery_gate.begin_recovery()
        task = asyncio.create_task(startup_recovery_gate.wait_for_recovery_ready())
        await asyncio.sleep(0.2)
        still_waiting_initially = not task.done()
        await asyncio.wait_for(task, timeout=3.0)
        return still_waiting_initially, task.done()

    blocked_briefly, completed = asyncio.run(_go())
    startup_recovery_gate.reset_for_tests()
    check("blocked briefly while recovery pending", blocked_briefly)
    check("completed without waiting for recovery forever", completed)


def test_interrupt_during_overlap_fans_out_and_displaces() -> None:
    print("T2 interrupt during overlap: fanout + displace queued prompt")
    c = _coord()
    fanned: list[str] = []
    c._cancel_turn_fanout = lambda run_id: fanned.append(run_id) or True
    sid = "sid-ovl"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]

    async def _go() -> tuple[bool, bool]:
        c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
        await asyncio.sleep(0.6)  # processor dequeued, parked on barrier
        landed = await c.turn_manager.cancel_turn(
            sid, interrupted_by_msg_id="lm-1",
        )
        # Assert before loop teardown — shutdown cancels the parked
        # processor, whose finally legitimately clears the pending.
        parked = c.turn_manager._pending_cancel.get(sid) == "lm-1"
        return landed, parked

    landed, parked = asyncio.run(_go())
    check("cancel landed", landed is True)
    check("fanout reached recovered run", fanned == ["recovered-run"])
    check("pending cancel parked for queued prompt", parked)


def test_stale_pending_cleared_by_item_finally() -> None:
    print("T3 stale pending cancel cleared by processor finally")
    c = _coord()
    sid = "sid-stale"

    async def _go() -> tuple[bool, bool]:
        c.submit_prompt(sid, {"prompt": "one", "app_session_id": sid})
        # Park a pending cancel mid-item, as a gap-window cancel would.
        c._in_flight_prompts[sid] = c._in_flight_prompts.get(sid, 0)  # no-op read
        c.turn_manager._pending_cancel[sid] = True
        for _ in range(40):
            if c.handled and not c._processor_tasks.get(sid):
                break
            await asyncio.sleep(0.25)
        cleared = sid not in c.turn_manager._pending_cancel
        # Next prompt must run normally.
        c.submit_prompt(sid, {"prompt": "two", "app_session_id": sid})
        for _ in range(40):
            if len(c.handled) == 2:
                break
            await asyncio.sleep(0.25)
        return cleared, len(c.handled) == 2

    cleared, second_ran = asyncio.run(_go())
    check("pending cleared after item", cleared)
    check("next prompt unaffected", second_ran)


def main() -> int:
    test_barrier_blocks_prompt_during_recovered_run()
    test_startup_recovery_gate_blocks_pre_registration_window()
    test_startup_recovery_failure_fails_closed()
    test_startup_recovery_gate_has_bounded_default_wait()
    test_interrupt_during_overlap_fans_out_and_displaces()
    test_stale_pending_cleared_by_item_finally()
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
