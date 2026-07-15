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
    res = communicate_mcp.ask_response("hi", target_session_id="worker-1")
    assert res["success"] is True
    assert captured[0][0] == "/api/internal/ask"
    assert captured[0][1]["ask_id"].startswith("ask_")
    assert captured[0][1]["sender_session_id"] == "mgr-session"
    assert captured[0][1]["target_session_id"] == "worker-1"
    assert captured[0][1]["mode"] == "wait_and_grab_last_assistant_mssg_in_turn"


def test_ask_fork_routes_to_delegate_engine():
    captured = _instrument()
    res = communicate_mcp.ask_response(
        "audit auth", target_session_id="worker-1", run_mode="fork",
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
    # ask-fork fires via the durable job funnel (_post_mcp_job): each
    # individual HTTP round-trip is capped at 30s, not the full poll budget.
    assert timeout == min(30.0, communicate_mcp._LONG_TIMEOUT)


def test_ask_fork_routes_ephemeral():
    captured = _instrument()
    res = communicate_mcp.ask_response(
        "audit auth", target_session_id="worker-1", run_mode="fork",
        worker_description="auditor", ephemeral=True,
    )
    assert res["success"] is True
    endpoint, payload, _timeout = captured[0]
    assert endpoint == "/api/internal/ask-fork"
    assert payload["ephemeral"] is True


def test_ask_direct_rejects_ephemeral():
    captured = _instrument()
    res = communicate_mcp.ask_response("hi", target_session_id="worker-1", ephemeral=True)
    assert res["success"] is False
    assert captured == []


def test_ask_fork_allows_missing_worker_description():
    captured = _instrument()
    res = communicate_mcp.ask_response("x", target_session_id="w", run_mode="fork")
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
    # create_worker now fires via the durable job funnel (_post_mcp_job):
    # each individual HTTP round-trip is capped at 30s, not the full poll
    # budget.
    assert timeout == min(30.0, communicate_mcp._LONG_TIMEOUT)


def test_create_worker_rejects_missing_fields():
    captured = _instrument()
    res = communicate_mcp.create_worker_response("", "j", "team")
    assert res["success"] is False
    assert captured == []


def test_mssg_still_routes_to_mssg_endpoint():
    captured = _instrument()
    communicate_mcp.mssg_response(
        "hello",
        target_session_id="w",
        provider_id="provider-1",
        model="model-1",
        reasoning_effort="high",
    )
    assert captured[0][0] == "/api/internal/mssg"
    assert captured[0][1]["provider_id"] == "provider-1"
    assert captured[0][1]["model"] == "model-1"
    assert captured[0][1]["reasoning_effort"] == "high"
    assert captured[0][2] == 30.0


def test_ask_async_mode_routes_to_ask_endpoint():
    captured = _instrument()
    communicate_mcp.ask_response(
        "run in background",
        target_worker_pool="testape",
        pool_affinity_key="thread-1",
        mode="continue_and_expect_mssg_back_async",
    )
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/ask"
    assert payload["sender_session_id"] == "mgr-session"
    assert payload["target_worker_pool"] == "testape"
    assert payload["pool_affinity_key"] == "thread-1"
    assert payload["message"] == "run in background"
    assert payload["mode"] == "continue_and_expect_mssg_back_async"
    assert payload["provider_id"] is None
    assert payload["model"] == ""
    assert payload["reasoning_effort"] is None
    assert timeout == 30.0


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
    # delegate_task fires via the durable job funnel (_post_mcp_job): each
    # individual HTTP round-trip is capped at 30s, not the full poll budget.
    assert timeout == min(30.0, communicate_mcp._LONG_TIMEOUT)


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


def _instrument_provision():
    captured: list[tuple] = []

    def fake_post(endpoint, payload, timeout):
        captured.append((endpoint, payload, timeout))
        spec = (payload.get("workers") or [{}])[0]
        return {"workers": [{
            "agent_session_id": "worker-session-1",
            "name": f"worker:{spec.get('role_key')}",
            "created": True,
            "orchestration_mode": spec.get("orchestration_mode"),
            "registry_cwd": payload.get("cwd"),
        }]}

    communicate_mcp._post_json = fake_post  # type: ignore[assignment]
    return captured


def test_ensure_named_worker_routes_to_provision_with_singleton_key():
    captured = _instrument_provision()
    res = communicate_mcp.ensure_named_worker_response(
        name="testape",
        cwd="/Users/ofekron/testape",
        orchestration_mode="team",
        provision_prompt="seed",
    )
    assert res["success"] is True
    assert res["agent_session_id"] == "worker-session-1"
    assert res["name"] == "worker:testape"
    assert res["created"] is True
    endpoint, payload, timeout = captured[0]
    # The provision endpoint is the idempotent get-or-create path.
    assert endpoint == "/api/internal/workers/provision"
    assert timeout == communicate_mcp._LONG_TIMEOUT
    spec = payload["workers"][0]
    # role_key=name is the singleton key: provision derives session name
    # `worker:<role_key>`, which the use-testape delegator recursion guard
    # compares against BETTER_CLAUDE_APP_SESSION_ID.
    assert spec["role_key"] == "testape"
    assert spec["orchestration_mode"] == "team"
    assert spec["provision_prompt"] == "seed"
    assert spec["tags"] == ["testape"]
    assert payload["cwd"] == "/Users/ofekron/testape"


def test_ensure_named_worker_drops_empty_optionals():
    captured = _instrument_provision()
    communicate_mcp.ensure_named_worker_response(
        name="testape",
        cwd="/repo",
        orchestration_mode="native",
    )
    spec = captured[0][1]["workers"][0]
    # Empty optionals must NOT be forwarded (provision would treat them as
    # explicit overrides over the creating session's defaults).
    assert "provision_prompt" not in spec
    assert "provider_id" not in spec
    assert "model" not in spec
    assert spec["tags"] == ["testape"]
    assert "reasoning_effort" not in spec
    # description defaults to worker:<name> so the session is named correctly.
    assert spec["description"] == "worker:testape"


def test_ensure_named_worker_rejects_missing_fields():
    captured = _instrument_provision()
    res = communicate_mcp.ensure_named_worker_response("", "/repo", "native")
    assert res["success"] is False
    assert captured == []
