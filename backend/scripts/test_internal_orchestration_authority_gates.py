"""Authority-invalid (403) rejection of the team-orchestration internal endpoints.

Each team endpoint in internal_orchestration_api.py re-checks
`internal_guards.authority_is_valid()` after the owning extension's
runtime-readiness gate. A request with no bound principal must be rejected
with HTTP 403. These are security boundaries with exactly one path each;
this file owns that one path so the handler tests can focus on happy paths.

Isolated BETTER_AGENT_HOME via _test_home.isolate; nothing leaks.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-intorch-authority-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import internal_guards  # noqa: E402
import internal_orchestration_api as api  # noqa: E402


@pytest.fixture
def authority_absent(monkeypatch):
    """The team-orchestration extension is runtime-ready, but no principal is
    bound to the request. Every team handler must fail closed with 403."""
    monkeypatch.setattr(
        internal_guards, "require_builtin_runtime_extension", lambda *_a, **_k: None
    )
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)


def _rejects_403(coro):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == 403


class TestTeamEndpointsRejectUnauthenticated:
    def test_create_team(self, authority_absent):
        _rejects_403(api.internal_create_team({"root_session_id": "x"}, "tok"))

    def test_register_member(self, authority_absent):
        _rejects_403(api.internal_register_team_member({}, "tok"))

    def test_activate_definition(self, authority_absent):
        _rejects_403(api.internal_activate_team_definition({"root_session_id": "x"}, "tok"))

    def test_get_activation(self, authority_absent):
        _rejects_403(api.internal_get_team_definition_activation("aid", "tok"))

    def test_finalize_member(self, authority_absent):
        _rejects_403(api.internal_finalize_team_definition_member({}, "tok"))
