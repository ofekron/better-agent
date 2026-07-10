from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import main  # noqa: E402
from jsonl_tailer import _Subscriber  # noqa: E402


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.started = asyncio.Event()

    async def send_text(self, _text: str) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


class _HangingCloseWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        await asyncio.Event().wait()


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition timed out")
        await asyncio.sleep(0.01)


def test_ws_outbox_blocked_writer_times_out_without_accepting_frame() -> None:
    async def run() -> None:
        websocket = _BlockingWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            send_timeout_s=0.05,
            enqueue_timeout_s=0.02,
            max_items=1,
        )
        assert await asyncio.wait_for(
            outbox.send({"type": "messages_replay", "data": {"messages": []}}),
            timeout=0.02,
        ) is True
        await asyncio.wait_for(websocket.started.wait(), timeout=0.2)
        assert await asyncio.wait_for(
            outbox.send({"type": "session_running_changed", "data": {}}),
            timeout=0.02,
        ) is True
        assert await asyncio.wait_for(
            outbox.send({"type": "agent_message", "data": {}}),
            timeout=0.1,
        ) is False
        await asyncio.wait_for(closed.wait(), timeout=0.5)
        assert websocket.closed is True
        assert await asyncio.wait_for(
            outbox.send({"type": "agent_message", "data": {}}),
            timeout=0.02,
        ) is False
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_sends_queued_frames_fifo() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(websocket, on_close=on_close)
        await outbox.send({"type": "first", "data": {"n": 1}})
        await outbox.send({"type": "second", "data": {"n": 2}})
        await _wait_for(lambda: len(websocket.sent) == 2)
        assert [json.loads(text)["type"] for text in websocket.sent] == [
            "first",
            "second",
        ]
        assert closed.is_set() is False
        await outbox.close()
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_backpressures_burst_larger_than_capacity() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()

        async def on_close() -> None:
            raise AssertionError("draining websocket must not close")

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            max_items=4,
            enqueue_timeout_s=0.2,
        )
        for n in range(300):
            accepted = await outbox.send({"type": "agent_message", "data": {"n": n}})
            assert accepted is True, n
        await _wait_for(lambda: len(websocket.sent) == 300)
        assert [json.loads(text)["data"]["n"] for text in websocket.sent] == list(range(300))
        assert websocket.closed is False
        await outbox.close()
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_close_rejects_waiting_enqueue() -> None:
    async def run() -> None:
        websocket = _BlockingWebSocket()

        async def on_close() -> None:
            return None

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            max_items=1,
            send_timeout_s=1.0,
            enqueue_timeout_s=1.0,
        )
        assert await outbox.send({"type": "first", "data": {}}) is True
        await websocket.started.wait()
        assert await outbox.send({"type": "second", "data": {}}) is True
        waiting = asyncio.create_task(outbox.send({"type": "third", "data": {}}))
        await asyncio.sleep(0)
        await outbox.close()
        assert await asyncio.wait_for(waiting, timeout=0.1) is False
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_close_is_timeout_bounded() -> None:
    async def run() -> None:
        websocket = _HangingCloseWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            max_items=1,
            enqueue_timeout_s=0.02,
            close_timeout_s=0.02,
        )
        assert await outbox.send({"type": "first", "data": {}}) is True
        await asyncio.wait_for(outbox.close(), timeout=0.05)
        await asyncio.wait_for(closed.wait(), timeout=0.2)
        await outbox.wait_closed()

    asyncio.run(run())


def test_production_callback_rejection_preserves_subscriber_watermark() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()

        async def on_close() -> None:
            return None

        outbox: main._WebSocketOutbox | None = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
        )
        await outbox.close()
        await outbox.wait_closed()

        async def ws_callback(event_dict):
            if outbox is None:
                return False
            return await outbox.send(event_dict)

        sub = _Subscriber(
            app_session_id="sid",
            ws_callback=ws_callback,
            from_seq=0,
            root_id="sid",
        )
        await sub.push_entry(
            {"seq": 1},
            {"type": "agent_message", "data": {}, "seq": 1},
        )
        assert sub.next_seq == 1
        production_source = inspect.getsource(main.websocket_chat)
        assert "return False\n        return await outbox.send(event_dict)" in production_source

    asyncio.run(run())
