from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from keyed_lane_executor import KeyedLaneExecutor


def test_same_key_same_lane_is_fifo() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-fifo")
    order: list[str] = []
    try:
        futures = [
            executor.submit("key", order.append, "append-1", lane="a"),
            executor.submit("key", order.append, "append-2", lane="a"),
            executor.submit("key", lambda: order.append("barrier"), lane="a"),
        ]
        for future in futures:
            future.result(timeout=1)
        assert order == ["append-1", "append-2", "barrier"]
    finally:
        executor.shutdown()


def test_unrelated_keys_never_block_each_other() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-keys")
    release = threading.Event()
    started = threading.Event()
    try:
        first = executor.submit("key-a", lambda: (started.set(), release.wait())[1], lane="a")
        assert started.wait(1)
        second = executor.submit("key-b", lambda: "key-b-durable", lane="a")
        assert second.result(timeout=1) == "key-b-durable"
        assert not first.done()
    finally:
        release.set()
        executor.shutdown()


def test_lanes_of_same_key_never_block_each_other() -> None:
    """The read/write split's whole point: a slow lane on one key must
    never delay another lane on that same key."""
    executor = KeyedLaneExecutor(lanes=("read", "write"), thread_name_prefix="test-kle-lanes")
    release = threading.Event()
    started = threading.Event()
    try:
        slow_write = executor.submit(
            "root", lambda: (started.set(), release.wait())[1], lane="write",
        )
        assert started.wait(1)
        fast_read = executor.submit("root", lambda: "read-durable", lane="read")
        assert fast_read.result(timeout=1) == "read-durable"
        assert not slow_write.done()
    finally:
        release.set()
        executor.shutdown()


def test_all_keys_run_fully_concurrently() -> None:
    """No shared pool: every (key, lane) gets its own thread."""
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-concurrent")
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    key_count = 12

    def work() -> None:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait()
        with lock:
            active -= 1

    futures = [
        executor.submit(f"key-{index}", work, lane="a") for index in range(key_count)
    ]
    try:
        deadline = time.monotonic() + 1
        while maximum < key_count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert maximum == key_count
    finally:
        release.set()
        for future in futures:
            future.result(timeout=1)
        executor.shutdown()


def test_unknown_lane_rejected() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-unknown")
    try:
        try:
            executor.submit("key", lambda: None, lane="b")
        except ValueError:
            pass
        else:
            raise AssertionError("expected unknown lane to be rejected")
    finally:
        executor.shutdown()


def test_exception_and_pending_cancellation_advance_key() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-errors")
    release = threading.Event()
    started = threading.Event()
    try:
        def fail() -> None:
            raise ValueError("poison")

        failed = executor.submit("key", fail, lane="a")
        after_failure = executor.submit("key", lambda: "after-failure", lane="a")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as waiter:
            caught = waiter.submit(failed.result)
            try:
                caught.result(timeout=1)
            except ValueError:
                pass
            else:
                raise AssertionError("expected poison failure")
        assert after_failure.result(timeout=1) == "after-failure"

        blocking = executor.submit(
            "key", lambda: (started.set(), release.wait())[1], lane="a",
        )
        assert started.wait(1)
        cancelled = executor.submit("key", lambda: "must-not-run", lane="a")
        after_cancel = executor.submit("key", lambda: "after-cancel", lane="a")
        assert cancelled.cancel()
        release.set()
        blocking.result(timeout=1)
        assert after_cancel.result(timeout=1) == "after-cancel"
        assert cancelled.cancelled()
    finally:
        release.set()
        executor.shutdown()


def test_idle_thread_is_torn_down_and_respawned() -> None:
    executor = KeyedLaneExecutor(
        lanes=("a",), idle_timeout=0.05, thread_name_prefix="test-kle-idle",
    )
    try:
        first = executor.submit("key", lambda: "first", lane="a")
        assert first.result(timeout=1) == "first"
        entry = executor._lanes_by_key[("key", "a")]
        deadline = time.monotonic() + 1
        while entry.worker_alive and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not entry.worker_alive

        second = executor.submit("key", lambda: "second", lane="a")
        assert second.result(timeout=1) == "second"
        assert executor._lanes_by_key[("key", "a")] is entry
    finally:
        executor.shutdown()


def test_shutdown_drains_rejects_and_leaves_no_scheduler_state() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-shutdown")
    release = threading.Event()
    started = threading.Event()
    ran: list[str] = []
    executor.submit(
        "key", lambda: (started.set(), release.wait(), ran.append("first")), lane="a",
    )
    executor.submit("key", ran.append, "tail", lane="a")
    assert started.wait(1)
    # wait=True: the admission gate (`_closed`) must flip promptly even
    # though the full drain+join blocks on `release` for a while yet.
    shutdown_done = threading.Event()
    shutdown = threading.Thread(
        target=lambda: (executor.shutdown(wait=True), shutdown_done.set()),
    )
    shutdown.start()
    deadline = time.monotonic() + 1
    while not executor._closed and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        executor.submit("other", lambda: None, lane="a")
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit raced through closed admission gate")
    assert not shutdown_done.is_set()
    release.set()
    shutdown.join(timeout=1)
    assert shutdown_done.is_set()
    assert ran == ["first", "tail"]
    for entry in executor._lanes_by_key.values():
        assert not entry.worker_alive
        assert not entry.queue


async def _run_executor_adapter_ordering() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-adapter")
    loop = asyncio.get_running_loop()
    order: list[str] = []
    try:
        await asyncio.gather(
            loop.run_in_executor(executor.executor("key", lane="a"), order.append, "turn"),
            loop.run_in_executor(executor.executor("key", lane="a"), order.append, "event"),
            loop.run_in_executor(executor.executor("key", lane="a"), order.append, "finish"),
        )
        assert order == ["turn", "event", "finish"]
    finally:
        executor.shutdown()


def test_executor_adapter_preserves_key_order() -> None:
    asyncio.run(_run_executor_adapter_ordering())


if __name__ == "__main__":
    test_same_key_same_lane_is_fifo()
    test_unrelated_keys_never_block_each_other()
    test_lanes_of_same_key_never_block_each_other()
    test_all_keys_run_fully_concurrently()
    test_unknown_lane_rejected()
    test_exception_and_pending_cancellation_advance_key()
    test_idle_thread_is_torn_down_and_respawned()
    test_shutdown_drains_rejects_and_leaves_no_scheduler_state()
    test_executor_adapter_preserves_key_order()
    print("keyed lane executor tests passed")
