"""Regression tests for low-priority cold recovered-run integration.

Run with:
    cd backend && PYTHONPATH=. python3 scripts/test_recovered_cold_runs_worker.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-cold-recovery-worker-")

import recovery  # noqa: E402

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


async def main_test() -> int:
    results = [
        await test_cold_runs_integrate_immediately_in_serial_session_batches(),
        await test_promote_recovered_session_claims_pending_batch(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
