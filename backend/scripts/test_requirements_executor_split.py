#!/usr/bin/env python3
"""Behavioral lock for the requirements executor split.

The text tests (test_hot_routes_off_loop, test_event_loop_blocking_regressions)
only pin the *source shape* of the wiring — they pass even if the runtime
deadlock remained, as long as the call sites spell the right names. This test
locks the *behavior*: a saturated processor pool must not starve the search
callback, which is the exact condition whose absence caused the 120s
get-requirements timeouts under >=2 concurrent public calls.

Background: the public /api/internal/get-requirements handler runs a long-lived
fork processor that holds a processor worker while the fork calls back into
/api/internal/get-requirements/search. If both endpoints shared one bounded
pool, saturating it with processors would queue every /search callback behind
them forever -> self-deadlock. The fix is two pools; this test proves both
halves of that argument at runtime.
"""
from __future__ import annotations

import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _saturate(loop: asyncio.AbstractEventLoop, executor: ThreadPoolExecutor, hold: asyncio.Event):
    """Submit two blocking tasks that occupy every worker of a 2-worker pool."""

    def _blocker() -> str:
        while not hold.is_set():
            time.sleep(0.005)
        return "released"

    return loop.run_in_executor(executor, _blocker), loop.run_in_executor(executor, _blocker)


def test_processor_and_search_are_distinct_pools() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        REQUIREMENTS_SEARCH_EXECUTOR,
    )

    assert REQUIREMENTS_PROCESSOR_EXECUTOR is not REQUIREMENTS_SEARCH_EXECUTOR
    assert isinstance(REQUIREMENTS_PROCESSOR_EXECUTOR, ThreadPoolExecutor)
    assert isinstance(REQUIREMENTS_SEARCH_EXECUTOR, ThreadPoolExecutor)
    print("PASS processor and search run on distinct ThreadPoolExecutors")


def test_search_completes_while_processor_pool_saturated() -> None:
    """Positive case (the fix): with split pools, saturating the processor pool
    does not block a /search callback running on the search pool."""
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        REQUIREMENTS_SEARCH_EXECUTOR,
        run_requirements_query,
    )

    async def _main() -> str:
        loop = asyncio.get_running_loop()
        hold = asyncio.Event()
        b1, b2 = _saturate(loop, REQUIREMENTS_PROCESSOR_EXECUTOR, hold)
        try:
            result = await asyncio.wait_for(
                run_requirements_query(
                    "requirements.search",
                    lambda: "search-ok",
                    executor=REQUIREMENTS_SEARCH_EXECUTOR,
                ),
                timeout=5,
            )
        finally:
            hold.set()
            await asyncio.gather(b1, b2)
        return result

    assert asyncio.run(_main()) == "search-ok"
    print("PASS search completes on its own pool while processor pool is saturated")


def test_shared_bounded_pool_self_deadlocks_under_saturation() -> None:
    """Negative case (the bug): the SAME pattern on one shared 2-worker pool
    deadlocks — proving the split is load-bearing, not decorative. Bounded by a
    short timeout so the suite never hangs."""
    from requirements_query_runner import run_requirements_query

    shared = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shared-test")

    async def _main() -> bool:
        loop = asyncio.get_running_loop()
        hold = asyncio.Event()
        b1, b2 = _saturate(loop, shared, hold)
        try:
            await asyncio.wait_for(
                run_requirements_query(
                    "requirements.search", lambda: "search-ok", executor=shared
                ),
                timeout=2,
            )
            deadlocked = False
        except asyncio.TimeoutError:
            deadlocked = True
        finally:
            hold.set()
            await asyncio.gather(b1, b2)
        return deadlocked

    try:
        assert asyncio.run(_main()) is True
    finally:
        shared.shutdown(wait=True, cancel_futures=True)
    print("PASS shared bounded pool self-deadlocks under saturation (split is required)")


def _run() -> int:
    failures: list[str] = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")
                print("FAIL", name, exc)
    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
