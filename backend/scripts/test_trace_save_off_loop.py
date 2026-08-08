"""Regression test: trace.save() must not block the event loop.

`trace_collector.TraceCollector.save()` performs synchronous file I/O:
`Path.mkdir()`, an exclusive `fcntl.flock` on `traces_index.lock`
(`portable_lock.lock_ex`, which blocks the calling thread until the
lock is acquired), a file open/write, then unlock. `turn_manager.py`
called `trace.save()` directly (no `asyncio.to_thread`/executor wrap)
at 4 call sites inside `run_turn`'s async control flow, so any
contention on `traces_index.lock` (e.g. two turns finishing near-
simultaneously across different sessions, or slow disk I/O under
memory/swap pressure) blocked the entire asyncio event loop for the
duration of the lock wait -- freezing every concurrent session's
websocket/request in the process. This matches the "lag-watchdog:
blocking stack candidate" faulthandler dumps showing the event-loop
thread parked at `session_manager.py:3727` (`with self._lock_for_root`)
directly downstream of `trace_collector.py:331` (`save`) in the same
stall window.

Fix: `turn_manager.py` now calls
`await _to_turn_dispatch_thread(trace.save)` instead of `trace.save()`,
routing the blocking I/O through the dedicated turn-dispatch thread
pool (same mechanism already used for other per-turn blocking calls;
see `test_turn_dispatch_executor_isolation.py`).

This test proves the loop stays responsive while `trace.save()` is
blocked waiting on a contended lock, and -- as a control -- that a
direct (unwrapped) call to the same blocking `save()` DOES freeze the
loop, so the test is not a false pass.

Run with:
    cd backend && .venv/bin/python scripts/test_trace_save_off_loop.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

import asyncio
import os
import sys
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-trace-save-off-loop-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import portable_lock  # noqa: E402
import trace_collector  # noqa: E402
import turn_manager  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

HOLD_SECONDS = 0.6


def _hold_index_lock_in_background(hold_seconds: float) -> threading.Thread:
    """Simulate lock contention: hold traces_index.lock's flock from another
    thread, exactly as a concurrent turn's trace.save() would."""
    lock_path = ba_home() / "traces_index.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ready = threading.Event()

    def _hold():
        handle = lock_path.open("a+b")
        portable_lock.lock_ex(handle.fileno())
        ready.set()
        time.sleep(hold_seconds)
        portable_lock.unlock(handle.fileno())
        handle.close()

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    ready.wait(timeout=5)
    return t


async def _count_ticks_while(coro) -> tuple[int, object]:
    """Run `coro` and, concurrently, a lightweight loop-heartbeat counter.
    Returns (ticks_observed, coro_result) -- ticks_observed stays near 0
    if the loop was blocked for the duration."""
    ticks = 0
    stop = False

    async def _heartbeat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.02)
            ticks += 1

    hb_task = asyncio.create_task(_heartbeat())
    result = await coro
    stop = True
    await hb_task
    return ticks, result


async def test_wrapped_save_does_not_block_loop() -> None:
    holder = _hold_index_lock_in_background(HOLD_SECONDS)
    trace = trace_collector.TraceCollector("sess-wrapped", "hello")
    trace.finalize()

    ticks, _ = await _count_ticks_while(
        turn_manager._to_turn_dispatch_thread(trace.save)
    )
    holder.join(timeout=5)

    # At ~20ms per tick over a ~600ms block, an unblocked loop should
    # accumulate double digits of ticks; a blocked loop accumulates ~0.
    ok = ticks >= 10
    print(
        f"{PASS if ok else FAIL} wrapped trace.save() via "
        f"_to_turn_dispatch_thread: loop ticks during {HOLD_SECONDS}s "
        f"lock hold = {ticks} (want >=10, proves loop stayed responsive)"
    )
    assert ok, (f"wrapped trace.save() blocked the loop: ticks={ticks} "
                f"during {HOLD_SECONDS}s lock hold (want >=10)")


async def test_unwrapped_save_blocks_loop_control() -> None:
    """Control: prove the OLD (unwrapped) call pattern really does freeze
    the loop, so a false pass above isn't hiding a no-op lock."""
    holder = _hold_index_lock_in_background(HOLD_SECONDS)
    trace = trace_collector.TraceCollector("sess-unwrapped", "hello")
    trace.finalize()

    async def _direct_save():
        # Mirrors the pre-fix call site: synchronous call directly in
        # async context, no thread offload.
        trace.save()

    ticks, _ = await _count_ticks_while(_direct_save())
    holder.join(timeout=5)

    ok = ticks == 0
    print(
        f"{PASS if ok else FAIL} control - unwrapped trace.save(): loop "
        f"ticks during {HOLD_SECONDS}s lock hold = {ticks} (want ==0, "
        f"proves the lock contention is real and would have blocked "
        f"the loop pre-fix)"
    )
    assert ok, (f"unwrapped trace.save() did not block the loop as expected: "
                f"ticks={ticks} during {HOLD_SECONDS}s lock hold (want ==0)")


async def test_turn_manager_call_sites_use_dispatch_thread() -> None:
    """Static guard: every `trace.save()` invocation inside turn_manager.py
    must be routed through `_to_turn_dispatch_thread`, not called bare."""
    import inspect
    src = inspect.getsource(turn_manager)
    bare_calls = [
        line for line in src.splitlines()
        if line.strip() == "trace.save()"
    ]
    ok = len(bare_calls) == 0
    print(
        f"{PASS if ok else FAIL} turn_manager.py has no bare `trace.save()` "
        f"call sites left (found {len(bare_calls)})"
    )
    assert ok, (f"turn_manager.py has bare `trace.save()` call sites left "
                f"(found {len(bare_calls)}, want 0)")


def main() -> int:
    runners = [
        ("wrapped save off-loop", test_wrapped_save_does_not_block_loop),
        ("unwrapped save control", test_unwrapped_save_blocks_loop_control),
        ("call-sites static guard", test_turn_manager_call_sites_use_dispatch_thread),
    ]
    failed = 0
    for label, runner in runners:
        try:
            asyncio.run(runner())
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL} {label}: {exc}")
        except Exception:
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'ALL PASS' if not failed else 'FAILURES PRESENT'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
