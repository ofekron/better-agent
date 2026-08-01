"""Unit tests for assistant_ui_api.py — pure helpers + API-router
delegation/gating, exercised in-process via TestClient (no spawned backend,
no live model). The assistant_ui module and the role guard are mocked at the
import seam."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import assistant_ui_api
from assistant_ui_api import _board_preamble, _session_summary

_TOKEN = {"X-Internal-Token": "t"}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(assistant_ui_api.router)
    return app


def _fake_assistant_ui() -> MagicMock:
    """A stand-in for the assistant_ui module with the seam functions split
    by kind: sync helpers as plain mocks, async helpers as AsyncMocks."""
    fake = MagicMock()
    fake.ensure_singleton = MagicMock(return_value={"id": "i", "name": "n", "cwd": "/c"})
    fake.ensure_monitor = MagicMock(return_value={"id": "m", "name": None, "cwd": None})
    fake.last_turn = MagicMock(return_value={"turn": "ok"})
    fake.search = AsyncMock(return_value={"hits": []})
    fake.resolve_ba_session = AsyncMock(return_value={"resolved": True})
    fake.adopt_native_session = AsyncMock(return_value={"adopted": True})
    fake.delegate = AsyncMock(return_value={"delegated": True})
    return fake


# --------------------------------------------------------------------------- #
# _board_preamble
# --------------------------------------------------------------------------- #
def test_board_preamble_non_dict_returns_none():
    assert _board_preamble(None) is None
    assert _board_preamble("x") is None


def test_board_preamble_missing_key_returns_none():
    assert _board_preamble({}) is None


def test_board_preamble_none_value_collapses_to_empty_string():
    # `str(body.get("board_preamble") or "")` — falsy values become "".
    assert _board_preamble({"board_preamble": None}) == ""
    assert _board_preamble({"board_preamble": ""}) == ""


def test_board_preamble_value_stringified():
    assert _board_preamble({"board_preamble": "hi"}) == "hi"
    assert _board_preamble({"board_preamble": 123}) == "123"


# --------------------------------------------------------------------------- #
# _session_summary
# --------------------------------------------------------------------------- #
def test_session_summary_full():
    assert _session_summary({"id": "i", "name": "n", "cwd": "/c"}) == {
        "id": "i",
        "name": "n",
        "cwd": "/c",
    }


def test_session_summary_missing_optional_fields():
    assert _session_summary({"id": "i"}) == {"id": "i", "name": None, "cwd": None}


# --------------------------------------------------------------------------- #
# ensure / ensure-monitor
# --------------------------------------------------------------------------- #
def test_ensure_passes_preamble_and_returns_summary():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal"
    ) as guard:
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/ensure",
            json={"board_preamble": "ctx"},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    assert resp.json() == {"id": "i", "name": "n", "cwd": "/c"}
    fake.ensure_singleton.assert_called_once_with("ctx")
    guard.assert_called_once_with("assistant")


def test_ensure_monitor_passes_preamble_and_returns_summary():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/ensure-monitor",
            json={"board_preamble": None},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    assert resp.json() == {"id": "m", "name": None, "cwd": None}
    fake.ensure_monitor.assert_called_once_with("")


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def test_search_defaults_max_results_to_ten():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/search",
            json={"query": "q"},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    fake.search.assert_awaited_once_with("q", max_results=10)


def test_search_zero_max_results_collapses_to_default():
    # `int(body.get("max_results") or 10)` — 0 is falsy, so the default wins.
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        client.post(
            "/api/internal/assistant-ui/search",
            json={"query": "q", "max_results": 0},
            headers=_TOKEN,
        )
    fake.search.assert_awaited_once_with("q", max_results=10)


def test_search_custom_max_results_and_empty_query():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        client.post(
            "/api/internal/assistant-ui/search",
            json={"max_results": 5},
            headers=_TOKEN,
        )
    fake.search.assert_awaited_once_with("", max_results=5)


# --------------------------------------------------------------------------- #
# resolve-ba-session / adopt-native-session
# --------------------------------------------------------------------------- #
def test_resolve_ba_session_passes_session_id():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/resolve-ba-session",
            json={"session_id": "s1"},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    fake.resolve_ba_session.assert_awaited_once_with("s1")


def test_adopt_native_session_passes_session_id_and_transcript_path():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/adopt-native-session",
            json={"session_id": "s2", "transcript_path": "/p/t.jsonl"},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    fake.adopt_native_session.assert_awaited_once_with("s2", transcript_path="/p/t.jsonl")


# --------------------------------------------------------------------------- #
# delegate (400 validation)
# --------------------------------------------------------------------------- #
def test_delegate_happy_path():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/delegate",
            json={"target_session_id": "t1", "prompt": "do it"},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    fake.delegate.assert_awaited_once_with("t1", "do it")


def test_delegate_missing_target_returns_400():
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/delegate",
            json={"prompt": "do it"},
            headers=_TOKEN,
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "target_session_id and prompt are required"


def test_delegate_missing_prompt_returns_400():
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/delegate",
            json={"target_session_id": "t1"},
            headers=_TOKEN,
        )
    assert resp.status_code == 400


def test_delegate_whitespace_only_args_stripped_to_400():
    # both fields are .strip()'d, so whitespace-only counts as missing.
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/delegate",
            json={"target_session_id": "   ", "prompt": "  "},
            headers=_TOKEN,
        )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# last-turn (400 validation)
# --------------------------------------------------------------------------- #
def test_last_turn_happy_path():
    fake = _fake_assistant_ui()
    with patch.object(assistant_ui_api, "assistant_ui", fake), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/last-turn",
            json={"session_id": "s3"},
            headers=_TOKEN,
        )
    assert resp.status_code == 200
    assert resp.json() == {"turn": "ok"}
    fake.last_turn.assert_called_once_with("s3")


def test_last_turn_missing_session_id_returns_400():
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/last-turn",
            json={},
            headers=_TOKEN,
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "session_id is required"


def test_last_turn_whitespace_only_session_id_returns_400():
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()), patch.object(
        assistant_ui_api, "require_role_internal", lambda role: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/last-turn",
            json={"session_id": "  "},
            headers=_TOKEN,
        )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# gating
# --------------------------------------------------------------------------- #
def test_route_403_when_authority_invalid():
    # No request principal in TestClient -> require_internal raises 403
    # before the role-readiness branch.
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/assistant-ui/ensure",
            json={},
            headers=_TOKEN,
        )
    assert resp.status_code == 403


def test_internal_route_missing_token_header_is_unprocessable():
    # Header(...) is required by FastAPI -> 422 before the handler runs.
    with patch.object(assistant_ui_api, "assistant_ui", _fake_assistant_ui()):
        client = TestClient(_app())
        resp = client.post("/api/internal/assistant-ui/ensure", json={})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# error-mapping sanity on the pure helper (defensive guard)
# --------------------------------------------------------------------------- #
def test_session_summary_requires_id():
    with pytest.raises(KeyError):
        _session_summary({})
