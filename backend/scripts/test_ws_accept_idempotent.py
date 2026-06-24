"""Regression for /ws/chat entering with an already-accepted WebSocket.

Run with:
    cd backend && .venv/bin/python scripts/test_ws_accept_idempotent.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ws-accept-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.websockets import WebSocketState  # noqa: E402

import main  # noqa: E402


class _FakeWebSocket:
    def __init__(self, state: WebSocketState) -> None:
        self.application_state = state
        self.accept_calls = 0

    async def accept(self) -> None:
        self.accept_calls += 1
        if self.application_state == WebSocketState.CONNECTED:
            raise RuntimeError(
                'Expected ASGI message "websocket.send" or '
                '"websocket.close", but got "websocket.accept".',
            )
        self.application_state = WebSocketState.CONNECTED


def test_accept_skips_already_connected_ws() -> None:
    ws = _FakeWebSocket(WebSocketState.CONNECTED)
    asyncio.run(main._accept_ws_if_needed(ws))  # type: ignore[arg-type]
    assert ws.accept_calls == 0


def test_accept_connecting_ws() -> None:
    ws = _FakeWebSocket(WebSocketState.CONNECTING)
    asyncio.run(main._accept_ws_if_needed(ws))  # type: ignore[arg-type]
    assert ws.accept_calls == 1
    assert ws.application_state == WebSocketState.CONNECTED


if __name__ == "__main__":
    try:
        test_accept_skips_already_connected_ws()
        test_accept_connecting_ws()
        print("OK: websocket accept is idempotent")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
