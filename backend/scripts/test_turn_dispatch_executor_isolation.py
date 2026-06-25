"""Regression test for turn-dispatch executor isolation.

`turn_manager._drive_cli_run` (and its immediate caller `run_turn`) run
~22 synchronous calls per turn per active session — session record
fetch, provider lookup, capability/context building — through
`asyncio.to_thread`. That call always routes through the process-wide
DEFAULT `ThreadPoolExecutor` (`min(32, cpu_count+4)` workers), which is
also shared by ~600 other `asyncio.to_thread` call sites across the
backend, some slow/unbounded. Under load, a slow caller elsewhere can
occupy enough of the default pool's worker slots to delay turn
dispatch — the same executor-starvation shape already fixed for
`jsonl_tailer.py`'s `_FILE_POLL_EXECUTOR` (see
`test_tailer_cursor_ledger_worker.py`).

Fix: `_TURN_DISPATCH_EXECUTOR` is a dedicated `ThreadPoolExecutor` sized
off `os.cpu_count()`, and `_to_turn_dispatch_thread` routes the hot
per-turn calls through it via `loop.run_in_executor(...)` instead of
the shared default pool — a pure resource-isolation change, not a
behavior change.

Three subtests:

  A. `_to_turn_dispatch_thread` is behavior-identical to
     `asyncio.to_thread`: same return value, exceptions propagate, and
     contextvars set in the caller are visible inside the dispatched
     function (asyncio.to_thread's own contract — a naive
     `run_in_executor` without `contextvars.copy_context()` would
     silently drop this).

  B. The dedicated pool's threads are named distinctly
     (`turn-dispatch`) and separate from the default pool.

  C. Throughput under load: saturate the DEFAULT `asyncio.to_thread`
     pool with slow blocking work sized to its full worker count, then
     prove `_to_turn_dispatch_thread` calls still complete fast — and,
     as a control, that a plain `asyncio.to_thread` call made at the
     same moment IS delayed by the saturation (proving the saturation
     was real, so the isolation result isn't a false pass).

Run with:
    cd backend && .venv/bin/python scripts/test_turn_dispatch_executor_isolation.py
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
import sys
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-turn-dispatch-executor-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import turn_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_PROBE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "turn_dispatch_test_probe", default="unset",
)


# ─── A: behavior parity with asyncio.to_thread ─────────────────────

async def test_a_behavior_parity() -> bool:
    def _echo(x: int) -> int:
        return x * 2

    result = await turn_manager._to_turn_dispatch_thread(_echo, 21)
    result_ok = result == 42

    def _boom() -> None:
        raise ValueError("dispatch-thread-boom")

    raised = False
    try:
        await turn_manager._to_turn_dispatch_thread(_boom)
    except ValueError as exc:
        raised = str(exc) == "dispatch-thread-boom"

    token = _PROBE_VAR.set("caller-set-value")
    try:
        seen_in_thread = await turn_manager._to_turn_dispatch_thread(_PROBE_VAR.get)
    finally:
        _PROBE_VAR.reset(token)
    context_ok = seen_in_thread == "caller-set-value"

    ok = result_ok and raised and context_ok
    print(
        f"{PASS if ok else FAIL} A: return value {'ok' if result_ok else 'WRONG'}, "
        f"exception propagation {'ok' if raised else 'WRONG'}, "
        f"contextvar propagation {'ok' if context_ok else 'WRONG'}"
    )
    return ok


# ─── B: dedicated pool is a distinct, named executor ───────────────

def test_b_dedicated_executor_identity() -> bool:
    executor = turn_manager._TURN_DISPATCH_EXECUTOR
    is_dedicated = (
        executor is not None
        and executor is not turn_manager._STREAM_EVENT_APPLY_EXECUTOR
    )
    prefix_ok = getattr(executor, "_thread_name_prefix", "") == "turn-dispatch"
    sized_off_cpu = executor._max_workers == (os.cpu_count() or 4) * 4

    ok = is_dedicated and prefix_ok and sized_off_cpu
    print(
        f"{PASS if ok else FAIL} B: dedicated executor "
        f"(distinct={is_dedicated}, name_prefix_ok={prefix_ok}, "
        f"sized_off_cpu_count={sized_off_cpu}, "
        f"max_workers={executor._max_workers})"
    )
    return ok


# ─── C: throughput isolated from default-pool saturation ──────────

async def test_c_isolated_from_default_pool_saturation() -> bool:
    default_pool_workers = min(32, (os.cpu_count() or 1) + 4)
    slow_seconds = 1.5
    started = [threading.Event() for _ in range(default_pool_workers)]

    def _slow_default_pool_work(ev: threading.Event) -> None:
        ev.set()
        time.sleep(slow_seconds)

    async def _await_event(ev: threading.Event, timeout: float) -> bool:
        # NOT ev.wait() directly — that's a blocking call and would
        # freeze this coroutine's event loop, starving the very tasks
        # (below) that need the loop to run in order to start.
        deadline = time.monotonic() + timeout
        while not ev.is_set():
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    # Saturate the SHARED default pool used by asyncio.to_thread — the
    # same pool ~600 other call sites in the backend still use.
    saturating_tasks = [
        asyncio.create_task(asyncio.to_thread(_slow_default_pool_work, ev))
        for ev in started
    ]
    for ev in started:
        assert await _await_event(ev, 2.0), "saturating task never started"

    # Control: a plain asyncio.to_thread call made NOW must queue behind
    # the saturating work (proves the saturation is real).
    control_start = time.monotonic()
    control_task = asyncio.create_task(asyncio.to_thread(lambda: "control"))
    await asyncio.sleep(0.05)
    control_delayed = not control_task.done()

    # The isolated dispatch path must NOT be delayed by the same saturation.
    dispatch_start = time.monotonic()
    dispatch_results = await asyncio.gather(*[
        turn_manager._to_turn_dispatch_thread(lambda i=i: i * i)
        for i in range(8)
    ])
    dispatch_elapsed = time.monotonic() - dispatch_start

    await asyncio.gather(*saturating_tasks)
    await control_task
    control_elapsed = time.monotonic() - control_start

    results_ok = dispatch_results == [i * i for i in range(8)]
    fast_ok = dispatch_elapsed < 0.5
    control_ok = control_delayed and control_elapsed >= slow_seconds * 0.9

    ok = results_ok and fast_ok and control_ok
    print(
        f"{PASS if ok else FAIL} C: {default_pool_workers} default-pool "
        f"threads saturated for {slow_seconds}s; dispatch calls "
        f"({'correct results, ' if results_ok else 'WRONG RESULTS, '}"
        f"{dispatch_elapsed:.3f}s) vs control asyncio.to_thread call "
        f"({control_elapsed:.3f}s, delayed={control_delayed}) "
        f"(want dispatch < 0.5s, control >= {slow_seconds * 0.9:.2f}s)"
    )
    return ok


# ─── runner ─────────────────────────────────────────────────────────

async def _run() -> int:
    results = [
        await test_a_behavior_parity(),
        test_b_dedicated_executor_identity(),
        await test_c_isolated_from_default_pool_saturation(),
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
