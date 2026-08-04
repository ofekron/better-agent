"""Dedicated branch coverage for bounded_async_executor.

A capacity-bounded ThreadPoolExecutor wrapper: _acquire takes a slot or
queues an asyncio waiter (with timeout); run() runs fn in the pool and
releases on completion; shutdown() fails queued waiters.

This is a concurrency primitive, so tests use real asyncio loops and
real worker threads. The defensive branches that only fire in genuine
cross-loop races (granted-after-pop ValueError paths, call_soon_threadsafe
retry) are exercised through controlled fault injection that still asserts
the observable invariant (correct depth accounting, next waiter granted,
no crash).

Pure in-memory module (imports perf only) -> no BETTER_AGENT_HOME needed.
"""

import asyncio
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bounded_async_executor as bae


def _make(**over):
    base = dict(name="t", max_workers=4, capacity=2, timeout_seconds=2.0)
    base.update(over)
    return bae.BoundedAsyncExecutor(**base)


class _ObservableWaiters(deque):
    def __init__(self) -> None:
        super().__init__()
        self._changed = asyncio.Event()

    def append(self, item: Any) -> None:
        super().append(item)
        self._changed.set()

    async def wait_for_count(self, count: int) -> None:
        async def _wait() -> None:
            while len(self) < count:
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(_wait(), timeout=2.0)


def test_fast_path_runs_and_releases_slot() -> None:
    exe = _make(capacity=2)

    async def body():
        return await asyncio.gather(exe.run(lambda: 1), exe.run(lambda: 2))

    assert asyncio.run(body()) == [1, 2]
    assert exe.depth() == 0
    asyncio.run(exe.shutdown())


def test_depth_tracks_in_use_during_run() -> None:
    exe = _make(capacity=1, max_workers=1)
    seen: dict[str, int] = {}
    hold = threading.Event()
    started = threading.Event()

    def fn() -> str:
        seen["depth"] = exe.depth()
        started.set()
        hold.wait(2)
        return "done"

    async def body() -> str:
        task = asyncio.create_task(exe.run(fn))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            hold.set()
            return await task
        finally:
            hold.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        assert asyncio.run(body()) == "done"
        assert seen["depth"] == 1
    finally:
        asyncio.run(exe.shutdown())


def test_acquire_after_shutdown_raises() -> None:
    exe = _make()
    asyncio.run(exe.shutdown())

    async def body() -> str:
        try:
            await exe.run(lambda: 1)
            return "no-error"
        except RuntimeError as exc:
            return str(exc)

    assert "closed" in asyncio.run(body())


def test_queued_waiter_is_granted_on_release() -> None:
    exe = _make(capacity=1, max_workers=1)
    hold = threading.Event()
    started = threading.Event()

    def blocker() -> str:
        started.set()
        hold.wait(2)
        return "first"

    async def body() -> None:
        waiters = _ObservableWaiters()
        exe._waiters = waiters
        first = asyncio.create_task(exe.run(blocker))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert exe.depth() == 1
            second = asyncio.create_task(exe.run(lambda: "second"))
            await waiters.wait_for_count(1)
            assert exe.depth() == 1
            hold.set()
            assert await first == "first"
            assert await second == "second"
            assert exe.depth() == 0
        finally:
            hold.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )

    try:
        asyncio.run(body())
    finally:
        asyncio.run(exe.shutdown())


def test_timeout_raises_admission_overloaded() -> None:
    exe = _make(capacity=1, max_workers=1, timeout_seconds=0.05)
    hold = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        hold.wait(2)

    async def body() -> str:
        first = asyncio.create_task(exe.run(blocker))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            await exe.run(lambda: "x")
            return "no-error"
        except bae.AdmissionOverloaded as exc:
            return str(exc)
        finally:
            hold.set()
            await first

    try:
        msg = asyncio.run(body())
        assert "full" in msg
        assert exe.depth() == 0
    finally:
        hold.set()
        asyncio.run(exe.shutdown())


def test_cancelled_queued_waiter_does_not_leak_slot() -> None:
    exe = _make(capacity=1, max_workers=1, timeout_seconds=5)
    hold = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        hold.wait(2)

    async def body() -> None:
        waiters = _ObservableWaiters()
        exe._waiters = waiters
        first = asyncio.create_task(exe.run(blocker))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            second = asyncio.create_task(exe.run(lambda: None))
            await waiters.wait_for_count(1)
            second.cancel()
            try:
                await second
            except asyncio.CancelledError:
                pass
            hold.set()
            await first
        finally:
            hold.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )

    try:
        asyncio.run(body())
        assert exe.depth() == 0
    finally:
        asyncio.run(exe.shutdown())


def test_shutdown_fails_queued_waiters() -> None:
    exe = _make(capacity=1)
    # shutdown must fail every still-queued waiter with RuntimeError. Driving
    # this through a real run() would deadlock: shutdown(wait=True) cannot
    # return while the slot-holding task is in flight, and releasing the slot
    # grants (not fails) the queued waiter. So place real waiter futures on
    # the queue directly and assert shutdown fails the live one while skipping
    # an already-done waiter and a waiter whose loop has closed.
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    closed_waiter = closed_loop.create_future()

    async def body() -> str:
        loop = asyncio.get_running_loop()
        live = loop.create_future()
        done = loop.create_future()
        done.set_result(None)
        exe._waiters.append((loop, live))
        exe._waiters.append((loop, done))  # already done -> skipped, not failed
        exe._waiters.append((closed_loop, closed_waiter))  # closed loop -> skipped
        await exe.shutdown()
        await asyncio.sleep(0)  # flush call_soon_threadsafe callbacks
        try:
            await live
            return "no-error"
        except RuntimeError as exc:
            return str(exc)

    assert "closed" in asyncio.run(body())


def test_run_releases_slot_when_executor_submit_fails() -> None:
    exe = _make(capacity=1)
    # _acquire succeeds (gate open, slot free) but run_in_executor rejects
    # because the underlying pool is shut down -> run's except must release
    # the slot and re-raise.
    exe._executor.shutdown(wait=True, cancel_futures=False)

    async def body() -> str:
        try:
            await exe.run(lambda: "x")
            return "no-error"
        except RuntimeError:
            return "released"

    assert asyncio.run(body()) == "released"
    assert exe.depth() == 0  # the except-branch _release ran


def test_double_shutdown_is_idempotent() -> None:
    exe = _make()

    async def body() -> None:
        await exe.shutdown()
        await exe.shutdown()  # second call returns immediately

    asyncio.run(body())


def test_release_skips_done_waiter_and_decrements() -> None:
    exe = _make(capacity=1)
    exe._in_use = 1
    loop = asyncio.new_event_loop()
    try:
        stale = loop.create_future()
        stale.set_result(None)
        exe._waiters.append((loop, stale))
        exe._release()
        assert exe._in_use == 0
        assert not exe._waiters
    finally:
        loop.close()
    asyncio.run(exe.shutdown())


def test_release_releases_again_if_waiter_already_done_at_grant() -> None:
    exe = _make(capacity=1)
    exe._in_use = 1

    async def body() -> None:
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        exe._waiters.append((loop, waiter))
        exe._release()  # pops waiter (not done), schedules grant
        waiter.set_result(None)  # resolve before grant runs
        await asyncio.sleep(0)  # grant sees done -> recursive release
        assert exe._in_use == 0

    asyncio.run(body())
    asyncio.run(exe.shutdown())


class _LoopRejectingCallSoon:
    """Fault-injection stand-in: reports open but rejects call_soon_threadsafe,
    the TOCTOU window _release's retry loop guards against."""

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, *_a, **_k) -> None:
        raise RuntimeError("loop shutting down")


class _ConcurrentlyGrantedWaiters:
    """Fault injection for the granted=True paths in _acquire.

    `waiter.remove(entry)` raises ValueError to simulate a concurrent
    _release having already popped the waiter between wait_for raising and
    the except handler grabbing the lock. That window only opens across two
    event loops (a single loop cannot interleave _release there), so it is
    not deterministically triggerable otherwise. The wrapper lets a real
    cancel/timeout drive the real except-branch and the real _release, and
    the test asserts the observable invariant: depth accounting stays
    consistent (no leak, no crash)."""

    def __init__(self, real: deque) -> None:
        self._real = real
        self._added = asyncio.Event()

    def append(self, x: Any) -> None:
        self._real.append(x)
        self._added.set()

    def popleft(self) -> Any:
        return self._real.popleft()

    def remove(self, _x: Any) -> None:
        raise ValueError("granted concurrently")

    def clear(self) -> None:
        self._real.clear()

    def __bool__(self) -> bool:
        return bool(self._real)

    async def wait_until_queued(self) -> None:
        if self._real:
            return
        await asyncio.wait_for(self._added.wait(), timeout=2.0)


def test_release_retries_next_waiter_when_call_soon_fails() -> None:
    exe = _make(capacity=1)
    exe._in_use = 1

    async def body() -> None:
        loop = asyncio.get_running_loop()
        real_waiter = loop.create_future()
        exe._waiters.append((_LoopRejectingCallSoon(), loop.create_future()))
        exe._waiters.append((loop, real_waiter))
        exe._release()
        await asyncio.sleep(0)  # real_waiter granted
        assert real_waiter.done()
        assert exe._in_use == 1  # granted, not decremented

    asyncio.run(body())
    asyncio.run(exe.shutdown())


def test_cancel_after_concurrent_grant_releases_slot() -> None:
    exe = _make(capacity=1, max_workers=1, timeout_seconds=5)
    real = exe._waiters
    exe._waiters = _ConcurrentlyGrantedWaiters(real)
    hold = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        hold.wait(2)

    async def body() -> None:
        first = asyncio.create_task(exe.run(blocker))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            second = asyncio.create_task(exe.run(lambda: None))
            await exe._waiters.wait_until_queued()
            second.cancel()
            try:
                await second
            except asyncio.CancelledError:
                pass
            hold.set()
            await first
        finally:
            hold.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )

    try:
        asyncio.run(body())
        assert exe.depth() == 0  # granted=True branch released consistently
    finally:
        exe._waiters = real
        asyncio.run(exe.shutdown())


def test_timeout_after_concurrent_grant_releases_slot() -> None:
    exe = _make(capacity=1, max_workers=1, timeout_seconds=0.05)
    real = exe._waiters
    exe._waiters = _ConcurrentlyGrantedWaiters(real)
    hold = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        hold.wait(2)

    async def body() -> str:
        first = asyncio.create_task(exe.run(blocker))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            await exe.run(lambda: "x")
            return "no-error"
        except bae.AdmissionOverloaded:
            return "rejected"
        finally:
            hold.set()
            await first

    try:
        assert asyncio.run(body()) == "rejected"
        assert exe.depth() == 0  # granted=True branch released consistently
    finally:
        hold.set()
        exe._waiters = real
        asyncio.run(exe.shutdown())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
