from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="bc-test-startup-recovery-priority-")
os.environ["BETTER_AGENT_HOME"] = HOME

import main  # noqa: E402
import perf  # noqa: E402
import startup_recovery_gate  # noqa: E402


def test_pending_recovery_is_restart_busy_without_cache_refresh() -> None:
    original = main.coordinator.turn_manager._refresh_cache
    startup_recovery_gate.begin_recovery()
    main.coordinator.turn_manager._refresh_cache = lambda: (_ for _ in ()).throw(
        AssertionError("pending recovery must short-circuit runtime cache refresh")
    )
    try:
        assert main._system_busy_for_auto_restart() is True
    finally:
        main.coordinator.turn_manager._refresh_cache = original
        startup_recovery_gate.reset_for_tests()


def test_startup_source_orders_recovery_before_maintenance() -> None:
    source = inspect.getsource(main.on_startup)
    provider = source.index("await _to_thread_join_on_cancel(load_all_providers)")
    recovery_create = source.index('name="startup-recover-in-flight"')
    recovery_wait = source.index("await recovery_task")
    housekeeping = source.index('"startup_tasks.housekeeping"')
    extensions = source.index('"startup_tasks.extension_reconciliation"')
    assert provider < recovery_create < recovery_wait < housekeeping < extensions
    assert "startup_recovery_gate.mark_recovery_failed" in source


def test_recovery_gate_opens_after_live_integration_before_background_recovery() -> None:
    source = inspect.getsource(main._recover_in_flight_task)
    integrate = source.index("await integrate_recovered_runs")
    cold = source.index("_enqueue_recovered_cold_runs(cold)")
    reenqueue = source.index("await _re_enqueue_queued_prompts")
    opened = source.index("startup_recovery_gate.mark_recovery_done()")
    assert integrate < opened < cold
    assert opened < reenqueue


def test_maintenance_metrics_cover_success_error_and_cancel() -> None:
    async def scenario() -> None:
        with perf._lock:
            perf._stats.clear()
            perf._counts.clear()
        assert await main._run_maintenance_phase("test_success", lambda: 7) == 7
        assert await main._run_maintenance_phase(
            "test_error", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        ) is None

        started = asyncio.Event()

        def block() -> None:
            started_loop.call_soon_threadsafe(started.set)
            release.wait()

        import threading
        release = threading.Event()
        nonlocal_state["release"] = release
        task = asyncio.create_task(main._run_maintenance_phase("test_cancel", block))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        with perf._lock:
            assert perf._counts["startup.maintenance.test_success.success"]["total"] == 1
            assert perf._counts["startup.maintenance.test_error.error"]["total"] == 1
            assert perf._counts["startup.maintenance.test_cancel.cancelled"]["total"] == 1
            assert all(
                f"startup.maintenance.test_{outcome}" in perf._stats
                for outcome in ("success", "error", "cancel")
            )

    nonlocal_state: dict[str, object] = {}
    started_loop = asyncio.new_event_loop()
    try:
        started_loop.run_until_complete(scenario())
    finally:
        release = nonlocal_state.get("release")
        if release is not None:
            release.set()
        started_loop.close()


def test_cancelled_thread_work_is_joined_before_cancellation_returns() -> None:
    async def scenario() -> None:
        import threading
        import runs_dir
        entered = threading.Event()
        release = threading.Event()
        contender_acquired = threading.Event()
        catalog_root = Path(HOME) / "runs"

        def blocked() -> None:
            with runs_dir.run_catalog_lock(catalog_root):
                entered.set()
                release.wait()

        def contender() -> None:
            with runs_dir.run_catalog_lock(catalog_root):
                contender_acquired.set()

        task = asyncio.create_task(main._to_thread_join_on_cancel(blocked))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        contender_task = asyncio.create_task(asyncio.to_thread(contender))
        await asyncio.sleep(0.05)
        assert not contender_acquired.is_set()
        release.set()
        try:
            await asyncio.wait_for(task, 2)
        except asyncio.CancelledError:
            pass
        await asyncio.wait_for(contender_task, 2)
        assert contender_acquired.is_set()
        assert task.done()

    asyncio.run(scenario())


def main_test() -> None:
    test_pending_recovery_is_restart_busy_without_cache_refresh()
    test_startup_source_orders_recovery_before_maintenance()
    test_recovery_gate_opens_after_live_integration_before_background_recovery()
    test_maintenance_metrics_cover_success_error_and_cancel()
    test_cancelled_thread_work_is_joined_before_cancellation_returns()
    print("ALL PASS")


if __name__ == "__main__":
    try:
        main_test()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
