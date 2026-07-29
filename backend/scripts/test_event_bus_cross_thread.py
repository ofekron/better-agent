import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_bus import BusEvent, EventBus


def _event(kind: str = "probe", **payload) -> BusEvent:
    return BusEvent(type=kind, root_id="r", sid="s", payload=payload)


def _run_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _main() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_main, daemon=True)
    thread.start()
    ready.wait()
    return loop, thread


def _stop_loop(loop, thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_same_loop_delivery_stays_inline_and_awaited() -> None:
    async def scenario() -> None:
        bus = EventBus()
        seen: list[str] = []

        async def handler(event: BusEvent) -> None:
            seen.append(event.type)

        bus.subscribe("probe", handler, name="inline", bind_current_loop=True)
        await bus.publish(_event())
        # Awaited inline: visible immediately after publish returns.
        assert seen == ["probe"]
        assert bus.pending_cross_thread() == 0

    asyncio.run(scenario())


def test_unpinned_subscriber_runs_on_publisher_loop() -> None:
    async def scenario() -> None:
        bus = EventBus()
        loops: list[object] = []

        async def handler(event: BusEvent) -> None:
            loops.append(asyncio.get_running_loop())

        bus.subscribe("probe", handler, name="unpinned")
        await bus.publish(_event())
        assert loops == [asyncio.get_running_loop()]

    asyncio.run(scenario())


def test_publish_from_worker_thread_reaches_pinned_loop() -> None:
    loop, thread = _run_loop_in_thread()
    try:
        bus = EventBus()
        delivered: list[tuple[str, object]] = []
        done = threading.Event()

        async def handler(event: BusEvent) -> None:
            delivered.append((event.type, asyncio.get_running_loop()))
            done.set()

        async def register() -> None:
            bus.subscribe("probe", handler, name="main", bind_current_loop=True)

        asyncio.run_coroutine_threadsafe(register(), loop).result(timeout=5)

        # Publish from a thread with no loop at all.
        bus.publish_threadsafe(_event())
        assert done.wait(timeout=5)
        assert delivered[0][0] == "probe"
        assert delivered[0][1] is loop
    finally:
        _stop_loop(loop, thread)


def test_cross_loop_publish_preserves_fifo_order() -> None:
    loop, thread = _run_loop_in_thread()
    try:
        bus = EventBus()
        seen: list[int] = []
        target_count = 50
        done = threading.Event()

        async def handler(event: BusEvent) -> None:
            # Yield inside the handler: a correct pump still serializes.
            await asyncio.sleep(0)
            seen.append(event.payload["i"])
            if len(seen) == target_count:
                done.set()

        async def register() -> None:
            bus.subscribe("probe", handler, name="main", bind_current_loop=True)

        asyncio.run_coroutine_threadsafe(register(), loop).result(timeout=5)

        for i in range(target_count):
            bus.publish_threadsafe(_event(i=i))
        assert done.wait(timeout=5)
        assert seen == list(range(target_count))
    finally:
        _stop_loop(loop, thread)


def test_publish_from_another_loop_hands_off_instead_of_running_inline() -> None:
    loop, thread = _run_loop_in_thread()
    try:
        bus = EventBus()
        ran_on: list[object] = []
        done = threading.Event()

        async def handler(event: BusEvent) -> None:
            ran_on.append(asyncio.get_running_loop())
            done.set()

        async def register() -> None:
            bus.subscribe("probe", handler, name="main", bind_current_loop=True)

        asyncio.run_coroutine_threadsafe(register(), loop).result(timeout=5)

        async def publisher() -> None:
            await bus.publish(_event())
            # Handed off, not awaited — must not have run on this loop.
            assert not ran_on

        asyncio.run(publisher())
        assert done.wait(timeout=5)
        assert ran_on == [loop]
    finally:
        _stop_loop(loop, thread)


def test_bind_unpinned_pins_import_time_subscribers() -> None:
    bus = EventBus()

    async def handler(event: BusEvent) -> None:
        return None

    bus.subscribe("probe", handler, name="import-time")
    assert bus.describe()[0]["pinned"] is False

    async def scenario() -> None:
        assert bus.bind_unpinned_to_current_loop() == 1

    asyncio.run(scenario())
    assert bus.describe()[0]["pinned"] is True


def test_closed_target_drops_without_raising() -> None:
    loop, thread = _run_loop_in_thread()
    bus = EventBus()

    async def handler(event: BusEvent) -> None:
        return None

    async def register() -> None:
        bus.subscribe("probe", handler, name="main", bind_current_loop=True)

    asyncio.run_coroutine_threadsafe(register(), loop).result(timeout=5)
    _stop_loop(loop, thread)

    # Loop is closed; publishing must be a logged drop, not a raise.
    bus.publish_threadsafe(_event())
    bus.drop_delivery_targets()


def test_subscriber_exception_on_target_does_not_stop_the_pump() -> None:
    loop, thread = _run_loop_in_thread()
    try:
        bus = EventBus()
        seen: list[int] = []
        done = threading.Event()

        async def handler(event: BusEvent) -> None:
            i = event.payload["i"]
            if i == 0:
                raise RuntimeError("boom")
            seen.append(i)
            if i == 2:
                done.set()

        async def register() -> None:
            bus.subscribe("probe", handler, name="main", bind_current_loop=True)

        asyncio.run_coroutine_threadsafe(register(), loop).result(timeout=5)
        for i in range(3):
            bus.publish_threadsafe(_event(i=i))
        assert done.wait(timeout=5)
        assert seen == [1, 2]
    finally:
        _stop_loop(loop, thread)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
