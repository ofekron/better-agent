from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from keyed_lane_executor import KeyedLaneExecutor


# ---------------------------------------------------------------------------
# Basic correctness: FIFO ordering, return values, args/kwargs plumbing
# ---------------------------------------------------------------------------

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


def test_fifo_holds_for_long_single_thread_sequence() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-longfifo")
    order: list[int] = []
    try:
        futures = [
            executor.submit("key", order.append, i, lane="a") for i in range(500)
        ]
        for future in futures:
            future.result(timeout=5)
        assert order == list(range(500))
    finally:
        executor.shutdown()


def test_return_value_is_propagated() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-retval")
    try:
        future = executor.submit("key", lambda: 41 + 1, lane="a")
        assert future.result(timeout=1) == 42
    finally:
        executor.shutdown()


def test_positional_and_keyword_args_are_forwarded() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-args")

    def combine(a, b, *, c, d=4):
        return (a, b, c, d)

    try:
        future = executor.submit("key", combine, 1, 2, lane="a", c=3)
        assert future.result(timeout=1) == (1, 2, 3, 4)
        future2 = executor.submit("key", combine, 1, 2, lane="a", c=3, d=99)
        assert future2.result(timeout=1) == (1, 2, 3, 99)
    finally:
        executor.shutdown()


def test_non_string_hashable_keys_are_supported() -> None:
    """Generic utility: keys just need to be hashable, not necessarily str."""
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-keytype")
    try:
        tuple_key_future = executor.submit(("root", 1), lambda: "tuple-key", lane="a")
        int_key_future = executor.submit(42, lambda: "int-key", lane="a")
        assert tuple_key_future.result(timeout=1) == "tuple-key"
        assert int_key_future.result(timeout=1) == "int-key"
    finally:
        executor.shutdown()


# ---------------------------------------------------------------------------
# Isolation: unrelated keys and lanes never block each other
# ---------------------------------------------------------------------------

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


def test_three_lanes_of_same_key_are_fully_independent() -> None:
    executor = KeyedLaneExecutor(
        lanes=("a", "b", "c"), thread_name_prefix="test-kle-3lanes",
    )
    releases = {name: threading.Event() for name in ("a", "b", "c")}
    started = {name: threading.Event() for name in ("a", "b", "c")}
    try:
        blockers = {
            name: executor.submit(
                "root",
                lambda n=name: (started[n].set(), releases[n].wait())[1],
                lane=name,
            )
            for name in ("a", "b", "c")
        }
        for name in ("a", "b", "c"):
            assert started[name].wait(1)
        for name in ("a", "b", "c"):
            assert not blockers[name].done()
    finally:
        for event in releases.values():
            event.set()
        for future in blockers.values():
            future.result(timeout=1)
        executor.shutdown()


def test_fifo_within_lane_is_independent_per_key_interleaved() -> None:
    """Interleaved submits across two keys must not cross-pollinate order."""
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-interleave")
    order_a: list[int] = []
    order_b: list[int] = []
    try:
        futures = []
        for i in range(100):
            futures.append(executor.submit("key-a", order_a.append, i, lane="a"))
            futures.append(executor.submit("key-b", order_b.append, i, lane="a"))
        for future in futures:
            future.result(timeout=5)
        assert order_a == list(range(100))
        assert order_b == list(range(100))
    finally:
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


def test_lanes_of_different_keys_all_run_concurrently() -> None:
    """Cross product of keys x lanes: every pair gets its own thread too."""
    executor = KeyedLaneExecutor(
        lanes=("read", "write"), thread_name_prefix="test-kle-cross",
    )
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    pairs = [(f"key-{i}", lane) for i in range(6) for lane in ("read", "write")]

    def work() -> None:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait()
        with lock:
            active -= 1

    futures = [executor.submit(key, work, lane=lane) for key, lane in pairs]
    try:
        deadline = time.monotonic() + 1
        while maximum < len(pairs) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert maximum == len(pairs)
    finally:
        release.set()
        for future in futures:
            future.result(timeout=1)
        executor.shutdown()


# ---------------------------------------------------------------------------
# Lane validation
# ---------------------------------------------------------------------------

def test_unknown_lane_rejected() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-unknown")
    try:
        try:
            executor.submit("key", lambda: None, lane="b")
        except ValueError as exc:
            assert "b" in str(exc)
        else:
            raise AssertionError("expected unknown lane to be rejected")
    finally:
        executor.shutdown()


def test_unknown_lane_does_not_spawn_a_thread_or_leak_state() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-unknown-noleak")
    try:
        try:
            executor.submit("key", lambda: None, lane="nope")
        except ValueError:
            pass
        assert executor._lanes_by_key == {}
    finally:
        executor.shutdown()


def test_default_lane_used_when_unspecified() -> None:
    executor = KeyedLaneExecutor(thread_name_prefix="test-kle-default")
    try:
        future = executor.submit("key", lambda: "default-lane-ran")
        assert future.result(timeout=1) == "default-lane-ran"
    finally:
        executor.shutdown()


def test_missing_default_lane_raises_when_not_configured() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-nodefault")
    try:
        try:
            executor.submit("key", lambda: None)
        except ValueError:
            pass
        else:
            raise AssertionError("expected missing default lane to raise")
    finally:
        executor.shutdown()


# ---------------------------------------------------------------------------
# Exceptions, cancellation, and BaseException handling
# ---------------------------------------------------------------------------

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


def test_many_consecutive_failures_do_not_kill_the_worker() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-manyfail")

    def fail() -> None:
        raise RuntimeError("boom")

    try:
        futures = [executor.submit("key", fail, lane="a") for _ in range(50)]
        tail = executor.submit("key", lambda: "still-alive", lane="a")
        for future in futures:
            try:
                future.result(timeout=1)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError")
        assert tail.result(timeout=1) == "still-alive"
    finally:
        executor.shutdown()


def test_base_exception_subclass_is_captured_not_propagated_to_worker() -> None:
    class _CustomSignal(BaseException):
        pass

    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-basexc")

    def raise_signal() -> None:
        raise _CustomSignal("stop")

    try:
        signalled = executor.submit("key", raise_signal, lane="a")
        try:
            signalled.result(timeout=1)
        except _CustomSignal:
            pass
        else:
            raise AssertionError("expected _CustomSignal")
        tail = executor.submit("key", lambda: "worker-survived", lane="a")
        assert tail.result(timeout=1) == "worker-survived"
    finally:
        executor.shutdown()


def test_cancel_returns_false_once_task_is_running() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-cancelrun")
    release = threading.Event()
    started = threading.Event()
    try:
        running = executor.submit(
            "key", lambda: (started.set(), release.wait())[1], lane="a",
        )
        assert started.wait(1)
        assert running.cancel() is False
    finally:
        release.set()
        running.result(timeout=1)
        executor.shutdown()


# ---------------------------------------------------------------------------
# Idle teardown and respawn, including the enqueue/teardown race
# ---------------------------------------------------------------------------

def test_idle_thread_is_torn_down_and_respawned() -> None:
    executor = KeyedLaneExecutor(
        lanes=("a",), idle_timeout=0.05, thread_name_prefix="test-kle-idle",
    )
    try:
        first = executor.submit("key", lambda: "first", lane="a")
        assert first.result(timeout=1) == "first"
        entry = executor._lanes_by_key[("key", "a")]
        first_thread = entry.thread
        deadline = time.monotonic() + 1
        while entry.worker_alive and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not entry.worker_alive
        first_thread.join(timeout=1)
        assert not first_thread.is_alive()

        second = executor.submit("key", lambda: "second", lane="a")
        assert second.result(timeout=1) == "second"
        assert executor._lanes_by_key[("key", "a")] is entry
        assert entry.thread is not first_thread
    finally:
        executor.shutdown()


def test_idle_teardown_only_affects_idle_key_not_busy_key() -> None:
    executor = KeyedLaneExecutor(
        lanes=("a",), idle_timeout=0.05, thread_name_prefix="test-kle-idle-mixed",
    )
    release = threading.Event()
    started = threading.Event()
    try:
        busy = executor.submit(
            "busy-key", lambda: (started.set(), release.wait())[1], lane="a",
        )
        assert started.wait(1)
        executor.submit("idle-key", lambda: "idle-done", lane="a").result(timeout=1)
        idle_lane = executor._lanes_by_key[("idle-key", "a")]
        deadline = time.monotonic() + 1
        while idle_lane.worker_alive and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not idle_lane.worker_alive
        # busy key's worker must still be alive and blocked, untouched by
        # the unrelated idle key's teardown.
        assert not busy.done()
        busy_lane = executor._lanes_by_key[("busy-key", "a")]
        assert busy_lane.worker_alive
    finally:
        release.set()
        busy.result(timeout=1)
        executor.shutdown()


def test_stress_idle_respawn_race_no_lost_or_duplicated_work() -> None:
    """Hammer submit() with an idle_timeout so short that a worker can
    decide to die on almost every gap between items -- this is exactly
    the check-then-die vs enqueue-then-maybe-spawn race the Condition
    is meant to close. Every item must run exactly once."""
    executor = KeyedLaneExecutor(
        lanes=("a",), idle_timeout=0.0005, thread_name_prefix="test-kle-stress-idle",
    )
    counter_lock = threading.Lock()
    counts: dict[int, int] = {}
    total = 800
    try:
        futures = []
        for i in range(total):
            def work(i=i):
                with counter_lock:
                    counts[i] = counts.get(i, 0) + 1
            futures.append(executor.submit("key", work, lane="a"))
            if i % 5 == 0:
                time.sleep(0.001)
        for future in futures:
            future.result(timeout=5)
        assert counts == {i: 1 for i in range(total)}
    finally:
        executor.shutdown()


def test_stress_multi_producer_multi_key_multi_lane() -> None:
    """Many producer threads hammering many (key, lane) pairs concurrently
    with a short idle timeout. Every submitted task must resolve exactly
    once and every future must eventually complete."""
    executor = KeyedLaneExecutor(
        lanes=("a", "b"), idle_timeout=0.002, thread_name_prefix="test-kle-stress-multi",
    )
    keys = [f"key-{i}" for i in range(16)]
    lanes = ("a", "b")
    producer_count = 8
    per_producer = 60
    lock = threading.Lock()
    seen: set[tuple[str, str, int, int]] = set()
    errors: list[BaseException] = []

    def producer(producer_id: int) -> None:
        try:
            local_futures = []
            for i in range(per_producer):
                key = keys[(producer_id + i) % len(keys)]
                lane = lanes[i % 2]

                def work(k=key, l=lane, pid=producer_id, idx=i):
                    with lock:
                        seen.add((k, l, pid, idx))
                    return (k, l, pid, idx)

                local_futures.append(executor.submit(key, work, lane=lane))
            for future in local_futures:
                future.result(timeout=10)
        except BaseException as exc:  # noqa: BLE001 - surface to main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=producer, args=(pid,)) for pid in range(producer_count)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive(), "producer thread did not finish in time"
    finally:
        executor.shutdown()
    assert not errors, errors
    assert len(seen) == producer_count * per_producer


# ---------------------------------------------------------------------------
# Shutdown semantics
# ---------------------------------------------------------------------------

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


def test_shutdown_rejects_every_lane_not_just_the_one_with_work() -> None:
    executor = KeyedLaneExecutor(
        lanes=("a", "b"), thread_name_prefix="test-kle-shutdown-lanes",
    )
    executor.submit("key", lambda: "a-ran", lane="a").result(timeout=1)
    executor.shutdown()
    for lane in ("a", "b"):
        try:
            executor.submit("key", lambda: None, lane=lane)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"expected lane {lane!r} to reject after shutdown")


def test_shutdown_with_no_lanes_ever_created_is_a_no_op() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-shutdown-empty")
    executor.shutdown()  # must not raise or hang
    assert executor._closed


def test_shutdown_is_idempotent() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-shutdown-twice")
    executor.submit("key", lambda: "ran", lane="a").result(timeout=1)
    executor.shutdown()
    executor.shutdown()  # must not raise on a second call


def test_shutdown_wait_false_returns_before_slow_work_finishes() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-shutdown-nowait")
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    blocking = executor.submit(
        "key", lambda: (started.set(), release.wait(), finished.set())[2], lane="a",
    )
    try:
        assert started.wait(1)
        started_shutdown_at = time.monotonic()
        executor.shutdown(wait=False)
        shutdown_call_duration = time.monotonic() - started_shutdown_at
        assert shutdown_call_duration < 0.5
        assert not finished.is_set()
    finally:
        release.set()
        blocking.result(timeout=1)


def test_worker_threads_are_daemon_threads() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-daemon")
    try:
        executor.submit("key", lambda: "ran", lane="a").result(timeout=1)
        entry = executor._lanes_by_key[("key", "a")]
        assert entry.thread is not None
        assert entry.thread.daemon is True
    finally:
        executor.shutdown()


def test_thread_names_include_prefix_lane_and_key() -> None:
    executor = KeyedLaneExecutor(
        lanes=("read",), thread_name_prefix="test-kle-naming",
    )
    release = threading.Event()
    started = threading.Event()
    try:
        future = executor.submit(
            "root-42", lambda: (started.set(), release.wait())[1], lane="read",
        )
        assert started.wait(1)
        names = [t.name for t in threading.enumerate()]
        assert any("test-kle-naming" in n and "read" in n and "root-42" in n for n in names), names
    finally:
        release.set()
        future.result(timeout=1)
        executor.shutdown()


# ---------------------------------------------------------------------------
# Executor adapter (concurrent.futures.Executor compatibility)
# ---------------------------------------------------------------------------

def test_executor_adapter_returns_cached_instance_per_key_lane() -> None:
    executor = KeyedLaneExecutor(lanes=("a", "b"), thread_name_prefix="test-kle-adaptercache")
    try:
        first = executor.executor("key", lane="a")
        second = executor.executor("key", lane="a")
        assert first is second
        other_lane = executor.executor("key", lane="b")
        assert other_lane is not first
        other_key = executor.executor("other-key", lane="a")
        assert other_key is not first
    finally:
        executor.shutdown()


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


async def _run_executor_adapter_different_keys_concurrent() -> None:
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-adapter-concur")
    loop = asyncio.get_running_loop()
    release = threading.Event()
    started = threading.Event()
    try:
        blocking = loop.run_in_executor(
            executor.executor("key-a", lane="a"),
            lambda: (started.set(), release.wait())[1],
        )
        await loop.run_in_executor(None, started.wait, 1)
        fast = await loop.run_in_executor(
            executor.executor("key-b", lane="a"), lambda: "fast-result",
        )
        assert fast == "fast-result"
        assert not blocking.done()
    finally:
        release.set()
        await blocking
        executor.shutdown()


def test_executor_adapter_different_keys_do_not_block() -> None:
    asyncio.run(_run_executor_adapter_different_keys_concurrent())


# ---------------------------------------------------------------------------
# Cleanup / no stray state
# ---------------------------------------------------------------------------

def test_no_stray_lane_state_for_keys_never_submitted() -> None:
    executor = KeyedLaneExecutor(lanes=("a", "b"), thread_name_prefix="test-kle-nostray")
    try:
        executor.submit("visited", lambda: "ok", lane="a").result(timeout=1)
        assert ("visited", "a") in executor._lanes_by_key
        assert ("visited", "b") not in executor._lanes_by_key
        assert ("never-visited", "a") not in executor._lanes_by_key
    finally:
        executor.shutdown()


def test_gc_does_not_disturb_pending_work() -> None:
    """Dropping all external references to the executor while work is
    queued must not affect in-flight futures (daemon worker threads keep
    their own strong references via closures/args)."""
    executor = KeyedLaneExecutor(lanes=("a",), thread_name_prefix="test-kle-gc")
    future = executor.submit("key", lambda: "survived-gc", lane="a")
    ref = executor
    del executor
    gc.collect()
    try:
        assert future.result(timeout=1) == "survived-gc"
    finally:
        ref.shutdown()


if __name__ == "__main__":
    test_same_key_same_lane_is_fifo()
    test_fifo_holds_for_long_single_thread_sequence()
    test_return_value_is_propagated()
    test_positional_and_keyword_args_are_forwarded()
    test_non_string_hashable_keys_are_supported()
    test_unrelated_keys_never_block_each_other()
    test_lanes_of_same_key_never_block_each_other()
    test_three_lanes_of_same_key_are_fully_independent()
    test_fifo_within_lane_is_independent_per_key_interleaved()
    test_all_keys_run_fully_concurrently()
    test_lanes_of_different_keys_all_run_concurrently()
    test_unknown_lane_rejected()
    test_unknown_lane_does_not_spawn_a_thread_or_leak_state()
    test_default_lane_used_when_unspecified()
    test_missing_default_lane_raises_when_not_configured()
    test_exception_and_pending_cancellation_advance_key()
    test_many_consecutive_failures_do_not_kill_the_worker()
    test_base_exception_subclass_is_captured_not_propagated_to_worker()
    test_cancel_returns_false_once_task_is_running()
    test_idle_thread_is_torn_down_and_respawned()
    test_idle_teardown_only_affects_idle_key_not_busy_key()
    test_stress_idle_respawn_race_no_lost_or_duplicated_work()
    test_stress_multi_producer_multi_key_multi_lane()
    test_shutdown_drains_rejects_and_leaves_no_scheduler_state()
    test_shutdown_rejects_every_lane_not_just_the_one_with_work()
    test_shutdown_with_no_lanes_ever_created_is_a_no_op()
    test_shutdown_is_idempotent()
    test_shutdown_wait_false_returns_before_slow_work_finishes()
    test_worker_threads_are_daemon_threads()
    test_thread_names_include_prefix_lane_and_key()
    test_executor_adapter_returns_cached_instance_per_key_lane()
    test_executor_adapter_preserves_key_order()
    test_executor_adapter_different_keys_do_not_block()
    test_no_stray_lane_state_for_keys_never_submitted()
    test_gc_does_not_disturb_pending_work()
    print("keyed lane executor tests passed")
