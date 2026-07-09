import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home
_test_home.isolate("bc_test_projection_")

import event_bus_subscribers  # noqa: E402
from event_bus import BusEvent  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from ordered_root_dispatcher import OrderedRootDispatcher  # noqa: E402


class _SlowSessionManager:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.applied: list[int] = []
        self.fail_seq: int | None = None
        self.dirty: list[str] = []

    def apply_written_journal_event(
        self, _root_id, _sid, _msg_id, _event_type, _data, seq,
    ) -> None:
        loop = _LOOP
        loop.call_soon_threadsafe(self.started.set)
        while not self.release.is_set():
            time.sleep(0.005)
        if seq == self.fail_seq:
            raise RuntimeError("induced projection failure")
        self.applied.append(seq)

    def mark_reconcile_dirty(self, root_id: str) -> None:
        self.dirty.append(root_id)


_LOOP: asyncio.AbstractEventLoop


async def _heartbeat(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        ticks += 1
        await asyncio.sleep(0.05)
    return ticks


async def _test_projection_keeps_loop_responsive() -> None:
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    original = event_bus_subscribers.session_manager
    slow = _SlowSessionManager()
    event_bus_subscribers.session_manager = slow
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop))
    try:
        for expected_seq in (1, 2):
            seq = event_ingester.ingest(
                "root",
                sid="sid",
                event_type="agent_message",
                data={"uuid": f"event-{expected_seq}"},
                source="test",
                msg_id="msg",
                cwd_override="",
            )
            assert seq == expected_seq
            event = BusEvent(
                type="event_journal.written",
                root_id="root",
                sid="sid",
                msg_id="msg",
                payload={"event_type": "agent_message", "seq": seq},
            )
            await event_bus_subscribers._refresh_session_content_projection(event)
            event.payload["event_type"] = "mutated_after_submit"
        await asyncio.wait_for(slow.started.wait(), timeout=1)
        await asyncio.sleep(0.2)
        assert slow.applied == [], "projection unexpectedly acknowledged while blocked"
        slow.release.set()
        await asyncio.wait_for(
            event_bus_subscribers._SESSION_PROJECTION_DISPATCHER.barrier("root"),
            timeout=1,
        )
        assert slow.applied == [1, 2], slow.applied
        await event_bus_subscribers._refresh_session_content_projection(
            BusEvent(
                type="event_journal.written",
                root_id="root",
                sid="sid",
                msg_id="msg",
                payload={
                    "event_type": "agent_message",
                    "seq": -1,
                    "appended": False,
                },
            ),
        )
        await asyncio.wait_for(
            event_bus_subscribers._SESSION_PROJECTION_DISPATCHER.barrier("root"),
            timeout=1,
        )
        assert slow.applied == [1, 2], slow.applied
        assert slow.dirty == [], slow.dirty
        seq = event_ingester.ingest(
            "root",
            sid="sid",
            event_type="agent_message",
            data={"uuid": "event-3"},
            source="test",
            msg_id="msg",
            cwd_override="",
        )
        slow.fail_seq = seq
        await event_bus_subscribers._refresh_session_content_projection(
            BusEvent(
                type="event_journal.written",
                root_id="root",
                sid="sid",
                msg_id="msg",
                payload={"event_type": "agent_message", "seq": 3},
            ),
        )
        await asyncio.wait_for(
            event_bus_subscribers._SESSION_PROJECTION_DISPATCHER.barrier("root"),
            timeout=1,
        )
        assert slow.dirty == ["root"], slow.dirty
        stop.set()
        ticks = await beat
        assert ticks >= 3, f"event loop blocked; heartbeat ticks={ticks}"
    finally:
        event_bus_subscribers.session_manager = original
        stop.set()
        if not beat.done():
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)


async def _test_dispatcher_bounds_backlog_and_drains_shutdown() -> None:
    started = threading.Event()
    release = threading.Event()
    applied: list[int] = []
    rejected: list[str] = []

    def _apply(item: int) -> None:
        started.set()
        release.wait()
        applied.append(item)

    dispatcher = OrderedRootDispatcher(
        _apply,
        pool_size=1,
        thread_name_prefix="projection-test",
        logger=event_bus_subscribers.logger,
        on_error=lambda root_id, _item, _exc: rejected.append(root_id),
        max_pending=1,
    )
    first = dispatcher.submit("root", 1)
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    second = dispatcher.submit("root", 2)
    await asyncio.sleep(0)
    assert isinstance(second.exception(), RuntimeError)
    for _ in range(100):
        if rejected:
            break
        await asyncio.sleep(0.01)
    assert rejected == ["root"], rejected
    closing = asyncio.create_task(asyncio.to_thread(dispatcher.shutdown, wait=True))
    await asyncio.sleep(0.05)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, timeout=1)
    assert first.result() is None
    assert applied == [1]


def main() -> int:
    try:
        asyncio.run(_test_projection_keeps_loop_responsive())
        asyncio.run(_test_dispatcher_bounds_backlog_and_drains_shutdown())
        print("PASS: session projection keeps event loop responsive")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
