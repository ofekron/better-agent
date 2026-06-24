"""Regression test for invalid alter sends over /ws/chat.

Run with:
    cd backend && .venv/bin/python scripts/test_alter_error_keeps_ws_alive.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-alter-ws-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _next_error(ws) -> dict:
    for _ in range(20):
        frame = ws.receive_json()
        if frame.get("type") == "error":
            return frame
    raise AssertionError("no error frame received")


async def _raise_alter_error(_session_id: str) -> dict:
    raise HTTPException(status_code=400, detail="Message has no agent_message_uuid")


def main_test() -> bool:
    session = session_manager.create(
        name="alter-ws",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    original = main._rewind_latest_user_for_alter
    main._rewind_latest_user_for_alter = _raise_alter_error
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({
                    "type": "send_message",
                    "prompt": "alter this",
                    "model": "m",
                    "cwd": "/tmp",
                    "app_session_id": sid,
                    "send_mode": "alter",
                })
                first = _next_error(ws)
                ws.send_json({"type": "send_message", "prompt": ""})
                second = _next_error(ws)
                disconnected = False
                try:
                    ws.send_json({"type": "stop_message", "app_session_id": sid})
                except WebSocketDisconnect:
                    disconnected = True
    finally:
        main._rewind_latest_user_for_alter = original

    ok = (
        first.get("type") == "error"
        and first.get("data", {}).get("error") == "Message has no agent_message_uuid"
        and second.get("type") == "error"
        and not disconnected
    )
    print(
        f"{PASS if ok else FAIL} invalid alter emits error and keeps websocket alive "
        f"-- first={first!r} second={second!r}",
    )
    return ok


if __name__ == "__main__":
    try:
        sys.exit(0 if main_test() else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
