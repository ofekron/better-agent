"""Agent-card discovery: fetch and strictly validate
`/.well-known/agent-card.json` from a remote A2A agent's base URL."""
from __future__ import annotations

import httpx

from a2a.models import AgentCardValidationError, validate_agent_card
from a2a.url_policy import validate_base_url

_DISCOVERY_TIMEOUT = 10.0


class AgentCardFetchError(RuntimeError):
    pass


async def fetch_agent_card(base_url: str) -> dict:
    """GET {base_url}/.well-known/agent-card.json and strictly validate
    the response shape. Raises AgentCardFetchError on any network/HTTP
    failure and AgentCardValidationError on a malformed card — callers
    must not persist a card that failed either check."""
    normalized = validate_base_url(base_url)
    url = f"{normalized}/.well-known/agent-card.json"
    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise AgentCardFetchError(f"failed to reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise AgentCardFetchError(f"{url} returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentCardFetchError(f"{url} did not return valid JSON: {exc}") from exc
    return validate_agent_card(data)
