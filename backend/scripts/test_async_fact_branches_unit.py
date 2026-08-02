"""Branch coverage for async_fact: the wait() race + no-timeout paths and the
three _wake branches (closed loop, same-loop synchronous set, stopped-loop
RuntimeError). White-box where a branch is only reachable via a documented
race or a loop-lifecycle edge; each test asserts a distinct behavioral
outcome, never just executes a line.

Run: env -u PYTHONPATH .venv/bin/python -m pytest scripts/test_async_fact_branches_unit.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_fact import AsyncFact, logger  # noqa: E402


def _loop_in_thread():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _main():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=_main, daemon=True)
    t.start()
    ready.wait()
    return loop, t


def _stop(loop, thread):
    if not loop.is_closed():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
    thread.join(timeout=5)
    if not loop.is_closed():
        loop.close()


def _wait_for(predicate, *, attempts=200, delay=0.01):
    for _ in range(attempts):
        if predicate():
            return True
        threading.Event().wait(delay)
    return predicate()


class _DebugCapture:
    """Collect this run's DEBUG records from async_fact.logger."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []
        self._handler = logging.Handler()
        self._handler.emit = self.records.append

    def __enter__(self):
        self._old_level = logger.level
        self._old_propagate = logger.propagate
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self._handler)
        # Avoid duplicate records through the root logger.
        logger.propagate = False
        return self

    def __exit__(self, *exc):
        logger.removeHandler(self._handler)
        logger.setLevel(self._old_level)
        logger.propagate = self._old_propagate
        return False

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def test_wait_no_timeout_path_wakes_on_set() -> None:
    """timeout is None -> `await event.wait()` happy path (not wait_for)."""
    fact = AsyncFact()
    loop, t = _loop_in_thread()
    try:
        fut = asyncio.run_coroutine_threadsafe(fact.wait(), loop)
        assert _wait_for(lambda: fact.waiter_count() == 1)
        setter = threading.Thread(target=fact.set)
        setter.start()
        setter.join(timeout=5)
        assert fut.result(timeout=5) is True
    finally:
        _stop(loop, t)


def test_wait_race_second_check_short_circuits() -> None:
    """A set() that lands between the unlocked read and the locked read is
    caught by the second `if self._set` guard: wait() returns True without
    registering a waiter. Models the TOCTOU window the guard exists for."""
    fact = AsyncFact()
    real_lock = fact._lock
    flipped = {"done": False}

    class _RaceLock:
        def __enter__(self):
            real_lock.acquire()
            if not flipped["done"]:
                flipped["done"] = True
                # Another thread set the fact while we waited for the lock.
                fact._set = True

        def __exit__(self, *exc):
            real_lock.release()
            return False

    fact._lock = _RaceLock()

    async def scenario():
        return await fact.wait(timeout=0.5)

    # The first lock acquire happens inside wait() at its `with self._lock`
    # line, so the flip lands between the unlocked read and the locked read.
    assert asyncio.run(scenario()) is True
    # Returned at the second check, never registered a waiter.
    assert fact.waiter_count() == 0
    assert fact.is_set() is True


def test_wake_closed_loop_returns_silently() -> None:
    """A waiter whose loop was closed before set() must not raise, and must
    return at the is_closed() guard WITHOUT logging the stopped-loop debug
    message (that message belongs to the RuntimeError backstop below)."""
    fact = AsyncFact()
    loop, t = _loop_in_thread()
    fut = asyncio.run_coroutine_threadsafe(fact.wait(timeout=5), loop)
    try:
        assert _wait_for(lambda: fact.waiter_count() == 1)
        _stop(loop, t)
        assert loop.is_closed() is True

        with _DebugCapture() as cap:
            fact.set()  # called from a thread with no running loop

        assert fact.is_set() is True
        assert fact.waiter_count() == 0
        assert not any("waiter loop stopped" in m for m in cap.messages)
    finally:
        fut.cancel()


def test_wake_same_loop_sets_event_directly() -> None:
    """When set() runs on the same loop as a waiter, _wake sets the event
    synchronously rather than hopping through call_soon_threadsafe."""
    fact = AsyncFact()

    async def scenario():
        wait_task = asyncio.ensure_future(fact.wait(timeout=5))
        assert await _wait_for_async(lambda: fact.waiter_count() == 1)
        fact.set()  # synchronous, on this same loop
        return await asyncio.wait_for(wait_task, timeout=5)

    assert asyncio.run(scenario()) is True
    assert fact.waiter_count() == 0


def test_wake_stopped_loop_runtime_error_is_swallowed() -> None:
    """If call_soon_threadsafe raises RuntimeError (loop stopped in the
    window between the is_closed() check and the wake), _wake swallows it
    and logs the debug message. set() must complete regardless."""
    fact = AsyncFact()

    class _ZombieLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback, *args):
            raise RuntimeError("loop stopped between check and wake")

    zombie_loop = _ZombieLoop()
    sentinel_event = asyncio.Event()
    fact._waiters[id(sentinel_event)] = (zombie_loop, sentinel_event)
    assert fact.waiter_count() == 1

    with _DebugCapture() as cap:
        fact.set()  # main thread, no running loop -> get_running_loop raises

    assert fact.is_set() is True
    assert fact.waiter_count() == 0
    assert any("waiter loop stopped" in m for m in cap.messages)


async def _wait_for_async(predicate, *, attempts=200, delay=0.01):
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return predicate()


def main() -> int:
    tests = [
        test_wait_no_timeout_path_wakes_on_set,
        test_wait_race_second_check_short_circuits,
        test_wake_closed_loop_returns_silently,
        test_wake_same_loop_sets_event_directly,
        test_wake_stopped_loop_runtime_error_is_swallowed,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n{len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
