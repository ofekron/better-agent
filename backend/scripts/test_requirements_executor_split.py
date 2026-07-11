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
import os
import sys
import time
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _saturate(
    loop: asyncio.AbstractEventLoop,
    executor: ThreadPoolExecutor,
    hold: asyncio.Event,
    count: int,
):
    """Submit blocking tasks that occupy every worker of the given pool."""

    def _blocker() -> str:
        while not hold.is_set():
            time.sleep(0.005)
        return "released"

    return [loop.run_in_executor(executor, _blocker) for _ in range(count)]


def test_processor_and_search_are_distinct_pools() -> None:
    from requirements_query_runner import (
        PROCESSOR_ADMISSION_TIMEOUT_SECONDS,
        PROCESSOR_RESULT_TIMEOUT_SECONDS,
        PROCESSOR_CAPACITY,
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        REQUIREMENTS_SEARCH_EXECUTOR,
    )

    assert PROCESSOR_CAPACITY >= 10
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
        PROCESSOR_CAPACITY,
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        REQUIREMENTS_SEARCH_EXECUTOR,
        run_requirements_query,
    )

    async def _main() -> str:
        loop = asyncio.get_running_loop()
        hold = asyncio.Event()
        blockers = _saturate(loop, REQUIREMENTS_PROCESSOR_EXECUTOR, hold, PROCESSOR_CAPACITY)
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
            await asyncio.gather(*blockers)
        return result

    assert asyncio.run(_main()) == "search-ok"
    print("PASS search completes on its own pool while processor pool is saturated")


def test_processor_admission_times_out_before_executor_queue_growth() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        PROCESSOR_CAPACITY,
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
            if started == PROCESSOR_CAPACITY:
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
            for idx in range(PROCESSOR_CAPACITY)
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
        PROCESSOR_CAPACITY,
        run_requirements_processor_query,
    )

    async def _main() -> bool:
        hold = asyncio.Event()
        started = 0
        started_event = asyncio.Event()

        def _blocker() -> str:
            nonlocal started
            started += 1
            if started == PROCESSOR_CAPACITY:
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
            for idx in range(PROCESSOR_CAPACITY)
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
        PROCESSOR_CAPACITY,
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
            if started == PROCESSOR_CAPACITY:
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
            for idx in range(PROCESSOR_CAPACITY)
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
        PROCESSOR_CAPACITY,
        run_requirements_processor_query,
    )

    async def _main() -> bool:
        hold = asyncio.Event()
        started = 0
        started_event = asyncio.Event()

        def _blocker() -> str:
            nonlocal started
            started += 1
            if started == PROCESSOR_CAPACITY:
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
            for idx in range(PROCESSOR_CAPACITY)
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
        restored = await asyncio.gather(*[
            run_requirements_processor_query(
                f"requirements.processed.processor.after_release.{idx}",
                lambda idx=idx: f"ok-{idx}",
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
            )
            for idx in range(PROCESSOR_CAPACITY)
        ])
        return held_until_done and sorted(restored) == [f"ok-{idx}" for idx in range(PROCESSOR_CAPACITY)]

    assert asyncio.run(_main()) is True
    print("PASS cancelled processor waiter keeps admission held until worker completion")


def test_cancelled_processor_waiter_runs_cancel_callback_after_admission() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        run_requirements_processor_query,
    )

    async def _main() -> bool:
        hold = threading.Event()
        started = threading.Event()
        callback_seen = asyncio.Event()
        callback_calls = 0

        def _blocker() -> str:
            started.set()
            hold.wait(timeout=2)
            return "released"

        async def _on_cancelled() -> None:
            nonlocal callback_calls
            callback_calls += 1
            callback_seen.set()

        task = asyncio.create_task(
            run_requirements_processor_query(
                "requirements.processed.processor.cancel.callback",
                _blocker,
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
                result_timeout_seconds=2.0,
                on_caller_cancelled=_on_cancelled,
            )
        )
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        callback_fired = callback_seen.is_set() and callback_calls == 1
        hold.set()
        restored = await run_requirements_processor_query(
            "requirements.processed.processor.cancel.callback.restored",
            lambda: "ok",
            executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
            admission_timeout_seconds=1.0,
        )
        return callback_fired and restored == "ok"

    assert asyncio.run(_main()) is True
    print("PASS cancelled processor waiter runs cancel callback after admission")


def test_processor_timeouts_do_not_run_cancel_callback() -> None:
    from requirements_query_runner import (
        REQUIREMENTS_PROCESSOR_EXECUTOR,
        PROCESSOR_CAPACITY,
        RequirementsAdmissionTimeout,
        RequirementsProviderTimeout,
        _REQUIREMENTS_PROCESSOR_ADMISSION,
        run_requirements_processor_query,
    )

    async def _main() -> tuple[bool, str]:
        callback_calls = 0

        async def _on_cancelled() -> None:
            nonlocal callback_calls
            callback_calls += 1

        hold = threading.Event()
        started = threading.Event()

        def _slow() -> str:
            started.set()
            hold.wait(timeout=2)
            return "late"

        try:
            await run_requirements_processor_query(
                "requirements.processed.processor.provider.timeout.callback",
                _slow,
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
                result_timeout_seconds=0.01,
                on_caller_cancelled=_on_cancelled,
            )
        except RequirementsProviderTimeout:
            pass
        else:
            return False, "provider timeout did not raise"
        if callback_calls != 0:
            return False, "provider timeout ran cancel callback"
        hold.set()
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        try:
            await run_requirements_processor_query(
                "requirements.processed.processor.provider.timeout.restored",
                lambda: "ok",
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
            )
        except Exception as exc:
            return False, f"provider timeout slot did not restore: {type(exc).__name__}"

        acquired = 0
        for _ in range(PROCESSOR_CAPACITY):
            if not _REQUIREMENTS_PROCESSOR_ADMISSION.acquire(blocking=False):
                break
            acquired += 1
        if acquired != PROCESSOR_CAPACITY:
            for _ in range(acquired):
                _REQUIREMENTS_PROCESSOR_ADMISSION.release()
            return False, "could not saturate admission semaphore"
        try:
            try:
                await run_requirements_processor_query(
                    "requirements.processed.processor.admission.timeout.callback.extra",
                    lambda: "late",
                    executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                    admission_timeout_seconds=0.01,
                    on_caller_cancelled=_on_cancelled,
                )
            except RequirementsAdmissionTimeout:
                pass
            else:
                return False, "admission timeout did not raise"
        finally:
            for _ in range(acquired):
                _REQUIREMENTS_PROCESSOR_ADMISSION.release()
        return callback_calls == 0, "admission timeout ran cancel callback"

    passed, reason = asyncio.run(_main())
    assert passed, reason
    print("PASS processor timeouts do not run cancel callback")


def test_delegation_cancel_marks_and_stops_only_exact_run_dir() -> None:
    old_home = os.environ.get("BETTER_AGENT_HOME")
    old_test_mode = os.environ.get("BETTER_AGENT_TEST_MODE")
    with tempfile.TemporaryDirectory() as home:
        try:
            os.environ["BETTER_AGENT_HOME"] = home
            os.environ["BETTER_AGENT_TEST_MODE"] = "1"
            from delegation_status_store import read_status, write_status
            from provisioning.dispatch import request_delegation_cancel

            run_dir = Path(home) / "runs" / "run-exact"
            run_dir.mkdir(parents=True)
            write_status(
                "delegation-exact",
                status="running",
                provider_run_id="run-exact",
                provider_run_dir=str(run_dir),
            )
            assert request_delegation_cancel("delegation-exact") is True
            exact_status = read_status("delegation-exact") or {}
            assert exact_status.get("cancel_requested") is True
            assert (run_dir / "cancel").exists()

            mismatch_dir = Path(home) / "runs" / "run-other"
            mismatch_dir.mkdir(parents=True)
            write_status(
                "delegation-mismatch",
                status="running",
                provider_run_id="different-run",
                provider_run_dir=str(mismatch_dir),
            )
            assert request_delegation_cancel("delegation-mismatch") is False
            assert not (mismatch_dir / "cancel").exists()

            write_status(
                "delegation-missing-dir",
                status="running",
                provider_id="provider-test",
                provider_run_id="run-missing-dir",
            )
            assert request_delegation_cancel("delegation-missing-dir") is False

            outside_dir = Path(home).parent / f"{Path(home).name}-outside-run"
            outside_dir.mkdir()
            try:
                write_status(
                    "delegation-outside-dir",
                    status="running",
                    provider_id="provider-test",
                    provider_run_id=outside_dir.name,
                    provider_run_dir=str(outside_dir),
                )
                assert request_delegation_cancel("delegation-outside-dir") is False
                assert not (outside_dir / "cancel").exists()
            finally:
                outside_dir.rmdir()
        finally:
            if old_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = old_home
            if old_test_mode is None:
                os.environ.pop("BETTER_AGENT_TEST_MODE", None)
            else:
                os.environ["BETTER_AGENT_TEST_MODE"] = old_test_mode

    print("PASS delegation cancel stops only exact run dir")


def test_shared_bounded_pool_self_deadlocks_under_saturation() -> None:
    """Negative case (the bug): the SAME pattern on one shared 2-worker pool
    deadlocks — proving the split is load-bearing, not decorative. Bounded by a
    short timeout so the suite never hangs."""
    from requirements_query_runner import run_requirements_query

    shared = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shared-test")

    async def _main() -> bool:
        loop = asyncio.get_running_loop()
        hold = asyncio.Event()
        blockers = _saturate(loop, shared, hold, 2)
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
            await asyncio.gather(*blockers)
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
