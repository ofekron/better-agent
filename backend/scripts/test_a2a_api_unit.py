"""Dedicated owner for a2a_api.py — the REST + WS-broadcast routes over the
outbound A2A agent registry (list/add/remove/probe/delegate).

Unit tier: the registry store, agent-card fetch, url validation, session
existence check, and delegation runner are collaborators patched to drive
every validation branch and short-circuit deterministically. Routes are
async, driven via asyncio.run (no pytest-asyncio).
"""

import asyncio

import pytest
from fastapi import HTTPException

import a2a_api
from a2a.discovery import AgentCardFetchError
from a2a.models import AgentCardValidationError
from a2a.url_policy import A2AUrlPolicyError


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_broadcast():
    a2a_api._broadcast_global = None
    yield
    a2a_api._broadcast_global = None


def _configure(captured):
    async def _bcast(event, payload):
        captured.append((event, payload))

    a2a_api.configure(_bcast)
    return _bcast


def _patch_registry(monkeypatch, *, list_all=None, redact=None, add=None,
                    remove=None, get=None, update_probe_result=None):
    reg = a2a_api.registry
    if list_all is not None:
        monkeypatch.setattr(reg, "list_all", list_all)
    monkeypatch.setattr(reg, "redact", redact or (lambda r: r))
    if add is not None:
        monkeypatch.setattr(reg, "add", add)
    if remove is not None:
        monkeypatch.setattr(reg, "remove", remove)
    if get is not None:
        monkeypatch.setattr(reg, "get", get)
    if update_probe_result is not None:
        monkeypatch.setattr(reg, "update_probe_result", update_probe_result)


# --- _require_configured / configure -------------------------------------

def test_route_raises_503_when_not_configured():
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.list_a2a_agents())
    assert exc.value.status_code == 503


# --- list_a2a_agents ------------------------------------------------------

def test_list_agents_returns_snapshot(monkeypatch):
    captured = []
    _configure(captured)
    _patch_registry(monkeypatch, list_all=lambda: [{"id": "a1"}])

    result = _run(a2a_api.list_a2a_agents())

    assert result == {"agents": [{"id": "a1"}]}


# --- add_a2a_agent --------------------------------------------------------

def _create_body(**over):
    base = dict(name="agent", base_url="https://host", auth_header_name="",
                auth_secret="")
    base.update(over)
    return a2a_api.A2AAgentCreate(**base)


def test_add_name_empty_rejected(monkeypatch):
    _configure([])
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(_create_body(name="   ")))
    assert exc.value.status_code == 400


def test_add_name_too_long_rejected(monkeypatch):
    _configure([])
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(_create_body(name="a" * 201)))
    assert exc.value.status_code == 400


def test_add_header_name_invalid_rejected(monkeypatch):
    _configure([])
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(_create_body(auth_header_name="bad header!")))
    assert exc.value.status_code == 400


def test_add_secret_too_long_rejected(monkeypatch):
    _configure([])
    monkeypatch.setattr(a2a_api, "validate_base_url", lambda u: u)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(
            _create_body(auth_secret="x" * (a2a_api._MAX_SECRET_LEN + 1))))
    assert exc.value.status_code == 400


def test_add_url_policy_rejected(monkeypatch):
    _configure([])

    def _raise(_u):
        raise A2AUrlPolicyError("bad url")

    monkeypatch.setattr(a2a_api, "validate_base_url", _raise)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(_create_body()))
    assert exc.value.status_code == 400


def test_add_fetch_error_rejected_502(monkeypatch):
    _configure([])
    monkeypatch.setattr(a2a_api, "validate_base_url", lambda u: u)

    async def _fetch(_u):
        raise AgentCardFetchError("boom")

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(_create_body()))
    assert exc.value.status_code == 502


def test_add_card_validation_error_rejected_422(monkeypatch):
    _configure([])
    monkeypatch.setattr(a2a_api, "validate_base_url", lambda u: u)

    async def _fetch(_u):
        raise AgentCardValidationError("bad card")

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.add_a2a_agent(_create_body()))
    assert exc.value.status_code == 422


def test_add_happy_creates_and_broadcasts(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(a2a_api, "validate_base_url", lambda u: u + "/norm")

    async def _fetch(_u):
        return {"card": True}

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)

    def _add(**kw):
        kw["id"] = "rec-1"
        return kw

    _patch_registry(monkeypatch, add=_add)

    result = _run(a2a_api.add_a2a_agent(
        _create_body(auth_header_name="Authorization", auth_secret="s")))

    assert result["id"] == "rec-1"
    assert result["base_url"] == "https://host/norm"
    assert result["agent_card"] == {"card": True}
    assert captured and captured[0][0] == "a2a_registry_changed"


# --- remove_a2a_agent -----------------------------------------------------

def test_remove_not_found_404(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, remove=lambda _aid: False)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.remove_a2a_agent("ghost"))
    assert exc.value.status_code == 404


def test_remove_happy_broadcasts(monkeypatch):
    captured = []
    _configure(captured)
    _patch_registry(monkeypatch, remove=lambda _aid: True)
    result = _run(a2a_api.remove_a2a_agent("a1"))
    assert result == {"deleted": True}
    assert captured


# --- probe_a2a_agent ------------------------------------------------------

def test_probe_not_found_404(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: None)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.probe_a2a_agent("ghost"))
    assert exc.value.status_code == 404


def test_probe_fetch_error_records_failure_with_update(monkeypatch):
    captured = []
    _configure(captured)
    record = {"id": "a1", "base_url": "https://host"}
    _patch_registry(monkeypatch, get=lambda _aid: record,
                    update_probe_result=lambda *a, **k: {"id": "a1", "ok": False})

    async def _fetch(_u):
        raise AgentCardFetchError("down")

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)

    result = _run(a2a_api.probe_a2a_agent("a1"))
    assert result["ok"] is False
    assert captured


def test_probe_fetch_error_falls_back_to_record(monkeypatch):
    captured = []
    _configure(captured)
    record = {"id": "a1", "base_url": "https://host"}
    _patch_registry(monkeypatch, get=lambda _aid: record,
                    update_probe_result=lambda *a, **k: None)

    async def _fetch(_u):
        raise AgentCardValidationError("bad")

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)

    result = _run(a2a_api.probe_a2a_agent("a1"))
    assert result is record


def test_probe_success_updates_with_card(monkeypatch):
    captured = []
    _configure(captured)
    record = {"id": "a1", "base_url": "https://host"}

    def _upd(*a, **k):
        return {"id": "a1", "ok": True, "agent_card": k.get("agent_card")}

    _patch_registry(monkeypatch, get=lambda _aid: record,
                    update_probe_result=_upd)

    async def _fetch(_u):
        return {"card": "fresh"}

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)

    result = _run(a2a_api.probe_a2a_agent("a1"))
    assert result["ok"] is True
    assert result["agent_card"] == {"card": "fresh"}
    assert captured


def test_probe_success_falls_back_to_record(monkeypatch):
    captured = []
    _configure(captured)
    record = {"id": "a1", "base_url": "https://host"}
    _patch_registry(monkeypatch, get=lambda _aid: record,
                    update_probe_result=lambda *a, **k: None)

    async def _fetch(_u):
        return {"card": "fresh"}

    monkeypatch.setattr(a2a_api, "fetch_agent_card", _fetch)

    result = _run(a2a_api.probe_a2a_agent("a1"))
    assert result is record


# --- delegate_to_a2a_agent ------------------------------------------------

def _delegate_body(**over):
    base = dict(app_session_id="sess-1", instructions="do thing")
    base.update(over)
    return a2a_api.A2ADelegateRequest(**base)


def test_delegate_not_found_404(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: None)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.delegate_to_a2a_agent("ghost", _delegate_body()))
    assert exc.value.status_code == 404


def test_delegate_empty_session_id_400(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: {"id": "a1"})
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.delegate_to_a2a_agent("a1", _delegate_body(app_session_id="  ")))
    assert exc.value.status_code == 400


def test_delegate_empty_instructions_400(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: {"id": "a1"})
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.delegate_to_a2a_agent("a1", _delegate_body(instructions="")))
    assert exc.value.status_code == 400


def test_delegate_instructions_too_long_400(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: {"id": "a1"})
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.delegate_to_a2a_agent(
            "a1", _delegate_body(instructions="y" * (a2a_api._MAX_INSTRUCTIONS_LEN + 1))))
    assert exc.value.status_code == 400


def test_delegate_session_not_found_404(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: {"id": "a1"})

    async def _no_session(_sid):
        return False

    monkeypatch.setattr("session_helpers.session_exists", _no_session)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.delegate_to_a2a_agent("a1", _delegate_body()))
    assert exc.value.status_code == 404


def test_delegate_runner_value_error_400(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: {"id": "a1"})

    async def _yes_session(_sid):
        return True

    async def _raises(**kw):
        raise ValueError("bad delegation")

    monkeypatch.setattr("session_helpers.session_exists", _yes_session)
    monkeypatch.setattr("a2a.delegation.run_a2a_delegation", _raises)
    with pytest.raises(HTTPException) as exc:
        _run(a2a_api.delegate_to_a2a_agent("a1", _delegate_body()))
    assert exc.value.status_code == 400


def test_delegate_happy_returns_id(monkeypatch):
    _configure([])
    _patch_registry(monkeypatch, get=lambda _aid: {"id": "a1"})

    async def _yes_session(_sid):
        return True

    async def _runner(**kw):
        return "delegation-9"

    monkeypatch.setattr("session_helpers.session_exists", _yes_session)
    monkeypatch.setattr("a2a.delegation.run_a2a_delegation", _runner)
    result = _run(a2a_api.delegate_to_a2a_agent("a1", _delegate_body()))
    assert result == {"delegation_id": "delegation-9"}
