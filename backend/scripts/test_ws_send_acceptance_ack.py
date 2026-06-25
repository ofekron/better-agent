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

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ws-send-ack-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import session_queue_projection  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _receive_json_or_none(ws, timeout: float = 0.05):
    import anyio
    import json

    with anyio.move_on_after(timeout):
        message = await ws._send_rx.receive()
        if message.get("type") == "websocket.send":
            text = message.get("text")
            if text is not None:
                return json.loads(text)
    return None


def _receive_type_or_none(ws, frame_type: str, attempts: int = 20):
    for _ in range(attempts):
        frame = ws.portal.call(_receive_json_or_none, ws, 0.2)
        if frame is None:
            continue
        if frame.get("type") == frame_type:
            return frame
    return None


def _frame_session_id(frame: dict) -> str | None:
    data = frame.get("data") or {}
    return data.get("app_session_id") or data.get("session_id")


def _receive_type_for_session_or_none(
    ws,
    frame_type: str,
    session_id: str,
    attempts: int = 30,
):
    for _ in range(attempts):
        frame = ws.portal.call(_receive_json_or_none, ws, 0.2)
        if frame is None:
            continue
        if frame.get("type") == frame_type and _frame_session_id(frame) == session_id:
            return frame
    return None


async def _slow_submit(_sid: str, params: dict) -> str:
    import asyncio

    await asyncio.sleep(0.35)
    return params["_queued_id"]


def main_test(client: TestClient) -> bool:
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
            frame = _receive_type_for_session_or_none(
                ws,
                "user_message_queued",
                sid,
            )
            elapsed = time.monotonic() - started
    finally:
        main.coordinator.submit_prompt_async = original_submit

    data = (frame or {}).get("data") or {}
    ok = (
        (frame or {}).get("type") == "user_message_queued"
        and data.get("app_session_id") == sid
        and data.get("client_id") == "client-ack-1"
        and data.get("kind") == "send"
        and elapsed < 0.35
    )
    print(
        f"{PASS if ok else FAIL} websocket send acks before coordinator submit returns "
        f"-- elapsed={elapsed:.3f}s frame={frame!r}",
    )
    return ok


def duplicate_queued_test(client: TestClient) -> bool:
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
        lifecycle = _receive_type_for_session_or_none(
            ws,
            "user_message_queued",
            sid,
        )
        prompt_queued = _receive_type_for_session_or_none(
            ws,
            "prompt_queued",
            sid,
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
        f"-- lifecycle={lifecycle!r} prompt_queued={prompt_queued!r}",
    )
    return ok


def duplicate_queued_stale_projection_test(client: TestClient) -> bool:
    session = session_manager.create(
        name="ws-duplicate-queued-stale-projection",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    client_id = "client-duplicate-queued-stale"
    session_manager.add_queued_prompt(
        sid,
        {
            "id": "queued-stale-1",
            "lifecycle_msg_id": "life-queued-stale-1",
            "content": "already queued stale",
            "kind": "queued_behind",
            "queue_position": 1,
            "images_count": 0,
            "orchestration_mode": "native",
            "client_id": client_id,
        },
    )
    original_submit = main.coordinator.submit_prompt_async
    original_projection_get = session_queue_projection.get
    called = False

    async def _unexpected_submit(_sid: str, _params: dict) -> str:
        nonlocal called
        called = True
        return "unexpected"

    main.coordinator.submit_prompt_async = _unexpected_submit
    session_queue_projection.get = lambda _sid: None
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "already queued stale",
                "model": "m",
                "cwd": "/tmp",
                "app_session_id": sid,
                "send_mode": "queue",
                "client_id": client_id,
            })
            lifecycle = _receive_type_for_session_or_none(
                ws,
                "user_message_queued",
                sid,
            )
            prompt_queued = _receive_type_for_session_or_none(
                ws,
                "prompt_queued",
                sid,
            )
    finally:
        main.coordinator.submit_prompt_async = original_submit
        session_queue_projection.get = original_projection_get
        main.coordinator._active_prompt_client_ids.pop((sid, client_id), None)
        for item_id, key in list(main.coordinator._prompt_client_id_by_item.items()):
            if key == (sid, client_id):
                main.coordinator._prompt_client_id_by_item.pop(item_id, None)

    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    lifecycle_data = (lifecycle or {}).get("data") or {}
    prompt_data = (prompt_queued or {}).get("data") or {}
    ok = (
        not called
        and len(queued) == 1
        and queued[0].get("id") == "queued-stale-1"
        and (lifecycle or {}).get("type") == "user_message_queued"
        and lifecycle_data.get("lifecycle_msg_id") == "life-queued-stale-1"
        and lifecycle_data.get("kind") == "queued_behind"
        and (prompt_queued or {}).get("type") == "prompt_queued"
        and prompt_data.get("queued_id") == "queued-stale-1"
    )
    print(
        f"{PASS if ok else FAIL} stale projection duplicate queued emits existing queued ack "
        f"-- called={called} queued={queued!r} lifecycle={lifecycle!r} prompt_queued={prompt_queued!r}",
    )
    return ok


def duplicate_internal_send_test(client: TestClient) -> bool:
    session = session_manager.create(
        name="ws-duplicate-internal-send-ack",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    client_id = "client-duplicate-internal-send"
    session_manager.add_queued_prompt(
        sid,
        {
            "id": "send-queued-1",
            "lifecycle_msg_id": "life-send-queued-1",
            "content": "already accepted",
            "kind": "send",
            "queue_position": 0,
            "images_count": 0,
            "orchestration_mode": "native",
            "client_id": client_id,
        },
    )
    original_submit = main.coordinator.submit_prompt_async
    submitted_items: list[tuple[str, str]] = []

    async def _unexpected_submit(_sid: str, _params: dict) -> str:
        submitted_items.append((_sid, _params.get("_queued_id") or ""))
        return "unexpected"

    main.coordinator.submit_prompt_async = _unexpected_submit
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "already accepted",
                "model": "m",
                "cwd": "/tmp",
                "app_session_id": sid,
                "send_mode": "queue",
                "client_id": client_id,
            })
            frame = _receive_type_for_session_or_none(
                ws,
                "user_message_queued",
                sid,
            )
            prompt_queued = _receive_type_for_session_or_none(
                ws,
                "prompt_queued",
                sid,
                attempts=3,
            )
    finally:
        main.coordinator.submit_prompt_async = original_submit

    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    data = (frame or {}).get("data") or {}
    duplicate_submitted = any(
        submitted_sid == sid
        and submitted_id
        and submitted_id != "send-queued-1"
        for submitted_sid, submitted_id in submitted_items
    )
    ok = (
        not duplicate_submitted
        and len(queued) == 1
        and queued[0].get("id") == "send-queued-1"
        and queued[0].get("kind") == "send"
        and (frame or {}).get("type") == "user_message_queued"
        and data.get("app_session_id") == sid
        and data.get("client_id") == client_id
        and data.get("lifecycle_msg_id") == "life-send-queued-1"
        and data.get("kind") == "send"
        and prompt_queued is None
    )
    print(
        f"{PASS if ok else FAIL} duplicate internal send emits lifecycle ack only "
        f"-- submitted_items={submitted_items!r} queued={queued!r} frame={frame!r} prompt_queued={prompt_queued!r}",
    )
    return ok


def duplicate_internal_send_stale_projection_test(client: TestClient) -> bool:
    session = session_manager.create(
        name="ws-duplicate-internal-send-stale-projection",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    client_id = "client-duplicate-internal-send-stale"
    session_manager.add_queued_prompt(
        sid,
        {
            "id": "send-stale-queued-1",
            "lifecycle_msg_id": "life-send-stale-queued-1",
            "content": "already accepted stale",
            "kind": "send",
            "queue_position": 0,
            "images_count": 0,
            "orchestration_mode": "native",
            "client_id": client_id,
        },
    )
    original_submit = main.coordinator.submit_prompt_async
    original_projection_get = session_queue_projection.get
    called = False

    async def _unexpected_submit(_sid: str, _params: dict) -> str:
        nonlocal called
        called = True
        return "unexpected"

    main.coordinator.submit_prompt_async = _unexpected_submit
    session_queue_projection.get = lambda _sid: None
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "already accepted stale",
                "model": "m",
                "cwd": "/tmp",
                "app_session_id": sid,
                "send_mode": "queue",
                "client_id": client_id,
            })
            frame = _receive_type_for_session_or_none(
                ws,
                "user_message_queued",
                sid,
            )
            prompt_queued = _receive_type_for_session_or_none(
                ws,
                "prompt_queued",
                sid,
                attempts=3,
            )
    finally:
        main.coordinator.submit_prompt_async = original_submit
        session_queue_projection.get = original_projection_get
        main.coordinator._active_prompt_client_ids.pop((sid, client_id), None)
        for item_id, key in list(main.coordinator._prompt_client_id_by_item.items()):
            if key == (sid, client_id):
                main.coordinator._prompt_client_id_by_item.pop(item_id, None)

    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    data = (frame or {}).get("data") or {}
    ok = (
        not called
        and len(queued) == 1
        and queued[0].get("id") == "send-stale-queued-1"
        and (frame or {}).get("type") == "user_message_queued"
        and data.get("lifecycle_msg_id") == "life-send-stale-queued-1"
        and data.get("kind") == "send"
        and prompt_queued is None
    )
    print(
        f"{PASS if ok else FAIL} stale projection duplicate internal send emits lifecycle ack only "
        f"-- called={called} queued={queued!r} frame={frame!r} prompt_queued={prompt_queued!r}",
    )
    return ok


def duplicate_active_test(client: TestClient) -> bool:
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
    submitted_items: list[tuple[str, str]] = []

    async def _unexpected_submit(_sid: str, _params: dict) -> str:
        submitted_items.append((_sid, _params.get("_queued_id") or ""))
        return "unexpected"

    main.coordinator.submit_prompt_async = _unexpected_submit
    try:
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
            frame = _receive_type_for_session_or_none(
                ws,
                "user_message_queued",
                sid,
            )
    finally:
        main.coordinator.submit_prompt_async = original_submit
        main.coordinator._active_prompt_client_ids.pop((sid, client_id), None)
        main.coordinator._prompt_client_id_by_item.pop(item_id, None)
        main.coordinator.user_prompt_manager.clear_in_flight_lifecycle_msg_id(sid)

    data = (frame or {}).get("data") or {}
    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    duplicate_submitted = any(
        submitted_sid == sid
        for submitted_sid, _submitted_id in submitted_items
    )
    ok = (
        not duplicate_submitted
        and not queued
        and (frame or {}).get("type") == "user_message_queued"
        and data.get("app_session_id") == sid
        and data.get("client_id") == client_id
        and data.get("lifecycle_msg_id") == lifecycle_msg_id
        and data.get("kind") == "send"
    )
    print(
        f"{PASS if ok else FAIL} duplicate active prompt reuses lifecycle ack "
        f"-- submitted_items={submitted_items!r} queued={queued!r} frame={frame!r}",
    )
    return ok


def duplicate_persisted_user_test(client: TestClient) -> bool:
    session = session_manager.create(
        name="ws-duplicate-persisted-user-ack",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    client_id = "client-duplicate-persisted-user"
    existing_user = session_manager.append_user_msg(sid, {
        "id": "user-existing-1",
        "role": "user",
        "content": "already persisted",
        "client_id": client_id,
    })
    original_submit = main.coordinator.submit_prompt_async
    original_emit_queued = main.emit_queued
    original_projection_get = session_queue_projection.get
    original_has_active_turn = main.coordinator.turn_manager.has_active_turn
    original_has_active_runs = main.coordinator.turn_manager.has_active_runs
    called = False
    queued_emits: list[dict] = []

    async def _unexpected_submit(_sid: str, _params: dict) -> str:
        nonlocal called
        called = True
        return "unexpected"

    async def _unexpected_emit_queued(**kwargs):
        queued_emits.append(kwargs)

    main.coordinator.submit_prompt_async = _unexpected_submit
    main.emit_queued = _unexpected_emit_queued
    session_queue_projection.get = lambda _sid: None
    main.coordinator.turn_manager.has_active_turn = lambda _sid: False
    main.coordinator.turn_manager.has_active_runs = lambda _sid: False
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "already persisted",
                "model": "m",
                "cwd": "/tmp",
                "app_session_id": sid,
                "send_mode": "queue",
                "client_id": client_id,
            })
            frame = _receive_type_for_session_or_none(
                ws,
                "user_message_persisted",
                sid,
            )
    finally:
        main.coordinator.submit_prompt_async = original_submit
        main.emit_queued = original_emit_queued
        session_queue_projection.get = original_projection_get
        main.coordinator.turn_manager.has_active_turn = original_has_active_turn
        main.coordinator.turn_manager.has_active_runs = original_has_active_runs
        main.coordinator._active_prompt_client_ids.pop((sid, client_id), None)
        for item_id, key in list(main.coordinator._prompt_client_id_by_item.items()):
            if key == (sid, client_id):
                main.coordinator._prompt_client_id_by_item.pop(item_id, None)

    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    data = (frame or {}).get("data") or {}
    ok = (
        existing_user is not None
        and not called
        and not queued_emits
        and not queued
        and (frame or {}).get("type") == "user_message_persisted"
        and data.get("session_id") == sid
        and (data.get("user_message") or {}).get("id") == "user-existing-1"
    )
    print(
        f"{PASS if ok else FAIL} duplicate persisted user emits persisted ack only "
        f"-- called={called} queued_emits={queued_emits!r} queued={queued!r} frame={frame!r}",
    )
    return ok


if __name__ == "__main__":
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
            ok = main_test(client)
            ok = duplicate_queued_test(client) and ok
            ok = duplicate_queued_stale_projection_test(client) and ok
            ok = duplicate_internal_send_test(client) and ok
            ok = duplicate_internal_send_stale_projection_test(client) and ok
            ok = duplicate_active_test(client) and ok
            ok = duplicate_persisted_user_test(client) and ok
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
