from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ask-dedup-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_search
import virtual_session_store


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def create_ask_session():
    virtual_session_store.upsert(
        session_search.ASK_EXTENSION_ID,
        {
            "id": session_search.ASK_SINGLETON_ID,
            "name": "Ask",
            "cwd": "/repo",
            "messages": [],
        },
    )


def test_ask_search_dedups_replayed_client_id(monkeypatch):
    create_ask_session()
    calls = 0

    async def fake_run_search(query: str, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "session_ids": [],
            "reasoning": f"matched {query}",
            "error": None,
        }

    monkeypatch.setattr(
        session_search,
        "run_search_sessions_session",
        fake_run_search,
    )

    first = asyncio.run(
        session_search.search(
            "find requirements",
            client_id="ask-client-1",
            lifecycle_msg_id="life-1",
        )
    )
    second = asyncio.run(
        session_search.search(
            "find requirements",
            client_id="ask-client-1",
            lifecycle_msg_id="life-2",
        )
    )

    messages = virtual_session_store.get(session_search.ASK_SINGLETON_ID)["messages"]
    user_messages = [m for m in messages if m.get("role") == "user"]

    assert first["error"] is None
    assert second["error"] == "duplicate_client_id"
    assert calls == 1
    assert len(user_messages) == 1
    assert user_messages[0]["client_id"] == "ask-client-1"
    assert user_messages[0]["lifecycle_msg_id"] == "life-1"


def test_ask_search_acks_user_message_before_worker_finishes(monkeypatch):
    create_ask_session()
    events: list[str] = []

    async def fake_run_search(query: str, **kwargs):
        events.append("worker_started")
        await asyncio.sleep(0)
        events.append("worker_finished")
        return {
            "session_ids": [],
            "reasoning": f"matched {query}",
            "error": None,
        }

    async def on_user_message(user_message: dict):
        events.append(f"ack:{user_message['client_id']}")

    monkeypatch.setattr(
        session_search,
        "run_search_sessions_session",
        fake_run_search,
    )

    result = asyncio.run(
        session_search.search(
            "find session",
            client_id="ask-client-2",
            lifecycle_msg_id="life-2",
            on_user_message=on_user_message,
        )
    )

    assert result["error"] is None
    assert events == [
        "ack:ask-client-2",
        "worker_started",
        "worker_finished",
    ]


def test_ask_search_projects_worker_events_into_virtual_turn(monkeypatch):
    create_ask_session()
    worker_events = [
        {
            "type": "agent_message",
            "data": {
                "uuid": "assistant-1",
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "matched from worker events"}
                    ],
                },
            },
        },
        {"type": "complete", "data": {"success": True}},
    ]

    async def fake_run_search(query: str, **kwargs):
        assert kwargs["include_worker_events"] is True
        return {
            "session_ids": [],
            "reasoning": f"summary for {query}",
            "error": None,
            "_worker_events": worker_events,
        }

    monkeypatch.setattr(
        session_search,
        "run_search_sessions_session",
        fake_run_search,
    )

    result = asyncio.run(
        session_search.search(
            "find projected turn",
            client_id="ask-client-3",
            lifecycle_msg_id="life-3",
        )
    )

    assert result["error"] is None
    assert "_worker_events" not in result
    messages = virtual_session_store.get(session_search.ASK_SINGLETON_ID)["messages"]
    assistant = next(m for m in messages if m.get("role") == "assistant")
    assert assistant["content"] == "matched from worker events"
    assert assistant["events"] == [worker_events[0]]
    assert assistant["completed_at"]


def test_ask_ui_search_sessions_is_pure(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    create_ask_session()

    async def fake_run_search(query: str, **kwargs):
        assert kwargs.get("include_worker_events") is not True
        return {
            "session_ids": ["target"],
            "reasoning": f"matched {query}",
            "error": None,
        }

    monkeypatch.setattr(main, "_require_ask_internal", lambda _token: None)
    monkeypatch.setattr(
        session_search,
        "run_search_sessions_session",
        fake_run_search,
    )

    with TestClient(main.app, client=("127.0.0.1", 50003)) as client:
        response = client.post(
            "/api/internal/ask-ui/search-sessions",
            json={"query": "find auth"},
            headers={"X-Internal-Token": main.coordinator.internal_token},
        )

    assert response.status_code == 200
    assert response.json()["session_ids"] == ["target"]
    messages = virtual_session_store.get(session_search.ASK_SINGLETON_ID)["messages"]
    assert messages == []
