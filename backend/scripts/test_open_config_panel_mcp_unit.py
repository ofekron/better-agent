"""Hermetic unit owner for open_config_panel_mcp.

The open-config-panel stdio MCP server is a network-surface module: its single
tool trigger POSTs over the internal loopback to core, which owns the config
panel. This owner covers, deterministically and without a live backend or
model:

- untrusted-input validation on ``capability_id`` (empty/whitespace/None) and
  ``scope`` (must be ``global``/``project``),
- require_env misconfiguration paths (backend URL / internal token / app
  session id absent),
- the two error-surfaces in ``open_config_panel_response``
  (``HTTPError`` -> ``HTTP {code}: {reason}``, generic ``Exception`` -> str),
- endpoint/payload/header wiring through a captured Request,
- ``cwd`` explicit-vs-env-vs-empty resolution,
- ``_env_required``/``_env_optional`` stripping semantics,
- ``_specs`` / ``build_server`` / ``main`` dispatch wiring.

The loopback HTTP boundary is stubbed at ``open_config_panel_mcp.loopback_urlopen``
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

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-ocp-mcp-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import open_config_panel_mcp  # noqa: E402
from open_config_panel_mcp import (  # noqa: E402
    _env_optional,
    _env_required,
    _post_open_config_panel,
    _specs,
    build_server,
    main,
    open_config_panel_response,
)

_BACKEND_URL = "http://127.0.0.1:9999"
_TOKEN = "internal-token-xyz"
_SESSION_ID = "bound-session-1"
_ENDPOINT = f"{_BACKEND_URL}/api/internal/open-config-panel"


def _raising(exc: BaseException):
    def _stub(*args, **kwargs):
        raise exc

    return _stub


def _set_runtime_env(monkeypatch, *, app_session_id=_SESSION_ID) -> None:
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    if app_session_id is not None:
        monkeypatch.setenv("BETTER_CLAUDE_APP_SESSION_ID", app_session_id)


def _clear_runtime_env(monkeypatch, *, name: str) -> None:
    agent_name = "BETTER_AGENT_" + name.removeprefix("BETTER_CLAUDE_")
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(agent_name, raising=False)


class _CapturedUrlopen:
    """Stand-in for loopback_urlopen that records the Request and returns a
    configurable body, or raises a configured exception."""

    def __init__(self, body=b'{"success": true}'):
        self.body = body
        self.request = None
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.request = request
        return self.body


# --- _env_required / _env_optional -------------------------------------


def test_env_required_returns_stripped_value(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "  http://x  ")
    assert _env_required("BETTER_CLAUDE_BACKEND_URL") == "http://x"


def test_env_required_raises_when_missing(monkeypatch):
    _clear_runtime_env(monkeypatch, name="BETTER_CLAUDE_INTERNAL_TOKEN")
    with pytest.raises(RuntimeError):
        _env_required("BETTER_CLAUDE_INTERNAL_TOKEN")


def test_env_optional_returns_stripped_value(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_CWD", "  /a/b  ")
    assert _env_optional("BETTER_CLAUDE_CWD") == "/a/b"


def test_env_optional_returns_empty_when_missing(monkeypatch):
    _clear_runtime_env(monkeypatch, name="BETTER_CLAUDE_CWD")
    assert _env_optional("BETTER_CLAUDE_CWD") == ""


# --- _post_open_config_panel -------------------------------------------


def test_post_routes_endpoint_method_headers_and_parses(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "panel": "inline"}')
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    result = _post_open_config_panel({"capability_id": "cap1", "scope": "project"})

    req = stub.request
    assert req.full_url == _ENDPOINT
    assert req.get_method() == "POST"
    assert req.headers["X-internal-token"] == _TOKEN
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data.decode("utf-8")) == {"capability_id": "cap1", "scope": "project"}
    assert result == {"success": True, "panel": "inline"}


def test_post_strips_trailing_slash_from_backend_url(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "http://x/")
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    _post_open_config_panel({})

    assert stub.request.full_url == "http://x/api/internal/open-config-panel"


def test_post_raises_when_backend_url_missing(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    _clear_runtime_env(monkeypatch, name="BETTER_CLAUDE_BACKEND_URL")
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _CapturedUrlopen())

    with pytest.raises(RuntimeError):
        _post_open_config_panel({})


def test_post_raises_on_malformed_body(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _CapturedUrlopen(b"not-json"))

    with pytest.raises(json.JSONDecodeError):
        _post_open_config_panel({})


# --- open_config_panel_response: validation ----------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_response_requires_capability_id(monkeypatch, blank):
    _set_runtime_env(monkeypatch)
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _CapturedUrlopen())

    result = open_config_panel_response(blank)

    assert result == {"success": False, "error": "`capability_id` is required"}


@pytest.mark.parametrize("scope", ["user", "global-ish", " system "])
def test_response_rejects_invalid_scope(monkeypatch, scope):
    # Invalid set must be genuinely-stripped-invalid: a value like "project "
    # strips to "project" (valid) and would proceed to POST, so it is excluded.
    _set_runtime_env(monkeypatch)
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _CapturedUrlopen())

    result = open_config_panel_response("cap1", scope=scope)

    assert result == {"success": False, "error": "`scope` must be 'global' or 'project'"}


@pytest.mark.parametrize("scope", ["global", "project"])
def test_response_accepts_valid_scope(monkeypatch, scope):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    result = open_config_panel_response("cap1", scope=scope)

    assert result == {"success": True}
    assert json.loads(stub.request.data.decode("utf-8"))["scope"] == scope


# --- open_config_panel_response: payload resolution --------------------


def test_response_strips_capability_id_before_post(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    open_config_panel_response("  cap1  ")

    assert json.loads(stub.request.data.decode("utf-8"))["capability_id"] == "cap1"


def test_response_strips_scope_whitespace(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    open_config_panel_response("cap1", scope="  project  ")

    assert json.loads(stub.request.data.decode("utf-8"))["scope"] == "project"


def test_response_default_scope_is_project(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    open_config_panel_response("cap1")

    body = json.loads(stub.request.data.decode("utf-8"))
    assert body["scope"] == "project"
    assert body["app_session_id"] == _SESSION_ID


def test_response_explicit_cwd_wins_over_env(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setenv("BETTER_CLAUDE_CWD", "/env-cwd")
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    open_config_panel_response("cap1", cwd="/explicit")

    assert json.loads(stub.request.data.decode("utf-8"))["cwd"] == "/explicit"


def test_response_cwd_falls_back_to_env(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setenv("BETTER_CLAUDE_CWD", "  /env-cwd  ")
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    open_config_panel_response("cap1")

    assert json.loads(stub.request.data.decode("utf-8"))["cwd"] == "/env-cwd"


def test_response_cwd_empty_when_none_and_env_absent(monkeypatch):
    _set_runtime_env(monkeypatch)
    _clear_runtime_env(monkeypatch, name="BETTER_CLAUDE_CWD")
    stub = _CapturedUrlopen()
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", stub)

    open_config_panel_response("cap1")

    assert json.loads(stub.request.data.decode("utf-8"))["cwd"] == ""


# --- open_config_panel_response: error surfaces ------------------------


def test_response_app_session_id_required_fail_closed(monkeypatch):
    _set_runtime_env(monkeypatch, app_session_id=None)
    _clear_runtime_env(monkeypatch, name="BETTER_CLAUDE_APP_SESSION_ID")
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _CapturedUrlopen())

    result = open_config_panel_response("cap1")

    assert result["success"] is False
    assert "required" in result["error"]


def test_response_surfaces_httperror(monkeypatch):
    _set_runtime_env(monkeypatch)
    err = urllib.error.HTTPError(_ENDPOINT, 500, "Server Error", {}, io.BytesIO(b""))
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _raising(err))

    result = open_config_panel_response("cap1")

    assert result == {"success": False, "error": "HTTP 500: Server Error"}


def test_response_surfaces_generic_exception(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setattr(open_config_panel_mcp, "loopback_urlopen", _raising(ValueError("boom")))

    result = open_config_panel_response("cap1")

    assert result == {"success": False, "error": "boom"}


# --- _specs / build_server / main dispatch -----------------------------


def test_specs_single_open_config_panel_tool():
    specs = _specs()
    assert [s.name for s in specs] == ["open_config_panel"]
    assert [s.operation for s in specs] == ["runtime_ui_open_config_panel"]
    assert specs[0].handler is open_config_panel_response


def test_build_server_name():
    server = build_server()
    assert getattr(server, "name", None) == "open-config-panel"


def test_main_remote_when_not_ambient(monkeypatch):
    captured: dict = {}

    def fake_run(name, specs, *, instructions="", local=False):
        captured.update(name=name, local=local, instructions=instructions)
        return 0

    monkeypatch.setattr(open_config_panel_mcp, "run_mcp_or_cli", fake_run)
    _clear_runtime_env(monkeypatch, name="BETTER_CLAUDE_AMBIENT_LAUNCH")

    assert main() == 0
    assert captured == {
        "name": "open-config-panel",
        "local": False,
        "instructions": open_config_panel_mcp._INSTRUCTIONS,
    }


def test_main_local_when_ambient(monkeypatch):
    captured: dict = {}

    def fake_run(name, specs, *, instructions="", local=False):
        captured.update(name=name, local=local)
        return 0

    monkeypatch.setattr(open_config_panel_mcp, "run_mcp_or_cli", fake_run)
    monkeypatch.setenv("BETTER_CLAUDE_AMBIENT_LAUNCH", "1")

    assert main() == 0
    assert captured == {"name": "open-config-panel", "local": True}
