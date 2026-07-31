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
from ingestion_versions import current_ingestion_version  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_cold_state() -> None:
    recovery._RECOVERED_COLD_PENDING.clear()
    recovery._RECOVERED_COLD_ACTIVE.clear()
    recovery._RECOVERED_COLD_READY.clear()


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

    async def fake_integrate(_coordinator, batch: list[dict]) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        batches.append([str(item.get("run_id")) for item in batch])
        # Yield so a second concurrent batch would overlap and be caught.
        await asyncio.sleep(0)
        active -= 1
        if sum(len(entries) for entries in batches) >= expected_runs:
            drained.set()

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


async def test_promote_recovered_session_claims_pending_batch() -> bool:
    """A watched session's cold batch leaves the shared pending map and is
    marked active, so the background worker cannot integrate it twice."""
    original_integrate = recovery.integrate_recovered_runs
    original_task = recovery._RECOVERED_COLD_RUN_WORKER_TASK
    integrated: list[list[str]] = []

    async def fake_integrate(_coordinator, batch: list[dict]) -> None:
        integrated.append([str(item.get("run_id")) for item in batch])

    recovery.integrate_recovered_runs = fake_integrate
    # No worker: _promote_recovered_session must do the integration itself.
    recovery._RECOVERED_COLD_RUN_WORKER_TASK = None
    _reset_cold_state()
    recovery._RECOVERED_COLD_PENDING["sid-a"] = [
        {"run_id": "a-0", "app_session_id": "sid-a"},
    ]
    recovery._RECOVERED_COLD_PENDING["sid-b"] = [
        {"run_id": "b-0", "app_session_id": "sid-b"},
    ]
    try:
        await recovery._promote_recovered_session("sid-a")
        claimed = "sid-a" not in recovery._RECOVERED_COLD_PENDING
        untouched = recovery._RECOVERED_COLD_PENDING.get("sid-b") is not None
    finally:
        recovery.integrate_recovered_runs = original_integrate
        recovery._RECOVERED_COLD_RUN_WORKER_TASK = original_task
        _reset_cold_state()

    ok = integrated == [["a-0"]] and claimed and untouched
    print(
        f"{PASS if ok else FAIL} promoting a watched session claims only its "
        f"own pending cold batch -- integrated={integrated!r} "
        f"claimed={claimed} other_session_untouched={untouched}",
    )
    return ok


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
        await test_promote_recovered_session_claims_pending_batch(),
        await test_sessionless_terminal_runs_are_reconciled_before_enqueue(),
        await test_sessionless_terminal_run_writes_marker_and_index(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
