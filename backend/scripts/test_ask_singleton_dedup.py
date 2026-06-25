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


def test_ask_msg_has_no_body_only_picker(monkeypatch):
    """The Ask assistant message carries NO body — empty content and no
    events. The reasoning lives only in `ask_result.reasoning` (rendered by
    the inline picker); stamping it into `content` too would render it twice
    (assistant bubble + picker) and pad the turn with an empty indented block.
    The Ask flow must also NOT request the worker fork's internal transcript
    (it is noise that lives in the worker panel/provenance only)."""
    create_ask_session()

    async def fake_run_search(query: str, **kwargs):
        assert "include_worker_events" not in kwargs
        return {
            "session_ids": [],
            "reasoning": f"summary for {query}",
            "error": None,
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
    # Body is empty; reasoning lives on the picker payload, not in content.
    assert assistant["content"] == ""
    assert assistant["events"] == []
    assert assistant["ask_result"]["reasoning"] == "summary for find projected turn"
    assert assistant["completed_at"]


def test_ask_search_emits_running_indicator(monkeypatch):
    """The Ask session never enters `_run_state`, so the normal
    `running_changed` recompute path can't flag the ~40s worker turn. The
    search must ping `session_running_changed` True on start and False on
    completion so the UI shows a running badge instead of looking frozen."""
    create_ask_session()
    broadcasts: list[tuple[str, dict]] = []

    async def fake_run_search(query: str, **kwargs):
        return {"session_ids": [], "reasoning": "x", "error": None}

    monkeypatch.setattr(
        session_search,
        "run_search_sessions_session",
        fake_run_search,
    )
    monkeypatch.setattr(
        session_search,
        "_broadcast_global_later",
        lambda event_type, data: broadcasts.append((event_type, data)),
    )

    result = asyncio.run(
        session_search.search(
            "running indicator",
            client_id="ask-client-run",
            lifecycle_msg_id="life-run",
        )
    )

    assert result["error"] is None
    running = [
        d["value"]
        for (event_type, d) in broadcasts
        if event_type == "session_running_changed"
    ]
    assert running == [True, False]


def test_ask_assistant_msg_has_empty_body():
    """The assistant message body is always empty — content "" and events [].
    Reasoning (and any error) is surfaced by the picker, never the bubble."""
    result = {"session_ids": [], "reasoning": "fallback", "error": "dispatch_failed"}
    msg = session_search._ask_assistant_message_from_worker_result(result)
    assert msg["events"] == []
    assert msg["content"] == ""


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
    assert "session_ids" not in response.json()
    assert response.json()["results"] == []
    messages = virtual_session_store.get(session_search.ASK_SINGLETON_ID)["messages"]
    assert messages == []
