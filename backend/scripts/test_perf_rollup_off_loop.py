"""Regression test: perf._rollup_loop's flush() must not block the event loop.

`perf.flush()` snapshots stats under `perf._lock` (fast), then calls
`logger.info("PERF rollup:\\n%s", body)`. Emitting a log record walks
every attached handler and, per the faulthandler evidence that
motivated this fix, can spend seconds inside `logging.Handler.flush()`
-- e.g. a handler doing synchronous disk I/O under memory/swap
pressure or against a large "PERF rollup" body (the depth_lines loop
scales with the number of registered queue gauges). `perf._rollup_loop`
called `flush()` directly (no `asyncio.to_thread`) every `ROLLUP_SECS`
from an asyncio task, so any slow handler flush blocked the ENTIRE
event loop for every concurrent session/websocket in the process.
Live faulthandler dumps showed exactly this stack (perf.py:171 `flush`
-> logging/__init__.py:1135 `flush` -> asyncio/runners.py:128 `run`),
with observed stalls up to 12.9s.

Fix: `_rollup_loop` now calls `await asyncio.to_thread(flush)` instead
of `flush()` directly. `flush()`'s own lock usage is a `threading.Lock`
(thread-safe already) held only for the fast snapshot+clear, released
before the slow logging emission -- safe to run off-thread.

This test proves the loop stays responsive while a rollup's logging
emission is slow, and -- as a control -- that the OLD direct-call
pattern really does freeze the loop for that duration.

Run with:
    cd backend && .venv/bin/python scripts/test_perf_rollup_off_loop.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time

import pytest

pytestmark = pytest.mark.anyio

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import perf  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

SLOW_SECONDS = 0.6


async def _count_ticks_while(coro) -> tuple[int, object]:
    """Run `coro` and, concurrently, a lightweight loop-heartbeat counter.
    Returns (ticks_observed, coro_result)."""
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


def _install_slow_handler(logger, hold_seconds: float):
    """Simulate the observed blocking pattern: a real logging.Handler whose
    emit() takes real wall-clock time (matches the faulthandler stack,
    which showed logging.Handler.flush directly downstream of a log call)."""

    import logging as _logging

    class _SlowHandler(_logging.Handler):
        def emit(self, record):
            time.sleep(hold_seconds)

    h = _SlowHandler()
    logger.addHandler(h)
    return h


async def test_offloaded_flush_does_not_block_loop() -> None:
    handler = _install_slow_handler(perf.logger, SLOW_SECONDS)
    original_level = perf.logger.level
    perf.logger.setLevel("INFO")  # match production: rollup logs at INFO
    try:
        perf.record("regression.probe", 1.0)
        ticks, _ = await _count_ticks_while(asyncio.to_thread(perf.flush))
    finally:
        perf.logger.removeHandler(handler)  # type: ignore[arg-type]
        perf.logger.setLevel(original_level)

    # The property under test is binary — did the loop keep making progress
    # concurrently with the slow flush, or was it synchronously blocked for
    # the whole hold? `test_direct_flush_blocks_loop_control` proves the
    # regressed (blocking) case always yields exactly 0 ticks, so any
    # non-zero count here already distinguishes "offloaded" from "blocking"
    # with no straddling ambiguity. A fixed throughput floor (e.g. ">=10"
    # ticks, assuming near-ideal 20ms-granularity scheduling) instead
    # measures host CPU contention, not this code's behavior, and false-fails
    # under a busy/CPU-capped CI runner even though the loop never blocked.
    ok = ticks >= 1
    print(
        f"{PASS if ok else FAIL} perf.flush() via asyncio.to_thread: loop "
        f"ticks during a {SLOW_SECONDS}s slow-handler flush = {ticks} "
        f"(want >=1, proves loop stayed responsive)"
    )
    assert ok, (
        f"loop froze: {ticks} ticks during off-thread flush "
        f"(want >=1 -- zero ticks means the loop was synchronously blocked)"
    )


async def test_direct_flush_blocks_loop_control() -> None:
    """Control: prove the OLD (direct) call pattern really does freeze
    the loop, so a false pass above isn't hiding a no-op handler."""
    handler = _install_slow_handler(perf.logger, SLOW_SECONDS)
    original_level = perf.logger.level
    perf.logger.setLevel("INFO")
    try:
        perf.record("regression.probe", 1.0)

        async def _direct_flush():
            perf.flush()  # mirrors the pre-fix call site

        ticks, _ = await _count_ticks_while(_direct_flush())
    finally:
        perf.logger.removeHandler(handler)  # type: ignore[arg-type]
        perf.logger.setLevel(original_level)

    ok = ticks == 0
    print(
        f"{PASS if ok else FAIL} control - direct perf.flush(): loop ticks "
        f"during a {SLOW_SECONDS}s slow-handler flush = {ticks} (want ==0, "
        f"proves a slow handler flush is real blocking, not a no-op)"
    )
    assert ok, (
        f"control failed: direct flush let {ticks} ticks through "
        f"(want ==0); slow handler may be a no-op"
    )


def test_rollup_loop_calls_flush_via_to_thread() -> None:
    """Static guard: _rollup_loop must not call flush() directly."""
    src = inspect.getsource(perf._rollup_loop)
    bare_calls = [line for line in src.splitlines() if line.strip() == "flush()"]
    wrapped_calls = [
        line for line in src.splitlines()
        if "asyncio.to_thread(flush)" in line
    ]
    ok = len(bare_calls) == 0 and len(wrapped_calls) == 1
    print(
        f"{PASS if ok else FAIL} perf._rollup_loop routes flush() through "
        f"asyncio.to_thread (bare calls={len(bare_calls)}, "
        f"wrapped calls={len(wrapped_calls)})"
    )
    assert ok, (
        f"_rollup_loop wiring wrong: bare flush() calls={len(bare_calls)}, "
        f"asyncio.to_thread(flush) calls={len(wrapped_calls)} (want 0 bare, 1 wrapped)"
    )


def main() -> int:
    failed = False
    for label, runner in [
        ("offloaded_flush_does_not_block_loop", lambda: asyncio.run(test_offloaded_flush_does_not_block_loop())),
        ("direct_flush_blocks_loop_control", lambda: asyncio.run(test_direct_flush_blocks_loop_control())),
        ("rollup_loop_calls_flush_via_to_thread", lambda: test_rollup_loop_calls_flush_via_to_thread()),
    ]:
        try:
            runner()
        except AssertionError as exc:
            failed = True
            print(f"{FAIL} {label}: {exc}")
        except Exception:
            failed = True
            import traceback
            traceback.print_exc()
    print(f"\n{'ALL PASS' if not failed else 'FAILURES PRESENT'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
