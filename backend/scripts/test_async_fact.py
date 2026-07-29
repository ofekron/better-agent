import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from async_fact import AsyncFact


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
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_already_set_returns_immediately() -> None:
    fact = AsyncFact()
    fact.set()
    assert asyncio.run(fact.wait(timeout=0.1)) is True


def test_timeout_returns_false() -> None:
    fact = AsyncFact()
    assert asyncio.run(fact.wait(timeout=0.05)) is False


def test_two_independent_loops_both_wake() -> None:
    """The reason this type exists: asyncio.Event binds to one loop."""
    fact = AsyncFact()
    loop_a, ta = _loop_in_thread()
    loop_b, tb = _loop_in_thread()
    try:
        fut_a = asyncio.run_coroutine_threadsafe(fact.wait(timeout=5), loop_a)
        fut_b = asyncio.run_coroutine_threadsafe(fact.wait(timeout=5), loop_b)
        # Give both a chance to register before the fact is set.
        for _ in range(100):
            if fact.waiter_count() == 2:
                break
            threading.Event().wait(0.01)
        assert fact.waiter_count() == 2

        # Set from a third thread entirely.
        setter = threading.Thread(target=fact.set)
        setter.start()
        setter.join(timeout=5)

        assert fut_a.result(timeout=5) is True
        assert fut_b.result(timeout=5) is True
    finally:
        _stop(loop_a, ta)
        _stop(loop_b, tb)


def test_waiters_are_released_on_set_and_registry_drains() -> None:
    fact = AsyncFact()
    loop, t = _loop_in_thread()
    try:
        fut = asyncio.run_coroutine_threadsafe(fact.wait(timeout=5), loop)
        for _ in range(100):
            if fact.waiter_count() == 1:
                break
            threading.Event().wait(0.01)
        fact.set()
        assert fut.result(timeout=5) is True
        assert fact.waiter_count() == 0
    finally:
        _stop(loop, t)


def test_timed_out_waiter_is_removed() -> None:
    fact = AsyncFact()

    async def scenario():
        assert await fact.wait(timeout=0.05) is False
        assert fact.waiter_count() == 0

    asyncio.run(scenario())


def test_set_is_idempotent_and_clear_resets() -> None:
    fact = AsyncFact()
    fact.set()
    fact.set()
    assert fact.is_set() is True
    fact.clear()
    assert fact.is_set() is False
    assert asyncio.run(fact.wait(timeout=0.05)) is False


def test_set_from_a_thread_with_no_loop() -> None:
    fact = AsyncFact()
    loop, t = _loop_in_thread()
    try:
        fut = asyncio.run_coroutine_threadsafe(fact.wait(timeout=5), loop)
        for _ in range(100):
            if fact.waiter_count() == 1:
                break
            threading.Event().wait(0.01)
        fact.set()  # plain main thread, no running loop
        assert fut.result(timeout=5) is True
    finally:
        _stop(loop, t)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
