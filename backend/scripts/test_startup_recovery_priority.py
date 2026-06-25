from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="bc-test-startup-recovery-priority-")
os.environ["BETTER_AGENT_HOME"] = HOME

import main  # noqa: E402
import app_version  # noqa: E402
import orchestrator  # noqa: E402
import perf  # noqa: E402
import provider  # noqa: E402
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


def test_auto_restart_compares_process_commit_to_repository_head() -> None:
    original_process_sha = app_version.current_commit_sha
    original_head_sha = app_version.repository_head_commit_sha
    try:
        app_version.current_commit_sha = lambda: "a" * 40
        app_version.repository_head_commit_sha = lambda: "b" * 40
        assert main._has_new_commit_for_auto_restart() is True

        app_version.repository_head_commit_sha = lambda: "a" * 40
        assert main._has_new_commit_for_auto_restart() is False
    finally:
        app_version.current_commit_sha = original_process_sha
        app_version.repository_head_commit_sha = original_head_sha


def test_startup_source_orders_recovery_before_maintenance() -> None:
    source = inspect.getsource(main.on_startup)
    provider = source.index("await _to_thread_join_on_cancel(load_all_providers)")
    recovery_create = source.index('name="startup-recover-in-flight"')
    recovery_wait = source.index("await recovery_task")
    housekeeping = source.index('"startup_tasks.housekeeping"')
    extensions = source.index('"startup_tasks.extension_reconciliation"')
    assert provider < recovery_create < recovery_wait < housekeeping < extensions
    assert "startup_recovery_gate.mark_recovery_failed" in source


def test_startup_orchestrator_failure_releases_recovery_gate() -> None:
    source = inspect.getsource(main.on_startup)
    callback = source.index("def _startup_orchestrator_done")
    registration = source.index("_STARTUP_ORCHESTRATOR_TASK.add_done_callback")
    assert callback < registration
    assert "startup_recovery_gate.is_pending()" in source[callback:registration]
    assert "startup_recovery_gate.mark_recovery_failed(str(exc))" in source[callback:registration]


def test_recovery_gate_opens_after_live_integration_before_background_recovery() -> None:
    source = inspect.getsource(main._recover_in_flight_task)
    integrate = source.index("await integrate_recovered_runs")
    cold = source.index("_enqueue_recovered_cold_runs(cold)")
    reenqueue = source.index("await _re_enqueue_queued_prompts")
    opened = source.index("startup_recovery_gate.mark_recovery_done()")
    assert integrate < opened < cold
    assert opened < reenqueue


def test_prompt_waits_only_for_session_recovery_gate() -> None:
    source = inspect.getsource(orchestrator.Coordinator._run_session_processor)
    assert "wait_for_session_recovery_ready" in source
    assert "wait_for_recovery_ready()" not in source


def test_provider_recovery_does_not_wrap_scan_in_catalog_lock() -> None:
    source = inspect.getsource(provider.recover_all_in_flight)
    assert "run_catalog_lock" not in source
    assert "_recover_all_in_flight_owned(loop)" in source


def test_provider_recovery_prioritizes_known_running_scan_buckets() -> None:
    source = inspect.getsource(provider._recover_all_in_flight_owned)
    split = source.index("_split_recovery_scan_run_ids")
    append_likely = source.index("scan_inputs.append((owner_id, owner, likely_running))")
    append_other = source.index("scan_inputs.append((owner_id, owner, other))")
    scan = source.index("def _scan_one")
    assert split < append_likely < append_other < scan


def test_live_recovery_registers_session_gates_and_sorts_priority() -> None:
    source = inspect.getsource(main._recover_in_flight_task)
    register = source.index("register_session_recovery")
    sort = source.index("_sort_recovered_runs_by_session_priority(live)")
    pop = source.index("_pop_next_recovered_session_batch")
    integrate = source.index("await integrate_recovered_runs(coordinator, batch)")
    mark = source.index("mark_session_recovery_done")
    assert register < sort < pop < integrate < mark


def test_late_priority_is_rechecked_between_live_recovery_batches() -> None:
    source = inspect.getsource(main._recover_in_flight_task)
    loop = source.index("while remaining_live:")
    pop = source.index("_pop_next_recovered_session_batch", loop)
    integrate = source.index("await integrate_recovered_runs(coordinator, batch)", pop)
    assert loop < pop < integrate


def test_recovery_prioritizes_sessions_with_queued_prompts() -> None:
    original = main.session_manager.queued_prompt_count
    main.session_manager.queued_prompt_count = lambda sid: 1 if sid == "queued" else 0
    try:
        ordered = main._sort_recovered_runs_by_session_priority([
            {"run_id": "b", "app_session_id": "idle"},
            {"run_id": "a", "app_session_id": "queued"},
        ])
    finally:
        main.session_manager.queued_prompt_count = original
    assert [item["app_session_id"] for item in ordered] == ["queued", "idle"]


def test_recovery_sort_caches_queued_prompt_counts_per_session() -> None:
    calls: list[str] = []
    original = main.session_manager.queued_prompt_count

    def counted(sid: str) -> int:
        calls.append(sid)
        return 1 if sid == "queued" else 0

    main.session_manager.queued_prompt_count = counted
    try:
        ordered = main._sort_recovered_runs_by_session_priority([
            {"run_id": "b", "app_session_id": "idle"},
            {"run_id": "c", "app_session_id": "queued"},
            {"run_id": "a", "app_session_id": "queued"},
        ])
    finally:
        main.session_manager.queued_prompt_count = original
    assert [item["run_id"] for item in ordered] == ["a", "c", "b"]
    assert calls.count("queued") == 1
    assert calls.count("idle") == 1


def test_provider_recovery_splits_likely_running_run_ids_first() -> None:
    root = Path(HOME) / "runs-priority"
    root.mkdir(parents=True, exist_ok=True)
    live = root / "live-run"
    cold = root / "cold-run"
    complete = root / "complete-run"
    for child in (live, cold, complete):
        child.mkdir()
    (live / "backend_state.json").write_text(
        json.dumps({"run_id": live.name, "runner_pid": 12345}),
        encoding="utf-8",
    )
    (cold / "backend_state.json").write_text(
        json.dumps({"run_id": cold.name, "runner_pid": 54321}),
        encoding="utf-8",
    )
    (complete / "backend_state.json").write_text(
        json.dumps({"run_id": complete.name, "runner_pid": 12345}),
        encoding="utf-8",
    )
    (complete / "complete.json").write_text("{}", encoding="utf-8")

    original_process_control = provider._process_control

    class FakeProcessControl:
        @staticmethod
        def pid_alive(pid: int) -> bool:
            return pid == 12345

    provider._process_control = lambda: FakeProcessControl()
    try:
        likely, other = provider._split_recovery_scan_run_ids(
            root,
            {live.name, cold.name, complete.name},
        )
    finally:
        provider._process_control = original_process_control

    assert likely == {live.name}
    assert other == {cold.name, complete.name}


def test_cold_recovery_uses_reschedulable_session_pending_set() -> None:
    source = inspect.getsource(main._enqueue_recovered_cold_runs)
    assert "_RECOVERED_COLD_PENDING" in source
    assert "_RECOVERED_COLD_READY.set()" in source
    assert "_RECOVERED_COLD_RUN_BATCH_MAX" not in inspect.getsource(main)
    worker = inspect.getsource(main._recovered_cold_run_worker)
    assert "_pop_next_recovered_cold_batch_locked()" in worker


def test_selected_session_recovery_has_parallel_fast_lane() -> None:
    source = inspect.getsource(main._promote_recovered_session)
    assert "_RECOVERED_COLD_PENDING.pop(app_session_id" in source
    assert "await integrate_recovered_runs(coordinator, batch)" in source
    ws_source = inspect.getsource(main.websocket_chat)
    subscribe = ws_source.index('if msg_type == "subscribe":')
    task = ws_source.index("_promote_recovered_session", subscribe)
    assert subscribe < task


def test_ws_subscribe_prioritizes_watched_session_recovery() -> None:
    source = inspect.getsource(main.websocket_chat)
    subscribe = source.index('if msg_type == "subscribe":')
    priority = source.index("request_session_priority", subscribe)
    register = source.index("_register(sub_sid", subscribe)
    assert subscribe < priority < register


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
    test_auto_restart_compares_process_commit_to_repository_head()
    test_startup_source_orders_recovery_before_maintenance()
    test_startup_orchestrator_failure_releases_recovery_gate()
    test_recovery_gate_opens_after_live_integration_before_background_recovery()
    test_prompt_waits_only_for_session_recovery_gate()
    test_provider_recovery_does_not_wrap_scan_in_catalog_lock()
    test_provider_recovery_prioritizes_known_running_scan_buckets()
    test_live_recovery_registers_session_gates_and_sorts_priority()
    test_late_priority_is_rechecked_between_live_recovery_batches()
    test_recovery_prioritizes_sessions_with_queued_prompts()
    test_recovery_sort_caches_queued_prompt_counts_per_session()
    test_provider_recovery_splits_likely_running_run_ids_first()
    test_cold_recovery_uses_reschedulable_session_pending_set()
    test_selected_session_recovery_has_parallel_fast_lane()
    test_ws_subscribe_prioritizes_watched_session_recovery()
    test_maintenance_metrics_cover_success_error_and_cancel()
    test_cancelled_thread_work_is_joined_before_cancellation_returns()
    print("ALL PASS")


if __name__ == "__main__":
    try:
        main_test()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
