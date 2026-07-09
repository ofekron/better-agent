"""Measured regression: Scheduler.fire_due / fire_task_triggers must run
the store `due()` reads OFF the event loop.

RCA (lag-watchdog main-loop blocks): the scheduler ticker runs on the
main asyncio loop every 10s and called `schedule_store.due(now)` /
`task_trigger_store.due(now)` synchronously. Each `due()` does a
synchronous `path.stat()` + `read_text()` + `json.loads()` under the
store lock. Under disk contention the `stat()` itself stalled for
seconds — faulthandler pinned blocks at `schedule_store._read` (4.5s)
and `task_trigger_store._fingerprint` -> `pathlib.stat` (5.3s) —
freezing every concurrent WS/request/session in the process.

Fix: wrap both `due()` calls in `asyncio.to_thread` so the synchronous
stat/read runs on a worker thread; the loop stays responsive and the
firing loop (submit_prompt, the serialized per-session funnel) still
runs on-loop in original order.

This test monkeypatches `due()` to a slow synchronous function and runs
a concurrent asyncio ticker. Before the fix the sync `due()` blocked the
loop and the ticker starved; after, `due()` runs off-loop and the ticker
keeps advancing. Measured before/after bound, not vibes.

Run with:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_scheduler_reads_off_loop.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sched-offloop-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from scheduler import Scheduler  # noqa: E402
from stores import schedule_store  # noqa: E402
from stores import task_trigger_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

# Simulate a contended stat/read. Long enough that a synchronous call
# starves a 20ms ticker for the whole window, short enough to keep the
# test fast.
_STALL_S = 0.30


def _slow_due(_now=None):
    time.sleep(_STALL_S)
    return []


async def _measure(coro_factory) -> tuple[int, float]:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        deadline = time.perf_counter() + 0.25
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.02)
            ticks += 1

    start = time.perf_counter()
    tick_task = asyncio.create_task(ticker())
    await coro_factory()
    await tick_task
    return ticks, time.perf_counter() - start


async def test_fire_due_off_loop() -> bool:
    sched = Scheduler(coordinator=None)
    orig = schedule_store.due
    schedule_store.due = _slow_due  # type: ignore[assignment]
    try:
        ticks, elapsed = await _measure(sched.fire_due)
    finally:
        schedule_store.due = orig  # type: ignore[assignment]
    # Before fix: sync due() blocks the loop ~_STALL_S -> ticker gets
    # ~0 ticks during the stall (only ticks after, in its 0.25s window).
    # After: due() off-loop -> loop free -> ticker advances throughout.
    ok = ticks >= 8
    print(f"{PASS if ok else FAIL} fire_due due() runs off-loop "
          f"(ticks={ticks} elapsed={elapsed*1000:.0f}ms)")
    return ok


async def test_fire_task_triggers_off_loop() -> bool:
    sched = Scheduler(coordinator=None)
    orig = task_trigger_store.due
    task_trigger_store.due = _slow_due  # type: ignore[assignment]
    try:
        ticks, elapsed = await _measure(sched.fire_task_triggers)
    finally:
        task_trigger_store.due = orig  # type: ignore[assignment]
    ok = ticks >= 8
    print(f"{PASS if ok else FAIL} fire_task_triggers due() runs off-loop "
          f"(ticks={ticks} elapsed={elapsed*1000:.0f}ms)")
    return ok


async def _run() -> int:
    results = [
        await test_fire_due_off_loop(),
        await test_fire_task_triggers_off_loop(),
    ]
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} subtests passed")
    return 0 if passed == total else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
