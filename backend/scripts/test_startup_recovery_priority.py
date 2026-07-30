from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

HOME = tempfile.mkdtemp(prefix="bc-test-startup-recovery-priority-")
os.environ["BETTER_AGENT_HOME"] = HOME

import main  # noqa: E402
import installation_profile  # noqa: E402
import recovery  # noqa: E402
import app_lifecycle  # noqa: E402
import orchestrator  # noqa: E402
import perf  # noqa: E402
import provider  # noqa: E402
import run_recovery  # noqa: E402
import startup_recovery_gate  # noqa: E402
import turn_manager  # noqa: E402
import ws_chat  # noqa: E402


def test_startup_source_orders_recovery_before_maintenance() -> None:
    source = inspect.getsource(app_lifecycle.on_startup)
    recovery_create = source.index('name="startup-recover-in-flight"')
    recovery_wait = source.index("await recovery_task")
    housekeeping = source.index('"startup_tasks.housekeeping"')
    extensions = source.index('"startup_tasks.extension_reconciliation"')
    assert recovery_create < recovery_wait < housekeeping < extensions
    assert "load_all_providers" not in source
    assert "startup_recovery_gate.mark_recovery_failed" in source


def test_startup_orchestrator_failure_releases_recovery_gate() -> None:
    source = inspect.getsource(app_lifecycle.on_startup)
    callback = source.index("def _startup_orchestrator_done")
    registration = source.index("_STARTUP_ORCHESTRATOR_TASK.add_done_callback")
    assert callback < registration
    assert "startup_recovery_gate.is_pending()" in source[callback:registration]
    assert "startup_recovery_gate.mark_recovery_failed(str(exc))" in source[callback:registration]


def test_recovery_gate_opens_after_live_integration_before_background_recovery() -> None:
    source = inspect.getsource(recovery._recover_in_flight_task)
    scan = source.index("pre_provider_orphan_candidates")
    integrate = source.index("await integrate_recovered_runs")
    cold = source.index("_enqueue_recovered_cold_runs(cold)")
    opened = source.index("startup_recovery_gate.mark_recovery_done()")
    # Re-enqueue is awaited inline after live integration, so a rehydrated
    # prompt can never start before its run is registered on the gate.
    register = source.index("register_session_recovery")
    candidates = source.index("candidates = await candidate_task")
    lifecycle_reconciled = source.index(
        "await reconcile_unbound_lifecycle_orphans("
    )
    reenqueue = source.index(
        "rehydrated_session_ids = await _re_enqueue_queued_prompts()"
    )
    start_processors = source.index(
        "await _coordinator_ref.start_session_processor_async(sid)",
    )
    assert scan < register < candidates < lifecycle_reconciled < opened
    assert opened < integrate < reenqueue < start_processors < cold


def test_slow_queued_prompt_rehydration_does_not_delay_live_recovery() -> None:
    async def scenario() -> None:
        reenqueue_release = asyncio.Event()
        candidate_release = threading.Event()
        recovery_started = threading.Event()
        live_integrated = asyncio.Event()
        processor_started = asyncio.Event()
        processor_start_count = 0

        def candidates() -> list:
            assert candidate_release.wait(timeout=2.0)
            return []

        async def reenqueue() -> set[str]:
            await reenqueue_release.wait()
            return {"sid"}

        def recover(*args, **kwargs) -> list[dict]:
            recovery_started.set()
            return [{"run_id": "run", "app_session_id": "sid", "alive": True}]

        async def reconcile(*args, **kwargs) -> None:
            return None

        async def reconcile_lifecycle(*args, **kwargs) -> int:
            return 0

        async def integrate(*args, **kwargs) -> None:
            live_integrated.set()

        async def reconcile_lifecycle_projection() -> None:
            return None

        async def start_processor(sid: str) -> None:
            nonlocal processor_start_count
            assert sid == "sid"
            assert not startup_recovery_gate.is_pending()
            processor_start_count += 1
            processor_started.set()

        startup_recovery_gate.reset_for_tests()
        startup_recovery_gate.begin_recovery()
        with (
            patch.object(recovery, "_re_enqueue_queued_prompts", side_effect=reenqueue),
            patch.object(recovery, "pre_provider_orphan_candidates", side_effect=candidates),
            patch.object(recovery, "recover_all_in_flight", side_effect=recover),
            patch.object(recovery, "take_recovery_scan_ownership", return_value=({}, True)),
            patch.object(recovery, "reconcile_pre_provider_orphans", side_effect=reconcile),
            patch.object(
                recovery,
                "reconcile_unbound_lifecycle_orphans",
                side_effect=reconcile_lifecycle,
            ),
            patch.object(recovery, "integrate_recovered_runs", side_effect=integrate),
            patch.object(
                installation_profile,
                "integrations_enabled",
                return_value=True,
            ),
            patch.object(
                main.coordinator.turn_manager,
                "reconcile_lifecycle_projection",
                side_effect=reconcile_lifecycle_projection,
            ),
            patch.object(
                main.coordinator,
                "start_session_processor_async",
                side_effect=start_processor,
            ),
        ):
            recovery_task = asyncio.create_task(recovery._recover_in_flight_task())
            assert await asyncio.to_thread(recovery_started.wait, 1.0)
            gate_wait = asyncio.create_task(
                startup_recovery_gate.wait_for_recovery_ready(timeout=None)
            )
            await asyncio.sleep(0)
            assert not gate_wait.done()
            assert not live_integrated.is_set()
            candidate_release.set()
            await asyncio.wait_for(gate_wait, timeout=1.0)
            await asyncio.wait_for(live_integrated.wait(), timeout=1.0)
            assert not recovery_task.done()
            assert not processor_started.is_set()
            reenqueue_release.set()
            await asyncio.wait_for(recovery_task, timeout=2.0)
            assert processor_started.is_set()
            assert processor_start_count == 1
        startup_recovery_gate.reset_for_tests()

    asyncio.run(scenario())


def test_prompt_waits_only_for_session_recovery_gate() -> None:
    source = inspect.getsource(orchestrator.Coordinator._run_session_processor)
    assert "wait_for_session_recovery_ready" in source
    assert "wait_for_recovery_ready()" not in source


def test_provider_start_does_not_repeat_orchestrator_recovery_gate() -> None:
    source = inspect.getsource(turn_manager.TurnManager._drive_cli_run)
    assert "wait_for_recovery_ready()" not in source
    assert "wait_for_session_recovery_ready(" not in source


def test_provider_recovery_does_not_wrap_scan_in_catalog_lock() -> None:
    source = inspect.getsource(provider.recover_all_in_flight)
    assert "run_catalog_lock" not in source
    assert "_recover_all_in_flight_owned(" in source


def test_provider_recovery_prioritizes_known_running_scan_buckets() -> None:
    source = inspect.getsource(provider._recover_all_in_flight_owned)
    split = source.index("_split_recovery_scan_run_ids")
    append_likely = source.index("scan_inputs.append((owner_id, owner, likely_running))")
    append_other = source.index("scan_inputs.append((owner_id, owner, other))")
    scan = source.index("def _scan_one")
    assert split < append_likely < append_other < scan


def test_live_recovery_finishes_before_cold_scan_starts() -> None:
    source = inspect.getsource(recovery._recover_in_flight_task)
    live_scan = source.index("live_only=True")
    gate_open = source.index("startup_recovery_gate.mark_recovery_done()")
    cold_scan = source.index("exclude_live=True")
    assert live_scan < gate_open < cold_scan


def test_bulk_terminal_marking_uses_bounded_marker_quantum() -> None:
    calls: list[tuple[int, float]] = []

    def write_quantum(entries, summary, budget_ms):
        calls.append((len(entries), budget_ms))
        return len(entries), len(entries), []

    with patch.object(
        run_recovery,
        "_write_terminal_marker_quantum",
        side_effect=write_quantum,
    ):
        marked = asyncio.run(
            run_recovery.mark_recovered_runs_terminal(
                [{"run_id": f"run-{index}"} for index in range(17)],
                "missing session",
            )
        )

    assert marked == 17
    assert calls == [
        (
            run_recovery._TERMINAL_MARKER_QUANTUM_MAX,
            run_recovery._TERMINAL_MARKER_QUANTUM_MS,
        ),
        (1, run_recovery._TERMINAL_MARKER_QUANTUM_MS),
    ]


def test_live_recovery_registers_session_gates_and_sorts_priority() -> None:
    source = inspect.getsource(recovery._recover_in_flight_task)
    register = source.index("register_session_recovery")
    sort = source.index("_sort_recovered_runs_by_session_priority(live)")
    pop = source.index("_pop_next_recovered_session_batch")
    integrate = source.index("await integrate_recovered_runs(_coordinator_ref, batch)")
    mark = source.index("mark_session_recovery_done")
    assert register < sort < pop < integrate < mark


def test_late_priority_is_rechecked_between_live_recovery_batches() -> None:
    source = inspect.getsource(recovery._recover_in_flight_task)
    loop = source.index("while remaining_live:")
    pop = source.index("_pop_next_recovered_session_batch", loop)
    integrate = source.index("await integrate_recovered_runs(_coordinator_ref, batch)", pop)
    assert loop < pop < integrate


def test_recovery_prioritizes_sessions_with_queued_prompts() -> None:
    original = main.session_manager.queued_prompt_count
    main.session_manager.queued_prompt_count = lambda sid: 1 if sid == "queued" else 0
    try:
        ordered = recovery._sort_recovered_runs_by_session_priority([
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
        ordered = recovery._sort_recovered_runs_by_session_priority([
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
    assert provider._run_was_likely_running_before_restart(root, "../escape") is False


def test_cold_recovery_uses_reschedulable_session_pending_set() -> None:
    source = inspect.getsource(recovery._enqueue_recovered_cold_runs)
    assert "_RECOVERED_COLD_PENDING" in source
    assert "_RECOVERED_COLD_READY.set()" in source
    assert "_RECOVERED_COLD_RUN_BATCH_MAX" not in inspect.getsource(main)
    worker = inspect.getsource(recovery._recovered_cold_run_worker)
    assert "_pop_next_recovered_cold_batch_locked()" in worker


def test_selected_session_recovery_has_parallel_fast_lane() -> None:
    source = inspect.getsource(recovery._promote_recovered_session)
    assert "_RECOVERED_COLD_PENDING.pop(app_session_id" in source
    assert "await integrate_recovered_runs(_coordinator_ref, batch)" in source
    ws_source = inspect.getsource(ws_chat.websocket_chat)
    subscribe = ws_source.index('if msg_type == "subscribe":')
    task = ws_source.index("_promote_recovered_session", subscribe)
    assert subscribe < task


def test_ws_subscribe_prioritizes_watched_session_recovery() -> None:
    source = inspect.getsource(ws_chat.websocket_chat)
    subscribe = source.index('if msg_type == "subscribe":')
    priority = source.index("request_session_priority", subscribe)
    register = source.index("_register(sub_sid", subscribe)
    assert subscribe < priority < register


def test_maintenance_metrics_cover_success_error_and_cancel() -> None:
    async def scenario() -> None:
        with perf._lock:
            perf._stats.clear()
            perf._counts.clear()
        assert await recovery._run_maintenance_phase("test_success", lambda: 7) == 7
        assert await recovery._run_maintenance_phase(
            "test_error", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        ) is None

        started = asyncio.Event()

        def block() -> None:
            started_loop.call_soon_threadsafe(started.set)
            release.wait()

        import threading
        release = threading.Event()
        nonlocal_state["release"] = release
        task = asyncio.create_task(recovery._run_maintenance_phase("test_cancel", block))
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

        task = asyncio.create_task(recovery._to_thread_join_on_cancel(blocked))
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
    test_startup_source_orders_recovery_before_maintenance()
    test_startup_orchestrator_failure_releases_recovery_gate()
    test_recovery_gate_opens_after_live_integration_before_background_recovery()
    test_slow_queued_prompt_rehydration_does_not_delay_live_recovery()
    test_prompt_waits_only_for_session_recovery_gate()
    test_provider_start_does_not_repeat_orchestrator_recovery_gate()
    test_provider_recovery_does_not_wrap_scan_in_catalog_lock()
    test_provider_recovery_prioritizes_known_running_scan_buckets()
    test_live_recovery_finishes_before_cold_scan_starts()
    test_bulk_terminal_marking_uses_bounded_marker_quantum()
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
