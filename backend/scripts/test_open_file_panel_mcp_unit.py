"""Hermetic unit owner for open_file_panel_mcp.

The ``ui`` stdio MCP server is a network-surface module: four tool triggers
(open_file_panel / request_user_input / request_user_approval /
start_file_discussion) POST over the internal loopback to core, which owns
the session-bound UI mutation. This owner covers, deterministically and
without a live backend or model:

- untrusted-input validation on every trigger (mode/path, questions array,
  prompt text, file_path + line bounds),
- the panel-vs-inline target-session resolution (explicit session_id vs env),
- the require_env misconfiguration path (env absent -> fail-closed),
- the HTTPError and generic-Exception error surfaces (per trigger),
- endpoint / payload / header / method wiring through a captured Request,
- ``_specs`` ambient / full / file-editing branches,
- ``build_server`` / ``main`` dispatch wiring.

The loopback HTTP boundary is stubbed at ``open_file_panel_mcp.urllib.request``
(a unit-tier I/O boundary); the stdio server loop in ``run_mcp_or_cli`` is
stubbed because it blocks. No real home is touched.

conftest engages an isolated per-module ba_home().
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-ofp-mcp-")

import pytest  # noqa: E402

import open_file_panel_mcp  # noqa: E402
from open_file_panel_mcp import (  # noqa: E402
    _env_required,
    _post_open_file_panel,
    _post_start_discussion,
    _post_user_input,
    _specs,
    build_server,
    main,
    open_file_panel_response,
    request_user_approval_response,
    request_user_input_response,
    start_file_discussion_response,
)

_BACKEND_URL = "http://127.0.0.1:9999"
_TOKEN = "internal-token-ofp"
_SESSION_ID = "bound-session-ofp"


def _set_runtime_env(monkeypatch, *, app_session_id=_SESSION_ID) -> None:
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    if app_session_id is not None:
        monkeypatch.setenv("BETTER_CLAUDE_APP_SESSION_ID", app_session_id)
        monkeypatch.setenv("BETTER_AGENT_APP_SESSION_ID", app_session_id)


class _FakeResp:
    """Context manager returned by the stubbed urlopen; serves a fixed body."""

    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CapturedUrlopen:
    """Stand-in for urllib.request.urlopen that records the Request and either
    returns a context-managed response body or raises a configured exception."""

    def __init__(self, body=b'{"success": true}', exc=None):
        self._body = body
        self._exc = exc
        self.request = None
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.request = request
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._body)


def _http_error(code: int, reason: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://internal", code=code, msg=reason, hdrs=None, fp=io.BytesIO(b"{}")
    )


def _patch_urlopen(monkeypatch, stub: _CapturedUrlopen) -> None:
    monkeypatch.setattr(open_file_panel_mcp.urllib.request, "urlopen", stub)


# --- _env_required ------------------------------------------------------


def test_env_required_returns_stripped_value(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "  http://x  ")
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", "  http://x  ")
    # require_env/get_env_stripped strips surrounding whitespace.
    assert _env_required("BETTER_CLAUDE_BACKEND_URL") == "http://x"


def test_env_required_raises_when_missing(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_BACKEND_URL", raising=False)
    monkeypatch.delenv("BETTER_AGENT_BACKEND_URL", raising=False)
    with pytest.raises(RuntimeError):
        _env_required("BETTER_CLAUDE_BACKEND_URL")


# --- POST helpers: endpoint / header / payload wiring -------------------


def test_post_open_file_panel_routes_wiring(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "panel_id": "p1"}')
    _patch_urlopen(monkeypatch, stub)

    result = _post_open_file_panel({"app_session_id": "s1", "mode": "panel", "path": "/a"})

    req = stub.request
    assert req.full_url == f"{_BACKEND_URL}/api/internal/open-file-panel"
    assert req.get_method() == "POST"
    assert req.headers["X-internal-token"] == _TOKEN
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data.decode("utf-8")) == {"app_session_id": "s1", "mode": "panel", "path": "/a"}
    assert result == {"success": True, "panel_id": "p1"}


def test_post_user_input_routes_wiring(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "answers": []}')
    _patch_urlopen(monkeypatch, stub)

    result = _post_user_input({"app_session_id": "s1", "kind": "input", "questions": []})

    assert stub.request.full_url == f"{_BACKEND_URL}/api/internal/user-input/request"
    assert stub.request.headers["X-internal-token"] == _TOKEN
    assert result == {"success": True, "answers": []}


def test_post_start_discussion_routes_wiring(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "thread_id": "t1"}')
    _patch_urlopen(monkeypatch, stub)

    result = _post_start_discussion({"app_session_id": "s1", "file_path": "/a", "line": 3})

    assert stub.request.full_url == f"{_BACKEND_URL}/api/internal/file-editor/start-discussion"
    assert stub.request.headers["X-internal-token"] == _TOKEN
    assert result == {"success": True, "thread_id": "t1"}


# --- open_file_panel_response -------------------------------------------


@pytest.mark.parametrize("mode", ["diff", ""])
def test_open_file_panel_rejects_invalid_mode_or_path(monkeypatch, mode):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = open_file_panel_response(mode, "/some/path")

    assert result == {"success": False, "error": "`mode` (panel|inline) and `path` are required"}
    assert stub.calls == 0


@pytest.mark.parametrize("blank_path", ["", "   "])
def test_open_file_panel_rejects_blank_path(monkeypatch, blank_path):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = open_file_panel_response("panel", blank_path)

    assert result["success"] is False
    assert stub.calls == 0


def test_open_file_panel_panel_mode_uses_explicit_session_id(monkeypatch):
    # panel mode with an explicit session_id must NOT consult the env session.
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    monkeypatch.delenv("BETTER_CLAUDE_APP_SESSION_ID", raising=False)
    monkeypatch.delenv("BETTER_AGENT_APP_SESSION_ID", raising=False)
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    open_file_panel_response("panel", "/a", start_line=1, end_line=2, selected_start=3, selected_end=4, session_id="  explicit-sid  ")

    sent = json.loads(stub.request.data.decode("utf-8"))
    assert sent["app_session_id"] == "explicit-sid"
    assert sent["mode"] == "panel"
    assert sent["path"] == "/a"
    assert sent["start_line"] == 1
    assert sent["end_line"] == 2
    assert sent["selected_start"] == 3
    assert sent["selected_end"] == 4


def test_open_file_panel_panel_mode_falls_back_to_env_session(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    open_file_panel_response("panel", "/a", session_id="")

    sent = json.loads(stub.request.data.decode("utf-8"))
    assert sent["app_session_id"] == _SESSION_ID


def test_open_file_panel_inline_mode_uses_env_session(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    open_file_panel_response("inline", "/a")

    sent = json.loads(stub.request.data.decode("utf-8"))
    assert sent["app_session_id"] == _SESSION_ID
    assert sent["mode"] == "inline"


def test_open_file_panel_fails_closed_when_session_env_missing(monkeypatch):
    # inline mode launched ambiently (no bound session) must fail closed via
    # require_env rather than silently dispatching.
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    monkeypatch.delenv("BETTER_CLAUDE_APP_SESSION_ID", raising=False)
    monkeypatch.delenv("BETTER_AGENT_APP_SESSION_ID", raising=False)
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = open_file_panel_response("inline", "/a")

    assert result["success"] is False
    assert "required" in result["error"]
    assert stub.calls == 0


def test_open_file_panel_surfaces_http_error(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=_http_error(403, "Forbidden")))

    result = open_file_panel_response("inline", "/a")

    assert result == {"success": False, "error": "HTTP 403: Forbidden"}


def test_open_file_panel_surfaces_generic_exception(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=RuntimeError("boom: downstream")))

    result = open_file_panel_response("inline", "/a")

    assert result == {"success": False, "error": "boom: downstream"}


# --- request_user_input_response ----------------------------------------


@pytest.mark.parametrize("bad", [None, "not-a-list", []])
def test_request_user_input_rejects_bad_questions(monkeypatch, bad):
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = request_user_input_response(bad)

    assert result == {"success": False, "error": "`questions` must be a non-empty array"}
    assert stub.calls == 0


def test_request_user_input_posts_questions(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "answers": ["yes"]}')
    _patch_urlopen(monkeypatch, stub)

    result = request_user_input_response([{"q": "ok?"}], timeout_seconds=12.5)

    sent = json.loads(stub.request.data.decode("utf-8"))
    assert sent == {
        "app_session_id": _SESSION_ID,
        "kind": "input",
        "questions": [{"q": "ok?"}],
        "timeout_seconds": 12.5,
    }
    assert result == {"success": True, "answers": ["yes"]}


def test_request_user_input_surfaces_http_error(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=_http_error(502, "Bad Gateway")))

    assert request_user_input_response([{"q": "ok?"}]) == {"success": False, "error": "HTTP 502: Bad Gateway"}


def test_request_user_input_surfaces_generic_exception(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=ValueError("parse failed")))

    assert request_user_input_response([{"q": "ok?"}]) == {"success": False, "error": "parse failed"}


# --- request_user_approval_response -------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_request_user_approval_rejects_blank_prompt(monkeypatch, blank):
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = request_user_approval_response(blank)

    assert result == {"success": False, "error": "`prompt` is required"}
    assert stub.calls == 0


def test_request_user_approval_posts_stripped_prompt(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "approved": true}')
    _patch_urlopen(monkeypatch, stub)

    result = request_user_approval_response("  approve?  ", timeout_seconds=30)

    sent = json.loads(stub.request.data.decode("utf-8"))
    assert sent == {
        "app_session_id": _SESSION_ID,
        "kind": "approval",
        "prompt": "approve?",
        "timeout_seconds": 30,
    }
    assert result == {"success": True, "approved": True}


def test_request_user_approval_surfaces_http_error(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=_http_error(418, "I'm a Teapot")))

    assert request_user_approval_response("ok") == {"success": False, "error": "HTTP 418: I'm a Teapot"}


def test_request_user_approval_surfaces_generic_exception(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=OSError("net down")))

    assert request_user_approval_response("ok") == {"success": False, "error": "net down"}


# --- start_file_discussion_response -------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_start_file_discussion_rejects_blank_path(monkeypatch, blank):
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = start_file_discussion_response(blank, 5)

    assert result == {"success": False, "error": "`file_path` is required"}
    assert stub.calls == 0


@pytest.mark.parametrize("bad_line", [0, -1])
def test_start_file_discussion_rejects_invalid_line(monkeypatch, bad_line):
    stub = _CapturedUrlopen()
    _patch_urlopen(monkeypatch, stub)

    result = start_file_discussion_response("/a", bad_line)

    assert result == {"success": False, "error": "`line` must be >= 1"}
    assert stub.calls == 0


def test_start_file_discussion_posts_payload(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "thread_id": "t"}')
    _patch_urlopen(monkeypatch, stub)

    result = start_file_discussion_response("  /a  ", 7, title="note")

    sent = json.loads(stub.request.data.decode("utf-8"))
    assert sent == {
        "app_session_id": _SESSION_ID,
        "file_path": "/a",
        "line": 7,
        "title": "note",
    }
    assert result == {"success": True, "thread_id": "t"}


def test_start_file_discussion_surfaces_http_error(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=_http_error(500, "Internal Server Error")))

    assert start_file_discussion_response("/a", 1) == {"success": False, "error": "HTTP 500: Internal Server Error"}


def test_start_file_discussion_surfaces_generic_exception(monkeypatch):
    _set_runtime_env(monkeypatch)
    _patch_urlopen(monkeypatch, _CapturedUrlopen(exc=TimeoutError("slow")))

    assert start_file_discussion_response("/a", 1) == {"success": False, "error": "slow"}


# --- _specs / build_server / main dispatch ------------------------------


def test_specs_ambient_offers_only_open_file_panel(monkeypatch):
    specs = _specs(ambient=True)
    assert [s.name for s in specs] == ["open_file_panel"]
    assert [s.operation for s in specs] == ["runtime_ui_open_file_panel"]
    assert specs[0].handler is open_file_panel_response


def test_specs_full_without_file_editing(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_FILE_EDITING", raising=False)
    monkeypatch.delenv("BETTER_AGENT_FILE_EDITING", raising=False)

    specs = _specs()
    assert [s.name for s in specs] == [
        "open_file_panel",
        "request_user_input",
        "request_user_approval",
    ]
    assert specs[1].handler is request_user_input_response
    assert specs[2].handler is request_user_approval_response


def test_specs_full_includes_file_discussion_when_enabled(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_FILE_EDITING", "1")

    specs = _specs()
    assert [s.name for s in specs] == [
        "open_file_panel",
        "request_user_input",
        "request_user_approval",
        "start_file_discussion",
    ]
    assert specs[3].operation == "runtime_ui_start_file_discussion"
    assert specs[3].handler is start_file_discussion_response


def test_specs_offers_no_file_discussion_when_flag_disabled(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_FILE_EDITING", "0")

    specs = _specs()
    assert "start_file_discussion" not in [s.name for s in specs]


def test_build_server_constructs_ui_server():
    server = build_server()
    assert getattr(server, "name", None) == "ui"


def test_main_dispatches_full_when_not_ambient(monkeypatch):
    captured: dict = {}

    def fake_run(name, specs, *, instructions="", local=False):
        captured.update(name=name, local=local, instructions=instructions, specs=specs)
        return 0

    monkeypatch.setattr(open_file_panel_mcp, "run_mcp_or_cli", fake_run)
    monkeypatch.delenv("BETTER_CLAUDE_AMBIENT_LAUNCH", raising=False)
    monkeypatch.delenv("BETTER_CLAUDE_FILE_EDITING", raising=False)
    monkeypatch.delenv("BETTER_AGENT_FILE_EDITING", raising=False)

    assert main() == 0
    assert captured["name"] == "ui"
    assert captured["local"] is False
    assert captured["instructions"] == open_file_panel_mcp._INSTRUCTIONS
    assert [s.name for s in captured["specs"]] == [
        "open_file_panel",
        "request_user_input",
        "request_user_approval",
    ]


def test_main_dispatches_local_ambient_specs_when_ambient(monkeypatch):
    captured: dict = {}

    def fake_run(name, specs, *, instructions="", local=False):
        captured.update(name=name, local=local, specs=specs)
        return 0

    monkeypatch.setattr(open_file_panel_mcp, "run_mcp_or_cli", fake_run)
    monkeypatch.setenv("BETTER_CLAUDE_AMBIENT_LAUNCH", "1")

    assert main() == 0
    assert captured["local"] is True
    # Ambient launch offers only the single session-less-capable tool.
    assert [s.name for s in captured["specs"]] == ["open_file_panel"]
