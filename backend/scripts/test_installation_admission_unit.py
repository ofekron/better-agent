#!/usr/bin/env python3
"""100% unit coverage for installation_admission.

Covers the capability_for_scope ASGI routing table (every capability bucket +
the lifespan / non-http / provider-internal-exact branches) and the
InstallationAdmissionMiddleware response paths (pass-through, websocket.close,
404 vs 503 JSON denial). The middleware's contract is routing given the
allows() predicate, so allows() is monkeypatched per-case; this keeps the test
independent of installation_profile's mode/provisioning state (and of the
optional firebase_admin dependency that state reads).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-installation-admission-")
import atexit  # noqa: E402

atexit.register(TEST_HOME.release)

import installation_admission  # noqa: E402
import installation_profile  # noqa: E402
from installation_admission import (  # noqa: E402
    InstallationAdmissionMiddleware,
    capability_for_scope,
)

B = installation_profile.BOOTSTRAP
M = installation_profile.MOBILE
I = installation_profile.INTEGRATIONS
P = installation_profile.PROVIDER_CONVERSATIONS


# --------------------------------------------------------------------------- #
# capability_for_scope routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "scope,expected",
    [
        # lifespan short-circuits to None (no admission decision)
        ({"type": "lifespan"}, None),
        # non-http/websocket scope types fall back to BOOTSTRAP
        ({"type": "spark"}, B),
        ({"type": None}, B),
        # paths outside /api (and not /ws/chat) are BOOTSTRAP-reachable
        ({"type": "http", "path": "/"}, B),
        ({"type": "http", "path": "/healthz"}, B),
        # /ws/chat is the one non-/api path that is still a conversation
        ({"type": "http", "path": "/ws/chat"}, P),
        # bootstrap exact + prefixes
        ({"type": "http", "path": "/readyz"}, B),
        ({"type": "http", "path": "/api/build-info"}, B),
        ({"type": "http", "path": "/api/installation-profile"}, B),
        ({"type": "http", "path": "/api/auth/callback"}, B),
        ({"type": "http", "path": "/api/desktop/whatever"}, B),
        ({"type": "http", "path": "/api/startup_tasks"}, B),
        # capability-toggle endpoint stays reachable while the capability is off
        ({"type": "http", "path": "/api/installation-profile/capabilities/mobile"}, B),
        # mobile
        ({"type": "http", "path": "/api/mobile/status"}, M),
        ({"type": "http", "path": "/api/download/android"}, M),
        # provider-internal exact -> conversations (NOT integrations)
        ({"type": "http", "path": "/api/internal/credential/execute"}, P),
        ({"type": "http", "path": "/api/internal/credential/request"}, P),
        # integration prefixes + /api/internal/ catch-all
        ({"type": "http", "path": "/api/extensions"}, I),
        ({"type": "http", "path": "/api/internal/marketplace"}, I),
        ({"type": "http", "path": "/api/internal/session-control/x"}, I),
        ({"type": "http", "path": "/api/internal/something-else"}, I),
        # default -> provider conversations
        ({"type": "http", "path": "/api/sessions/abc/messages"}, P),
        # websocket conversations
        ({"type": "websocket", "path": "/ws/chat"}, P),
        ({"type": "websocket", "path": "/api/mobile/x"}, M),
    ],
)
def test_capability_for_scope(scope, expected):
    assert capability_for_scope(scope) == expected


def test_capability_for_scope_missing_path_defaults():
    # scope without an explicit path string
    assert capability_for_scope({"type": "http"}) == B
    assert capability_for_scope({"type": "http", "path": None}) == B


# --------------------------------------------------------------------------- #
# InstallationAdmissionMiddleware
# --------------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)


class _Channel:
    """Records ASGI app invocations and outbound send frames."""

    def __init__(self):
        self.app_scopes: list[dict] = []
        self.sent: list[dict] = []

    async def app(self, scope, receive, send):
        self.app_scopes.append(scope)

    async def send(self, msg):
        self.sent.append(msg)


def _make_mw(monkeypatch, allow):
    """Middleware with installation_profile.allows pinned to `allow`."""
    monkeypatch.setattr(installation_admission.installation_profile, "allows", lambda c: allow)
    ch = _Channel()
    return InstallationAdmissionMiddleware(ch.app), ch


def test_middleware_passes_through_when_allowed(monkeypatch):
    mw, ch = _make_mw(monkeypatch, allow=True)
    scope = {"type": "http", "path": "/api/sessions"}
    _run(mw(scope, None, ch.send))
    assert ch.app_scopes == [scope]
    assert ch.sent == []


def test_middleware_lifespan_passes_through(monkeypatch):
    # capability_for_scope returns None for lifespan -> always passes, regardless of allows
    mw, ch = _make_mw(monkeypatch, allow=False)
    scope = {"type": "lifespan"}
    _run(mw(scope, None, ch.send))
    assert ch.app_scopes == [scope]
    assert ch.sent == []


def test_middleware_http_denies_conversations_as_503(monkeypatch):
    mw, ch = _make_mw(monkeypatch, allow=False)
    _run(mw({"type": "http", "path": "/api/sessions"}, None, ch.send))
    assert ch.app_scopes == []
    assert ch.sent[0]["type"] == "http.response.start"
    assert ch.sent[0]["status"] == 503
    body = ch.sent[1]["body"]
    assert json.loads(body) == {"detail": "installation setup is required"}
    assert ch.sent[0]["headers"][0] == (b"content-type", b"application/json")
    assert ch.sent[0]["headers"][1] == (b"content-length", str(len(body)).encode("ascii"))


@pytest.mark.parametrize("path,expected_detail", [
    ("/api/mobile/status", "installation capability is unavailable"),
    ("/api/extensions", "installation capability is unavailable"),
])
def test_middleware_http_denies_mobile_and_integrations_as_404(monkeypatch, path, expected_detail):
    mw, ch = _make_mw(monkeypatch, allow=False)
    _run(mw({"type": "http", "path": path}, None, ch.send))
    assert ch.app_scopes == []
    assert ch.sent[0]["status"] == 404
    assert json.loads(ch.sent[1]["body"]) == {"detail": expected_detail}


def test_middleware_websocket_denial_closes(monkeypatch):
    mw, ch = _make_mw(monkeypatch, allow=False)
    _run(mw({"type": "websocket", "path": "/api/mobile/x"}, None, ch.send))
    assert ch.app_scopes == []
    assert ch.sent == [{"type": "websocket.close", "code": 1008, "reason": "installation capability unavailable"}]
