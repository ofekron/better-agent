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

import pytest

pytestmark = pytest.mark.anyio

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


async def test_fire_due_off_loop() -> None:
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
    assert ok, (f"fire_due due() did not run off-loop: ticks={ticks} "
                f"(want >=8) elapsed={elapsed*1000:.0f}ms")


async def test_fire_task_triggers_off_loop() -> None:
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
    assert ok, (f"fire_task_triggers due() did not run off-loop: ticks={ticks} "
                f"(want >=8) elapsed={elapsed*1000:.0f}ms")


async def test_receipt_snapshot_off_loop() -> None:
    sched = Scheduler(coordinator=None)
    rec = {"id": "receipt", "task_id": "task", "kind": "turn_end_once"}
    original_due = task_trigger_store.due
    original_snapshot = task_trigger_store.receipt_task_snapshot
    original_mark = task_trigger_store.mark_fired
    task_trigger_store.due = lambda _now=None: [rec]

    def slow_snapshot(_trigger_id):
        time.sleep(_STALL_S)
        return False, None

    task_trigger_store.receipt_task_snapshot = slow_snapshot
    task_trigger_store.mark_fired = lambda *_args: None
    try:
        ticks, elapsed = await _measure(sched.fire_task_triggers)
    finally:
        task_trigger_store.due = original_due
        task_trigger_store.receipt_task_snapshot = original_snapshot
        task_trigger_store.mark_fired = original_mark
    ok = ticks >= 8
    print(f"{PASS if ok else FAIL} receipt/task snapshot runs off-loop "
          f"(ticks={ticks} elapsed={elapsed*1000:.0f}ms)")
    assert ok, (f"receipt/task snapshot did not run off-loop: ticks={ticks} "
                f"(want >=8) elapsed={elapsed*1000:.0f}ms")


async def _run() -> int:
    runners = [
        ("fire_due off-loop", test_fire_due_off_loop),
        ("fire_task_triggers off-loop", test_fire_task_triggers_off_loop),
        ("receipt snapshot off-loop", test_receipt_snapshot_off_loop),
    ]
    failed = 0
    for label, runner in runners:
        try:
            await runner()
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL} {label}: {exc}")
        except Exception:
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{len(runners) - failed}/{len(runners)} subtests passed")
    return 1 if failed else 0


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
