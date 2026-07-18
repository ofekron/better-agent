#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("ba-test-communicate-stop-turn-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_CLAUDE_BACKEND_URL"] = "http://backend"
os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "token"
os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "caller-session"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import communicate_mcp  # noqa: E402
import session_manager  # noqa: E402


def _create_session(name: str) -> str:
    return session_manager.manager.create(name=name, cwd=str(ROOT))["id"]


def test_stop_turn_response_forwards_caller_identity() -> None:
    captured: list[tuple[str, dict, float]] = []

    def fake_post(endpoint: str, payload: dict, timeout: float) -> dict:
        captured.append((endpoint, payload, timeout))
        return {"success": True, "stopped": True}

    original = communicate_mcp._post_json
    communicate_mcp._post_json = fake_post  # type: ignore[assignment]
    try:
        result = communicate_mcp.stop_turn_response("target-session")
    finally:
        communicate_mcp._post_json = original  # type: ignore[assignment]

    assert result == {"success": True, "stopped": True}
    assert captured == [(
        "/api/internal/stop-turn",
        {
            "caller_session_id": "caller-session",
            "target_session_id": "target-session",
        },
        30.0,
    )]


def test_stop_turn_endpoint_enforces_creator() -> None:
    from fastapi.testclient import TestClient
    import auth
    import main

    caller_sid = _create_session("caller")
    other_sid = _create_session("other")
    target_sid = _create_session("target")
    event = asyncio.Event()
    turn_manager = main.coordinator.turn_manager
    turn_manager.cancel_events[target_sid] = event
    turn_manager._turn_creators[target_sid] = caller_sid

    try:
        client = TestClient(main.app)
        try:
            client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
            internal_token = main.coordinator.internal_token

            blocked = client.post(
                "/api/internal/stop-turn",
                headers={"X-Internal-Token": internal_token},
                json={
                    "caller_session_id": other_sid,
                    "target_session_id": target_sid,
                },
            )
            assert blocked.status_code == 403
            assert event.is_set() is False

            turn_manager._turn_creators.pop(target_sid, None)
            unowned = client.post(
                "/api/internal/stop-turn",
                headers={"X-Internal-Token": internal_token},
                json={
                    "caller_session_id": caller_sid,
                    "target_session_id": target_sid,
                },
            )
            assert unowned.status_code == 403
            assert event.is_set() is False

            turn_manager._turn_creators[target_sid] = caller_sid
            allowed = client.post(
                "/api/internal/stop-turn",
                headers={"X-Internal-Token": internal_token},
                json={
                    "caller_session_id": caller_sid,
                    "target_session_id": target_sid,
                },
            )
            assert allowed.status_code == 200
            assert allowed.json() == {
                "success": True,
                "stopped": True,
                "target_session_id": target_sid,
            }
            assert event.is_set() is True
        finally:
            client.close()
    finally:
        turn_manager._turn_creators.pop(target_sid, None)
        turn_manager.cancel_events.pop(target_sid, None)
        for sid in (caller_sid, other_sid, target_sid):
            session_manager.manager.delete(sid)


def test_recovered_turn_creator_comes_from_persisted_team_message() -> None:
    import main

    caller_sid = _create_session("caller-recovered")
    target_sid = _create_session("target-recovered")
    user_msg = {
        "id": "user-recovered",
        "role": "user",
        "content": "work",
        "team_message": {
            "metadata": {"sender_session_id": caller_sid},
        },
    }
    assistant_msg = {
        "id": "assistant-recovered",
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    }
    session_manager.manager.append_user_msg(target_sid, user_msg)
    session_manager.manager.append_assistant_msg(target_sid, assistant_msg)
    turn_manager = main.coordinator.turn_manager
    turn_manager.register_recovered_turn_creator(
        target_sid,
        session_manager.manager.get(target_sid),
        assistant_msg["id"],
    )
    turn_manager.active_run_ids[target_sid] = ["recovered-run"]
    turn_manager.run_state_add(
        target_sid,
        run_id="recovered-run",
        kind="native",
        target_message_id=assistant_msg["id"],
    )

    try:
        assert turn_manager.current_turn_creator(target_sid) == caller_sid
    finally:
        turn_manager.run_state_remove(target_sid, "recovered-run")
        turn_manager.active_run_ids.pop(target_sid, None)
        session_manager.manager.delete(caller_sid)
        session_manager.manager.delete(target_sid)


def main() -> int:
    test_stop_turn_response_forwards_caller_identity()
    test_stop_turn_endpoint_enforces_creator()
    test_recovered_turn_creator_comes_from_persisted_team_message()
    print("communicate stop turn: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
