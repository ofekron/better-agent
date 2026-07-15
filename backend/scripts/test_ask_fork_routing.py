"""Phase 2: ask fork-mode routing.

ask is the model-facing name for both execution models. run_mode='direct'
posts to the team-message endpoint (/api/internal/ask, with ask_id); run_mode
='fork' reuses the single delegation engine (/api/internal/delegate) with the
instructions/worker/delegation-id contract, so no fork logic is duplicated.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-ask-fork-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner


def _instrument():
    captured: list[tuple] = []

    def fake_post(payload, *, internal_token, url_path,
                  timeout, non_json_t_key, log_prefix, backoff_cap, recover=None):
        captured.append((url_path, payload, internal_token))
        return {"success": True}

    runner._post_loopback_sync = fake_post  # type: ignore[assignment]
    runner._tool_success_result = lambda r: r  # type: ignore[assignment]
    runner._tool_error_response = lambda n, e: {"is_error": True}  # type: ignore[assignment]
    return captured


def test_ask_direct_routes_to_team_endpoint():
    captured = _instrument()
    ask = runner._build_ask_tool(
        sender_session_id="s1", app_session_id="app1", model="m", cwd="/r",
        internal_token="t",
    )
    asyncio.run(ask.handler({"target_session_id": "w1", "message": "hi"}))
    assert captured[0][0] == "/api/internal/ask"
    assert captured[0][1]["ask_id"].startswith("ask_")
    assert captured[0][2] == "t"


def test_ask_fork_routes_to_delegate_engine():
    captured = _instrument()
    ask = runner._build_ask_tool(
        sender_session_id="s1", app_session_id="app1", model="m", cwd="/r",
        internal_token="t",
    )
    asyncio.run(ask.handler({
        "target_session_id": "w1",
        "message": "audit the auth layer",
        "run_mode": "fork",
        "worker_description": "auditor",
        "worker_registry_cwd": "/r",
    }))
    url, payload, token = captured[0]
    assert token == "t"
    assert url == "/api/internal/ask-fork"
    assert payload["instructions"] == "audit the auth layer"
    assert payload["worker_session_id"] == "w1"
    assert payload["worker_description"] == "auditor"
    assert payload["run_mode"] == "fork"
    assert payload["ephemeral"] is False
    assert payload["worker_registry_cwd"] == "/r"
    assert payload["client_delegation_id"].startswith("del_")
    assert payload["app_session_id"] == "app1"


def test_ask_fork_routes_ephemeral():
    captured = _instrument()
    ask = runner._build_ask_tool(
        sender_session_id="s1", app_session_id="app1", model="m", cwd="/r",
        internal_token="t",
    )
    asyncio.run(ask.handler({
        "target_session_id": "w1",
        "message": "audit the auth layer",
        "run_mode": "fork",
        "worker_description": "auditor",
        "ephemeral": True,
    }))
    url, payload, _token = captured[0]
    assert url == "/api/internal/ask-fork"
    assert payload["ephemeral"] is True


def test_ask_direct_rejects_ephemeral():
    captured = _instrument()
    ask = runner._build_ask_tool(
        sender_session_id="s1", app_session_id="app1", model="m", cwd="/r",
        internal_token="t",
    )
    res = asyncio.run(ask.handler({
        "target_session_id": "w1",
        "message": "hi",
        "ephemeral": True,
    }))
    assert res.get("is_error") is True
    assert captured == []


def test_ask_fork_allows_missing_worker_description():
    captured = _instrument()
    ask = runner._build_ask_tool(
        sender_session_id="s1", app_session_id="app1", model="m", cwd="/r",
        internal_token="t",
    )
    asyncio.run(ask.handler({
        "target_session_id": "w1", "message": "x", "run_mode": "fork",
    }))
    assert captured[0][0] == "/api/internal/ask-fork"
    assert captured[0][1]["worker_description"] == ""
