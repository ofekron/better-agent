from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
import sys
import threading
import time

import pytest

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-hot-path-executor-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_REQUEST_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("request_tag")


async def _run() -> bool:
    import perf
    from hot_path_executor import HotPathExecutor

    recorded: list[tuple[str, float]] = []
    original_record = perf.record
    perf.record = lambda name, value: recorded.append((name, value))

    busy = HotPathExecutor("test-busy", 2)
    idle = HotPathExecutor("test-idle", 2)
    try:
        # Pool isolation: saturating one pool must not delay another.
        release = threading.Event()

        def _block() -> None:
            release.wait(timeout=5.0)

        saturating = [
            asyncio.create_task(busy.run("test.busy.block", _block))
            for _ in range(4)
        ]
        await asyncio.sleep(0.05)
        start = time.perf_counter()
        await idle.run("test.idle.quick", lambda: None)
        idle_elapsed = time.perf_counter() - start
        release.set()
        await asyncio.gather(*saturating)
        if idle_elapsed > 0.5:
            print(f"{FAIL} a saturated pool delayed an unrelated pool: {idle_elapsed:.3f}s")
            return False

        # Each pool runs on its own named threads.
        thread_name = await busy.run("test.busy.thread", lambda: threading.current_thread().name)
        if not thread_name.startswith("test-busy"):
            print(f"{FAIL} work ran on an unexpected thread: {thread_name!r}")
            return False

        # The caller's contextvars must reach the worker thread.
        _REQUEST_TAG.set("req-42")
        seen = await idle.run("test.idle.ctx", _REQUEST_TAG.get)
        if seen != "req-42":
            print(f"{FAIL} contextvars did not propagate into the worker: {seen!r}")
            return False

        # Every call reports queue wait and total duration.
        recorded.clear()
        await idle.run("test.idle.metrics", lambda: None)
        names = [name for name, _ in recorded]
        if "test.idle.metrics.queue_wait" not in names or "test.idle.metrics" not in names:
            print(f"{FAIL} missing queue_wait/duration metrics: {names!r}")
            return False

        # A failing call still reports its duration and propagates.
        recorded.clear()

        def _boom() -> None:
            raise ValueError("boom")

        try:
            await idle.run("test.idle.failure", _boom)
        except ValueError:
            pass
        else:
            print(f"{FAIL} exception from the worker was swallowed")
            return False
        if "test.idle.failure" not in [name for name, _ in recorded]:
            print(f"{FAIL} a failing call must still record its duration: {recorded!r}")
            return False

        print(f"{PASS} hot-path pools isolate, name threads, carry context and record metrics")
        return True
    finally:
        perf.record = original_record
        busy.shutdown()
        idle.shutdown()


def test_name_and_run_return_value() -> None:
    from hot_path_executor import HotPathExecutor

    pool = HotPathExecutor("unit-name", 2)
    try:
        assert pool.name == "unit-name"
        assert asyncio.run(pool.run("unit.add", lambda a, b: a + b, 3, 4)) == 7
    finally:
        pool.shutdown()


def test_run_carries_context_records_metrics_and_propagates_errors() -> None:
    import perf
    from hot_path_executor import HotPathExecutor

    recorded: list[tuple[str, float]] = []
    original_record = perf.record
    perf.record = lambda name, value: recorded.append((name, value))
    tag: contextvars.ContextVar[str] = contextvars.ContextVar("unit-tag")
    pool = HotPathExecutor("unit-ctx", 2)
    try:
        async def scenario() -> None:
            tag.set("req-7")
            seen = await pool.run("unit.see", tag.get)
            assert seen == "req-7"

            recorded.clear()

            def _boom() -> None:
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                await pool.run("unit.boom", _boom)
            names = [name for name, _ in recorded]
            assert "unit.boom.queue_wait" in names, f"missing queue_wait metric: {names}"
            assert "unit.boom" in names, f"a failing call must still record duration: {names}"

        asyncio.run(scenario())
    finally:
        perf.record = original_record
        pool.shutdown()


def test_shutdown_all_drains_every_pool() -> None:
    import hot_path_executor
    from hot_path_executor import HotPathExecutor

    fakes = [HotPathExecutor("drain-a", 1), HotPathExecutor("drain-b", 1)]
    original = hot_path_executor._ALL
    hot_path_executor._ALL = tuple(fakes)
    try:
        hot_path_executor.shutdown_all()
        for pool in fakes:
            with pytest.raises(RuntimeError):
                pool._executor.submit(int)
    finally:
        hot_path_executor._ALL = original


if __name__ == "__main__":
    try:
        ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
