"""Regression test for websocket send acceptance acks.

Run with:
    cd backend && .venv/bin/python scripts/test_ws_send_acceptance_ack.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ws-send-ack-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _slow_submit(_sid: str, params: dict) -> str:
    import asyncio

    await asyncio.sleep(0.35)
    return params["_queued_id"]


def main_test() -> bool:
    session = session_manager.create(
        name="ws-send-ack",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    original_submit = main.coordinator.submit_prompt_async
    main.coordinator.submit_prompt_async = _slow_submit
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
            token = auth.create_token("test")
            with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                started = time.monotonic()
                ws.send_json({
                    "type": "send_message",
                    "prompt": "hello",
                    "model": "m",
                    "cwd": "/tmp",
                    "app_session_id": sid,
                    "send_mode": "queue",
                    "client_id": "client-ack-1",
                })
                frame = None
                for _ in range(8):
                    candidate = ws.receive_json()
                    if candidate.get("type") == "user_message_queued":
                        frame = candidate
                        break
                elapsed = time.monotonic() - started
    finally:
        main.coordinator.submit_prompt_async = original_submit

    data = (frame or {}).get("data") or {}
    ok = (
        (frame or {}).get("type") == "user_message_queued"
        and data.get("app_session_id") == sid
        and data.get("client_id") == "client-ack-1"
        and data.get("kind") == "send"
        and elapsed < 0.25
    )
    print(
        f"{PASS if ok else FAIL} websocket send acks before coordinator submit returns "
        f"-- elapsed={elapsed:.3f}s frame={frame!r}",
    )
    return ok


def duplicate_queued_test() -> bool:
    session = session_manager.create(
        name="ws-duplicate-queued-ack",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.add_queued_prompt(
        sid,
        {
            "id": "queued-1",
            "lifecycle_msg_id": "life-queued-1",
            "content": "already queued",
            "kind": "queued_behind",
            "queue_position": 1,
            "images_count": 0,
            "orchestration_mode": "native",
            "client_id": "client-duplicate-queued",
        },
    )
    with TestClient(main.app, client=("127.0.0.1", 50001)) as client:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "already queued",
                "model": "m",
                "cwd": "/tmp",
                "app_session_id": sid,
                "send_mode": "queue",
                "client_id": "client-duplicate-queued",
            })
            frames = [ws.receive_json() for _ in range(2)]

    lifecycle = next(
        (frame for frame in frames if frame.get("type") == "user_message_queued"),
        None,
    )
    prompt_queued = next(
        (frame for frame in frames if frame.get("type") == "prompt_queued"),
        None,
    )
    data = (lifecycle or {}).get("data") or {}
    ok = (
        lifecycle is not None
        and prompt_queued is not None
        and data.get("app_session_id") == sid
        and data.get("client_id") == "client-duplicate-queued"
        and data.get("lifecycle_msg_id") == "life-queued-1"
        and data.get("kind") == "queued_behind"
    )
    print(
        f"{PASS if ok else FAIL} duplicate queued prompt emits lifecycle ack "
        f"-- frames={frames!r}",
    )
    return ok


def duplicate_active_test() -> bool:
    session = session_manager.create(
        name="ws-duplicate-active-ack",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    client_id = "client-duplicate-active"
    item_id = "active-item-1"
    lifecycle_msg_id = "life-active-1"
    main.coordinator._active_prompt_client_ids[(sid, client_id)] = item_id
    main.coordinator._prompt_client_id_by_item[item_id] = (sid, client_id)
    main.coordinator.user_prompt_manager.set_in_flight_lifecycle_msg_id(
        sid,
        lifecycle_msg_id,
    )
    original_submit = main.coordinator.submit_prompt_async
    called = False

    async def _unexpected_submit(_sid: str, _params: dict) -> str:
        nonlocal called
        called = True
        return "unexpected"

    main.coordinator.submit_prompt_async = _unexpected_submit
    try:
        with TestClient(main.app, client=("127.0.0.1", 50002)) as client:
            token = auth.create_token("test")
            with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                ws.send_json({
                    "type": "send_message",
                    "prompt": "active duplicate",
                    "model": "m",
                    "cwd": "/tmp",
                    "app_session_id": sid,
                    "send_mode": "queue",
                    "client_id": client_id,
                })
                frame = ws.receive_json()
    finally:
        main.coordinator.submit_prompt_async = original_submit
        main.coordinator._active_prompt_client_ids.pop((sid, client_id), None)
        main.coordinator._prompt_client_id_by_item.pop(item_id, None)
        main.coordinator.user_prompt_manager.clear_in_flight_lifecycle_msg_id(sid)

    data = (frame or {}).get("data") or {}
    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    ok = (
        not called
        and not queued
        and (frame or {}).get("type") == "user_message_queued"
        and data.get("app_session_id") == sid
        and data.get("client_id") == client_id
        and data.get("lifecycle_msg_id") == lifecycle_msg_id
        and data.get("kind") == "send"
    )
    print(
        f"{PASS if ok else FAIL} duplicate active prompt reuses lifecycle ack "
        f"-- called={called} queued={queued!r} frame={frame!r}",
    )
    return ok


if __name__ == "__main__":
    try:
        ok = main_test()
        ok = duplicate_queued_test() and ok
        ok = duplicate_active_test() and ok
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
