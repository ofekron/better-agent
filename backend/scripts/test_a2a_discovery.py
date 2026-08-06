"""Agent-card validation (fail-closed) and discovery fetch. HTTP is
mocked via httpx.MockTransport — no real network calls, mirroring
scripts/test_runner_better_agent_codex_subscription.py's convention."""
from __future__ import annotations

import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx
import pytest

from a2a.discovery import AgentCardFetchError, fetch_agent_card
from a2a.models import AgentCardValidationError, validate_agent_card
from a2a.url_policy import A2AUrlPolicyError

_VALID_CARD = {
    "name": "Weather Agent",
    "description": "Reports weather.",
    "url": "https://agent.example.com/a2a",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"id": "weather", "name": "Weather", "description": "Get weather", "tags": ["weather"]},
    ],
}


def test_valid_card_passes():
    assert validate_agent_card(dict(_VALID_CARD)) == _VALID_CARD


def test_non_dict_rejected():
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(["not", "a", "dict"])


@pytest.mark.parametrize("field", ["name", "description", "url", "version"])
def test_missing_required_string_field_rejected(field):
    card = dict(_VALID_CARD)
    del card[field]
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


def test_capabilities_not_dict_rejected():
    card = dict(_VALID_CARD)
    card["capabilities"] = "streaming"
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


def test_default_modes_not_string_list_rejected():
    card = dict(_VALID_CARD)
    card["defaultInputModes"] = "text/plain"
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


def test_skills_not_list_rejected():
    card = dict(_VALID_CARD)
    card["skills"] = {"id": "weather"}
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


def test_skill_missing_field_rejected():
    card = dict(_VALID_CARD)
    card["skills"] = [{"id": "weather", "name": "Weather"}]  # missing description
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    return lambda *a, **k: _RealAsyncClient(*a, transport=httpx.MockTransport(handler), **k)


def _run(coro):
    return asyncio.run(coro)


def test_fetch_agent_card_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://agent.example.com/.well-known/agent-card.json"
        return httpx.Response(200, content=json.dumps(_VALID_CARD))

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    card = _run(fetch_agent_card("https://agent.example.com"))
    assert card["name"] == "Weather Agent"


def test_fetch_agent_card_http_error_raises_fetch_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(AgentCardFetchError):
        _run(fetch_agent_card("https://agent.example.com"))


def test_fetch_agent_card_invalid_shape_raises_validation_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"name": "x"}))

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(AgentCardValidationError):
        _run(fetch_agent_card("https://agent.example.com"))


def test_fetch_agent_card_rejects_non_loopback_http():
    with pytest.raises(A2AUrlPolicyError):
        _run(fetch_agent_card("http://agent.example.com"))
