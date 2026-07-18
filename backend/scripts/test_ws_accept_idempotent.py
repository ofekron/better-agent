"""Regression for /ws/chat entering with an already-accepted WebSocket.

Run with:
    cd backend && .venv/bin/python scripts/test_ws_accept_idempotent.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ws-accept-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.websockets import WebSocketState  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
from auth_test_helpers import authenticate_client  # noqa: E402
from ws_snapshot_binary import SNAPSHOT_BINARY_SUBPROTOCOL  # noqa: E402
from ws_snapshot_transport import SNAPSHOT_THRESHOLD_BYTES  # noqa: E402


class _FakeWebSocket:
    def __init__(self, state: WebSocketState, protocols: str = "") -> None:
        self.application_state = state
        self.accept_calls = 0
        self.accepted_subprotocol = None
        self.headers = {"sec-websocket-protocol": protocols}
        self.scope = {}

    async def accept(self, subprotocol=None) -> None:
        self.accept_calls += 1
        self.accepted_subprotocol = subprotocol
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


def test_accept_negotiates_only_the_supported_binary_protocol() -> None:
    ws = _FakeWebSocket(
        WebSocketState.CONNECTING,
        "unrelated, better-agent.snapshot.binary-v1",
    )
    asyncio.run(main._accept_ws_if_needed(ws))  # type: ignore[arg-type]
    assert ws.accepted_subprotocol == "better-agent.snapshot.binary-v1"
    assert main._snapshot_binary_enabled(ws) is True

    legacy = _FakeWebSocket(WebSocketState.CONNECTING, "unrelated")
    asyncio.run(main._accept_ws_if_needed(legacy))  # type: ignore[arg-type]
    assert legacy.accepted_subprotocol is None
    assert main._snapshot_binary_enabled(legacy) is False


def test_real_websocket_handshake_negotiates_binary_and_allows_legacy() -> None:
    app = FastAPI()

    @app.websocket("/ws")
    async def endpoint(websocket: WebSocket) -> None:
        await main._accept_ws_if_needed(websocket)
        await websocket.send_json({"binary": main._snapshot_binary_enabled(websocket)})
        await websocket.close()

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws",
            subprotocols=["better-agent.snapshot.binary-v1"],
        ) as websocket:
            assert websocket.accepted_subprotocol == "better-agent.snapshot.binary-v1"
            assert websocket.receive_json() == {"binary": True}
        with client.websocket_connect("/ws") as websocket:
            assert websocket.accepted_subprotocol is None
            assert websocket.receive_json() == {"binary": False}


def test_real_chat_endpoint_binary_fifo_and_reconnect_resume() -> None:
    callbacks = []
    original_register = main.coordinator.register_global_ws
    main.coordinator.register_global_ws = callbacks.append
    try:
        with TestClient(main.app, client=("127.0.0.1", 50002)) as client:
            authenticate_client(client)
            token = auth.create_token("snapshot-binary-e2e")
            assert client.portal is not None
            event = {
                "type": "messages_replay",
                "data": {
                    "app_session_id": "binary-e2e-sid",
                    "messages": [{"content": "x" * (SNAPSHOT_THRESHOLD_BYTES + 100)}],
                },
            }
            with client.websocket_connect(
                f"/ws/chat?token={token}",
                subprotocols=[SNAPSHOT_BINARY_SUBPROTOCOL],
            ) as websocket:
                assert websocket.accepted_subprotocol == SNAPSHOT_BINARY_SUBPROTOCOL
                assert len(callbacks) == 1
                assert client.portal.call(callbacks[0], event) is True
                begin = websocket.receive_json()
                assert begin["type"] == "snapshot_begin"
                assert begin["data"]["encoding"] == "binary-v1"
                assert client.portal.call(callbacks[0], {
                    "type": "agent_message",
                    "data": {"text": "live-after-begin"},
                }) is True
                assert websocket.receive_json()["type"] == "agent_message"
                websocket.send_json({
                    "type": "snapshot_ack",
                    "data": {
                        "snapshot_id": begin["data"]["snapshot_id"],
                        "revision": begin["data"]["revision"],
                        "next_chunk": 0,
                    },
                })
                first_binary = websocket.receive_bytes()
                assert first_binary[:4] == b"BASN"
                assert struct.unpack("!I", first_binary[24:28])[0] == 0

            with client.websocket_connect(
                f"/ws/chat?token={token}",
                subprotocols=[SNAPSHOT_BINARY_SUBPROTOCOL],
            ) as resumed:
                assert len(callbacks) == 2
                resumed.send_json({
                    "type": "snapshot_resume",
                    "data": {
                        "snapshot_id": begin["data"]["snapshot_id"],
                        "revision": begin["data"]["revision"],
                        "digest": begin["data"]["digest"],
                        "next_chunk": 1,
                    },
                })
                resumed_begin = resumed.receive_json()
                assert resumed_begin["type"] == "snapshot_begin"
                assert resumed_begin["data"]["resume_from"] == 1
                resumed_binary = resumed.receive_bytes()
                assert struct.unpack("!I", resumed_binary[24:28])[0] == 1
                resumed.send_json({
                    "type": "snapshot_ack",
                    "data": {
                        "snapshot_id": begin["data"]["snapshot_id"],
                        "revision": begin["data"]["revision"],
                        "next_chunk": begin["data"]["total_chunks"],
                    },
                })
                assert resumed.receive_json()["type"] == "snapshot_end"
    finally:
        main.coordinator.register_global_ws = original_register


if __name__ == "__main__":
    try:
        test_accept_skips_already_connected_ws()
        test_accept_connecting_ws()
        test_accept_negotiates_only_the_supported_binary_protocol()
        test_real_websocket_handshake_negotiates_binary_and_allows_legacy()
        test_real_chat_endpoint_binary_fifo_and_reconnect_resume()
        print("OK: websocket accept is idempotent")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
