"""Regression tests for low-priority cold recovered-run integration.

Run with:
    cd backend && PYTHONPATH=. python3 scripts/test_recovered_cold_runs_worker.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-cold-recovery-worker-")

import main  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def test_cold_runs_queue_immediately_in_bounded_batches() -> bool:
    original_integrate = main.integrate_recovered_runs
    original_task = main._RECOVERED_COLD_RUN_WORKER_TASK
    batch_max = main._RECOVERED_COLD_RUN_BATCH_MAX
    batches: list[list[str]] = []
    active = 0
    max_active = 0

    async def fake_integrate(_coordinator, batch: list[dict]) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        batches.append([str(item.get("run_id")) for item in batch])
        await asyncio.sleep(0.01)
        active -= 1

    main.integrate_recovered_runs = fake_integrate
    main._RECOVERED_COLD_RUN_WORKER_TASK = None
    while not main._RECOVERED_COLD_RUN_QUEUE.empty():
        main._RECOVERED_COLD_RUN_QUEUE.get_nowait()
        main._RECOVERED_COLD_RUN_QUEUE.task_done()

    recovered = [{"run_id": f"run-{i}"} for i in range(batch_max + 3)]
    try:
        main._enqueue_recovered_cold_runs(recovered)
        worker = main._RECOVERED_COLD_RUN_WORKER_TASK
        if worker is None or worker.done():
            print(f"{FAIL} worker was not started immediately")
            return False
        await asyncio.wait_for(main._RECOVERED_COLD_RUN_QUEUE.join(), timeout=2.0)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    finally:
        main.integrate_recovered_runs = original_integrate
        main._RECOVERED_COLD_RUN_WORKER_TASK = original_task
        while not main._RECOVERED_COLD_RUN_QUEUE.empty():
            main._RECOVERED_COLD_RUN_QUEUE.get_nowait()
            main._RECOVERED_COLD_RUN_QUEUE.task_done()

    ok = (
        batches == [
            [f"run-{i}" for i in range(batch_max)],
            [f"run-{i}" for i in range(batch_max, batch_max + 3)],
        ]
        and max_active == 1
    )
    print(
        f"{PASS if ok else FAIL} cold recovered runs queue immediately in bounded serial batches "
        f"-- batches={batches!r} max_active={max_active}",
    )
    return ok


async def main_test() -> int:
    ok = await test_cold_runs_queue_immediately_in_bounded_batches()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
