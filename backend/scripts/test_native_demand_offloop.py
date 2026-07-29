"""WS disconnect reaches _publish_native_demand / _maybe_stop_wire_tailer
from a worker thread with no running loop (unregister_all_ws goes through
asyncio.to_thread). Both used to bind to the caller's loop and silently
drop their work there, so native_files_manager never learned a subscriber
detached.

Fails before the fix (fact never published), passes after.
"""

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths

paths.engage_test_home(tempfile.mkdtemp(prefix="native-demand-"))

from event_bus import bus  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


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


def test_demand_fact_survives_a_threaded_disconnect() -> None:
    """Publish from a plain worker thread — the WS-disconnect shape."""
    loop, thread = _run_loop_in_thread()
    previous = session_manager._loop
    seen: list[dict] = []
    done = threading.Event()

    async def handler(event):
        seen.append(dict(event.payload))
        done.set()

    try:
        session_manager.bind_loop(loop)

        async def register():
            bus.subscribe(
                "native_files.demand",
                handler,
                name="test-native-demand",
                bind_current_loop=True,
            )

        asyncio.run_coroutine_threadsafe(register(), loop).result(timeout=5)

        from event_bus import BusEvent

        def worker():
            # No running loop here — exactly what to_thread gives the
            # orchestrator's demand publish on disconnect.
            session_manager.schedule_on_bound_loop(
                bus.publish(
                    BusEvent(
                        type="native_files.demand",
                        root_id="root-1",
                        sid="sid-1",
                        payload={"owning_session": "sid-1", "present": False},
                        persist=False,
                    )
                ),
            )

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert done.wait(timeout=5), "demand fact was dropped on the no-loop path"
        assert seen[0]["present"] is False
    finally:
        bus.unsubscribe("test-native-demand")
        bus.drop_delivery_targets()
        session_manager._loop = previous
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_orchestrator_no_longer_binds_to_the_calling_loop() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "orchestrator.py"
    ).read_text(encoding="utf-8")
    demand = source.index("def _publish_native_demand")
    stop = source.index("def _maybe_stop_wire_tailer")
    body = source[demand:demand + 1600] + source[stop:stop + 1600]
    assert "schedule_on_bound_loop" in body
    # The old shape: give up when the caller has no loop.
    assert "asyncio.get_running_loop()\n        except RuntimeError:\n            return" not in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
