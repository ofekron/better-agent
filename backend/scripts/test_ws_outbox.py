from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import main  # noqa: E402


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


def test_ws_outbox_does_not_block_producers_on_slow_socket() -> None:
    async def run() -> None:
        websocket = _BlockingWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            send_timeout_s=0.05,
            max_items=4,
        )
        await asyncio.wait_for(
            outbox.send({"type": "messages_replay", "data": {"messages": []}}),
            timeout=0.02,
        )
        await asyncio.wait_for(websocket.started.wait(), timeout=0.2)
        await asyncio.wait_for(
            outbox.send({"type": "session_running_changed", "data": {}}),
            timeout=0.02,
        )
        await asyncio.wait_for(closed.wait(), timeout=0.5)
        assert websocket.closed is True
        await asyncio.wait_for(
            outbox.send({"type": "agent_message", "data": {}}),
            timeout=0.02,
        )
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
            close_timeout_s=0.02,
        )
        await outbox.send({"type": "first", "data": {}})
        await outbox.send({"type": "second", "data": {}})
        await asyncio.wait_for(closed.wait(), timeout=0.2)
        await asyncio.wait_for(outbox.close(), timeout=0.05)
        await outbox.wait_closed()

    asyncio.run(run())
