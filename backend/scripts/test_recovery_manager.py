import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recovery_manager as rm_mod
from event_bus import bus


def test_work_runs_on_the_recovery_loop_not_the_caller() -> None:
    manager = rm_mod.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            main_loop = asyncio.get_running_loop()

            async def work():
                return asyncio.get_running_loop(), threading.current_thread().name

            loop, thread_name = await manager.run(work)
            assert loop is manager.loop
            assert loop is not main_loop
            assert thread_name == "recovery-manager"

        asyncio.run(scenario())
    finally:
        manager.stop()


def test_blocking_io_uses_the_managers_own_executor() -> None:
    manager = rm_mod.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            async def work():
                return await asyncio.to_thread(
                    lambda: threading.current_thread().name,
                )

            name = await manager.run(work)
            # Its own pool, not asyncio's shared default executor.
            assert name.startswith("recovery-io"), name

        asyncio.run(scenario())
    finally:
        manager.stop()


def test_exceptions_propagate_to_the_caller() -> None:
    manager = rm_mod.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            async def work():
                raise ValueError("boom")

            try:
                await manager.run(work)
            except ValueError as exc:
                assert str(exc) == "boom"
            else:
                raise AssertionError("exception did not propagate")

        asyncio.run(scenario())
    finally:
        manager.stop()


def test_run_falls_back_inline_when_manager_is_down() -> None:
    manager = rm_mod.RecoveryManager()

    async def scenario():
        main_loop = asyncio.get_running_loop()

        async def work():
            return asyncio.get_running_loop()

        # Never started: work must still run, not be silently skipped.
        assert await manager.run(work) is main_loop

    asyncio.run(scenario())


def test_cancel_does_not_abandon_in_flight_work() -> None:
    """Startup relied on join-on-cancel before the scan moved here:
    shutdown must not tear the manager down under a half-done scan."""
    import time

    manager = rm_mod.RecoveryManager()
    manager.start()
    state = {"finished": False}
    try:
        async def scenario():
            started = threading.Event()

            async def work():
                def blocking():
                    started.set()
                    time.sleep(0.6)
                    state["finished"] = True
                    return "done"

                return await asyncio.to_thread(blocking)

            task = asyncio.create_task(manager.run(work))
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # The cancel waited for the work instead of abandoning it.
            assert state["finished"] is True

        asyncio.run(scenario())
    finally:
        manager.stop()


def test_start_and_stop_are_idempotent() -> None:
    manager = rm_mod.RecoveryManager()
    manager.start()
    first = manager.loop
    manager.start()
    assert manager.loop is first
    manager.stop()
    assert manager.running is False
    manager.stop()


def test_facts_reach_a_main_loop_subscriber_from_the_recovery_thread() -> None:
    manager = rm_mod.RecoveryManager()
    manager.start()
    received: list[tuple[str, object]] = []
    done = threading.Event()

    async def handler(event):
        received.append((event.type, asyncio.get_running_loop()))
        done.set()

    async def scenario():
        main_loop = asyncio.get_running_loop()
        bus.subscribe(
            rm_mod.RECOVERY_SESSION_READY,
            handler,
            name="test-recovery-fact",
            bind_current_loop=True,
        )

        async def work():
            manager.publish_fact(
                rm_mod.RECOVERY_SESSION_READY, sid="sid-1",
            )

        await manager.run(work)
        # Delivered back onto the main loop, not the recovery loop.
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)
        assert done.is_set()
        assert received[0][0] == rm_mod.RECOVERY_SESSION_READY
        assert received[0][1] is main_loop

    try:
        asyncio.run(scenario())
    finally:
        bus.unsubscribe("test-recovery-fact")
        bus.drop_delivery_targets()
        manager.stop()


def test_stop_releases_the_thread() -> None:
    manager = rm_mod.RecoveryManager()
    manager.start()
    names = {t.name for t in threading.enumerate()}
    assert "recovery-manager" in names
    manager.stop()
    for _ in range(100):
        if "recovery-manager" not in {t.name for t in threading.enumerate()}:
            break
        threading.Event().wait(0.02)
    assert "recovery-manager" not in {t.name for t in threading.enumerate()}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
