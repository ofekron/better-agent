"""Tests for codex ChatGPT-subscription support in better_agent_runner:
- enforcement points allow kind="codex" + runner="better_agent_runner" +
  mode="subscription" (and keep rejecting kind="openai" subscription, the
  pre-existing regression case)
- ResponsesAPI request building (messages/tools translation)
- SSE response parsing into ba-runner's internal turn representation
- OAuth token refresh (trigger, persistence, fail-closed on error)
- no credential value ever appears in a log record

Uses a temp BETTER_AGENT_HOME (paths.engage_test_home) so no real session or
credential state is touched. All HTTP is mocked via httpx.MockTransport —
no real network calls to chatgpt.com / auth.openai.com.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

_TMP_HOME = tempfile.mkdtemp(prefix="codex_subscription_test_home_")
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)

import paths  # noqa: E402
paths.engage_test_home(_TMP_HOME)

import httpx  # noqa: E402

import config_store  # noqa: E402
import provider  # noqa: E402
import runtime_profile  # noqa: E402
import runner_better_agent_codex_subscription as codex_sub  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _fake_jwt(claims: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return f"{_b64url(header)}.{_b64url(claims)}.sig"


FAKE_ACCESS_TOKEN = "fake-access-token-value-should-never-leak"
FAKE_REFRESH_TOKEN = "fake-refresh-token-value-should-never-leak"


def _fresh_auth_json(*, account_id: str = "acct-123") -> dict:
    exp = int(time.time()) + 3600
    access_token = _fake_jwt({
        "exp": exp,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "plus",
        },
    })
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": access_token,
            "access_token": access_token,
            "refresh_token": FAKE_REFRESH_TOKEN,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).isoformat(),
    }


def _expiring_auth_json() -> dict:
    auth = _fresh_auth_json()
    expired_access = _fake_jwt({
        "exp": int(time.time()) - 10,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
    })
    auth["tokens"]["access_token"] = expired_access
    auth["tokens"]["id_token"] = expired_access
    auth["last_refresh"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    return auth


def _write_auth_json(codex_home: Path, auth: dict) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


# --------------------------------------------------------------------------
# 1. enforcement points
# --------------------------------------------------------------------------

def test_supported_runners_allows_codex_subscription_ba_runner():
    record = {"kind": "codex", "mode": "subscription"}
    assert "better_agent_runner" in runtime_profile.supported_runners(record)


def test_supported_runners_excludes_ba_runner_for_codex_api_key_mode():
    # codex has no api_key mode in practice; the carve-out is subscription-only.
    record = {"kind": "codex", "mode": "api_key"}
    assert "better_agent_runner" not in runtime_profile.supported_runners(record)


def test_reject_unsupported_provider_config_allows_codex_subscription():
    # Must not raise.
    config_store._reject_unsupported_provider_config("codex", "subscription", "better_agent_runner")


def test_reject_unsupported_provider_config_still_rejects_openai_subscription():
    with pytest.raises(ValueError, match="subscription"):
        config_store._reject_unsupported_provider_config("openai", "subscription", "better_agent_runner")


def test_reject_unsupported_provider_config_still_rejects_fugu_subscription():
    with pytest.raises(ValueError, match="subscription"):
        config_store._reject_unsupported_provider_config("fugu", "subscription", "better_agent_runner")


def test_runtime_kind_resolves_codex_subscription_ba_runner_to_openai_class():
    record = {"kind": "codex", "mode": "subscription", "runner": "better_agent_runner"}
    resolved_kind = runtime_profile.runtime_kind(record, "better_agent_runner")
    assert resolved_kind == "openai"
    cls = provider._resolve_class(resolved_kind)
    assert cls.__name__ == "OpenAIProvider"


def test_runtime_kind_native_codex_still_resolves_to_codex_provider():
    record = {"kind": "codex", "mode": "subscription", "runner": "native"}
    resolved_kind = runtime_profile.runtime_kind(record, "native")
    assert resolved_kind == "codex"
    cls = provider._resolve_class(resolved_kind)
    assert cls.__name__ == "CodexProvider"


def test_codex_manifest_spec_exposes_better_agent_runner_choice():
    import provider_manifest
    spec = provider_manifest.spec_for("codex")
    assert "better_agent_runner" in spec.runner_choices
    assert "native" in spec.runner_choices


# --------------------------------------------------------------------------
# 2. request building
# --------------------------------------------------------------------------

def test_messages_to_responses_input_splits_system_as_instructions():
    messages = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file1\nfile2"},
    ]
    instructions, items = codex_sub.messages_to_responses_input(messages)
    assert instructions == "You are a helpful agent."
    assert items[0] == {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "hello"}],
    }
    assert items[1] == {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "hi there"}],
    }
    assert items[2] == {
        "type": "function_call", "call_id": "call_1", "name": "Bash",
        "arguments": '{"command":"ls"}',
    }
    assert items[3] == {
        "type": "function_call_output", "call_id": "call_1", "output": "file1\nfile2",
    }


def test_messages_to_responses_input_translates_image_parts():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]
    _, items = codex_sub.messages_to_responses_input(messages)
    assert items[0]["content"] == [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]


def test_tools_to_responses_tools_flattens_chat_completions_shape():
    tool_schemas = [{
        "type": "function",
        "function": {"name": "Bash", "description": "run a command", "parameters": {"type": "object"}},
    }]
    out = codex_sub.tools_to_responses_tools(tool_schemas)
    assert out == [{
        "type": "function", "name": "Bash", "description": "run a command",
        "parameters": {"type": "object"},
    }]


def test_build_request_body_shape_and_locked_base_url():
    body = codex_sub._build_request_body(
        model="gpt-5.1-codex", messages=[{"role": "system", "content": "sys"}],
        tools=[], reasoning_effort="high", session_id="sess-1",
    )
    assert body["model"] == "gpt-5.1-codex"
    assert body["instructions"] == "sys"
    assert body["input"] == []
    assert body["stream"] is True
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["prompt_cache_key"] == "sess-1"
    assert "tools" not in body
    assert codex_sub.CODEX_SUBSCRIPTION_BASE_URL == "https://chatgpt.com/backend-api/codex"


# --------------------------------------------------------------------------
# 3. SSE response parsing -> internal turn representation
# --------------------------------------------------------------------------

def _sse_body(events: list[dict]) -> bytes:
    lines = []
    for event in events:
        lines.append(f"data: {json.dumps(event)}\n\n")
    return "".join(lines).encode("utf-8")


def _collect_stream(**kwargs) -> list[dict]:
    """Run `codex_sub.stream_chat(**kwargs)` to completion on a private event
    loop and return the collected chunks. Matches this codebase's
    `asyncio.run(...)`-in-a-sync-test convention (no pytest-asyncio
    dependency — see scripts/test_openai_provider.py)."""
    async def _go() -> list[dict]:
        return [chunk async for chunk in codex_sub.stream_chat(**kwargs)]
    return asyncio.run(_go())


_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    # Must build on the ORIGINAL AsyncClient, not `httpx.AsyncClient` — that
    # attribute is what gets monkeypatched to this factory's return value,
    # so calling through it here would recurse into itself.
    return lambda *a, **k: _RealAsyncClient(*a, transport=httpx.MockTransport(handler), **k)


def test_stream_chat_parses_text_and_tool_call_and_completion(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _fresh_auth_json())

    sse_events = [
        {"type": "response.created", "response": {"id": "resp1"}},
        {"type": "response.output_text.delta", "delta": "Hello "},
        {"type": "response.output_text.delta", "delta": "world"},
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call_abc", "name": "Bash",
            "arguments": '{"command":"echo hi"}',
        }},
        {"type": "response.completed", "response": {
            "id": "resp1",
            "usage": {
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 2},
            },
        }},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://chatgpt.com/backend-api/codex/responses"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["chatgpt-account-id"] == "acct-123"
        assert request.headers["originator"] == "codex_cli_rs"
        return httpx.Response(200, content=_sse_body(sse_events),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    chunks = _collect_stream(
        codex_home=codex_home, model="gpt-5.1-codex",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=[], reasoning_effort=None, session_id="sess-1",
    )

    text_deltas = [
        c["choices"][0]["delta"].get("content")
        for c in chunks
        if c["choices"][0]["delta"].get("content")
    ]
    assert "".join(text_deltas) == "Hello world"

    tool_call_chunks = [
        c for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_call_chunks) == 1
    tc = tool_call_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "Bash"
    assert tc["function"]["arguments"] == '{"command":"echo hi"}'

    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"]["prompt_tokens"] == 10
    assert final["usage"]["completion_tokens"] == 5
    assert final["usage"]["prompt_tokens_details"]["cached_tokens"] == 2


def test_stream_chat_skips_unrecognized_events_without_crashing(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _fresh_auth_json())

    sse_events = [
        {"type": "response.some_future_event_we_have_never_seen", "foo": "bar"},
        {"type": "response.completed", "response": {"id": "resp1"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(sse_events),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    chunks = _collect_stream(
        codex_home=codex_home, model="gpt-5.1-codex",
        messages=[{"role": "system", "content": "sys"}], tools=[],
        reasoning_effort=None, session_id="sess-1",
    )
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_chat_raises_when_no_terminal_event_ever_arrives(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _fresh_auth_json())

    sse_events = [{"type": "response.output_text.delta", "delta": "partial"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(sse_events),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(RuntimeError, match="without a recognized completion"):
        _collect_stream(
            codex_home=codex_home, model="gpt-5.1-codex",
            messages=[{"role": "system", "content": "sys"}], tools=[],
            reasoning_effort=None, session_id="sess-1",
        )


def test_stream_chat_raises_on_response_failed(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _fresh_auth_json())

    sse_events = [
        {"type": "response.failed", "response": {
            "id": "resp1", "error": {"code": "rate_limit_exceeded", "message": "slow down"},
        }},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(sse_events),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    with pytest.raises(RuntimeError, match="slow down"):
        _collect_stream(
            codex_home=codex_home, model="gpt-5.1-codex",
            messages=[{"role": "system", "content": "sys"}], tools=[],
            reasoning_effort=None, session_id="sess-1",
        )


# --------------------------------------------------------------------------
# 4. token refresh
# --------------------------------------------------------------------------

def test_ensure_fresh_credentials_returns_tokens_without_refresh_when_fresh(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _fresh_auth_json())

    def _unexpected_refresh(*a, **k):
        raise AssertionError("refresh should not be called for a fresh token")

    monkeypatch.setattr(codex_sub, "_refresh_tokens", _unexpected_refresh)
    access_token, account_id = codex_sub.ensure_fresh_credentials(codex_home)
    assert access_token
    assert account_id == "acct-123"


def test_ensure_fresh_credentials_refreshes_expiring_token_and_persists(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _expiring_auth_json())

    new_access = _fake_jwt({"exp": int(time.time()) + 7200,
                             "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}})
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        assert url == "https://auth.openai.com/oauth/token"
        assert json["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
        assert json["grant_type"] == "refresh_token"
        assert json["refresh_token"] == FAKE_REFRESH_TOKEN

        class _Resp:
            status_code = 200

            def json(self):
                return {"access_token": new_access, "refresh_token": "new-refresh-token"}
        return _Resp()

    monkeypatch.setattr(codex_sub.httpx, "post", fake_post)
    access_token, account_id = codex_sub.ensure_fresh_credentials(codex_home)
    assert access_token == new_access
    assert account_id == "acct-123"
    assert len(calls) == 1

    persisted = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert persisted["tokens"]["access_token"] == new_access
    assert persisted["tokens"]["refresh_token"] == "new-refresh-token"
    # id_token was not present in the refresh response -> preserved, not cleared.
    assert persisted["tokens"]["id_token"]


def test_ensure_fresh_credentials_fails_closed_on_refresh_error(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    original = _expiring_auth_json()
    _write_auth_json(codex_home, original)

    def fake_post(url, json=None, headers=None, timeout=None):
        class _Resp:
            status_code = 401

            def json(self):
                raise AssertionError("should not be called")
        return _Resp()

    monkeypatch.setattr(codex_sub.httpx, "post", fake_post)
    with pytest.raises(codex_sub.CodexSubscriptionAuthError):
        codex_sub.ensure_fresh_credentials(codex_home)

    # Fail closed: auth.json must be untouched, never partially written.
    on_disk = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert on_disk == original


def test_load_auth_rejects_non_chatgpt_auth_mode(tmp_path):
    codex_home = tmp_path / "codex_home"
    auth = _fresh_auth_json()
    auth["auth_mode"] = "apikey"
    _write_auth_json(codex_home, auth)
    with pytest.raises(codex_sub.CodexSubscriptionAuthError, match="ChatGPT subscription"):
        codex_sub.load_auth(codex_home)


def test_load_auth_rejects_missing_file(tmp_path):
    codex_home = tmp_path / "does_not_exist"
    with pytest.raises(codex_sub.CodexSubscriptionAuthError):
        codex_sub.load_auth(codex_home)


# --------------------------------------------------------------------------
# 5. no secret leakage
# --------------------------------------------------------------------------

def test_refresh_failure_message_never_contains_tokens(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _expiring_auth_json())

    def fake_post(url, json=None, headers=None, timeout=None):
        class _Resp:
            status_code = 400

            def json(self):
                return {"error": "invalid_grant"}
        return _Resp()

    monkeypatch.setattr(codex_sub.httpx, "post", fake_post)
    with pytest.raises(codex_sub.CodexSubscriptionAuthError) as excinfo:
        codex_sub.ensure_fresh_credentials(codex_home)
    assert FAKE_ACCESS_TOKEN not in str(excinfo.value)
    assert FAKE_REFRESH_TOKEN not in str(excinfo.value)


def test_no_credential_value_in_log_records(tmp_path, monkeypatch, caplog):
    codex_home = tmp_path / "codex_home"
    _write_auth_json(codex_home, _fresh_auth_json())

    sse_events = [
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "response.completed", "response": {"id": "resp1"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(sse_events),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))

    async def _drive():
        async for _ in codex_sub.stream_chat(
            codex_home=codex_home, model="gpt-5.1-codex",
            messages=[{"role": "system", "content": "sys"}], tools=[],
            reasoning_effort=None, session_id="sess-1",
        ):
            pass

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_drive())

    access_token = json.loads((codex_home / "auth.json").read_text())["tokens"]["access_token"]
    for record in caplog.records:
        message = record.getMessage()
        assert access_token not in message
        assert FAKE_REFRESH_TOKEN not in message
