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
        PROCESSOR_ADMISSION_TIMEOUT_SECONDS,
        PROCESSOR_RESULT_TIMEOUT_SECONDS,
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        REQUIREMENTS_SEARCH_EXECUTOR,
    )

    assert PROCESSOR_ADMISSION_TIMEOUT_SECONDS >= 30.0
    assert PROCESSOR_ADMISSION_TIMEOUT_SECONDS < PROCESSOR_RESULT_TIMEOUT_SECONDS
    assert REQUIREMENTS_PROCESSOR_EXECUTOR is not REQUIREMENTS_SEARCH_EXECUTOR
    assert isinstance(REQUIREMENTS_PROCESSOR_EXECUTOR, ThreadPoolExecutor)
    assert isinstance(REQUIREMENTS_SEARCH_EXECUTOR, ThreadPoolExecutor)
    print("PASS processor admission waits are longer and search uses a distinct pool")


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


def test_processor_admission_times_out_before_executor_queue_growth() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        REQUIREMENTS_SEARCH_EXECUTOR,
        run_requirements_processor_query,
        run_requirements_query,
    )

    async def _main() -> bool:
        hold = asyncio.Event()
        started = 0
        started_event = asyncio.Event()

        def _blocker() -> str:
            nonlocal started
            started += 1
            if started == 2:
                started_event.set()
            while not hold.is_set():
                time.sleep(0.005)
            return "released"

        blockers = [
            asyncio.create_task(
                run_requirements_processor_query(
                    f"requirements.processed.processor.blocker.{idx}",
                    _blocker,
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=0.05,
                )
            )
            for idx in range(2)
        ]
        try:
            await asyncio.wait_for(started_event.wait(), timeout=2)
            started_at = time.perf_counter()
            try:
                await run_requirements_processor_query(
                    "requirements.processed.processor",
                    lambda: "late",
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=0.05,
                )
            except TimeoutError:
                timed_out = True
            else:
                timed_out = False
            search = await asyncio.wait_for(
                run_requirements_query(
                    "requirements.search",
                    lambda: "search-ok",
                    executor=REQUIREMENTS_SEARCH_EXECUTOR,
                ),
                timeout=2,
            )
        finally:
            hold.set()
            await asyncio.gather(*blockers)
        return timed_out and search == "search-ok" and time.perf_counter() - started_at < 1

    assert asyncio.run(_main()) is True
    print("PASS saturated processor admission times out quickly without starving search")


def test_default_processor_admission_waits_past_old_one_second_window() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        run_requirements_processor_query,
    )

    async def _main() -> bool:
        hold = asyncio.Event()
        started = 0
        started_event = asyncio.Event()

        def _blocker() -> str:
            nonlocal started
            started += 1
            if started == 2:
                started_event.set()
            while not hold.is_set():
                time.sleep(0.005)
            return "released"

        blockers = [
            asyncio.create_task(
                run_requirements_processor_query(
                    f"requirements.processed.processor.old_window.{idx}",
                    _blocker,
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=0.05,
                )
            )
            for idx in range(2)
        ]

        async def _release_after_old_window() -> None:
            await asyncio.sleep(1.2)
            hold.set()

        releaser: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(started_event.wait(), timeout=2)
            releaser = asyncio.create_task(_release_after_old_window())
            started_at = time.perf_counter()
            result = await asyncio.wait_for(
                run_requirements_processor_query(
                    "requirements.processed.processor.default_wait",
                    lambda: "late-ok",
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                ),
                timeout=5,
            )
            elapsed = time.perf_counter() - started_at
        finally:
            hold.set()
            await asyncio.gather(*blockers)
            if releaser is not None:
                await releaser
        return result == "late-ok" and elapsed >= 1.0

    assert asyncio.run(_main()) is True
    print("PASS default processor admission waits past the old one-second window")


def test_processor_admission_reports_admitted_transition() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        run_requirements_processor_query,
    )

    async def _main() -> bool:
        hold = asyncio.Event()
        started = 0
        started_event = asyncio.Event()
        admitted = asyncio.Event()
        events: list[str] = []

        def _blocker() -> str:
            nonlocal started
            started += 1
            if started == 2:
                started_event.set()
            while not hold.is_set():
                time.sleep(0.005)
            return "released"

        async def _on_admitted() -> None:
            events.append("admitted")
            admitted.set()

        blockers = [
            asyncio.create_task(
                run_requirements_processor_query(
                    f"requirements.processed.processor.admit.blocker.{idx}",
                    _blocker,
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=0.05,
                )
            )
            for idx in range(2)
        ]
        waiter = None
        try:
            await asyncio.wait_for(started_event.wait(), timeout=2)
            waiter = asyncio.create_task(
                run_requirements_processor_query(
                    "requirements.processed.processor.admit.waiter",
                    lambda: "admitted-ok",
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=2.0,
                    on_admitted=_on_admitted,
                )
            )
            await asyncio.sleep(0.05)
            assert not admitted.is_set()
            hold.set()
            result = await asyncio.wait_for(waiter, timeout=2)
            return result == "admitted-ok" and events == ["admitted"]
        finally:
            hold.set()
            await asyncio.gather(*blockers)
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    assert asyncio.run(_main()) is True
    print("PASS processor admission reports the admitted transition")


def test_cancelled_processor_waiter_does_not_release_capacity_early() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        run_requirements_processor_query,
    )

    async def _main() -> bool:
        hold = asyncio.Event()
        started = 0
        started_event = asyncio.Event()

        def _blocker() -> str:
            nonlocal started
            started += 1
            if started == 2:
                started_event.set()
            while not hold.is_set():
                time.sleep(0.005)
            return "released"

        blockers = [
            asyncio.create_task(
                run_requirements_processor_query(
                    f"requirements.processed.processor.cancel.{idx}",
                    _blocker,
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=0.05,
                )
            )
            for idx in range(2)
        ]
        await asyncio.wait_for(started_event.wait(), timeout=2)
        blockers[0].cancel()
        try:
            await blockers[0]
        except asyncio.CancelledError:
            pass
        try:
            await run_requirements_processor_query(
                "requirements.processed.processor.after_cancel",
                lambda: "early",
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=0.05,
            )
        except TimeoutError:
            held_until_done = True
        else:
            held_until_done = False
        hold.set()
        await asyncio.gather(blockers[1])
        restored = await asyncio.gather(
            run_requirements_processor_query(
                "requirements.processed.processor.after_release.1",
                lambda: "ok-1",
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
            ),
            run_requirements_processor_query(
                "requirements.processed.processor.after_release.2",
                lambda: "ok-2",
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
            ),
        )
        return held_until_done and sorted(restored) == ["ok-1", "ok-2"]

    assert asyncio.run(_main()) is True
    print("PASS cancelled processor waiter keeps admission held until worker completion")


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
