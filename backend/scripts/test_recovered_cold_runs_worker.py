"""Regression tests for low-priority cold recovered-run integration.

Run with:
    cd backend && PYTHONPATH=. python3 scripts/test_recovered_cold_runs_worker.py
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-cold-recovery-worker-")

import recovery  # noqa: E402
import provider  # noqa: E402
import runs_dir  # noqa: E402
import ws_chat  # noqa: E402
from ingestion_versions import current_ingestion_version  # noqa: E402
from run_recovery import RecoveryIntegrationOutcome  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_cold_state() -> None:
    recovery._RECOVERED_COLD_PENDING.clear()
    recovery._RECOVERED_COLD_ACTIVE.clear()
    failed = getattr(recovery, "_RECOVERED_COLD_FAILED", None)
    if failed is not None:
        failed.clear()
    retry_requested = getattr(
        recovery,
        "_RECOVERED_COLD_RETRY_REQUESTED",
        None,
    )
    if retry_requested is not None:
        retry_requested.clear()
    recovery._RECOVERED_COLD_READY = asyncio.Event()
    recovery._RECOVERED_COLD_LOCK = asyncio.Lock()
    recovery._RECOVERED_COLD_ACCEPTING_PRE_ENQUEUE_DEMAND = True


async def test_cold_runs_integrate_immediately_in_serial_session_batches() -> bool:
    """Cold runs enter the background worker immediately, are batched one
    session at a time, and never integrate two batches concurrently."""
    original_integrate = recovery.integrate_recovered_runs
    original_task = recovery._RECOVERED_COLD_RUN_WORKER_TASK
    batches: list[list[str]] = []
    active = 0
    max_active = 0
    drained = asyncio.Event()
    expected_runs = 5

    async def fake_integrate(
        _coordinator,
        batch: list[dict],
    ) -> dict[str, RecoveryIntegrationOutcome]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        batches.append([str(item.get("run_id")) for item in batch])
        # Yield so a second concurrent batch would overlap and be caught.
        await asyncio.sleep(0)
        active -= 1
        if sum(len(entries) for entries in batches) >= expected_runs:
            drained.set()
        return {
            str(batch[0]["app_session_id"]): RecoveryIntegrationOutcome.SUCCESS,
        }

    recovery.integrate_recovered_runs = fake_integrate
    recovery._RECOVERED_COLD_RUN_WORKER_TASK = None
    _reset_cold_state()

    recovered = (
        [{"run_id": f"a-{i}", "app_session_id": "sid-a"} for i in range(3)]
        + [{"run_id": f"b-{i}", "app_session_id": "sid-b"} for i in range(2)]
    )
    pending_after_enqueue = False
    worker_started = False
    try:
        recovery._enqueue_recovered_cold_runs(recovered)
        worker = recovery._RECOVERED_COLD_RUN_WORKER_TASK
        worker_started = worker is not None and not worker.done()
        pending_after_enqueue = recovery._cold_recovery_integration_pending()
        if not worker_started:
            print(f"{FAIL} worker was not started immediately")
            return False
        await asyncio.wait_for(drained.wait(), timeout=5.0)
        pending_drained = not recovery._RECOVERED_COLD_PENDING
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    finally:
        recovery.integrate_recovered_runs = original_integrate
        recovery._RECOVERED_COLD_RUN_WORKER_TASK = original_task
        _reset_cold_state()

    ok = (
        sorted(sorted(entries) for entries in batches) == [
            ["a-0", "a-1", "a-2"],
            ["b-0", "b-1"],
        ]
        and max_active == 1
        and pending_after_enqueue
        and pending_drained
    )
    print(
        f"{PASS if ok else FAIL} cold recovered runs integrate immediately in "
        f"serial per-session batches "
        f"-- batches={batches!r} max_active={max_active} "
        f"pending_after_enqueue={pending_after_enqueue} "
        f"pending_drained={pending_drained}",
    )
    return ok


async def test_ws_demand_before_enqueue_preserves_one_retry() -> None:
    import startup_recovery_gate

    original_integrate = recovery.integrate_recovered_runs
    original_mark_done = startup_recovery_gate.mark_session_recovery_done
    original_task = recovery._RECOVERED_COLD_RUN_WORKER_TASK
    attempts = 0
    ready = asyncio.Event()
    batch = [{"run_id": "demand-0", "app_session_id": "sid-demand"}]

    async def fail_then_succeed(
        _coordinator,
        _batch: list[dict],
    ) -> dict[str, RecoveryIntegrationOutcome]:
        nonlocal attempts
        attempts += 1
        return {
            "sid-demand": (
                RecoveryIntegrationOutcome.SUCCESS
                if attempts > 1
                else RecoveryIntegrationOutcome.RETRYABLE
            ),
        }

    def mark_done(session_id: str) -> None:
        original_mark_done(session_id)
        if session_id == "sid-demand":
            ready.set()

    recovery.integrate_recovered_runs = fail_then_succeed
    startup_recovery_gate.mark_session_recovery_done = mark_done
    recovery._RECOVERED_COLD_RUN_WORKER_TASK = None
    _reset_cold_state()
    startup_recovery_gate.reset_for_tests()
    startup_recovery_gate.begin_recovery()
    startup_recovery_gate.register_session_recovery({"sid-demand"})
    try:
        await ws_chat._request_subscribed_session_recovery("sid-demand")
        assert attempts == 0
        assert "sid-demand" in recovery._RECOVERED_COLD_RETRY_REQUESTED
        recovery._enqueue_recovered_cold_runs(batch)
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        assert attempts == 2
        assert "sid-demand" not in recovery._RECOVERED_COLD_PENDING
        assert "sid-demand" not in recovery._RECOVERED_COLD_FAILED
    finally:
        worker = recovery._RECOVERED_COLD_RUN_WORKER_TASK
        if worker is not None and worker is not original_task:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        recovery.integrate_recovered_runs = original_integrate
        startup_recovery_gate.mark_session_recovery_done = original_mark_done
        recovery._RECOVERED_COLD_RUN_WORKER_TASK = original_task
        startup_recovery_gate.reset_for_tests()
        _reset_cold_state()


async def test_demand_during_active_failure_schedules_one_retry() -> None:
    import startup_recovery_gate

    original_integrate = recovery.integrate_recovered_runs
    original_task = recovery._RECOVERED_COLD_RUN_WORKER_TASK
    attempts = 0
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()
    retry_completed = asyncio.Event()
    batch = [{"run_id": "active-0", "app_session_id": "sid-active"}]

    async def fail_then_succeed(
        _coordinator,
        _batch: list[dict],
    ) -> dict[str, RecoveryIntegrationOutcome]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
            return {"sid-active": RecoveryIntegrationOutcome.RETRYABLE}
        retry_completed.set()
        return {"sid-active": RecoveryIntegrationOutcome.SUCCESS}

    recovery.integrate_recovered_runs = fail_then_succeed
    recovery._RECOVERED_COLD_RUN_WORKER_TASK = None
    _reset_cold_state()
    startup_recovery_gate.reset_for_tests()
    startup_recovery_gate.begin_recovery()
    startup_recovery_gate.register_session_recovery({"sid-active"})
    try:
        recovery._enqueue_recovered_cold_runs(batch)
        await asyncio.wait_for(first_attempt_started.wait(), timeout=5.0)
        await ws_chat._request_subscribed_session_recovery("sid-active")
        assert "sid-active" in recovery._RECOVERED_COLD_RETRY_REQUESTED
        release_first_attempt.set()
        await startup_recovery_gate.wait_for_session_recovery_ready(
            "sid-active",
            timeout=5.0,
        )
        assert retry_completed.is_set()
        assert attempts == 2
        assert "sid-active" not in recovery._RECOVERED_COLD_PENDING
        assert "sid-active" not in recovery._RECOVERED_COLD_FAILED
        assert "sid-active" not in recovery._RECOVERED_COLD_RETRY_REQUESTED
    finally:
        worker = recovery._RECOVERED_COLD_RUN_WORKER_TASK
        if worker is not None and worker is not original_task:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        recovery.integrate_recovered_runs = original_integrate
        recovery._RECOVERED_COLD_RUN_WORKER_TASK = original_task
        startup_recovery_gate.reset_for_tests()
        _reset_cold_state()


async def test_sessionless_terminal_runs_are_reconciled_before_enqueue() -> bool:
    original_existing = recovery._existing_session_ids_async
    original_mark = recovery.mark_recovered_runs_terminal
    marked: list[dict] = []

    async def fake_existing(session_ids: set[str]) -> set[str]:
        return {
            session_id
            for session_id in session_ids
            if not session_id.startswith("missing-")
        }

    async def fake_mark(descriptors: list[dict], reason: str) -> int:
        assert reason == "missing session"
        marked.extend(descriptors)
        return len(descriptors)

    recovery._existing_session_ids_async = fake_existing
    recovery.mark_recovered_runs_terminal = fake_mark
    terminal = {"run_id": "terminal", "has_complete_json": True}
    cancelled = {"run_id": "cancelled", "cancelled": True}
    claimed = {"run_id": "claimed", "has_complete_json": True}
    missing_claimed = {"run_id": "missing-claimed", "has_complete_json": True}
    ambiguous = {"run_id": "ambiguous", "has_complete_json": True}
    conflicting = {
        "run_id": "conflicting",
        "persist_to": "missing-deleted",
        "has_complete_json": True,
    }
    unknown = {"run_id": "unknown"}
    routable = {
        "run_id": "routable",
        "app_session_id": "sid-a",
        "has_complete_json": True,
    }
    try:
        remaining = await recovery._reconcile_missing_session_runs(
            [
                terminal,
                cancelled,
                claimed,
                missing_claimed,
                ambiguous,
                conflicting,
                unknown,
                routable,
            ],
            ownership_documents=[
                {"run_id": "claimed", "persist_to": "sid-a"},
                {
                    "run_id": "missing-claimed",
                    "persist_to": "missing-session",
                },
                {"run_id": "ambiguous", "persist_to": "sid-a"},
                {"run_id": "ambiguous", "persist_to": "sid-b"},
                {"run_id": "conflicting", "persist_to": "sid-a"},
            ],
        )
        unsafe = {
            "run_id": "unsafe",
            "persist_to": "missing-unsafe",
            "has_complete_json": True,
        }
        unsafe_remaining = await recovery._reconcile_missing_session_runs(
            [unsafe],
            ownership_safe=False,
        )
    finally:
        recovery._existing_session_ids_async = original_existing
        recovery.mark_recovered_runs_terminal = original_mark

    ok = (
        marked == [terminal, cancelled, missing_claimed]
        and remaining == [claimed, ambiguous, conflicting, unknown, routable]
        and unsafe_remaining == [unsafe]
    )
    print(
        f"{PASS if ok else FAIL} sessionless terminal runs reconcile before "
        f"cold enqueue -- marked={[item['run_id'] for item in marked]!r} "
        f"remaining={[item['run_id'] for item in remaining]!r}",
    )
    return ok


async def test_sessionless_terminal_run_writes_marker_and_index() -> bool:
    root = runs_dir.runs_root()
    run_id = "terminal-marker"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "complete.json").write_text("{}", encoding="utf-8")
    descriptor = {
        "run_id": run_id,
        "provider_kind": "claude",
        "has_complete_json": True,
        "ingestion_version": current_ingestion_version("claude"),
    }

    remaining = await recovery._reconcile_missing_session_runs([descriptor])
    marker = json.loads(
        (run_dir / "reconciled.marker").read_text(encoding="utf-8"),
    )
    indexed = runs_dir.load_reconciled_marker_index(root)
    with mock.patch.object(provider, "default_provider", side_effect=AssertionError):
        second_scan = provider.recover_all_in_flight()
    ok = (
        remaining == []
        and marker["provider_kind"] == "claude"
        and indexed[run_id]["ingestion_version"]
        == current_ingestion_version("claude")
        and second_scan == []
    )
    print(
        f"{PASS if ok else FAIL} sessionless terminal run writes marker and "
        f"index -- remaining={remaining!r} indexed={run_id in indexed} "
        f"second_scan={second_scan!r}",
    )
    return ok


async def main_test() -> int:
    results = [
        await test_cold_runs_integrate_immediately_in_serial_session_batches(),
        await test_ws_demand_before_enqueue_preserves_one_retry(),
        await test_demand_during_active_failure_schedules_one_retry(),
        await test_sessionless_terminal_runs_are_reconciled_before_enqueue(),
        await test_sessionless_terminal_run_writes_marker_and_index(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
