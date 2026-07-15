"""SDK Client backend-URL validation locks.

Locks:
- Client rejects a malformed backend URL at construction with an actionable
  BetterAgentError instead of failing deep inside urllib at request time
  (regression: a startup stdout leak produced
  "http://127.0.0.1:Stopping previous Better Agent BFF process(es): 58127\n18765",
  surfacing as an opaque "nonnumeric port" from every extension MCP call)
- valid URLs (including the default) still resolve unchanged
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))

from better_agent_sdk import BetterAgentError, Client

_DIRTY_URL = (
    "http://127.0.0.1:Stopping previous Better Agent BFF process(es): 58127\n18765"
)

_ENV_NAMES = ("BETTER_AGENT_BACKEND_URL", "BETTER_CLAUDE_BACKEND_URL")


@pytest.fixture(autouse=True)
def _clean_backend_url_env(monkeypatch: pytest.MonkeyPatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.mark.parametrize(
    "url",
    [
        _DIRTY_URL,
        "http://127.0.0.1: 58127\n18765",
        "http://127.0.0.1:58127 18765",
        "http://127.0.0.1:abc",
        "http://127.0.0.1:99999",
        "ftp://127.0.0.1:18765",
        "127.0.0.1:18765/no/scheme",
    ],
)
def test_client_rejects_malformed_backend_url_override(url: str) -> None:
    with pytest.raises(BetterAgentError, match="invalid backend URL"):
        Client(backend_url=url)


def test_client_rejects_malformed_backend_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _ENV_NAMES:
        monkeypatch.setenv(name, _DIRTY_URL)
    with pytest.raises(BetterAgentError, match="invalid backend URL"):
        Client()


def test_client_accepts_valid_backend_urls() -> None:
    assert Client(backend_url="http://127.0.0.1:18765/").backend_url == "http://127.0.0.1:18765"
    assert Client(backend_url="https://localhost:443").backend_url == "https://localhost:443"
    assert Client().backend_url == "http://localhost:8000"
