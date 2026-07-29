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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
