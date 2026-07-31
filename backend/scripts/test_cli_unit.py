"""Unit-tier pytest coverage for the ``cli`` driver.

The existing ``test_cli_*.py`` files are standalone ``__main__`` scripts, so
pytest never collects their assertions — cli.py sat at 14% line coverage.
These tests exercise the renderers, formatting helpers, HTTP/session helpers,
provider/session resolution, ClientBackend, login/logout, and the turn driver
as pure unit tests (network/keychain/getpass stubbed at the seam).

Run with:
    cd backend && env -u PYTHONPATH .venv/bin/python -m pytest \
        scripts/test_cli_unit.py --cov=cli
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_test_home.isolate("bc-cli-unit-")

import cli  # noqa: E402
import urllib.error  # noqa: E402


pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_auth_token():
    original = cli._AUTH_TOKEN
    cli._AUTH_TOKEN = None
    yield
    cli._AUTH_TOKEN = original


class _FakeResp:
    """Minimal context-manager urlopen response."""

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _set_urlopen(monkeypatch, behavior):
    """Install a fake urlopen. ``behavior`` is a callable(Request) -> _FakeResp
    or a list of such callables consumed in order, or an Exception to raise."""

    def fake_urlopen(req, timeout=None):
        if isinstance(behavior, list):
            step = behavior.pop(0)
        else:
            step = behavior
        if isinstance(step, BaseException):
            raise step
        return step(req) if callable(step) else step

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def test_fmt_tokens_empty_and_missing():
    assert cli._fmt_tokens(None) == "-"
    assert cli._fmt_tokens({}) == "-"
    assert cli._fmt_tokens({"input_tokens": 0, "output_tokens": 0}) == "-"


def test_fmt_tokens_in_out_only():
    assert cli._fmt_tokens({"input_tokens": 1000, "output_tokens": 5}) == "1,000 in / 5 out"


def test_fmt_tokens_with_cache():
    out = cli._fmt_tokens(
        {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}
    )
    assert "cache: 7 read / 3 write" in out


def test_fmt_tokens_none_values_treated_as_zero():
    # cache fields None -> treated as 0, no cache part.
    assert cli._fmt_tokens({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": None}) == "1 in / 1 out"


def test_truncate_short_kept_and_newlines_collapsed():
    assert cli._truncate("hello") == "hello"
    assert cli._truncate("a\nb\nc") == "a b c"
    long = "x" * 200
    assert cli._truncate(long, maxlen=10).endswith("...")
    assert len(cli._truncate(long, maxlen=10)) == 10


def test_fmt_args_non_dict():
    assert cli._fmt_args("whatever") == "whatever"


def test_fmt_args_prefers_informative_field():
    assert cli._fmt_args({"file_path": "/a/b.py", "extra": "z"}) == "file_path=/a/b.py"
    assert cli._fmt_args({"command": "ls -la"}) == "command=ls -la"
    assert cli._fmt_args({"prompt": "hi", "file_path": ""}) == "prompt=hi"


def test_fmt_args_falls_back_to_json():
    out = cli._fmt_args({"weird": [1, 2], "n": 3})
    assert "weird" in out and "n" in out


def test_fmt_args_json_failure_falls_back_to_str(monkeypatch):
    def boom(*a, **k):
        raise ValueError("nope")

    monkeypatch.setattr(cli.json, "dumps", boom)
    assert cli._fmt_args({"x": 1}) == "{'x': 1}"


# --------------------------------------------------------------------------- #
# Auth / token helpers
# --------------------------------------------------------------------------- #
def test_auth_headers_no_token():
    assert cli._auth_headers() == {}
    assert cli._auth_headers({"X": "y"}) == {"X": "y"}


def test_auth_headers_with_token():
    cli._AUTH_TOKEN = "abc"
    assert cli._auth_headers()["Authorization"] == "Bearer abc"
    extra = cli._auth_headers({"Content-Type": "application/json"})
    assert extra["Authorization"] == "Bearer abc"
    assert extra["Content-Type"] == "application/json"


def test_cli_token_account_scoped_by_port():
    assert cli._cli_token_account(18765) == "cli_token:18765"


def test_ws_chat_url_with_and_without_token():
    cli._AUTH_TOKEN = "tok en"
    url = cli._ws_chat_url(8000)
    assert url.startswith("ws://127.0.0.1:8000/ws/chat?token=")
    assert "tok%20en" in url  # quoted
    cli._AUTH_TOKEN = None
    assert cli._ws_chat_url(8000) == "ws://127.0.0.1:8000/ws/chat"


def test_load_stored_token_strips_trailing_newline(monkeypatch):
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: "secret\n")
    assert cli._load_stored_token(8000) == "secret"
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: None)
    assert cli._load_stored_token(8000) is None


def test_load_stored_token_swallows_runtime_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(cli.oskeychain, "get", boom)
    assert cli._load_stored_token(8000) is None


def test_store_and_delete_token_delegate_to_oskeychain(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.oskeychain, "store", lambda *a: calls.append(("store", a)))
    monkeypatch.setattr(cli.oskeychain, "delete", lambda *a: calls.append(("delete", a)))
    cli._store_token(8000, "tok")
    cli._delete_stored_token(8000)
    assert len(calls) == 2


def test_disable_colors_zeros_globals():
    cli._disable_colors()
    for name in ("DIM", "BOLD", "ITALIC", "RESET", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN"):
        assert getattr(cli, name) == ""


# --------------------------------------------------------------------------- #
# Renderers (pure event -> stdout)
# --------------------------------------------------------------------------- #
def test_json_renderer_emits_jsonl(capsys):
    cli.JsonRenderer().handle({"type": "turn_start", "data": {"x": 1}})
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"type": "turn_start", "data": {"x": 1}}


def test_pretty_renderer_unknown_event_swallowed(capsys):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r.handle({"type": "totally_unknown"})
    assert capsys.readouterr().out == ""


def test_pretty_renderer_lifecycle_events(capsys):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r.turn_started("p")
    r.handle({"type": "turn_start", "data": {"manager_session_id": "abcdef1234"}})
    r.handle({"type": "session_renamed", "data": {"name": "my session"}})
    r.handle({"type": "worker_creation_requested", "data": {"proposed_description": "researcher"}})
    r.handle({"type": "turn_stopped", "data": {}})
    r.handle({"type": "error", "data": {"error": "boom"}})
    r.turn_finished()
    out = capsys.readouterr().out
    assert "turn" in out
    assert "abcdef12" in out  # session id truncated to 8
    assert "session renamed to \"my session\"" in out
    assert "auto-approved" in out and "researcher" in out
    assert "stopped" in out
    assert "error: boom" in out


def test_pretty_renderer_turn_complete_is_quiet(capsys):
    # NOTE: PrettyRenderer.handle has two `elif etype == "turn_complete"`
    # branches. The first (line 241) is an intentional `pass` that always
    # wins, so the footer-rendering duplicate at line 265 is UNREACHABLE dead
    # code (a real source defect — filed as a separate card, not fixed here).
    # Actual behavior: a bare turn_complete event renders nothing.
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r.handle({
        "type": "turn_complete",
        "data": {"total_token_usage": {"input_tokens": 5, "output_tokens": 5}},
    })
    assert capsys.readouterr().out == ""


def test_pretty_renderer_worker_events(capsys):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r.handle({
        "type": "worker_start",
        "data": {"worker_description": "helper", "worker_session_id": "deadbeef9", "is_new": True},
    })
    r.handle({
        "type": "worker_complete",
        "data": {"success": False, "error": "denied", "token_usage": {"input_tokens": 3, "output_tokens": 1}},
    })
    out = capsys.readouterr().out
    assert "worker" in out and "new" in out
    assert "worker done" in out
    assert "3 in / 1 out" in out
    assert "denied" in out


def test_pretty_renderer_worker_resumed_tag(capsys):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r.handle({
        "type": "worker_complete",
        "data": {"success": True, "worker_session_id": "ffff", "token_usage": None},
    })
    out = capsys.readouterr().out
    assert "worker done" in out
    # token_usage None renders as "-"
    assert "-" in out


def test_pretty_renderer_inner_text_thinking_tool():
    cli._disable_colors()
    r = cli.PrettyRenderer()
    # Capture stdout via a StringIO since handle writes through sys.stdout.
    buf = io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "stdout", buf)
    try:
        r._render_inner(
            {
                "type": "output",
                "data": {"output": "hello world"},
            },
            indent="",
        )
        r._render_inner({"type": "thinking", "data": {"thought": "deep"}}, indent="")
        r._render_inner({"type": "tool_call", "data": {"tool": "bash", "args": {"command": "ls"}}}, indent="")
        r._render_inner({"type": "error", "data": {"error": "bad"}}, indent="")
        r._render_inner({"type": "session_discovered", "data": {}}, indent="")
        r._render_inner({"type": "complete", "data": {}}, indent="")
    finally:
        monkey.undo()
    out = buf.getvalue()
    assert "hello world" in out
    assert "deep" in out  # thinking truncated
    assert "bash" in out and "command=ls" in out
    assert "error: bad" in out


def test_pretty_renderer_inner_output_empty_skipped():
    cli._disable_colors()
    r = cli.PrettyRenderer()
    buf = io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "stdout", buf)
    try:
        r._render_inner({"type": "output", "data": {"output": ""}}, indent="")
        r._render_inner({"type": "thinking", "data": {"thought": ""}}, indent="")
        r._render_inner({"type": "output", "data": {}}, indent="")
    finally:
        monkey.undo()
    assert buf.getvalue() == ""


def test_pretty_renderer_inner_indented_output_prefixes_lines():
    cli._disable_colors()
    r = cli.PrettyRenderer()
    buf = io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "stdout", buf)
    try:
        r._render_inner({"type": "output", "data": {"output": "line1\nline2"}}, indent="    ")
    finally:
        monkey.undo()
    out = buf.getvalue()
    # Each continuation line gets the indent.
    assert "    line1" in out or out.startswith("    ")
    assert "\n    line2" in out


def test_pretty_renderer_agent_message_text_thinking_tool():
    cli._disable_colors()
    r = cli.PrettyRenderer()
    buf = io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "stdout", buf)
    try:
        r._render_inner(
            {
                "type": "agent_message",
                "data": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "answer"},
                            {"type": "thinking", "thinking": "hmm"},
                            {"type": "tool_use", "name": "mcp__foo__delegate", "input": {"prompt": "x"}},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                            {"type": "text", "text": ""},
                        ]
                    },
                },
            },
            indent="",
        )
    finally:
        monkey.undo()
    out = buf.getvalue()
    assert "answer" in out
    assert "hmm" in out
    # delegate tool name is simplified to "delegate"
    assert "delegate" in out and "mcp__foo__delegate" not in out
    assert "Bash" in out and "command=pwd" in out


def test_pretty_renderer_agent_message_non_assistant_skipped():
    cli._disable_colors()
    r = cli.PrettyRenderer()
    buf = io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "stdout", buf)
    try:
        r._render_inner({"type": "agent_message", "data": {"type": "user"}}, indent="")
        r._render_inner(
            {"type": "agent_message", "data": {"type": "assistant", "message": {"content": "not-a-list"}}},
            indent="",
        )
        r._render_inner(
            {
                "type": "agent_message",
                "data": {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}},
            },
            indent="",
        )
    finally:
        monkey.undo()
    assert buf.getvalue() == ""


def test_pretty_renderer_turn_finished_flushes_inline(capsys):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r._writeinline("partial")
    assert not r._at_line_start
    r.turn_finished()
    assert capsys.readouterr().out.endswith("\n")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def test_probe_backend_true_on_200(monkeypatch):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, b"{}"))
    assert cli._probe_backend(8000, retries=1) is True


def test_probe_backend_false_on_401(monkeypatch):
    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    _set_urlopen(monkeypatch, behavior)
    assert cli._probe_backend(8000, retries=2) is False


def test_probe_backend_none_when_unreachable(monkeypatch):
    def behavior(req):
        raise urllib.error.URLError("conn refused")

    _set_urlopen(monkeypatch, behavior)
    # retries=1 avoids the inline time.sleep retry-delay entirely.
    assert cli._probe_backend(8000, retries=1) is None


def test_probe_backend_500_caught_as_unreachable(monkeypatch):
    # A non-404/non-401 HTTPError is re-raised by the inner handler but caught
    # by the outer except -> treated like an unreachable backend (None).
    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    _set_urlopen(monkeypatch, behavior)
    assert cli._probe_backend(8000, retries=1) is None


def test_fetch_backend_session_happy(monkeypatch):
    body = json.dumps({"id": "s1", "cwd": "/x"}).encode()
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, body))
    assert cli._fetch_backend_session(8000, "s1") == {"id": "s1", "cwd": "/x"}


def test_fetch_backend_session_non_200_returns_none(monkeypatch):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(404, b"{}"))
    assert cli._fetch_backend_session(8000, "missing") is None


def test_fetch_backend_session_swallows_errors(monkeypatch):
    _set_urlopen(monkeypatch, urllib.error.URLError("down"))
    assert cli._fetch_backend_session(8000, "s1") is None
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, b"not json"))
    assert cli._fetch_backend_session(8000, "s1") is None


def test_request_json_happy(monkeypatch):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, json.dumps({"ok": 1}).encode()))
    assert cli._request_json(8000, "/api/x") == {"ok": 1}


def test_request_json_empty_body_returns_empty_dict(monkeypatch):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, b""))
    assert cli._request_json(8000, "/api/x") == {}


def test_request_json_post_sends_body(monkeypatch):
    captured = {}

    def behavior(req):
        captured["data"] = req.data
        captured["method"] = req.method
        captured["ctype"] = req.headers.get("Content-type", "")
        return _FakeResp(200, b"{}")

    _set_urlopen(monkeypatch, behavior)
    cli._request_json(8000, "/api/x", method="POST", body={"a": 1})
    assert json.loads(captured["data"]) == {"a": 1}
    assert captured["method"] == "POST"
    assert "application/json" in captured["ctype"]


def test_request_json_http_error_with_detail(monkeypatch):
    def behavior(req):
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {}, io.BytesIO(json.dumps({"detail": "nope"}).encode())
        )

    _set_urlopen(monkeypatch, behavior)
    with pytest.raises(SystemExit, match="nope"):
        cli._request_json(8000, "/api/x")


def test_request_json_http_error_without_detail(monkeypatch):
    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 500, "bad", {}, io.BytesIO(b"garbage"))

    _set_urlopen(monkeypatch, behavior)
    with pytest.raises(SystemExit, match="backend"):
        cli._request_json(8000, "/api/x")


def test_request_json_url_error(monkeypatch):
    _set_urlopen(monkeypatch, urllib.error.URLError("down"))
    with pytest.raises(SystemExit, match="backend"):
        cli._request_json(8000, "/api/x")


def test_list_backend_sessions(monkeypatch):
    _set_urlopen(
        monkeypatch,
        lambda req: _FakeResp(200, json.dumps({"sessions": [{"id": "a"}, {"id": "b"}]}).encode()),
    )
    assert cli._list_backend_sessions(8000) == [{"id": "a"}, {"id": "b"}]


def test_list_backend_sessions_missing_key(monkeypatch):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, json.dumps({}).encode()))
    assert cli._list_backend_sessions(8000) == []


def test_create_backend_session_body(monkeypatch):
    captured = {}

    def behavior(req):
        captured["body"] = json.loads(req.data)
        return _FakeResp(200, json.dumps({"id": "new"}).encode())

    _set_urlopen(monkeypatch, behavior)
    result = cli._create_backend_session(
        port=8000, cwd="/r", model="m", mode="native", provider_id="p",
        node_id="primary", worker_creation_policy="deny", bare_config=True,
        harness_profile_id="hp1",
    )
    assert result == {"id": "new"}
    body = captured["body"]
    assert body["cwd"] == "/r" and body["bare_config"] is True
    assert body["harness_profile_id"] == "hp1"
    assert body["worker_creation_policy"] == "deny"


def test_create_backend_session_defaults(monkeypatch):
    captured = {}

    def behavior(req):
        captured["body"] = json.loads(req.data)
        return _FakeResp(200, b"{}")

    _set_urlopen(monkeypatch, behavior)
    cli._create_backend_session(
        port=8000, cwd="/r", model="m", mode=None, provider_id=None,
        node_id="primary", worker_creation_policy=None, bare_config=False,
    )
    body = captured["body"]
    assert body["orchestration_mode"] == "team"
    assert body["worker_creation_policy"] == "ask"
    assert "harness_profile_id" not in body


def test_apply_harness_profile(monkeypatch):
    captured = {}

    def behavior(req):
        captured["method"] = req.method
        captured["body"] = json.loads(req.data)
        return _FakeResp(200, json.dumps({"id": "s", "harness_profile_id": "hp"}).encode())

    _set_urlopen(monkeypatch, behavior)
    out = cli._apply_harness_profile(8000, "s", "hp")
    assert out["harness_profile_id"] == "hp"
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"harness_profile_id": "hp"}


# --------------------------------------------------------------------------- #
# resolve_provider + resolve_backend_session
# --------------------------------------------------------------------------- #
def test_resolve_provider_none_when_blank():
    assert cli.resolve_provider(None) is None
    assert cli.resolve_provider("") is None


def test_resolve_provider_by_id():
    provider = {"id": "pid", "name": "Z.AI", "kind": "claude"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli.config_store, "list_providers", lambda: {"providers": [provider]})
    try:
        assert cli.resolve_provider("pid")["id"] == "pid"
        # by unique name (case-insensitive)
        assert cli.resolve_provider("z.ai")["id"] == "pid"
    finally:
        monkey.undo()


def test_resolve_provider_ambiguous_name():
    providers = [
        {"id": "a", "name": "Same"},
        {"id": "b", "name": "Same"},
    ]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli.config_store, "list_providers", lambda: {"providers": providers})
    try:
        with pytest.raises(SystemExit, match="ambiguous"):
            cli.resolve_provider("Same")
    finally:
        monkey.undo()


def test_resolve_provider_not_found():
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli.config_store, "list_providers", lambda: {"providers": []})
    try:
        with pytest.raises(SystemExit, match="not found"):
            cli.resolve_provider("ghost")
    finally:
        monkey.undo()


def test_resolve_backend_session_resume_not_found(monkeypatch):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(404, b"{}"))
    with pytest.raises(SystemExit, match="not found"):
        cli.resolve_backend_session(
            port=8000, session_id="ghost", cwd=None, model="m", mode=None, provider_id=None,
        )


def test_resolve_backend_session_resume_provider_mismatch(monkeypatch):
    _set_urlopen(
        monkeypatch,
        lambda req: _FakeResp(200, json.dumps({"id": "s", "provider_id": "a", "cwd": "/x", "node_id": "primary"}).encode()),
    )
    with pytest.raises(SystemExit, match="--provider does not match"):
        cli.resolve_backend_session(
            port=8000, session_id="s", cwd=None, model="m", mode=None, provider_id="b",
        )


def test_resolve_backend_session_resume_node_mismatch(monkeypatch):
    _set_urlopen(
        monkeypatch,
        lambda req: _FakeResp(200, json.dumps({"id": "s", "node_id": "primary", "cwd": "/x"}).encode()),
    )
    with pytest.raises(SystemExit, match="--node does not match"):
        cli.resolve_backend_session(
            port=8000, session_id="s", cwd=None, model="m", mode=None, provider_id=None, node_id="worker-1",
        )


def test_resolve_backend_session_resume_cwd_mismatch(monkeypatch):
    _set_urlopen(
        monkeypatch,
        lambda req: _FakeResp(200, json.dumps({"id": "s", "cwd": "/x", "node_id": "primary"}).encode()),
    )
    with pytest.raises(SystemExit, match="--cwd does not match"):
        cli.resolve_backend_session(
            port=8000, session_id="s", cwd="/y", model="m", mode=None, provider_id=None,
        )


def test_resolve_backend_session_resume_applies_harness_profile(monkeypatch):
    calls = []

    def behavior(req):
        calls.append(req.full_url)
        if req.full_url.endswith("/harness-profile"):
            return _FakeResp(200, json.dumps({"id": "s", "harness_profile_id": "hp"}).encode())
        return _FakeResp(
            200,
            json.dumps({"id": "s", "cwd": "/x", "node_id": "primary", "harness_profile_id": "old"}).encode(),
        )

    _set_urlopen(monkeypatch, behavior)
    session = cli.resolve_backend_session(
        port=8000, session_id="s", cwd=None, model="m", mode=None, provider_id=None,
        harness_profile_id="hp",
    )
    assert session["harness_profile_id"] == "hp"
    assert any(u.endswith("/harness-profile") for u in calls)


def test_resolve_backend_session_reuses_existing_cli_default(monkeypatch):
    # First call lists sessions (one cli-default match), then fetches it.
    seq = [
        lambda req: _FakeResp(200, json.dumps({"sessions": [{"id": "exist", "name": "cli-default", "cwd": "/r", "node_id": "primary"}]}).encode()),
        lambda req: _FakeResp(200, json.dumps({"id": "exist", "cwd": "/r", "node_id": "primary"}).encode()),
    ]
    _set_urlopen(monkeypatch, seq)
    session = cli.resolve_backend_session(
        port=8000, session_id=None, cwd="/r", model="m", mode=None, provider_id=None,
    )
    assert session["id"] == "exist"


def test_resolve_backend_session_creates_when_no_match(monkeypatch):
    seq = [
        lambda req: _FakeResp(200, json.dumps({"sessions": []}).encode()),
        lambda req: _FakeResp(200, json.dumps({"id": "fresh", "cwd": "/r"}).encode()),
    ]
    _set_urlopen(monkeypatch, seq)
    session = cli.resolve_backend_session(
        port=8000, session_id=None, cwd="/r", model="m", mode=None, provider_id=None,
    )
    assert session["id"] == "fresh"


# --------------------------------------------------------------------------- #
# ClientBackend
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)

    async def recv(self):
        if not self._incoming:
            raise RuntimeError("no more messages")
        return self._incoming.pop(0)

    async def close(self):
        self._incoming = []


async def test_client_backend_send_prompt_streams_until_terminal():
    backend = cli.ClientBackend(8000)
    backend._ws = _FakeWS([
        json.dumps({"type": "manager_event", "data": {}}),
        json.dumps({"type": "turn_complete", "data": {}}),
        # a malformed frame after terminal is never read
    ])
    cli._disable_colors()
    terminal = await backend.send_prompt(
        prompt="hi", session={"id": "s"}, model="m", cwd="/r", mode="team",
        renderer=cli.JsonRenderer(),
    )
    assert terminal == "turn_complete"
    sent = json.loads(backend._ws.sent[0])
    assert sent["prompt"] == "hi"
    assert sent["app_session_id"] == "s"


async def test_client_backend_send_prompt_handles_garbage_frame():
    backend = cli.ClientBackend(8000)
    backend._ws = _FakeWS([
        "not-json",
        json.dumps({"type": "error", "data": {"error": "x"}}),
    ])
    cli._disable_colors()
    terminal = await backend.send_prompt(
        prompt="hi", session={"id": "s"}, model="m", cwd="/r", mode="team",
        renderer=cli.JsonRenderer(),
    )
    assert terminal == "error"


async def test_client_backend_send_prompt_requires_start():
    backend = cli.ClientBackend(8000)
    with pytest.raises(AssertionError):
        await backend.send_prompt(
            prompt="hi", session={"id": "s"}, model="m", cwd="/r", mode="team",
            renderer=cli.JsonRenderer(),
        )


async def test_client_backend_cancel_and_close_swallows_errors():
    backend = cli.ClientBackend(8000)
    # No ws set -> cancel/close are no-ops.
    await backend.cancel("s")
    await backend.close()
    # With a ws that errors on send -> swallowed.
    backend._ws = _FakeWS([])
    # _FakeWS.send appends; force close to raise by replacing.
    await backend.cancel("s")


def test_client_backend_set_worker_creation_policy(monkeypatch):
    captured = {}

    def behavior(req):
        captured["method"] = req.method
        captured["data"] = json.loads(req.data)
        captured["url"] = req.full_url
        return _FakeResp(200, b"{}")

    _set_urlopen(monkeypatch, behavior)
    backend = cli.ClientBackend(8000)
    backend.set_worker_creation_policy("sid", "approve")
    assert captured["method"] == "PUT"
    assert captured["data"] == {"worker_creation_policy": "approve"}
    assert "/api/sessions/sid/worker_creation_policy" in captured["url"]


# --------------------------------------------------------------------------- #
# Prompt + known-workers helpers
# --------------------------------------------------------------------------- #
def test_read_one_shot_prompt_passthrough():
    assert cli._read_one_shot_prompt("hello") == "hello"


def test_read_one_shot_prompt_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin\n"))
    assert cli._read_one_shot_prompt("-") == "from stdin\n"


def test_load_known_workers_file_none():
    assert cli._load_known_workers_file(None) is None


def test_load_known_workers_file_happy(tmp_path):
    f = tmp_path / "kw.json"
    f.write_text(json.dumps([{"agent_session_id": "s", "registry_cwd": str(tmp_path)}]))
    data = cli._load_known_workers_file(str(f))
    assert data and data[0]["agent_session_id"] == "s"


def test_load_known_workers_file_rejects_non_list(tmp_path):
    f = tmp_path / "kw.json"
    f.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(SystemExit, match="JSON list"):
        cli._load_known_workers_file(str(f))


def test_load_known_workers_file_rejects_non_object_entry(tmp_path):
    f = tmp_path / "kw.json"
    f.write_text(json.dumps(["nope"]))
    with pytest.raises(SystemExit, match="entries must be objects"):
        cli._load_known_workers_file(str(f))


def test_load_known_workers_file_rejects_relative_cwd(tmp_path):
    f = tmp_path / "kw.json"
    f.write_text(json.dumps([{"registry_cwd": "relative/path"}]))
    with pytest.raises(SystemExit, match="absolute"):
        cli._load_known_workers_file(str(f))


def test_known_worker_registry_cwds_none_and_empty():
    assert cli._known_worker_registry_cwds(None) is None
    assert cli._known_worker_registry_cwds([]) is None


def test_known_worker_registry_cwds_builds_map(tmp_path):
    abs_cwd = str(tmp_path)
    out = cli._known_worker_registry_cwds([{"agent_session_id": "s1", "registry_cwd": abs_cwd}])
    assert out == {"s1": str(Path(abs_cwd).expanduser().resolve())}


def test_known_worker_registry_cwds_conflict():
    abs_cwd = str(Path("/tmp").resolve())
    out = cli._known_worker_registry_cwds(
        [{"agent_session_id": "s", "cwd": abs_cwd}, {"agent_session_id": "s", "cwd": abs_cwd}]
    )
    assert out == {"s": abs_cwd}


def test_known_worker_registry_cwds_conflicting_paths_raise():
    a = str(Path("/tmp").resolve())
    b = str(Path("/var").resolve())
    with pytest.raises(SystemExit, match="conflicting registry_cwd"):
        cli._known_worker_registry_cwds([
            {"agent_session_id": "s", "registry_cwd": a},
            {"agent_session_id": "s", "cwd": b},
        ])


# --------------------------------------------------------------------------- #
# _resolve_auth_token / _resolve_cli_cwd / _build_cli_prompt_override
# --------------------------------------------------------------------------- #
def test_resolve_auth_token_precedence(monkeypatch):
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: "stored")
    monkeypatch.delenv("BETTER_CLAUDE_CLI_TOKEN", raising=False)
    # arg wins
    assert cli._resolve_auth_token(8000, "arg") == "arg"
    # then stored
    assert cli._resolve_auth_token(8000, None) == "stored"
    # then env
    monkeypatch.setenv("BETTER_CLAUDE_CLI_TOKEN", "envtok")
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: None)
    assert cli._resolve_auth_token(8000, None) == "envtok"


def test_resolve_cli_cwd_primary_defaults_to_none():
    assert cli._resolve_cli_cwd(None, None) is None
    assert cli._resolve_cli_cwd("/x", None) == os.path.abspath("/x")


def test_resolve_cli_cwd_remote_requires_cwd():
    with pytest.raises(SystemExit, match="--cwd is required"):
        cli._resolve_cli_cwd(None, "worker-1")


def test_resolve_cli_cwd_remote_invalid_path():
    with pytest.raises(SystemExit, match="invalid --cwd"):
        cli._resolve_cli_cwd("relative/path", "worker-1")


def test_build_cli_prompt_override_skips_non_manager():
    assert cli._build_cli_prompt_override(
        session={"id": "s"}, cwd="/r", prompt="p", mode="native", known_workers=[{"agent_session_id": "x"}]
    ) is None


def test_build_cli_prompt_override_skips_when_no_known_workers():
    assert cli._build_cli_prompt_override(
        session={"id": "s"}, cwd="/r", prompt="p", mode="manager", known_workers=None
    ) is None


def test_build_cli_prompt_override_skips_bare_config():
    assert cli._build_cli_prompt_override(
        session={"id": "s", "bare_config": True}, cwd="/r", prompt="p", mode="manager",
        known_workers=[{"agent_session_id": "x", "registry_cwd": "/tmp"}],
    ) is None


def test_build_cli_prompt_override_builds_wrapped_prompt():
    out = cli._build_cli_prompt_override(
        session={"id": "s", "agent_session_id": None, "name": "mgr"}, cwd="/tmp", prompt="do it",
        mode="manager", known_workers=[{"agent_session_id": "w", "registry_cwd": "/tmp"}],
    )
    assert isinstance(out, str) and out
    assert "do it" in out


# --------------------------------------------------------------------------- #
# _drive_turn
# --------------------------------------------------------------------------- #
class _RecordingBackend(cli.Backend):
    def __init__(self, terminal="turn_complete"):
        self.terminal = terminal
        self.calls = []

    async def send_prompt(self, **kwargs):
        self.calls.append(kwargs)
        return self.terminal


async def test_drive_turn_publishes_command_received_and_returns_terminal():
    backend = _RecordingBackend("turn_complete")
    cli._disable_colors()
    terminal = await cli._drive_turn(
        backend=backend, renderer=cli.JsonRenderer(), prompt="hi",
        session={"id": "sid"}, model="m", cwd="/r", mode="team",
    )
    assert terminal == "turn_complete"
    assert backend.calls[0]["prompt"] == "hi"


async def test_drive_turn_swallows_journal_failure(monkeypatch):
    # Force publish_event to fail; _drive_turn must still run the turn.
    import event_journal

    async def boom(**kwargs):
        raise RuntimeError("journal down")

    monkeypatch.setattr(event_journal, "publish_event", boom)
    backend = _RecordingBackend("turn_stopped")
    cli._disable_colors()
    terminal = await cli._drive_turn(
        backend=backend, renderer=cli.JsonRenderer(), prompt="hi",
        session={"id": "sid"}, model="m", cwd="/r", mode="team",
    )
    assert terminal == "turn_stopped"


# --------------------------------------------------------------------------- #
# login / logout / harness-profiles
# --------------------------------------------------------------------------- #
def test_do_login_happy(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")
    _set_urlopen(
        monkeypatch,
        lambda req: _FakeResp(200, json.dumps({"token": "abc"}).encode()),
    )
    stored = []
    monkeypatch.setattr(cli.oskeychain, "store", lambda *a: stored.append(a))
    rc = cli._do_login(8000, "user")
    assert rc == 0
    assert stored  # token persisted


def test_do_login_missing_username_prompted(monkeypatch, capsys):
    inputs = iter(["u"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, json.dumps({"token": "t"}).encode()))
    monkeypatch.setattr(cli.oskeychain, "store", lambda *a: None)
    assert cli._do_login(8000, None) == 0


def test_do_login_username_interrupt_returns_130(monkeypatch):
    def boom(*a, **k):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert cli._do_login(8000, None) == 130


def test_do_login_empty_username(monkeypatch, capsys):
    inputs = iter([""])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    rc = cli._do_login(8000, None)
    assert rc == 2


def test_do_login_password_interrupt_returns_130(monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.getpass, "getpass", boom)
    assert cli._do_login(8000, "u") == 130


def test_do_login_empty_password(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "")
    rc = cli._do_login(8000, "u")
    assert rc == 2


def test_do_login_invalid_credentials(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")

    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    _set_urlopen(monkeypatch, behavior)
    assert cli._do_login(8000, "u") == 1
    assert "invalid credentials" in capsys.readouterr().err


def test_do_login_too_many_attempts(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")

    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 429, "no", {}, None)

    _set_urlopen(monkeypatch, behavior)
    assert cli._do_login(8000, "u") == 1
    assert "too many attempts" in capsys.readouterr().err


def test_do_login_other_http_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")

    def behavior(req):
        raise urllib.error.HTTPError(req.full_url, 500, "no", {}, None)

    _set_urlopen(monkeypatch, behavior)
    assert cli._do_login(8000, "u") == 1
    assert "login failed (500)" in capsys.readouterr().err


def test_do_login_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")
    _set_urlopen(monkeypatch, urllib.error.URLError("down"))
    assert cli._do_login(8000, "u") == 2
    assert "could not reach backend" in capsys.readouterr().err


def test_do_login_no_token_returned(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, json.dumps({}).encode()))
    assert cli._do_login(8000, "u") == 1
    assert "no token was returned" in capsys.readouterr().err


def test_do_login_keychain_store_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "pw")
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, json.dumps({"token": "t"}).encode()))

    def boom(*a, **k):
        raise RuntimeError("locked")

    monkeypatch.setattr(cli.oskeychain, "store", boom)
    assert cli._do_login(8000, "u") == 1
    assert "could not save token" in capsys.readouterr().err


def test_do_logout_happy(monkeypatch, capsys):
    monkeypatch.setattr(cli.oskeychain, "delete", lambda *a: None)
    assert cli._do_logout(8000) == 0
    assert "removed stored token" in capsys.readouterr().out


def test_do_logout_keychain_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli.oskeychain, "delete", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert cli._do_logout(8000) == 1
    assert "could not remove token" in capsys.readouterr().err


def test_do_list_harness_profiles(monkeypatch, capsys):
    _set_urlopen(
        monkeypatch,
        lambda req: _FakeResp(
            200,
            json.dumps({"profiles": [
                {"id": "p1", "name": "Primary", "base_profile_id": "base"},
                {"id": "p2"},
                "ignored-not-dict",
            ]}).encode(),
        ),
    )
    assert cli._do_list_harness_profiles(8000) == 0
    out = capsys.readouterr().out
    assert "p1\tPrimary (base: base)" in out
    assert "p2\tp2" in out  # name defaults to id, no base suffix


def test_do_list_harness_profiles_missing(monkeypatch, capsys):
    _set_urlopen(monkeypatch, lambda req: _FakeResp(200, json.dumps({}).encode()))
    assert cli._do_list_harness_profiles(8000) == 0
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# main() command dispatch
# --------------------------------------------------------------------------- #
def test_main_dispatch_login(monkeypatch):
    called = {}

    def fake_login(port, username):
        called["args"] = (port, username)
        return 0

    monkeypatch.setattr(cli, "_do_login", fake_login)
    monkeypatch.setattr(sys, "argv", ["cli", "login", "--username", "u", "--port", "9000"])
    assert cli.main() == 0
    assert called["args"] == (9000, "u")


def test_main_dispatch_logout(monkeypatch):
    monkeypatch.setattr(cli, "_do_logout", lambda port: 0)
    monkeypatch.setattr(sys, "argv", ["cli", "logout"])
    assert cli.main() == 0


def test_main_dispatch_harness_profiles(monkeypatch):
    monkeypatch.setattr(cli, "_do_list_harness_profiles", lambda port: 0)
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: None)
    monkeypatch.delenv("BETTER_CLAUDE_CLI_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["cli", "harness-profiles"])
    assert cli.main() == 0


def test_main_chat_dispatch_runs_async_main(monkeypatch):
    async def fake_async_main(args):
        return 0

    monkeypatch.setattr(cli, "_async_main", fake_async_main)
    monkeypatch.setattr(sys, "argv", ["cli", "-p", "hi"])
    assert cli.main() == 0


def test_main_chat_dispatch_keyboard_interrupt_returns_130(monkeypatch):
    async def boom(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_async_main", boom)
    monkeypatch.setattr(sys, "argv", ["cli", "-p", "hi"])
    assert cli.main() == 130


# --------------------------------------------------------------------------- #
# Backend base class defaults + _probe retry-sleep
# --------------------------------------------------------------------------- #
async def test_backend_base_defaults_are_noops():
    b = cli.Backend()
    await b.start()
    await b.cancel("s")
    await b.close()
    with pytest.raises(NotImplementedError):
        await b.send_prompt(
            prompt="p", session={"id": "s"}, model="m", cwd="/r", mode="team",
            renderer=cli.JsonRenderer(),
        )


def test_probe_backend_retries_with_sleep(monkeypatch):
    import time as _time

    slept = []
    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))
    attempts = {"n": 0}

    def behavior(req):
        attempts["n"] += 1
        raise urllib.error.URLError("down")

    _set_urlopen(monkeypatch, behavior)
    assert cli._probe_backend(8000, retries=3) is None
    assert attempts["n"] == 3  # one urlopen per path per retry... actually per attempt


def test_load_known_workers_file_skips_entry_with_no_cwd(tmp_path):
    f = tmp_path / "kw.json"
    # Entry with neither registry_cwd nor cwd -> registry_cwd None -> skipped.
    f.write_text(json.dumps([{"agent_session_id": "x"}, {"registry_cwd": str(tmp_path)}]))
    data = cli._load_known_workers_file(str(f))
    assert data and len(data) == 2


async def test_repl_eof_returns_zero(monkeypatch):
    def boom(*a, **k):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    cli._disable_colors()
    rc = await cli._repl(
        backend=_RecordingBackend(), renderer=cli.JsonRenderer(),
        session={"id": "s"}, model="m", cwd="/r", mode="team",
    )
    assert rc == 0


# --------------------------------------------------------------------------- #
# _async_main error exits (user-facing: no backend / auth rejected)
# --------------------------------------------------------------------------- #
def _chat_args(**overrides):
    import argparse

    base = dict(
        command=None, username=None, prompt=None, session=None, mode=None,
        harness_profile=None, cwd=None, node_id=None, provider=None, model=None,
        json=False, no_color=True, port=8000, token=None, bare_config=False,
        worker_creation_policy=None, disallowed_tools=[], disabled_builtin_extensions=[],
        known_workers_file=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


async def test_async_main_no_backend_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_probe_backend", lambda port, **k: None)
    rc = await cli._async_main(_chat_args())
    assert rc == 2
    assert "no Better Agent backend reachable" in capsys.readouterr().err


async def test_async_main_auth_rejected_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_probe_backend", lambda port, **k: False)
    rc = await cli._async_main(_chat_args())
    assert rc == 2
    assert "rejected authentication" in capsys.readouterr().err


async def test_async_main_one_shot_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_probe_backend", lambda port, **k: True)
    monkeypatch.setattr(cli, "resolve_provider", lambda sel: None)
    monkeypatch.setattr(cli.config_store, "default_session_model", lambda: "m")
    monkeypatch.setattr(cli.config_store, "apply_env_vars", lambda *a: None)
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: None)
    monkeypatch.delenv("BETTER_CLAUDE_CLI_TOKEN", raising=False)
    fake_session = {"id": "s1234567890", "cwd": "/r", "orchestration_mode": "team", "node_id": "primary"}
    monkeypatch.setattr(cli, "resolve_backend_session", lambda **k: fake_session)

    class _FakeBackend:
        def __init__(self, port):
            self.started = False
            self.policy = None

        async def start(self):
            self.started = True

        def set_worker_creation_policy(self, sid, pol):
            self.policy = pol

        async def close(self):
            pass

    async def fake_drive(**kwargs):
        return "turn_complete"

    monkeypatch.setattr(cli, "ClientBackend", _FakeBackend)
    monkeypatch.setattr(cli, "_drive_turn", fake_drive)
    cli._disable_colors()
    rc = await cli._async_main(_chat_args(prompt="hi", no_color=False, json=False, worker_creation_policy="approve"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "connected to running backend" in out


async def test_async_main_backend_start_failure_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_probe_backend", lambda port, **k: True)
    monkeypatch.setattr(cli, "resolve_provider", lambda sel: None)
    monkeypatch.setattr(cli.config_store, "default_session_model", lambda: "m")
    monkeypatch.setattr(cli.config_store, "apply_env_vars", lambda *a: None)
    monkeypatch.setattr(cli.oskeychain, "get", lambda *a: None)
    monkeypatch.delenv("BETTER_CLAUDE_CLI_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "resolve_backend_session", lambda **k: {"id": "s", "cwd": "/r", "orchestration_mode": "team", "node_id": "primary"}
    )

    class _BadBackend:
        def __init__(self, port):
            pass

        async def start(self):
            raise RuntimeError("ws down")

        async def close(self):
            pass

    monkeypatch.setattr(cli, "ClientBackend", _BadBackend)
    cli._disable_colors()
    rc = await cli._async_main(_chat_args(prompt="hi"))
    assert rc == 2
    assert "failed to start backend" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# ClientBackend.start / cancel / close error paths
# --------------------------------------------------------------------------- #
async def test_client_backend_start_connects_websocket(monkeypatch):
    import websockets

    fake_ws = _FakeWS([])

    async def fake_connect(*a, **k):
        return fake_ws

    monkeypatch.setattr(websockets, "connect", fake_connect)
    backend = cli.ClientBackend(8000)
    await backend.start()
    assert backend._ws is fake_ws


async def test_client_backend_cancel_swallows_send_error():
    backend = cli.ClientBackend(8000)

    class _BadSend:
        sent = []

        async def send(self, raw):
            raise RuntimeError("send failed")

    backend._ws = _BadSend()
    # Must not raise.
    await backend.cancel("s")


async def test_client_backend_close_with_ws_and_error():
    backend = cli.ClientBackend(8000)

    class _BadClose:
        async def close(self):
            raise RuntimeError("close failed")

    backend._ws = _BadClose()
    await backend.close()  # swallows


async def test_client_backend_close_no_ws_is_noop():
    backend = cli.ClientBackend(8000)
    await backend.close()


# --------------------------------------------------------------------------- #
# _repl interactive loop
# --------------------------------------------------------------------------- #
async def test_repl_exit_immediately_returns_zero(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "exit")
    cli._disable_colors()
    rc = await cli._repl(
        backend=_RecordingBackend(), renderer=cli.JsonRenderer(),
        session={"id": "s"}, model="m", cwd="/r", mode="team",
    )
    assert rc == 0


async def test_repl_blank_lines_skipped_then_exit(monkeypatch):
    answers = iter(["", "   ", "exit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    cli._disable_colors()
    rc = await cli._repl(
        backend=_RecordingBackend(), renderer=cli.JsonRenderer(),
        session={"id": "s"}, model="m", cwd="/r", mode="team",
    )
    assert rc == 0


async def test_repl_runs_one_turn_then_exit(monkeypatch):
    answers = iter(["do something", "quit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    cli._disable_colors()
    backend = _RecordingBackend("turn_complete")
    rc = await cli._repl(
        backend=backend, renderer=cli.JsonRenderer(),
        session={"id": "s"}, model="m", cwd="/r", mode="team",
    )
    assert rc == 0
    assert backend.calls[0]["prompt"] == "do something"


async def test_repl_turn_error_sets_exit_code(monkeypatch):
    answers = iter(["hi", "exit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    class _ErrBackend(cli.Backend):
        async def send_prompt(self, **kwargs):
            raise RuntimeError("turn blew up")

    cli._disable_colors()
    rc = await cli._repl(
        backend=_ErrBackend(), renderer=cli.JsonRenderer(),
        session={"id": "s"}, model="m", cwd="/r", mode="team",
    )
    assert rc == 1


# --------------------------------------------------------------------------- #
# PrettyRenderer manager/worker inner dispatch + indented agent_message
# --------------------------------------------------------------------------- #
def _capture_render(event, indent=""):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    buf = io.StringIO()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "stdout", buf)
    try:
        r._render_inner(event, indent=indent)
    finally:
        monkey.undo()
    return buf.getvalue()


def test_pretty_renderer_manager_event_dispatches_inner():
    out = _capture_render({"type": "output", "data": {"output": "mgr-text"}})
    assert "mgr-text" in out


def test_pretty_renderer_indented_agent_message_text():
    content = [{"type": "text", "text": "a\nb"}]
    event = {"type": "agent_message", "data": {"type": "assistant", "message": {"content": content}}}
    out = _capture_render(event, indent="    ")
    assert "a" in out and "\n    b" in out


def test_pretty_renderer_agent_message_non_dict_block_skipped():
    content = ["not-a-dict", {"type": "text", "text": "ok"}]
    event = {"type": "agent_message", "data": {"type": "assistant", "message": {"content": content}}}
    out = _capture_render(event)
    assert "ok" in out


def test_pretty_renderer_agent_message_empty_thinking_skipped():
    content = [{"type": "thinking", "thinking": ""}]
    event = {"type": "agent_message", "data": {"type": "assistant", "message": {"content": content}}}
    assert _capture_render(event) == ""


def test_pretty_renderer_handle_manager_and_worker_events(capsys):
    cli._disable_colors()
    r = cli.PrettyRenderer()
    r.handle({"type": "manager_event", "data": {"event": {"type": "output", "data": {"output": "m"}}}})
    r.handle({"type": "worker_start", "data": {"worker_description": "w", "is_new": True}})
    r.handle({"type": "worker_event", "data": {"event": {"type": "output", "data": {"output": "wrk"}}}})
    out = capsys.readouterr().out
    assert "m" in out and "wrk" in out


# --------------------------------------------------------------------------- #
# resolve_backend_session: reuse existing with harness-profile mismatch
# --------------------------------------------------------------------------- #
def test_resolve_backend_session_reuse_applies_harness_profile(monkeypatch):
    seq = [
        lambda req: _FakeResp(200, json.dumps({"sessions": [{"id": "ex", "name": "cli-default", "cwd": "/r", "node_id": "primary"}]}).encode()),
        lambda req: _FakeResp(200, json.dumps({"id": "ex", "cwd": "/r", "node_id": "primary", "harness_profile_id": "old"}).encode()),
        lambda req: _FakeResp(200, json.dumps({"id": "ex", "harness_profile_id": "new"}).encode()),
    ]
    _set_urlopen(monkeypatch, seq)
    session = cli.resolve_backend_session(
        port=8000, session_id=None, cwd="/r", model="m", mode=None, provider_id=None,
        harness_profile_id="new",
    )
    assert session["harness_profile_id"] == "new"


def test_resolve_backend_session_skips_fetch_when_no_existing_id(monkeypatch):
    # Match by name/cwd/node but id missing -> falls through to create.
    # _list_backend_sessions is called once; then _create_backend_session.
    seq = [
        lambda req: _FakeResp(200, json.dumps({"sessions": [{"name": "cli-default", "cwd": "/r", "node_id": "primary"}]}).encode()),
        lambda req: _FakeResp(200, json.dumps({"id": "fresh", "cwd": "/r"}).encode()),
    ]
    _set_urlopen(monkeypatch, seq)
    session = cli.resolve_backend_session(
        port=8000, session_id=None, cwd="/r", model="m", mode=None, provider_id=None,
    )
    assert session["id"] == "fresh"


# --------------------------------------------------------------------------- #
# known-workers validation gaps
# --------------------------------------------------------------------------- #
def test_load_known_workers_file_rejects_non_string_cwd(tmp_path):
    f = tmp_path / "kw.json"
    f.write_text(json.dumps([{"registry_cwd": 123}]))
    with pytest.raises(SystemExit, match="non-empty string"):
        cli._load_known_workers_file(str(f))


def test_load_known_workers_file_rejects_empty_cwd(tmp_path):
    f = tmp_path / "kw.json"
    f.write_text(json.dumps([{"registry_cwd": "   "}]))
    with pytest.raises(SystemExit, match="non-empty string"):
        cli._load_known_workers_file(str(f))


def test_known_worker_registry_cwds_skips_entries_missing_fields():
    out = cli._known_worker_registry_cwds([
        {"agent_session_id": "s", "registry_cwd": "/tmp"},
        {"agent_session_id": None, "registry_cwd": "/x"},  # missing sid
        {"agent_session_id": "t", "registry_cwd": None},  # missing cwd
    ])
    assert set(out) == {"s"}


def test_resolve_cli_cwd_remote_valid_absolute(monkeypatch):
    # An absolute path passes node_path_authority and is returned as-is.
    out = cli._resolve_cli_cwd("/tmp/test", "worker-1")
    assert out == "/tmp/test"


def test_parse_args_defaults():
    original = sys.argv
    sys.argv = ["cli"]
    try:
        args = cli._parse_args()
        assert args.command is None
        assert args.json is False
        assert args.disallowed_tools == []
        assert args.port == cli.DEFAULT_BACKEND_PORT
    finally:
        sys.argv = original
