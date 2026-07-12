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
from session_manager import manager as session_manager  # noqa: E402
from runs_dir import atomic_write_json, runs_root  # noqa: E402
from run_recovery import _refresh_recovery_descriptor  # noqa: E402

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
    c._queued_edit_events = {}
    c._active_prompt_client_ids = {}
    c._prompt_client_id_by_item = {}
    c._processor_tasks = {}
    c._in_flight_prompts = {}
    c._cancelled_ids = {}
    c._session_cancelled = {}
    c.user_prompt_manager = _UPM()
    c.turn_manager = TurnManager(c)
    c.handled: list[dict] = []
    c.dispatched: list[tuple[str, dict]] = []

    async def dispatch_raw(sid, event):
        c.dispatched.append((sid, event))

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


def test_recovery_descriptor_refresh_observes_terminal_transition() -> None:
    print("T1a delayed recovery refresh observes completed run")
    run_id = "refresh-terminal-run"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stale = {
        "run_id": run_id,
        "alive": True,
        "has_complete_json": False,
        "pid": 99999999,
    }

    class Provider:
        def recover_in_flight(self, *, run_id_filter=None):
            assert run_id_filter == {run_id}
            return [{
                **stale,
                "alive": False,
                "has_complete_json": (run_dir / "complete.json").exists(),
            }]

    (run_dir / "complete.json").write_text(
        '{"success": true, "error": null}',
        encoding="utf-8",
    )
    refreshed = asyncio.run(_refresh_recovery_descriptor(Provider(), stale))
    check("fresh completion overrides stale scan", refreshed["has_complete_json"] is True)
    check("completed run is not reattached alive", refreshed["alive"] is False)


def test_startup_recovery_gate_blocks_pre_registration_window() -> None:
    print("T1b startup recovery gate blocks before active_run_ids exist")
    c = _coord()
    sid = session_manager.create(name="recoverable", cwd="/tmp", orchestration_mode="native")["id"]
    session_manager.set_agent_sid(sid, "native", "provider-thread-1")
    run_dir = runs_root() / "recoverable-run"
    run_dir.mkdir(parents=True)
    jsonl_path = Path(tempfile.gettempdir()) / "provider-thread-1.jsonl"
    jsonl_path.write_text("", encoding="utf-8")
    atomic_write_json(
        run_dir / "state.json",
        {
            "session_id": "provider-thread-1",
            "app_session_id": sid,
            "jsonl_path": str(jsonl_path),
        },
    )

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


def test_startup_recovery_gate_does_not_block_never_ran_session() -> None:
    print("T1b2 startup recovery gate does not block never-ran sessions")
    c = _coord()
    sid = session_manager.create(name="new", cwd="/tmp", orchestration_mode="native")["id"]

    async def _go() -> bool:
        startup_recovery_gate.begin_recovery()
        c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
        for _ in range(20):
            if c.handled:
                break
            await asyncio.sleep(0.05)
        return len(c.handled) == 1

    ran = asyncio.run(_go())
    startup_recovery_gate.reset_for_tests()
    check("ran while unrelated startup recovery remained pending", ran)


def test_startup_recovery_gate_does_not_block_agent_session_without_run_dir() -> None:
    print("T1b3 startup recovery gate does not block unrelated agent session")
    c = _coord()
    sid = session_manager.create(name="old-no-run", cwd="/tmp", orchestration_mode="native")["id"]
    session_manager.set_agent_sid(sid, "native", "provider-thread-without-run")

    async def _go() -> bool:
        startup_recovery_gate.begin_recovery()
        c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
        for _ in range(20):
            if c.handled:
                break
            await asyncio.sleep(0.05)
        return len(c.handled) == 1

    ran = asyncio.run(_go())
    startup_recovery_gate.reset_for_tests()
    check("ran while startup recovery pending for other sessions", ran)


def test_startup_recovery_failure_fails_closed() -> None:
    print("T1c startup recovery failure prevents prompt handling")
    c = _coord()
    sid = session_manager.create(name="failed-recovery", cwd="/tmp", orchestration_mode="native")["id"]
    session_manager.set_agent_sid(sid, "native", "provider-thread-2")
    run_dir = runs_root() / "failed-recovery-run"
    run_dir.mkdir(parents=True)
    jsonl_path = Path(tempfile.gettempdir()) / "provider-thread-2.jsonl"
    jsonl_path.write_text("", encoding="utf-8")
    atomic_write_json(
        run_dir / "state.json",
        {
            "session_id": "provider-thread-2",
            "app_session_id": sid,
            "jsonl_path": str(jsonl_path),
        },
    )

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


def test_startup_recovery_gate_default_wait_is_fail_closed() -> None:
    print("T1d startup recovery gate default wait is fail-closed")

    async def _go() -> tuple[bool, bool]:
        startup_recovery_gate.begin_recovery()
        task = asyncio.create_task(startup_recovery_gate.wait_for_recovery_ready())
        await asyncio.sleep(0.2)
        still_waiting_initially = not task.done()
        await asyncio.sleep(2.1)
        remained_blocked = not task.done()
        startup_recovery_gate.mark_recovery_done()
        await asyncio.wait_for(task, timeout=1.0)
        return still_waiting_initially, remained_blocked and task.done()

    blocked_briefly, completed = asyncio.run(_go())
    startup_recovery_gate.reset_for_tests()
    check("blocked briefly while recovery pending", blocked_briefly)
    check("completed only after recovery became ready", completed)


def test_startup_recovery_gate_foreign_loop_waits_without_crashing() -> None:
    print("T1e startup recovery gate supports a foreign event loop")

    async def _main_loop_setup_and_release() -> tuple[bool, bool, str | None]:
        startup_recovery_gate.begin_recovery()
        # Bind the Event to this loop by registering a waiter, then leave it
        # pending while another event loop calls wait_for_recovery_ready().
        binder = asyncio.create_task(startup_recovery_gate.wait_for_recovery_ready(timeout=None))
        await asyncio.sleep(0.1)
        initially_blocked = not binder.done()

        result: dict[str, object] = {}

        def _foreign_wait() -> None:
            async def _go() -> None:
                try:
                    result["started"] = True
                    await startup_recovery_gate.wait_for_recovery_ready(timeout=1.0)
                    result["ok"] = True
                except BaseException as exc:  # pragma: no cover - diagnostic path
                    result["error"] = repr(exc)

            asyncio.run(_go())

        worker = asyncio.create_task(asyncio.to_thread(_foreign_wait))
        for _ in range(20):
            if result.get("started"):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        foreign_still_waiting = result.get("ok") is not True and "error" not in result
        startup_recovery_gate.mark_recovery_done()
        await asyncio.wait_for(binder, timeout=1.0)
        await asyncio.wait_for(worker, timeout=2.0)
        return initially_blocked, foreign_still_waiting and result.get("ok") is True, result.get("error")  # type: ignore[return-value]

    blocked, foreign_ok, error = asyncio.run(_main_loop_setup_and_release())
    startup_recovery_gate.reset_for_tests()
    check("main-loop waiter blocked while pending", blocked)
    check("foreign-loop waiter observed recovery without RuntimeError", foreign_ok)
    check("foreign-loop waiter had no error", error is None)


def test_startup_recovery_gate_first_waiter_foreign_loop_releases_promptly() -> None:
    print("T1f startup recovery gate signals first foreign-loop waiter promptly")

    async def _main_marks_done_after_foreign_bind() -> tuple[bool, bool, float, str | None]:
        startup_recovery_gate.begin_recovery()
        result: dict[str, object] = {}

        def _foreign_wait() -> None:
            async def _go() -> None:
                loop = asyncio.get_running_loop()
                result["started"] = loop.time()
                try:
                    await startup_recovery_gate.wait_for_recovery_ready(timeout=1.0)
                    result["elapsed"] = loop.time() - float(result["started"])
                    result["ok"] = True
                except BaseException as exc:  # pragma: no cover - diagnostic path
                    result["error"] = repr(exc)

            asyncio.run(_go())

        worker = asyncio.create_task(asyncio.to_thread(_foreign_wait))
        # This is the hazardous order: the first waiter is on a foreign loop,
        # so asyncio.Event binds there before the main loop marks recovery done.
        for _ in range(40):
            ready = startup_recovery_gate._ready  # intentionally white-boxed regression test
            if result.get("started") and ready is not None and getattr(ready, "_loop", None) is not None:
                break
            await asyncio.sleep(0.025)
        bound_to_foreign = startup_recovery_gate._ready is not None and getattr(startup_recovery_gate._ready, "_loop", None) is not asyncio.get_running_loop()
        startup_recovery_gate.mark_recovery_done()
        await asyncio.wait_for(worker, timeout=1.0)
        elapsed = float(result.get("elapsed") or 99.0)
        return bound_to_foreign, result.get("ok") is True, elapsed, result.get("error")  # type: ignore[return-value]

    bound, ok, elapsed, error = asyncio.run(_main_marks_done_after_foreign_bind())
    startup_recovery_gate.reset_for_tests()
    check("event bound to foreign loop first", bound)
    check("first foreign-loop waiter observed recovery", ok)
    check("first foreign-loop waiter released before timeout", elapsed < 0.5)
    check("first foreign-loop waiter had no error", error is None)


def test_session_recovery_gate_first_waiter_foreign_loop_releases_promptly() -> None:
    print("T1g session recovery gate signals first foreign-loop waiter promptly")

    async def _main_marks_done_after_foreign_bind() -> tuple[bool, bool, float, str | None]:
        startup_recovery_gate.begin_recovery()
        startup_recovery_gate.register_session_recovery({"sid-foreign"})
        result: dict[str, object] = {}

        def _foreign_wait() -> None:
            async def _go() -> None:
                loop = asyncio.get_running_loop()
                result["started"] = loop.time()
                try:
                    await startup_recovery_gate.wait_for_session_recovery_ready("sid-foreign")
                    result["elapsed"] = loop.time() - float(result["started"])
                    result["ok"] = True
                except BaseException as exc:  # pragma: no cover - diagnostic path
                    result["error"] = repr(exc)

            asyncio.run(_go())

        worker = asyncio.create_task(asyncio.to_thread(_foreign_wait))
        for _ in range(40):
            ready = startup_recovery_gate._session_ready.get("sid-foreign")
            if result.get("started") and ready is not None and getattr(ready, "_loop", None) is not None:
                break
            await asyncio.sleep(0.025)
        ready = startup_recovery_gate._session_ready.get("sid-foreign")
        bound_to_foreign = ready is not None and getattr(ready, "_loop", None) is not asyncio.get_running_loop()
        startup_recovery_gate.mark_session_recovery_done("sid-foreign")
        await asyncio.wait_for(worker, timeout=1.0)
        elapsed = float(result.get("elapsed") or 99.0)
        return bound_to_foreign, result.get("ok") is True, elapsed, result.get("error")  # type: ignore[return-value]

    bound, ok, elapsed, error = asyncio.run(_main_marks_done_after_foreign_bind())
    startup_recovery_gate.reset_for_tests()
    check("session event bound to foreign loop first", bound)
    check("session foreign-loop waiter observed recovery", ok)
    check("session foreign-loop waiter released before timeout", elapsed < 0.5)
    check("session foreign-loop waiter had no error", error is None)


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


def test_claimed_queued_prompt_removed_from_persisted_queue() -> None:
    print("T4 claimed queued prompt is removed only when ready to run")
    c = _coord()
    session = session_manager.create(name="queued", cwd="/tmp", orchestration_mode="native")
    sid = session["id"]
    queued_id = "queued-item-1"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]
    session_manager.add_queued_prompt(sid, {
        "id": queued_id,
        "prompt": "queued prompt",
        "client_id": "client-1",
    })

    async def _go() -> tuple[bool, bool, bool, bool]:
        c.submit_prompt(sid, {
            "prompt": "queued prompt",
            "app_session_id": sid,
            "_queued_id": queued_id,
            "client_id": "client-1",
        })
        await asyncio.sleep(0.4)
        blocked_queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
        persisted_while_blocked = any(item.get("id") == queued_id for item in blocked_queued)
        no_consumed_while_blocked = not any(
            event.get("type") == "queue_consumed"
            and (event.get("data") or {}).get("queued_id") == queued_id
            for _event_sid, event in c.dispatched
        )
        c.turn_manager.active_run_ids.pop(sid, None)
        for _ in range(40):
            queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
            if c.handled and not queued:
                break
            await asyncio.sleep(0.05)
        queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
        consumed = any(
            event.get("type") == "queue_consumed"
            and (event.get("data") or {}).get("queued_id") == queued_id
            for _event_sid, event in c.dispatched
        )
        return persisted_while_blocked, no_consumed_while_blocked, not queued, consumed

    persisted_while_blocked, no_consumed_while_blocked, removed, consumed = asyncio.run(_go())
    check("persisted while blocked before start", persisted_while_blocked)
    check("queue_consumed not emitted before start", no_consumed_while_blocked)
    check("persisted queued prompt removed", removed)
    check("queue_consumed emitted for claimed item", consumed)


def test_blocked_queued_prompt_can_be_cancelled_before_start() -> None:
    print("T5 blocked queued prompt can be cancelled before start")
    c = _coord()
    session = session_manager.create(name="cancel-blocked", cwd="/tmp", orchestration_mode="native")
    sid = session["id"]
    queued_id = "blocked-cancel-1"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]
    session_manager.add_queued_prompt(sid, {
        "id": queued_id,
        "prompt": "queued prompt",
        "client_id": "client-cancel",
    })

    async def _go() -> tuple[bool, bool, bool]:
        c.submit_prompt(sid, {
            "prompt": "queued prompt",
            "app_session_id": sid,
            "_queued_id": queued_id,
            "client_id": "client-cancel",
        })
        await asyncio.sleep(0.4)
        cancelled = c.cancel_queued(sid, queued_id)
        await asyncio.to_thread(session_manager.remove_queued_prompt, sid, queued_id)
        c.turn_manager.active_run_ids.pop(sid, None)
        await asyncio.sleep(0.4)
        queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
        consumed = any(
            event.get("type") == "queue_consumed"
            and (event.get("data") or {}).get("queued_id") == queued_id
            for _event_sid, event in c.dispatched
        )
        return cancelled, not c.handled, not queued and not consumed

    cancelled, skipped, cleared = asyncio.run(_go())
    check("cancel_queued accepted blocked item", cancelled)
    check("cancelled blocked item did not run", skipped)
    check("cancelled blocked item left no queue state", cleared)


def main() -> int:
    test_barrier_blocks_prompt_during_recovered_run()
    test_recovery_descriptor_refresh_observes_terminal_transition()
    test_startup_recovery_gate_blocks_pre_registration_window()
    test_startup_recovery_gate_does_not_block_never_ran_session()
    test_startup_recovery_gate_does_not_block_agent_session_without_run_dir()
    test_startup_recovery_failure_fails_closed()
    test_startup_recovery_gate_default_wait_is_fail_closed()
    test_startup_recovery_gate_foreign_loop_waits_without_crashing()
    test_startup_recovery_gate_first_waiter_foreign_loop_releases_promptly()
    test_session_recovery_gate_first_waiter_foreign_loop_releases_promptly()
    test_interrupt_during_overlap_fans_out_and_displaces()
    test_stale_pending_cleared_by_item_finally()
    test_claimed_queued_prompt_removed_from_persisted_queue()
    test_blocked_queued_prompt_can_be_cancelled_before_start()
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
