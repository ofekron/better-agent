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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home

_TEST_HOME = _test_home.TestHome.acquire("bc_test_recovl_")

from orchestrator import Coordinator  # noqa: E402
import session_queue_projection  # noqa: E402
import startup_recovery_gate  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from runs_dir import atomic_write_json, runs_root  # noqa: E402


def setup_function(_function) -> None:
    _test_home.engage(_TEST_HOME.path)

def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    assert ok, name


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def _wait_for_event(event: asyncio.Event, *, timeout: float = 2.0) -> None:
    await asyncio.wait_for(event.wait(), timeout=timeout)


def _observe_run_barrier(c: Coordinator) -> asyncio.Event:
    entered = asyncio.Event()
    wait_for_clear_runs = c.turn_manager.wait_for_clear_runs

    async def observed_wait_for_clear_runs(app_session_id: str) -> None:
        entered.set()
        await wait_for_clear_runs(app_session_id)

    c.turn_manager.wait_for_clear_runs = observed_wait_for_clear_runs
    return entered


async def _quiesce(c: Coordinator) -> None:
    c.turn_manager.active_run_ids.clear()
    await c.quiesce_prompt_processors()


def _run(coro):
    try:
        return asyncio.run(coro)
    finally:
        startup_recovery_gate.reset_for_tests()


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
    c = Coordinator()
    c.user_prompt_manager = _UPM()
    c.handled: list[dict] = []
    c.dispatched: list[tuple[str, dict]] = []
    c.handled_event = asyncio.Event()
    c.dispatched_event = asyncio.Event()
    c._cancel_recovered_run_fanout = None

    async def dispatch_raw(sid, event):
        c.dispatched.append((sid, event))
        c.dispatched_event.set()

    c.dispatch_raw = dispatch_raw

    async def handle_prompt(**params):
        c.handled.append(params)
        c.handled_event.set()

    c.handle_prompt = handle_prompt
    return c


def test_barrier_blocks_prompt_during_recovered_run() -> None:
    print("T1 prompt waits for recovered run to clear")
    c = _coord()
    sid = "sid-recov"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]

    async def _go() -> tuple[bool, bool]:
        barrier_entered = _observe_run_barrier(c)
        try:
            c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
            await _wait_for_event(barrier_entered)
            blocked = len(c.handled) == 0
            c.turn_manager.active_run_ids.pop(sid, None)
            await _wait_for_event(c.handled_event)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            return blocked, len(c.handled) == 1
        finally:
            await _quiesce(c)

    blocked, ran = _run(_go())
    check("blocked while recovered run alive", blocked)
    check("ran after recovered run cleared", ran)


def test_startup_recovery_gate_blocks_pre_registration_window() -> None:
    print("T1b startup recovery gate blocks before active_run_ids exist")
    c = _coord()
    sid = session_manager.create(
        name="recoverable", cwd=_TEST_HOME.path, orchestration_mode="native"
    )["id"]
    session_manager.set_agent_sid(sid, "native", "provider-thread-1")
    run_dir = runs_root() / "recoverable-run"
    run_dir.mkdir(parents=True)
    jsonl_path = Path(_TEST_HOME.path) / "provider-thread-1.jsonl"
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
        try:
            c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
            await _wait_until(lambda: startup_recovery_gate._ready.waiter_count() == 1)
            blocked = len(c.handled) == 0
            startup_recovery_gate.mark_recovery_done()
            await _wait_for_event(c.handled_event)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            return blocked, len(c.handled) == 1
        finally:
            if startup_recovery_gate.is_pending():
                startup_recovery_gate.mark_recovery_done()
            await _quiesce(c)

    try:
        blocked, ran = _run(_go())
        check("blocked before recovery registration", blocked)
        check("ran after recovery scan/integration completed", ran)
    finally:
        session_manager.delete(sid)


def test_session_recovery_gate_survives_global_classification() -> None:
    print("T1b1 session gate survives global recovery classification")

    async def _go() -> tuple[bool, bool]:
        startup_recovery_gate.begin_recovery()
        startup_recovery_gate.register_session_recovery({"live-session"})
        startup_recovery_gate.mark_recovery_done()
        task = asyncio.create_task(
            startup_recovery_gate.wait_for_session_recovery_ready("live-session")
        )
        try:
            fact = startup_recovery_gate._session_ready["live-session"]
            await _wait_until(lambda: fact.waiter_count() == 1)
            blocked = not task.done()
            startup_recovery_gate.mark_session_recovery_done("live-session")
            await task
            return blocked, task.done()
        finally:
            startup_recovery_gate.mark_session_recovery_done("live-session")
            await asyncio.gather(task, return_exceptions=True)

    blocked, released = _run(_go())
    check("session remained blocked after global classification", blocked)
    check("session released after its integration completed", released)


def test_startup_recovery_gate_does_not_block_never_ran_session() -> None:
    print("T1b2 startup recovery gate does not block never-ran sessions")
    c = _coord()
    sid = session_manager.create(
        name="new", cwd=_TEST_HOME.path, orchestration_mode="native"
    )["id"]

    async def _go() -> bool:
        startup_recovery_gate.begin_recovery()
        try:
            c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
            await _wait_for_event(c.handled_event)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            return len(c.handled) == 1
        finally:
            startup_recovery_gate.mark_recovery_done()
            await _quiesce(c)

    try:
        ran = _run(_go())
        check("ran while unrelated startup recovery remained pending", ran)
    finally:
        session_manager.delete(sid)


def test_startup_recovery_gate_does_not_block_agent_session_without_run_dir() -> None:
    print("T1b3 startup recovery gate does not block unrelated agent session")
    c = _coord()
    sid = session_manager.create(
        name="old-no-run", cwd=_TEST_HOME.path, orchestration_mode="native"
    )["id"]
    session_manager.set_agent_sid(sid, "native", "provider-thread-without-run")

    async def _go() -> bool:
        startup_recovery_gate.begin_recovery()
        try:
            c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
            await _wait_for_event(c.handled_event)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            return len(c.handled) == 1
        finally:
            startup_recovery_gate.mark_recovery_done()
            await _quiesce(c)

    try:
        ran = _run(_go())
        check("ran while startup recovery pending for other sessions", ran)
    finally:
        session_manager.delete(sid)


def test_startup_recovery_failure_fails_closed() -> None:
    print("T1c startup recovery failure prevents prompt handling")
    c = _coord()
    sid = session_manager.create(
        name="failed-recovery", cwd=_TEST_HOME.path, orchestration_mode="native"
    )["id"]
    session_manager.set_agent_sid(sid, "native", "provider-thread-2")
    run_dir = runs_root() / "failed-recovery-run"
    run_dir.mkdir(parents=True)
    jsonl_path = Path(_TEST_HOME.path) / "provider-thread-2.jsonl"
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
        try:
            c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
            await _wait_until(lambda: startup_recovery_gate._ready.waiter_count() == 1)
            startup_recovery_gate.mark_recovery_failed("boom")
            await _wait_for_event(c.dispatched_event)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            return len(c.handled) == 0
        finally:
            await _quiesce(c)

    try:
        blocked = _run(_go())
        check("did not handle prompt after recovery failure", blocked)
    finally:
        session_manager.delete(sid)


def test_startup_recovery_gate_default_wait_is_fail_closed() -> None:
    print("T1d startup recovery gate default wait is fail-closed")

    async def _go() -> tuple[bool, bool]:
        startup_recovery_gate.begin_recovery()
        observed_timeouts: list[float | None] = []
        original_wait = startup_recovery_gate._ready.wait

        async def observed_wait(timeout=None):
            observed_timeouts.append(timeout)
            return await original_wait(timeout)

        startup_recovery_gate._ready.wait = observed_wait
        task = asyncio.create_task(startup_recovery_gate.wait_for_recovery_ready())
        try:
            await _wait_until(lambda: startup_recovery_gate._ready.waiter_count() == 1)
            still_waiting_initially = not task.done() and observed_timeouts == [None]
            startup_recovery_gate.mark_recovery_done()
            await asyncio.wait_for(task, timeout=1.0)
            return still_waiting_initially, task.done()
        finally:
            startup_recovery_gate._ready.wait = original_wait
            startup_recovery_gate.mark_recovery_done()
            await asyncio.gather(task, return_exceptions=True)

    blocked_briefly, completed = _run(_go())
    check("blocked briefly while recovery pending", blocked_briefly)
    check("completed only after recovery became ready", completed)


def test_startup_recovery_gate_foreign_loop_waits_without_crashing() -> None:
    print("T1e startup recovery gate supports a foreign event loop")

    async def _main_loop_setup_and_release() -> tuple[bool, bool, str | None]:
        startup_recovery_gate.begin_recovery()
        # Bind the Event to this loop by registering a waiter, then leave it
        # pending while another event loop calls wait_for_recovery_ready().
        binder = asyncio.create_task(startup_recovery_gate.wait_for_recovery_ready(timeout=None))
        worker = None

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

        try:
            await _wait_until(lambda: startup_recovery_gate._ready.waiter_count() == 1)
            initially_blocked = not binder.done()
            worker = asyncio.create_task(asyncio.to_thread(_foreign_wait))
            await _wait_until(lambda: startup_recovery_gate._ready.waiter_count() == 2)
            foreign_still_waiting = not worker.done()
            startup_recovery_gate.mark_recovery_done()
            await asyncio.wait_for(binder, timeout=1.0)
            await asyncio.wait_for(worker, timeout=2.0)
            return initially_blocked, foreign_still_waiting and result.get("ok") is True, result.get("error")  # type: ignore[return-value]
        finally:
            startup_recovery_gate.mark_recovery_done()
            await asyncio.gather(
                binder,
                *((worker,) if worker is not None else ()),
                return_exceptions=True,
            )

    blocked, foreign_ok, error = _run(_main_loop_setup_and_release())
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
        try:
            await _wait_until(lambda: startup_recovery_gate._ready.waiter_count() == 1)
            bound_to_foreign = startup_recovery_gate._ready.waiter_count() > 0
            startup_recovery_gate.mark_recovery_done()
            await asyncio.wait_for(worker, timeout=1.0)
            elapsed = float(result.get("elapsed") or 99.0)
            return bound_to_foreign, result.get("ok") is True, elapsed, result.get("error")  # type: ignore[return-value]
        finally:
            startup_recovery_gate.mark_recovery_done()
            await asyncio.gather(worker, return_exceptions=True)

    bound, ok, elapsed, error = _run(_main_marks_done_after_foreign_bind())
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
        try:
            ready = startup_recovery_gate._session_ready["sid-foreign"]
            await _wait_until(lambda: ready.waiter_count() == 1)
            bound_to_foreign = ready.waiter_count() > 0
            startup_recovery_gate.mark_session_recovery_done("sid-foreign")
            await asyncio.wait_for(worker, timeout=1.0)
            elapsed = float(result.get("elapsed") or 99.0)
            return bound_to_foreign, result.get("ok") is True, elapsed, result.get("error")  # type: ignore[return-value]
        finally:
            startup_recovery_gate.mark_session_recovery_done("sid-foreign")
            await asyncio.gather(worker, return_exceptions=True)

    bound, ok, elapsed, error = _run(_main_marks_done_after_foreign_bind())
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
        barrier_entered = _observe_run_barrier(c)
        try:
            c.submit_prompt(sid, {"prompt": "hi", "app_session_id": sid})
            await _wait_for_event(barrier_entered)
            landed = await c.turn_manager.cancel_turn(
                sid, interrupted_by_msg_id="lm-1",
            )
            parked = c.turn_manager._pending_cancel.get(sid) == "lm-1"
            return landed, parked
        finally:
            await _quiesce(c)

    landed, parked = _run(_go())
    check("cancel landed", landed is True)
    check("fanout reached recovered run", fanned == ["recovered-run"])
    check("pending cancel parked for queued prompt", parked)


def test_stale_pending_cleared_by_item_finally() -> None:
    print("T3 stale pending cancel cleared by processor finally")
    c = _coord()
    sid = "sid-stale"

    async def _go() -> tuple[bool, bool]:
        first_entered = asyncio.Event()
        first_release = asyncio.Event()
        second_handled = asyncio.Event()

        async def observed_handle_prompt(**params) -> None:
            c.handled.append(params)
            if len(c.handled) == 1:
                first_entered.set()
                await first_release.wait()
            else:
                second_handled.set()

        c.handle_prompt = observed_handle_prompt
        try:
            c.submit_prompt(sid, {"prompt": "one", "app_session_id": sid})
            await _wait_for_event(first_entered)
            c.turn_manager._pending_cancel[sid] = True
            first_release.set()
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            cleared = sid not in c.turn_manager._pending_cancel
            c.submit_prompt(sid, {"prompt": "two", "app_session_id": sid})
            await _wait_for_event(second_handled)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            return cleared, len(c.handled) == 2
        finally:
            first_release.set()
            await _quiesce(c)

    cleared, second_ran = _run(_go())
    check("pending cleared after item", cleared)
    check("next prompt unaffected", second_ran)


def test_claimed_queued_prompt_persists_at_delivery_boundary() -> None:
    print("T4 claimed queued prompt is consumed only at durable delivery")
    c = _coord()
    session = session_manager.create(
        name="queued", cwd=_TEST_HOME.path, orchestration_mode="native"
    )
    sid = session["id"]
    queued_id = "queued-item-1"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]
    session_manager.add_queued_prompt(sid, {
        "id": queued_id,
        "prompt": "queued prompt",
        "client_id": "client-1",
    })

    async def _go() -> tuple[bool, bool, bool]:
        barrier_entered = asyncio.Event()
        delivery_attempted = asyncio.Event()
        delivery_errors: list[BaseException] = []
        wait_for_clear_runs = c.turn_manager.wait_for_clear_runs

        async def observed_wait_for_clear_runs(session_id: str) -> None:
            barrier_entered.set()
            await wait_for_clear_runs(session_id)

        async def persist_prompt(**params) -> None:
            c.handled.append(params)
            try:
                persisted_session = session_manager.get(params["app_session_id"])
                assert persisted_session is not None
                await c._init_turn_messages(
                    session=persisted_session,
                    app_session_id=params["app_session_id"],
                    prompt=params["prompt"],
                    images=params.get("images"),
                    files=params.get("files"),
                    client_id=params.get("client_id"),
                    source=params.get("source"),
                    cli_prompt=params.get("cli_prompt"),
                    queue_item_id=params.get("queue_item_id"),
                    team_message=params.get("team_message"),
                    file_discussion_id=params.get("file_discussion_id"),
                )
            except BaseException as exc:
                delivery_errors.append(exc)
                raise
            finally:
                delivery_attempted.set()

        c.turn_manager.wait_for_clear_runs = observed_wait_for_clear_runs
        c.handle_prompt = persist_prompt
        try:
            c.submit_prompt(sid, {
                "prompt": "queued prompt",
                "app_session_id": sid,
                "_queued_id": queued_id,
                "client_id": "client-1",
            })
            await _wait_for_event(barrier_entered)
            blocked_queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
            persisted_while_blocked = any(item.get("id") == queued_id for item in blocked_queued)
            no_user_message_while_blocked = not (
                (session_manager.get(sid) or {}).get("messages") or []
            )
            c.turn_manager.active_run_ids.pop(sid, None)
            await _wait_for_event(delivery_attempted)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            persisted = session_manager.get(sid) or {}
            queued = persisted.get("queued_prompts") or []
            user_messages = [
                message
                for message in persisted.get("messages") or []
                if message.get("role") == "user"
            ]
            return persisted_while_blocked, no_user_message_while_blocked, (
                not delivery_errors
                and not queued
                and len(user_messages) == 1
                and user_messages[0].get("content") == "queued prompt"
            )
        finally:
            await _quiesce(c)

    try:
        persisted_while_blocked, no_user_message_while_blocked, delivered = _run(_go())
        check("persisted while blocked before start", persisted_while_blocked)
        check("user message not persisted before start", no_user_message_while_blocked)
        check("queue consumed with durable user message", delivered)
    finally:
        session_manager.delete(sid)


def test_blocked_queued_prompt_can_be_cancelled_before_start() -> None:
    print("T5 blocked queued prompt can be cancelled before start")
    c = _coord()
    session = session_manager.create(
        name="cancel-blocked", cwd=_TEST_HOME.path, orchestration_mode="native"
    )
    sid = session["id"]
    queued_id = "blocked-cancel-1"
    c.turn_manager.active_run_ids[sid] = ["recovered-run"]
    session_manager.add_queued_prompt(sid, {
        "id": queued_id,
        "prompt": "queued prompt",
        "client_id": "client-cancel",
    })

    async def _go() -> tuple[bool, bool, bool]:
        barrier_entered = _observe_run_barrier(c)
        try:
            c.submit_prompt(sid, {
                "prompt": "queued prompt",
                "app_session_id": sid,
                "_queued_id": queued_id,
                "client_id": "client-cancel",
            })
            await _wait_for_event(barrier_entered)
            cancelled = c.cancel_queued(sid, queued_id)
            await asyncio.to_thread(session_manager.remove_queued_prompt, sid, queued_id)
            c.turn_manager.active_run_ids.pop(sid, None)
            await _wait_until(lambda: sid not in c._in_flight_prompts)
            queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
            consumed = any(
                event.get("type") == "queue_consumed"
                and (event.get("data") or {}).get("queued_id") == queued_id
                for _event_sid, event in c.dispatched
            )
            return cancelled, not c.handled, not queued and not consumed
        finally:
            await _quiesce(c)

    try:
        cancelled, skipped, cleared = _run(_go())
        check("cancel_queued accepted blocked item", cancelled)
        check("cancelled blocked item did not run", skipped)
        check("cancelled blocked item left no queue state", cleared)
    finally:
        session_manager.delete(sid)


def main() -> int:
    test_barrier_blocks_prompt_during_recovered_run()
    test_startup_recovery_gate_blocks_pre_registration_window()
    test_session_recovery_gate_survives_global_classification()
    test_startup_recovery_gate_does_not_block_never_ran_session()
    test_startup_recovery_gate_does_not_block_agent_session_without_run_dir()
    test_startup_recovery_failure_fails_closed()
    test_startup_recovery_gate_default_wait_is_fail_closed()
    test_startup_recovery_gate_foreign_loop_waits_without_crashing()
    test_startup_recovery_gate_first_waiter_foreign_loop_releases_promptly()
    test_session_recovery_gate_first_waiter_foreign_loop_releases_promptly()
    test_interrupt_during_overlap_fans_out_and_displaces()
    test_stale_pending_cleared_by_item_finally()
    test_claimed_queued_prompt_persists_at_delivery_boundary()
    test_blocked_queued_prompt_can_be_cancelled_before_start()
    print()
    print("ALL PASS")
    return 0


def teardown_module() -> None:
    session_manager.flush_pending_persists()
    session_queue_projection.shutdown(timeout=None)
    _TEST_HOME.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        teardown_module()
