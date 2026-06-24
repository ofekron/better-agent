import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_projection_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_bus_subscribers  # noqa: E402
from event_bus import BusEvent  # noqa: E402


class _SlowSessionManager:
    def refresh_message_content_from_events(self, *_args):
        time.sleep(0.35)


async def _heartbeat(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        ticks += 1
        await asyncio.sleep(0.05)
    return ticks


async def _test_projection_keeps_loop_responsive() -> None:
    original = event_bus_subscribers.session_manager
    event_bus_subscribers.session_manager = _SlowSessionManager()
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop))
    try:
        await event_bus_subscribers._refresh_session_content_projection(
            BusEvent(
                type="event_journal.written",
                root_id="root",
                sid="sid",
                msg_id="msg",
                payload={"event_type": "agent_message"},
            ),
        )
        stop.set()
        ticks = await beat
        assert ticks >= 3, f"event loop blocked; heartbeat ticks={ticks}"
    finally:
        event_bus_subscribers.session_manager = original
        stop.set()
        if not beat.done():
            beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)


def main() -> int:
    try:
        asyncio.run(_test_projection_keeps_loop_responsive())
        print("PASS: session projection keeps event loop responsive")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
