#!/usr/bin/env python3
"""Unit owner for communicate_mcp.py.

communicate_mcp.py has zero pytest-visible importers — it is exercised only by
standalone __main__ e2e scripts (full-stack tier), so it is effectively
pytest-ownerless. This file is its pytest owner.

Strategy:
- HTTP helpers _post_json / _post_mcp_job driven by patching _post_json with a
  recording stub (ready-poll loop exercised directly), and the REAL _post_json
  exercised by patching urllib.request.urlopen.
- *_response tool handlers driven via the _post_json recorder + env vars;
  store-delegating handlers (chat/inbox) driven by patching chat_store /
  inbox_store at the module boundary.
- _safe_result / _specs / build_server / main driven directly with the SDK
  builders patched.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import chat_store
import communicate_mcp
import inbox_store


SENDER = "sender-123"


# ---------- fakes / helpers ----------


def _ready(result):
    return {"ready": True, "result": result}


class FakeResponse:
    """urllib urlopen context manager returning canned JSON bytes."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


@pytest.fixture
def sender_env(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID", SENDER)


@pytest.fixture
def post(monkeypatch):
    """Patch communicate_mcp._post_json with a recording stub.

    `post.calls` records every (endpoint, payload, timeout) call. `post.state`
    is the live dict the recorder reads: set `post.state["queue"]` to a list of
    canned responses returned in order (default sentinel {"ok": True}), and
    `post.state["raise_endpoint"]` to make the next call to that endpoint raise
    (for the _post_mcp_job except branch).
    """
    calls: list[dict] = []
    # `state` is the live dict the recorder closure reads; tests mutate it via
    # post.state (NOT by reassigning an attribute, which would break the link).
    state = {"queue": [], "raise_endpoint": None}

    def _rec(endpoint, payload, timeout):
        calls.append({"endpoint": endpoint, "payload": payload, "timeout": timeout})
        if state["raise_endpoint"] == endpoint:
            state["raise_endpoint"] = None
            raise RuntimeError("boom")
        if state["queue"]:
            return state["queue"].pop(0)
        # Plain sentinel: _post_mcp_job returns it as-is (no ready flag), and
        # direct _post_json callers return it unchanged — so both handler
        # families observe {"ok": True}. The _post_mcp_job ready-unwrap path is
        # exercised by the dedicated _post_mcp_job tests with explicit queues.
        return {"ok": True}

    monkeypatch.setattr(communicate_mcp, "_post_json", _rec)
    return SimpleNamespace(calls=calls, state=state)


# ---------- _env / _env_required ----------


def test_env_legacy_prefix_reads_better_claude(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_MODEL", "  gpt-x  ")
    assert communicate_mcp._env("BETTER_CLAUDE_MODEL") == "gpt-x"


def test_env_legacy_prefix_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("BETTER_AGENT_MODEL", raising=False)
    assert communicate_mcp._env("BETTER_CLAUDE_MODEL", "fallback") == "fallback"


def test_env_plain_name_reads_os_environ(monkeypatch):
    monkeypatch.setenv("PLAIN_VAR", "val")
    assert communicate_mcp._env("PLAIN_VAR") == "val"


def test_env_plain_name_default(monkeypatch):
    monkeypatch.delenv("PLAIN_VAR", raising=False)
    assert communicate_mcp._env("PLAIN_VAR", "d") == "d"


def test_env_required_better_claude_returns_value(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "http://b")
    assert communicate_mcp._env_required("BETTER_CLAUDE_BACKEND_URL") == "http://b"


def test_env_required_better_claude_raises_when_missing(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("BETTER_AGENT_INTERNAL_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        communicate_mcp._env_required("BETTER_CLAUDE_INTERNAL_TOKEN")


def test_env_required_plain_name_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("SOME_PLAIN_REQUIRED", raising=False)
    with pytest.raises(RuntimeError, match="is required"):
        communicate_mcp._env_required("SOME_PLAIN_REQUIRED")


def test_env_required_plain_name_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_PLAIN_REQUIRED", "v")
    assert communicate_mcp._env_required("SOME_PLAIN_REQUIRED") == "v"


# ---------- _disabled_builtin_tools ----------


def test_disabled_builtin_tools_filters_unknown_and_whitespace(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS", " mssg , bogus ,chat,, ")
    assert communicate_mcp._disabled_builtin_tools() == {"mssg", "chat"}


def test_disabled_builtin_tools_empty(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS", raising=False)
    assert communicate_mcp._disabled_builtin_tools() == set()


# ---------- _post_json (real, urlopen patched) ----------


def test_post_json_builds_authed_post_and_decodes_json(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "http://host:9/")
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", "tok")

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["headers"] = dict(req.header_items())
        captured["data"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse({"hello": "world"})

    monkeypatch.setattr(communicate_mcp.urllib.request, "urlopen", fake_urlopen)
    result = communicate_mcp._post_json("/api/x", {"a": 1}, timeout=12.0)
    assert result == {"hello": "world"}
    assert captured["url"] == "http://host:9/api/x"
    assert captured["method"] == "POST"
    assert captured["data"] == {"a": 1}
    assert captured["timeout"] == 12.0
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["x-internal-token"] == "tok"
    assert headers["content-type"] == "application/json"


def test_post_json_requires_backend_url(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_BACKEND_URL", raising=False)
    monkeypatch.delenv("BETTER_AGENT_BACKEND_URL", raising=False)
    with pytest.raises(RuntimeError):
        communicate_mcp._post_json("/api/x", {}, timeout=1.0)


# ---------- _post_mcp_job (poll loop) ----------


def test_post_mcp_job_returns_result_when_ready(post):
    post.state["queue"] = [_ready({"done": True})]
    out = communicate_mcp._post_mcp_job("/api/internal/mssg", "mssg", {"m": 1}, timeout=30.0)
    assert out == {"done": True}
    # first call carries the fire payload with job id + wait
    fire = post.calls[0]
    assert fire["endpoint"] == "/api/internal/mssg"
    assert fire["payload"]["_mcp_job_id"].startswith("mcp_")
    assert fire["payload"]["_mcp_job_wait"] == 0
    assert fire["payload"]["m"] == 1


def test_post_mcp_job_invalid_result_shape_returns_error(post):
    post.state["queue"] = [_ready(["not", "a", "dict"])]
    out = communicate_mcp._post_mcp_job("/e", "op", {}, timeout=30.0)
    assert out == {"success": False, "error": "MCP job returned invalid result"}


def test_post_mcp_job_ready_without_result_returns_response(post):
    post.state["queue"] = [{"ready": True}]
    out = communicate_mcp._post_mcp_job("/e", "op", {}, timeout=30.0)
    assert out == {"ready": True}


def test_post_mcp_job_non_dict_response_returned_as_is(post):
    post.state["queue"] = [["nope"]]
    out = communicate_mcp._post_mcp_job("/e", "op", {}, timeout=30.0)
    assert out == ["nope"]


def test_post_mcp_job_polls_until_ready(post, monkeypatch):
    monkeypatch.setattr(communicate_mcp.time, "sleep", lambda *_: None)
    post.state["queue"] = [{"ready": False}, _ready({"done": True})]
    out = communicate_mcp._post_mcp_job("/api/internal/ask", "ask", {}, timeout=30.0)
    assert out == {"done": True}
    # second call hits the results endpoint
    assert post.calls[1]["endpoint"] == "/api/internal/mcp-jobs/results"
    assert post.calls[1]["payload"]["operation"] == "ask"
    assert post.calls[1]["payload"]["_mcp_job_wait"] > 0


def test_post_mcp_job_deadline_exceeded_returns_pending(post):
    post.state["queue"] = [{"ready": False}]
    out = communicate_mcp._post_mcp_job("/e", "op", {}, timeout=0.0)
    assert out == {"ready": False}
    # only the fire call happened — no results poll
    assert len(post.calls) == 1


def test_post_mcp_job_falls_back_to_results_on_fire_failure(post):
    post.state["raise_endpoint"] = "/api/internal/delegate-task"
    post.state["queue"] = [_ready({"recovered": True})]
    out = communicate_mcp._post_mcp_job("/api/internal/delegate-task", "delegate-task", {}, timeout=30.0)
    assert out == {"recovered": True}
    # fire raised → fallback hit results endpoint
    assert post.calls[1]["endpoint"] == "/api/internal/mcp-jobs/results"


# ---------- _safe_result ----------


def test_safe_result_passes_through_return_value():
    @communicate_mcp._safe_result
    def fn(x):
        return {"value": x}

    assert fn(5) == {"value": 5}
    assert fn.__name__ == "fn"


def test_safe_result_wraps_http_error():
    @communicate_mcp._safe_result
    def fn():
        raise urllib.error.HTTPError("http://x", 503, "Unavailable", {}, io.BytesIO(b""))

    assert fn() == {"success": False, "error": "HTTP 503: Unavailable"}


def test_safe_result_wraps_generic_exception():
    @communicate_mcp._safe_result
    def fn():
        raise ValueError("boom")

    assert fn() == {"success": False, "error": "boom"}


# ---------- _communication_payload / mssg ----------


def test_communication_payload_requires_exactly_one_target(post, sender_env):
    out = communicate_mcp._communication_payload("", "", "", "", "hi")
    assert out["success"] is False


def test_communication_payload_rejects_multiple_targets(post, sender_env):
    out = communicate_mcp._communication_payload("s1", "w1", "", "", "hi")
    assert out["success"] is False


def test_communication_payload_requires_message(post, sender_env):
    out = communicate_mcp._communication_payload("s1", "", "", "", "")
    assert out["success"] is False


def test_communication_payload_happy_normalizes_optionals(post, sender_env):
    out = communicate_mcp._communication_payload("s1", "", "", "", "hi")
    assert out["sender_session_id"] == SENDER
    assert out["target_session_id"] == "s1"
    assert out["message"] == "hi"
    assert out["provider_id"] is None
    assert out["reasoning_effort"] is None
    assert out["runner"] is None
    assert out["queue_turn"] is False


def test_communication_payload_passes_through_set_optionals(post, sender_env):
    out = communicate_mcp._communication_payload(
        "", "wid", "", "aff", "hi",
        provider_id="p", model="m", reasoning_effort="r", runner="ru",
        collapse_key="ck", collapse_policy="cp", queue_turn=True,
    )
    assert out["target_worker_id"] == "wid"
    assert out["pool_affinity_key"] == "aff"
    assert out["provider_id"] == "p"
    assert out["model"] == "m"
    assert out["reasoning_effort"] == "r"
    assert out["runner"] == "ru"
    assert out["collapse_key"] == "ck"
    assert out["collapse_policy"] == "cp"
    assert out["queue_turn"] is True


def test_mssg_response_returns_validation_error(post, sender_env):
    assert communicate_mcp.mssg_response(message="hi")["success"] is False


def test_mssg_response_posts_to_mssg_endpoint(post, sender_env):
    out = communicate_mcp.mssg_response(message="hello", target_session_id="s1")
    assert out == {"ok": True}
    assert post.calls[0]["endpoint"] == "/api/internal/mssg"
    assert post.calls[0]["payload"]["target_session_id"] == "s1"


# ---------- stop_turn / _resolve_cwd ----------


def test_stop_turn_response_requires_target(post, sender_env):
    assert communicate_mcp.stop_turn_response("")["success"] is False


def test_stop_turn_response_posts(post, sender_env):
    out = communicate_mcp.stop_turn_response("s1")
    assert out == {"ok": True}
    call = post.calls[0]
    assert call["endpoint"] == "/api/internal/stop-turn"
    assert call["payload"]["target_session_id"] == "s1"
    assert call["payload"]["caller_session_id"] == SENDER


def test_resolve_cwd_uses_supplied(post, sender_env):
    assert communicate_mcp._resolve_cwd("/path") == "/path"


def test_resolve_cwd_inherits_env_when_blank(monkeypatch, sender_env):
    monkeypatch.setenv("BETTER_CLAUDE_CWD", "/inherited")
    assert communicate_mcp._resolve_cwd("") == "/inherited"


# ---------- delegate_task ----------


def test_delegate_task_requires_task(post, sender_env):
    assert communicate_mcp.delegate_task_response("")["success"] is False


def test_delegate_task_happy_merges_harness_fields(post, sender_env):
    out = communicate_mcp.delegate_task_response("do work", harness_profile_id="prof1")
    assert out == {"ok": True}
    call = post.calls[0]
    assert call["endpoint"] == "/api/internal/delegate-task"
    assert call["payload"]["task"] == "do work"
    assert call["payload"]["sender_session_id"] == SENDER
    assert call["payload"]["harness_profile_id"] == "prof1"
    assert call["payload"]["target_session_id"] is None
    assert call["payload"]["provider_id"] is None
    assert call["payload"]["sub_session"] is True


def test_delegate_task_with_target_and_options(post, sender_env):
    communicate_mcp.delegate_task_response(
        "do work", target_session_id="t1", provider_id="p", model="m",
        reasoning_effort="r", runner="ru", sub_session=False,
        cwd="/c", folder_id="f", tag_ids=["t"], harness_profile_id="h",
    )
    payload = post.calls[0]["payload"]
    assert payload["target_session_id"] == "t1"
    assert payload["provider_id"] == "p"
    assert payload["model"] == "m"
    assert payload["reasoning_effort"] == "r"
    assert payload["runner"] == "ru"
    assert payload["sub_session"] is False
    assert payload["cwd"] == "/c"
    assert payload["folder_id"] == "f"
    assert payload["tag_ids"] == ["t"]


# ---------- ask ----------


def test_ask_requires_target_and_message(post, sender_env):
    assert communicate_mcp.ask_response(message="hi")["success"] is False
    assert communicate_mcp.ask_response(message="", target_session_id="s1")["success"] is False


def test_ask_rejects_invalid_mode(post, sender_env):
    out = communicate_mcp.ask_response(message="hi", target_session_id="s1", mode="bogus")
    assert out["success"] is False
    assert "mode must be" in out["error"]


def test_ask_rejects_invalid_run_mode(post, sender_env):
    out = communicate_mcp.ask_response(message="hi", target_session_id="s1", run_mode="bogus")
    assert out["success"] is False
    assert "run_mode must be" in out["error"]


def test_ask_rejects_async_with_fork(post, sender_env):
    out = communicate_mcp.ask_response(
        message="hi", target_session_id="s1",
        mode="continue_and_expect_inbox_back_async", run_mode="fork",
    )
    assert out["success"] is False
    assert "run_mode='direct'" in out["error"]


def test_ask_fork_requires_target_session_id(post, sender_env):
    out = communicate_mcp.ask_response(
        message="hi", target_worker_id="w1", run_mode="fork",
    )
    assert out["success"] is False
    assert "target_session_id" in out["error"]


def test_ask_ephemeral_requires_fork(post, sender_env):
    out = communicate_mcp.ask_response(
        message="hi", target_session_id="s1", ephemeral=True, run_mode="direct",
    )
    assert out["success"] is False
    assert "run_mode='fork'" in out["error"]


def test_ask_fork_path(post, sender_env):
    out = communicate_mcp.ask_response(
        message="hi", target_session_id="s1", run_mode="fork",
        worker_description="wd", worker_registry_cwd="/rc",
        ephemeral=True, provider_id="p", model="m", reasoning_effort="r", runner="ru",
    )
    assert out == {"ok": True}
    call = post.calls[0]
    assert call["endpoint"] == "/api/internal/ask-fork"
    payload = call["payload"]
    assert payload["worker_session_id"] == "s1"
    assert payload["instructions"] == "hi"
    assert payload["worker_description"] == "wd"
    assert payload["worker_registry_cwd"] == "/rc"
    assert payload["run_mode"] == "fork"
    assert payload["ephemeral"] is True
    assert payload["client_delegation_id"].startswith("del_")
    assert payload["ask_mode"] == "wait_and_grab_last_assistant_mssg_in_turn"


def test_ask_direct_path_posts(post, sender_env):
    out = communicate_mcp.ask_response(message="hi", target_session_id="s1")
    assert out == {"ok": True}
    call = post.calls[0]
    assert call["endpoint"] == "/api/internal/ask"
    # direct path uses the model param verbatim (no env fallback); empty here.
    assert call["payload"]["model"] == ""
    assert call["payload"]["ask_id"].startswith("ask_")
    assert call["payload"]["mode"] == "wait_and_grab_last_assistant_mssg_in_turn"


def test_ask_fork_model_env_fallback(monkeypatch, post, sender_env):
    # selected_model (model param or BETTER_CLAUDE_MODEL) is consumed by the
    # fork path; an empty model param falls back to the env value there.
    monkeypatch.setenv("BETTER_CLAUDE_MODEL", "env-model")
    out = communicate_mcp.ask_response(message="hi", target_session_id="s1", run_mode="fork")
    assert out == {"ok": True}
    assert post.calls[0]["endpoint"] == "/api/internal/ask-fork"
    assert post.calls[0]["payload"]["model"] == "env-model"


# ---------- create_worker ----------


def test_create_worker_requires_all_fields(post, sender_env):
    err = communicate_mcp.create_worker_response("", "j", "team")
    assert err["success"] is False
    err = communicate_mcp.create_worker_response("d", "", "team")
    assert err["success"] is False
    err = communicate_mcp.create_worker_response("d", "j", "")
    assert err["success"] is False


def test_create_worker_rejects_invalid_mode(post, sender_env):
    err = communicate_mcp.create_worker_response("d", "j", "bogus")
    assert err["success"] is False
    assert "team' or 'native" in err["error"]


def test_create_worker_normalizes_manager_to_team(post, sender_env):
    communicate_mcp.create_worker_response("d", "j", "manager")
    assert post.calls[0]["payload"]["orchestration_mode"] == "team"


def test_create_worker_happy(post, sender_env):
    out = communicate_mcp.create_worker_response(
        "d", "j", "native", node_id="n1", cwd="/c", folder_id="f",
        tag_ids=["t"], harness_profile_id="h",
    )
    assert out == {"ok": True}
    payload = post.calls[0]["payload"]
    assert post.calls[0]["endpoint"] == "/api/internal/create-worker"
    assert payload["orchestration_mode"] == "native"
    assert payload["app_session_id"] == SENDER
    assert payload["node_id"] == "n1"
    assert payload["cwd"] == "/c"
    assert payload["client_request_id"].startswith("cw_")
    assert payload["harness_profile_id"] == "h"


# ---------- ensure_named_worker ----------


def test_ensure_named_worker_requires_name_and_mode(post, sender_env):
    assert communicate_mcp.ensure_named_worker_response("", "team")["success"] is False
    assert communicate_mcp.ensure_named_worker_response("n", "")["success"] is False


def test_ensure_named_worker_rejects_invalid_mode(post, sender_env):
    err = communicate_mcp.ensure_named_worker_response("n", "bogus")
    assert err["success"] is False
    assert "team' or 'native" in err["error"]


def test_ensure_named_worker_minimal_spec_defaults_description(post, sender_env):
    communicate_mcp.ensure_named_worker_response("n", "manager")
    payload = post.calls[0]["payload"]
    assert post.calls[0]["endpoint"] == "/api/internal/workers/provision"
    spec = payload["workers"][0]
    assert spec["role_key"] == "n"
    assert spec["description"] == "worker:n"
    assert spec["orchestration_mode"] == "team"
    assert spec["tags"] == ["n"]


def test_ensure_named_worker_full_spec(post, sender_env):
    communicate_mcp.ensure_named_worker_response(
        "n", "native", provision_prompt="pp", description="desc",
        provider_id="p", model="m", reasoning_effort="r", runner="ru",
        node_id="n1", folder_id="f", tag_ids=["t"], harness_profile_id="h",
    )
    spec = post.calls[0]["payload"]["workers"][0]
    assert spec["provision_prompt"] == "pp"
    assert spec["description"] == "desc"
    assert spec["provider_id"] == "p"
    assert spec["model"] == "m"
    assert spec["reasoning_effort"] == "r"
    assert spec["runner"] == "ru"
    assert spec["node_id"] == "n1"
    assert spec["folder_id"] == "f"


def test_ensure_named_worker_no_workers_returned(post, sender_env):
    post.state["queue"] = [{"workers": []}]
    out = communicate_mcp.ensure_named_worker_response("n", "native")
    assert out["success"] is False
    assert "no worker" in out["error"]


def test_ensure_named_worker_projects_first_worker(post, sender_env):
    post.state["queue"] = [{"workers": [{"agent_session_id": "a1", "name": "n", "created": True,
                                "orchestration_mode": "native", "cwd": "/c"}]}]
    out = communicate_mcp.ensure_named_worker_response("n", "native")
    assert out["success"] is True
    assert out["agent_session_id"] == "a1"
    assert out["created"] is True
    assert out["registry_cwd"] == "/c"


def test_ensure_named_worker_registry_cwd_fallback(post, sender_env):
    post.state["queue"] = [{"workers": [{"agent_session_id": "a1", "registry_cwd": "/rc"}]}]
    out = communicate_mcp.ensure_named_worker_response("n", "native")
    assert out["registry_cwd"] == "/rc"


# ---------- create_session / create_sub_session ----------


def test_create_session_requires_name(post, sender_env):
    assert communicate_mcp.create_session_response("")["success"] is False


def test_create_session_rejects_invalid_mode(post, sender_env):
    err = communicate_mcp.create_session_response("n", orchestration_mode="bogus")
    assert err["success"] is False


def test_create_session_manager_normalized(post, sender_env):
    communicate_mcp.create_session_response("n", orchestration_mode="manager")
    assert post.calls[0]["payload"]["orchestration_mode"] == "team"


def test_create_session_happy(post, sender_env):
    out = communicate_mcp.create_session_response(
        "n", node_id="n1", provider_id="p", model="m", reasoning_effort="r",
        runner="ru", cwd="/c", folder_id="f", tag_ids=["t"],
        mcp_servers=["s"], harness_profile_id="h",
    )
    assert out == {"ok": True}
    call = post.calls[0]
    assert call["endpoint"] == "/api/internal/create-session"
    payload = call["payload"]
    assert payload["name"] == "n"
    assert payload["sender_session_id"] == SENDER
    assert payload["node_id"] == "n1"
    assert payload["mcp_servers"] == ["s"]
    assert payload["harness_profile_id"] == "h"


def test_create_sub_session_posts(post, sender_env):
    out = communicate_mcp.create_sub_session_response(
        description="d", node_id="n1", provider_id="p", model="m",
        reasoning_effort="r", runner="ru", cwd="/c", folder_id="f",
        tag_ids=["t"], mcp_servers=["s"], harness_profile_id="h",
    )
    assert out == {"ok": True}
    call = post.calls[0]
    assert call["endpoint"] == "/api/internal/create-sub-session"
    payload = call["payload"]
    assert payload["description"] == "d"
    assert payload["sender_session_id"] == SENDER
    assert payload["node_id"] == "n1"
    assert payload["mcp_servers"] == ["s"]


# ---------- store-delegating handlers ----------


def test_chat_response_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(chat_store, "post_and_read", lambda **kw: {"chat": kw})
    out = communicate_mcp.chat_response("c1", message="m", history_mode="h")
    assert out["chat"]["chat_id"] == "c1"
    assert out["chat"]["reader_id"] == SENDER
    assert out["chat"]["message"] == "m"
    assert out["chat"]["history_mode"] == "h"


def test_inbox_response_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(inbox_store, "post_or_read", lambda **kw: {"inbox": kw})
    out = communicate_mcp.inbox_response(recipient_session_id="r1", message="m")
    assert out["inbox"]["caller_session_id"] == SENDER
    assert out["inbox"]["recipient_session_id"] == "r1"
    assert out["inbox"]["message"] == "m"


def test_read_inbox_history_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(inbox_store, "read_history", lambda **kw: {"hist": kw})
    out = communicate_mcp.read_inbox_history_response(limit=10, before_seq=5)
    assert out["hist"]["recipient_session_id"] == SENDER
    assert out["hist"]["limit"] == 10
    assert out["hist"]["before_seq"] == 5


def test_read_chat_history_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(chat_store, "read_history", lambda **kw: {"hist": kw})
    out = communicate_mcp.read_chat_history_response("c1", limit=5, before_seq=2)
    assert out["hist"]["chat_id"] == "c1"
    assert out["hist"]["limit"] == 5
    assert out["hist"]["before_seq"] == 2


def test_create_chat_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(chat_store, "create_chat", lambda **kw: {"created": kw})
    out = communicate_mcp.create_chat_response(
        "c1", name="n", new_readers_see_history=False,
        sender_policy="allowlist", sender_ids=["s"],
    )
    assert out["created"]["created_by"] == SENDER
    assert out["created"]["chat_id"] == "c1"
    assert out["created"]["name"] == "n"
    assert out["created"]["new_readers_see_history"] is False
    assert out["created"]["sender_policy"] == "allowlist"
    assert out["created"]["sender_ids"] == ["s"]


def test_set_chat_sender_policy_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(chat_store, "set_sender_policy", lambda **kw: {"set": kw})
    out = communicate_mcp.set_chat_sender_policy_response("c1", "open", sender_ids=["s"])
    assert out["set"]["owner_id"] == SENDER
    assert out["set"]["chat_id"] == "c1"
    assert out["set"]["sender_policy"] == "open"
    assert out["set"]["sender_ids"] == ["s"]


def test_delete_chat_delegates(post, sender_env, monkeypatch):
    monkeypatch.setattr(chat_store, "delete_chat", lambda chat_id: {"deleted": chat_id})
    assert communicate_mcp.delete_chat_response("c1") == {"deleted": "c1"}


# ---------- _specs / build_server / main ----------


def test_specs_includes_all_default_names():
    specs = communicate_mcp._specs()
    names = {getattr(s, "name") for s in specs}
    expected = {
        "mssg", "stop_turn", "list_available_provider_models", "chat", "inbox",
        "read_inbox_history", "read_chat_history", "create_chat",
        "set_chat_sender_policy", "delete_chat", "delegate_task",
        "create_session", "create_sub_session", "ask", "ensure_named_worker",
        "create_worker",
    }
    assert names == expected


def test_specs_operation_namespace_and_create_worker_always_present(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS", raising=False)
    specs = communicate_mcp._specs()
    for s in specs:
        assert getattr(s, "operation") == f"runtime_communication_{getattr(s, 'name')}"
    # create_worker is appended unconditionally even though not disableable-filtered
    assert any(getattr(s, "name") == "create_worker" for s in specs)


def test_specs_filters_disabled_but_keeps_create_worker(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS", "mssg,chat,bogus")
    specs = communicate_mcp._specs()
    names = {getattr(s, "name") for s in specs}
    assert "mssg" not in names
    assert "chat" not in names
    assert "ask" in names
    # create_worker bypasses the disabled filter
    assert "create_worker" in names


def test_build_server_calls_sdk_builder(monkeypatch):
    captured = {}

    def fake_build(name, specs, instructions):
        captured["name"] = name
        captured["specs"] = specs
        captured["instructions"] = instructions
        return "SERVER"

    monkeypatch.setattr(communicate_mcp, "build_mcp_server", fake_build)
    assert communicate_mcp.build_server() == "SERVER"
    assert captured["name"] == "communicate"
    assert captured["instructions"] == communicate_mcp._INSTRUCTIONS
    # _specs() wraps handlers fresh each call, so compare by name set + count,
    # not by OperationSpec equality (handler identity differs).
    assert len(captured["specs"]) == len(communicate_mcp._specs())
    assert {getattr(s, "name") for s in captured["specs"]} == {
        getattr(s, "name") for s in communicate_mcp._specs()
    }


def test_main_runs_mcp_or_cli(monkeypatch):
    captured = {}

    def fake_run(name, specs, instructions):
        captured["name"] = name
        captured["specs"] = specs
        captured["instructions"] = instructions
        return 0

    monkeypatch.setattr(communicate_mcp, "run_mcp_or_cli", fake_run)
    assert communicate_mcp.main() == 0
    assert captured["name"] == "communicate"
    assert captured["instructions"] == communicate_mcp._INSTRUCTIONS
