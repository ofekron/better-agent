"""Hermetic unit owner for capabilities_mcp.

The capabilities stdio MCP server is a network-surface module: three tool
triggers (list/load/release) POST over the internal loopback to core, which
owns the active-capability write. This owner covers, deterministically and
without a live backend or model:

- untrusted-input validation on ``capability_id`` (empty/whitespace/None),
- the no-bound-session fail-closed path in ``_post_capabilities``,
- the require_env misconfiguration path (backend URL absent),
- the ``_safe_result`` decorator's three error-surfaces (HTTPError with body,
  HTTPError without body -> reason, generic Exception),
- endpoint/payload/header wiring through a captured Request,
- ``_target_session_id`` explicit-vs-env resolution,
- ``_enabled``'s bare-config and env-presence branches,
- ``_specs`` / ``build_server`` / ``main`` dispatch wiring.

The loopback HTTP boundary is stubbed at ``capabilities_mcp.loopback_urlopen``
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

_TMP_HOME = _test_home.isolate("bc-test-cap-mcp-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import capabilities_mcp  # noqa: E402
from capabilities_mcp import (  # noqa: E402
    _enabled,
    _post_capabilities,
    _specs,
    _target_session_id,
    build_server,
    list_capabilities_response,
    load_capability_response,
    main,
    release_capability_response,
)

_BACKEND_URL = "http://127.0.0.1:9999"
_TOKEN = "internal-token-xyz"
_SESSION_ID = "bound-session-1"


def _raising(exc: BaseException):
    def _stub(*args, **kwargs):
        raise exc

    return _stub


def _wrapped_handler(name: str):
    """The _safe_result-wrapped tool body as the MCP layer invokes it
    (response fns are bare; only the OperationSpec handler is wrapped)."""
    return {s.name: s.handler for s in _specs()}[name]


def _set_runtime_env(monkeypatch, *, app_session_id=_SESSION_ID) -> None:
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", _BACKEND_URL)
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    if app_session_id is not None:
        monkeypatch.setenv("BETTER_CLAUDE_APP_SESSION_ID", app_session_id)


class _CapturedUrlopen:
    """Stand-in for loopback_urlopen that records the Request and returns a
    configurable body, or raises a configured exception."""

    def __init__(self, body=b'{"success": true, "active": []}'):
        self.body = body
        self.request = None
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.request = request
        return self.body


# --- _target_session_id -------------------------------------------------


def test_target_session_id_explicit_stripped_wins(monkeypatch):
    monkeypatch.setenv("BETTER_CLAUDE_APP_SESSION_ID", _SESSION_ID)
    assert _target_session_id("  explicit-sid  ") == "explicit-sid"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_target_session_id_falls_back_to_env(monkeypatch, blank):
    monkeypatch.setenv("BETTER_CLAUDE_APP_SESSION_ID", _SESSION_ID)
    assert _target_session_id(blank) == _SESSION_ID


# --- _post_capabilities -------------------------------------------------


def test_post_capabilities_requires_session_when_unbound(monkeypatch):
    _set_runtime_env(monkeypatch, app_session_id=None)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(capabilities_mcp, "loopback_urlopen", stub)

    result = _post_capabilities({"action": "list"}, session_id="")

    assert result == {"success": False, "error": "session_id is required (no bound session)"}
    assert stub.calls == 0


def test_post_capabilities_routes_endpoint_headers_and_parses(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen(b'{"success": true, "active": ["x"]}')
    monkeypatch.setattr(capabilities_mcp, "loopback_urlopen", stub)

    result = _post_capabilities({"action": "list"}, session_id="sid-9")

    req = stub.request
    assert req.full_url == f"{_BACKEND_URL}/api/internal/sessions/sid-9/capabilities"
    assert req.get_method() == "POST"
    assert req.headers["X-internal-token"] == _TOKEN
    assert req.headers["Content-type"] == "application/json"
    assert result == {"success": True, "active": ["x"]}


def test_wrapper_fails_closed_when_backend_url_missing(monkeypatch):
    # A misconfigured launch (no backend URL) makes require_env raise inside
    # _post_capabilities; the _safe_result wrapper must surface that as a
    # {success: False} tool result rather than crashing the stdio server.
    monkeypatch.setenv("BETTER_CLAUDE_INTERNAL_TOKEN", _TOKEN)
    monkeypatch.setenv("BETTER_CLAUDE_APP_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("BETTER_CLAUDE_BACKEND_URL", raising=False)
    monkeypatch.delenv("BETTER_AGENT_BACKEND_URL", raising=False)
    monkeypatch.setattr(capabilities_mcp, "loopback_urlopen", _CapturedUrlopen())

    result = _wrapped_handler("list_capabilities")()

    assert result["success"] is False
    assert "required" in result["error"]


# --- load / release validation -----------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_load_capability_requires_id(monkeypatch, blank):
    assert load_capability_response(blank) == {
        "success": False,
        "error": "capability_id is required",
    }


def test_load_capability_posts_stripped_id(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(capabilities_mcp, "loopback_urlopen", stub)

    load_capability_response("  ofek.foo:bar  ", session_id="s2")

    payload = json.loads(stub.request.data.decode("utf-8"))
    assert payload == {"action": "load", "capability_id": "ofek.foo:bar"}


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_release_capability_requires_id(monkeypatch, blank):
    assert release_capability_response(blank) == {
        "success": False,
        "error": "capability_id is required",
    }


def test_release_capability_posts_stripped_id(monkeypatch):
    _set_runtime_env(monkeypatch)
    stub = _CapturedUrlopen()
    monkeypatch.setattr(capabilities_mcp, "loopback_urlopen", stub)

    release_capability_response("ofek.foo:bar")

    payload = json.loads(stub.request.data.decode("utf-8"))
    assert payload == {"action": "release", "capability_id": "ofek.foo:bar"}


# --- _safe_result error surfaces ---------------------------------------


def test_safe_result_httperror_with_body_surfaces_detail(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setattr(
        capabilities_mcp,
        "loopback_urlopen",
        _raising(
            urllib.error.HTTPError("http://x", 500, "Server Error", {}, io.BytesIO(b"boom detail"))
        ),
    )

    result = _wrapped_handler("list_capabilities")()

    assert result == {"success": False, "error": "HTTP 500: boom detail"}


def test_safe_result_httperror_without_body_surfaces_reason(monkeypatch):
    _set_runtime_env(monkeypatch)
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b""))
    err.fp = None  # a body-less response: the defensive else-branch
    monkeypatch.setattr(capabilities_mcp, "loopback_urlopen", _raising(err))

    result = _wrapped_handler("list_capabilities")()

    assert result == {"success": False, "error": "HTTP 404: Not Found"}


def test_safe_result_generic_exception_surfaces_message(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setattr(
        capabilities_mcp, "loopback_urlopen", _raising(ValueError("kaboom"))
    )

    result = _wrapped_handler("list_capabilities")()

    assert result == {"success": False, "error": "kaboom"}


def test_safe_result_wraps_spec_handlers():
    # _specs() wraps each response fn through _safe_result; @wraps must carry
    # the original identity forward so tool introspection names the real fn.
    spec_handlers = {s.name: s.handler for s in _specs()}
    assert spec_handlers["list_capabilities"].__name__ == "list_capabilities_response"
    assert spec_handlers["load_capability"].__name__ == "load_capability_response"
    assert spec_handlers["release_capability"].__name__ == "release_capability_response"


# --- _specs / build_server / main dispatch -----------------------------


def test_specs_build_three_tools_with_operations():
    specs = _specs()
    assert [s.name for s in specs] == ["list_capabilities", "load_capability", "release_capability"]
    assert [s.operation for s in specs] == [
        "runtime_capabilities_list",
        "runtime_capabilities_load",
        "runtime_capabilities_release",
    ]


def test_build_server_constructs_capabilities_server():
    server = build_server()
    assert getattr(server, "name", None) == "capabilities"


def test_main_dispatches_remote_when_not_ambient(monkeypatch):
    captured: dict = {}

    def fake_run(name, specs, *, instructions="", local=False):
        captured.update(name=name, local=local, instructions=instructions)
        return 0

    monkeypatch.setattr(capabilities_mcp, "run_mcp_or_cli", fake_run)
    monkeypatch.delenv("BETTER_CLAUDE_AMBIENT_LAUNCH", raising=False)
    monkeypatch.delenv("BETTER_AGENT_AMBIENT_LAUNCH", raising=False)

    assert main() == 0
    assert captured == {
        "name": "capabilities",
        "local": False,
        "instructions": capabilities_mcp._INSTRUCTIONS,
    }


def test_main_dispatches_local_when_ambient(monkeypatch):
    captured: dict = {}

    def fake_run(name, specs, *, instructions="", local=False):
        captured.update(name=name, local=local)
        return 0

    monkeypatch.setattr(capabilities_mcp, "run_mcp_or_cli", fake_run)
    monkeypatch.setenv("BETTER_CLAUDE_AMBIENT_LAUNCH", "1")

    assert main() == 0
    assert captured == {"name": "capabilities", "local": True}


# --- _enabled -----------------------------------------------------------


def test_enabled_false_when_bare_config(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setenv("BETTER_CLAUDE_BARE_CONFIG", "1")
    assert _enabled() is False


def test_enabled_true_when_all_channels_present(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.delenv("BETTER_CLAUDE_BARE_CONFIG", raising=False)
    monkeypatch.delenv("BETTER_AGENT_BARE_CONFIG", raising=False)
    assert _enabled() is True


@pytest.mark.parametrize(
    "missing",
    ["BETTER_CLAUDE_APP_SESSION_ID", "BETTER_CLAUDE_BACKEND_URL", "BETTER_CLAUDE_INTERNAL_TOKEN"],
)
def test_enabled_false_when_a_channel_missing(monkeypatch, missing):
    _set_runtime_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    agent_name = "BETTER_AGENT_" + missing.removeprefix("BETTER_CLAUDE_")
    monkeypatch.delenv(agent_name, raising=False)
    monkeypatch.delenv("BETTER_CLAUDE_BARE_CONFIG", raising=False)
    monkeypatch.delenv("BETTER_AGENT_BARE_CONFIG", raising=False)
    assert _enabled() is False
