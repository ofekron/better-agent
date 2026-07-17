from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from event_bus import BusEvent
import event_journal
from event_journal import EventJournalWriter, _KeyedSerialExecutor


def test_unrelated_roots_do_not_head_of_line_block() -> None:
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-hol")
    release = threading.Event()
    started = threading.Event()
    try:
        first = executor.submit("root-a", lambda: (started.set(), release.wait())[1])
        assert started.wait(1)
        second = executor.submit("root-b", lambda: "root-b-durable")
        assert second.result(timeout=1) == "root-b-durable"
        assert not first.done()
    finally:
        release.set()
        executor.shutdown()


def test_same_root_fifo_and_barrier_order() -> None:
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-fifo")
    order: list[str] = []
    try:
        futures = [
            executor.submit("root", order.append, "append-1"),
            executor.submit("root", order.append, "append-2"),
            executor.submit("root", lambda: order.append("barrier")),
        ]
        for future in futures:
            future.result(timeout=1)
        assert order == ["append-1", "append-2", "barrier"]
    finally:
        executor.shutdown()


def test_many_roots_run_fully_concurrently() -> None:
    """No shared pool means every root gets its own thread — a slow
    root can never make another root wait for a worker slot."""
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-concurrent")
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    root_count = 12

    def work() -> None:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait()
        with lock:
            active -= 1

    futures = [executor.submit(f"root-{index}", work) for index in range(root_count)]
    try:
        deadline = time.monotonic() + 1
        while maximum < root_count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert maximum == root_count
    finally:
        release.set()
        for future in futures:
            future.result(timeout=1)
        executor.shutdown()


def test_exception_and_pending_cancellation_advance_root() -> None:
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-errors")
    release = threading.Event()
    started = threading.Event()
    try:
        def fail() -> None:
            raise ValueError("poison")

        failed = executor.submit("root", fail)
        after_failure = executor.submit("root", lambda: "after-failure")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as waiter:
            caught = waiter.submit(failed.result)
            try:
                caught.result(timeout=1)
            except ValueError:
                pass
            else:
                raise AssertionError("expected poison failure")
        assert after_failure.result(timeout=1) == "after-failure"

        blocking = executor.submit("root", lambda: (started.set(), release.wait())[1])
        assert started.wait(1)
        cancelled = executor.submit("root", lambda: "must-not-run")
        after_cancel = executor.submit("root", lambda: "after-cancel")
        assert cancelled.cancel()
        release.set()
        blocking.result(timeout=1)
        assert after_cancel.result(timeout=1) == "after-cancel"
        assert cancelled.cancelled()
    finally:
        release.set()
        executor.shutdown()


def test_idle_root_thread_is_torn_down() -> None:
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-idle")
    try:
        executor.submit("root", lambda: "done").result(timeout=1)
        deadline = time.monotonic() + 1
        while executor.active_roots_count() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert executor.active_roots_count() == 0
        assert not executor._queues
        assert not executor._threads
        assert not executor._adapters
    finally:
        executor.shutdown()


def test_shutdown_drains_rejects_and_leaves_no_scheduler_state() -> None:
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-shutdown")
    release = threading.Event()
    started = threading.Event()
    ran: list[str] = []
    executor.submit("root", lambda: (started.set(), release.wait(), ran.append("first")))
    executor.submit("root", ran.append, "tail")
    assert started.wait(1)
    shutdown_done = threading.Event()
    shutdown = threading.Thread(
        target=lambda: (executor.shutdown(wait=False), shutdown_done.set()),
    )
    shutdown.start()
    deadline = time.monotonic() + 1
    while not executor._closed and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        executor.submit("other", lambda: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit raced through closed admission gate")
    assert not shutdown_done.is_set()
    release.set()
    shutdown.join(timeout=1)
    assert shutdown_done.is_set()
    assert ran == ["first", "tail"]
    assert executor.pending_count() == 0
    assert not executor._queues
    assert not executor._threads
    assert not executor._adapters


async def _run_executor_adapter_ordering() -> None:
    executor = _KeyedSerialExecutor(thread_name_prefix="test-ejw-bus")
    loop = asyncio.get_running_loop()
    order: list[str] = []
    event = BusEvent(type="test", root_id="root", sid="root", payload={})
    try:
        await asyncio.gather(
            loop.run_in_executor(executor.executor(event.root_id), order.append, "turn"),
            loop.run_in_executor(executor.executor(event.root_id), order.append, "event"),
            loop.run_in_executor(executor.executor(event.root_id), order.append, "finish"),
        )
        assert order == ["turn", "event", "finish"]
    finally:
        executor.shutdown()


def test_executor_adapter_preserves_bus_root_order() -> None:
    asyncio.run(_run_executor_adapter_ordering())


def test_barrier_records_enqueue_to_start_wait() -> None:
    writer = EventJournalWriter()
    recorded: list[tuple[str, float]] = []
    original_record = event_journal.perf.record
    original_cursor = event_journal.event_ingester.cursor
    event_journal.perf.record = lambda name, value: recorded.append((name, value))
    event_journal.event_ingester.cursor = lambda root_id: 7
    try:
        assert writer.barrier_sync("root") == 7
    finally:
        event_journal.perf.record = original_record
        event_journal.event_ingester.cursor = original_cursor
        writer.close()
    samples = [value for name, value in recorded if name == "event_journal.barrier.queue_wait"]
    assert len(samples) == 1
    assert samples[0] >= 0


if __name__ == "__main__":
    test_unrelated_roots_do_not_head_of_line_block()
    test_same_root_fifo_and_barrier_order()
    test_many_roots_run_fully_concurrently()
    test_exception_and_pending_cancellation_advance_root()
    test_idle_root_thread_is_torn_down()
    test_shutdown_drains_rejects_and_leaves_no_scheduler_state()
    test_executor_adapter_preserves_bus_root_order()
    test_barrier_records_enqueue_to_start_wait()
    print("event journal keyed executor tests passed")
