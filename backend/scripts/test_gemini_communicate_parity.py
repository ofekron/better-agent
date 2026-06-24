"""Gemini team-mode parity: the `communicate` stdio MCP server now exposes
create_worker and ask(fork), matching Claude/Codex. Verifies the payload
routing (ask direct→/api/internal/ask, ask fork→/api/internal/delegate,
create_worker→/api/internal/create-worker) by monkeypatching _post_json.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-gemini-comms-")
os.environ["BETTER_CLAUDE_BACKEND_URL"] = "http://x"
os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "t"
os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "mgr-session"
os.environ["BETTER_CLAUDE_MODEL"] = "gemini-x"
os.environ["BETTER_CLAUDE_CWD"] = "/repo"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import communicate_mcp


def _instrument():
    captured: list[tuple] = []

    def fake_post(endpoint, payload, timeout):
        captured.append((endpoint, payload, timeout))
        return {"success": True}

    communicate_mcp._post_json = fake_post  # type: ignore[assignment]
    return captured


def test_ask_direct_routes_to_ask_endpoint():
    captured = _instrument()
    res = communicate_mcp.ask_response("worker-1", "hi")
    assert res["success"] is True
    assert captured[0][0] == "/api/internal/ask"
    assert captured[0][1]["ask_id"].startswith("ask_")
    assert captured[0][1]["sender_session_id"] == "mgr-session"
    assert captured[0][1]["target_session_id"] == "worker-1"


def test_ask_fork_routes_to_delegate_engine():
    captured = _instrument()
    res = communicate_mcp.ask_response(
        "worker-1", "audit auth", run_mode="fork",
        worker_description="auditor", worker_registry_cwd="/repo",
    )
    assert res["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/ask-fork"
    assert payload["instructions"] == "audit auth"
    assert payload["worker_session_id"] == "worker-1"
    assert payload["worker_description"] == "auditor"
    assert payload["run_mode"] == "fork"
    assert payload["ephemeral"] is False
    assert payload["app_session_id"] == "mgr-session"  # caller = sender
    assert payload["model"] == "gemini-x"
    assert payload["cwd"] == "/repo"
    assert payload["client_delegation_id"].startswith("del_")
    assert timeout == communicate_mcp._LONG_TIMEOUT


def test_ask_fork_routes_ephemeral():
    captured = _instrument()
    res = communicate_mcp.ask_response(
        "worker-1", "audit auth", run_mode="fork",
        worker_description="auditor", ephemeral=True,
    )
    assert res["success"] is True
    endpoint, payload, _timeout = captured[0]
    assert endpoint == "/api/internal/ask-fork"
    assert payload["ephemeral"] is True


def test_ask_direct_rejects_ephemeral():
    captured = _instrument()
    res = communicate_mcp.ask_response("worker-1", "hi", ephemeral=True)
    assert res["success"] is False
    assert captured == []


def test_ask_fork_allows_missing_worker_description():
    captured = _instrument()
    res = communicate_mcp.ask_response("w", "x", run_mode="fork")
    assert res["success"] is True
    assert captured[0][0] == "/api/internal/ask-fork"
    assert captured[0][1]["worker_description"] == ""


def test_create_worker_routes_to_create_worker_endpoint():
    captured = _instrument()
    res = communicate_mcp.create_worker_response(
        "impl worker", "none fit", "team", node_id="primary",
    )
    assert res["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/create-worker"
    assert payload["worker_description"] == "impl worker"
    assert payload["justification"] == "none fit"
    assert payload["orchestration_mode"] == "team"
    assert payload["app_session_id"] == "mgr-session"
    assert "model" not in payload
    assert payload["cwd"] == "/repo"
    assert payload["client_request_id"].startswith("cw_")
    assert payload["node_id"] == "primary"
    assert timeout == communicate_mcp._LONG_TIMEOUT


def test_create_worker_rejects_missing_fields():
    captured = _instrument()
    res = communicate_mcp.create_worker_response("", "j", "team")
    assert res["success"] is False
    assert captured == []


def test_mssg_still_routes_to_mssg_endpoint():
    captured = _instrument()
    communicate_mcp.mssg_response("w", "hello")
    assert captured[0][0] == "/api/internal/mssg"
    assert captured[0][2] == 30.0


def test_delegate_task_routes_to_delegate_task_endpoint():
    captured = _instrument()
    res = communicate_mcp.delegate_task_response(
        "do the tangent",
        target_session_id="worker-1",
        provider_id="provider-1",
        model="model-1",
        reasoning_effort="high",
        sub_session=False,
    )
    assert res["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/delegate-task"
    assert payload["task"] == "do the tangent"
    assert payload["target_session_id"] == "worker-1"  # explicit bypass
    assert payload["cwd"] == "/repo"
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
    assert payload["sub_session"] is False
    assert timeout == communicate_mcp._LONG_TIMEOUT


def test_delegate_task_auto_routes_without_target():
    captured = _instrument()
    res = communicate_mcp.delegate_task_response("some off-topic task")
    assert res["success"] is True
    endpoint, payload, _timeout = captured[0]
    assert endpoint == "/api/internal/delegate-task"
    assert payload["task"] == "some off-topic task"
    assert payload["target_session_id"] is None  # omitted → router decides
    assert payload["provider_id"] is None
    assert payload["model"] == ""
    assert payload["reasoning_effort"] is None
    assert payload["sub_session"] is True


def test_create_session_routes_to_create_session_endpoint():
    captured = _instrument()
    server = communicate_mcp.build_server()
    assert "complex tasks" in server.instructions
    res = communicate_mcp.create_session_response(
        "scratch",
        orchestration_mode="native",
        provider_id="provider-1",
        model="model-1",
        reasoning_effort="high",
    )
    assert res["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/create-session"
    assert payload["sender_session_id"] == "mgr-session"
    assert payload["name"] == "scratch"
    assert payload["orchestration_mode"] == "native"
    assert payload["cwd"] == "/repo"
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
    assert payload["node_id"] is None


def test_create_session_leaves_selectors_unprovided_by_default():
    captured = _instrument()
    res = communicate_mcp.create_session_response("scratch")
    assert res["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/create-session"
    assert payload["sender_session_id"] == "mgr-session"
    assert payload["provider_id"] is None
    assert payload["model"] == ""
    assert payload["reasoning_effort"] is None


def test_create_session_rejects_empty_name():
    captured = _instrument()
    res = communicate_mcp.create_session_response("")
    assert res["success"] is False
    assert captured == []


def test_create_sub_session_routes_to_create_sub_session_endpoint():
    captured = _instrument()
    res = communicate_mcp.create_sub_session_response(
        description="hidden reviewer",
        provider_id="provider-1",
        model="model-1",
        reasoning_effort="high",
    )
    assert res["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/create-sub-session"
    assert payload["sender_session_id"] == "mgr-session"
    assert "prompt" not in payload
    assert payload["description"] == "hidden reviewer"
    assert payload["cwd"] == "/repo"
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
