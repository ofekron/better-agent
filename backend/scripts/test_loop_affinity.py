import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop_affinity


def _run_loop_in_thread():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _main():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_main, daemon=True)
    thread.start()
    ready.wait()
    return loop, thread


def _stop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_schedules_from_a_thread_with_no_loop() -> None:
    loop, thread = _run_loop_in_thread()
    loop_affinity.reset_for_tests()
    try:
        loop_affinity.bind_main_loop(loop)
        ran_on = []
        done = threading.Event()

        async def work():
            ran_on.append(asyncio.get_running_loop())
            done.set()

        result = {}
        t = threading.Thread(
            target=lambda: result.update(
                ok=loop_affinity.schedule_on_main(work()),
            ),
        )
        t.start()
        t.join(timeout=5)

        assert result["ok"] is True
        assert done.wait(timeout=5)
        assert ran_on == [loop]
    finally:
        loop_affinity.reset_for_tests()
        _stop(loop, thread)


def test_same_loop_caller_uses_create_task() -> None:
    async def scenario():
        loop = asyncio.get_running_loop()
        loop_affinity.bind_main_loop(loop)
        ran = []

        async def work():
            ran.append(True)

        assert loop_affinity.schedule_on_main(work()) is True
        await asyncio.sleep(0)
        assert ran == [True]

    loop_affinity.reset_for_tests()
    try:
        asyncio.run(scenario())
    finally:
        loop_affinity.reset_for_tests()


def test_unbound_loop_closes_the_coroutine() -> None:
    loop_affinity.reset_for_tests()

    async def work():
        return None

    coro = work()
    assert loop_affinity.schedule_on_main(coro) is False
    # Closed, so Python emits no "never awaited" warning and the caller
    # can tell the work did not happen.
    try:
        coro.send(None)
    except StopIteration:
        raise AssertionError("coroutine was not closed")
    except RuntimeError:
        pass


def test_closed_loop_is_refused() -> None:
    loop, thread = _run_loop_in_thread()
    _stop(loop, thread)
    loop_affinity.reset_for_tests()
    loop_affinity.bind_main_loop(loop)

    async def work():
        return None

    try:
        assert loop_affinity.schedule_on_main(work()) is False
    finally:
        loop_affinity.reset_for_tests()


def test_main_loop_round_trip() -> None:
    loop, thread = _run_loop_in_thread()
    loop_affinity.reset_for_tests()
    try:
        assert loop_affinity.main_loop() is None
        loop_affinity.bind_main_loop(loop)
        assert loop_affinity.main_loop() is loop
    finally:
        loop_affinity.reset_for_tests()
        _stop(loop, thread)
        assert loop_affinity.main_loop() is None


def test_call_on_main_runs_inline_when_no_main_loop() -> None:
    loop_affinity.reset_for_tests()

    async def main():
        loop = asyncio.get_running_loop()
        seen = {}

        async def work():
            seen["loop"] = asyncio.get_running_loop()

        # No main loop bound -> falls through to running factory inline.
        await loop_affinity.call_on_main(lambda: work())
        return seen["loop"] is loop

    try:
        assert asyncio.run(main()) is True
    finally:
        loop_affinity.reset_for_tests()


def test_call_on_main_runs_inline_when_already_on_main() -> None:
    async def main():
        loop = asyncio.get_running_loop()
        loop_affinity.bind_main_loop(loop)
        seen = {}

        async def work():
            seen["loop"] = asyncio.get_running_loop()

        await loop_affinity.call_on_main(lambda: work())
        return seen["loop"] is loop

    loop_affinity.reset_for_tests()
    try:
        assert asyncio.run(main()) is True
    finally:
        loop_affinity.reset_for_tests()


def test_call_on_main_runs_on_bound_loop_from_another_loop() -> None:
    main_loop, main_thread = _run_loop_in_thread()
    loop_affinity.reset_for_tests()
    loop_affinity.bind_main_loop(main_loop)
    ran_on = []

    async def work():
        ran_on.append(asyncio.get_running_loop())
        return 42

    async def caller():
        return await loop_affinity.call_on_main(lambda: work())

    try:
        assert asyncio.run(caller()) == 42
        assert ran_on == [main_loop]
    finally:
        loop_affinity.reset_for_tests()
        _stop(main_loop, main_thread)


def test_call_on_main_falls_back_to_threadsafe_when_get_running_loop_raises() -> None:
    # The `except RuntimeError: pass` at the get_running_loop check is defensive
    # against a caller with no running loop, which cannot occur while a
    # coroutine is being awaited normally. Force that single check to raise so
    # the defensive branch executes and execution continues through the
    # threadsafe fallback to the bound main loop. Only the first call raises;
    # every later call (including work() on the main loop) uses the real loop.
    main_loop, main_thread = _run_loop_in_thread()
    loop_affinity.reset_for_tests()
    loop_affinity.bind_main_loop(main_loop)
    ran_on = []
    orig = asyncio.get_running_loop
    calls = []

    def flaky_get_running_loop():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated: no running loop")
        return orig()

    async def work():
        ran_on.append(asyncio.get_running_loop())
        return 7

    async def caller():
        asyncio.get_running_loop = flaky_get_running_loop
        try:
            return await loop_affinity.call_on_main(lambda: work())
        finally:
            asyncio.get_running_loop = orig

    try:
        assert asyncio.run(caller()) == 7
        assert ran_on == [main_loop]
    finally:
        asyncio.get_running_loop = orig
        loop_affinity.reset_for_tests()
        _stop(main_loop, main_thread)


def test_schedule_on_closes_coroutine_when_loop_stops_before_submit() -> None:
    # schedule_on's RuntimeError except guards the window where a loop passes
    # the is_closed() check but run_coroutine_threadsafe rejects it. Run from a
    # thread with no running loop so execution reaches the threadsafe submit,
    # then force that one call to raise and prove the coroutine is closed.
    loop, thread = _run_loop_in_thread()
    loop_affinity.reset_for_tests()
    orig = asyncio.run_coroutine_threadsafe

    def rejected(coro, target):
        raise RuntimeError("loop stopped mid-submit")

    async def work():
        return None

    coro = work()
    asyncio.run_coroutine_threadsafe = rejected
    try:
        assert loop_affinity.schedule_on(loop, coro) is False
    finally:
        asyncio.run_coroutine_threadsafe = orig
        loop_affinity.reset_for_tests()
        _stop(loop, thread)

    try:
        coro.send(None)
        raise AssertionError("coroutine was not closed")
    except StopIteration:
        raise AssertionError("coroutine was not closed")
    except RuntimeError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
