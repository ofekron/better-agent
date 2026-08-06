"""REST + WS-broadcast routes for the outbound A2A agent registry:
list/add/remove/probe registered remote agents, and trigger a
worker-panel delegation to one. Mirrors `runtime_profiles_api.py`'s
shape (router skeleton, `configure(broadcast_global)`, snapshot +
broadcast-on-change)."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import a2a_registry_store as registry
from a2a.discovery import AgentCardFetchError, fetch_agent_card
from a2a.models import AgentCardValidationError
from a2a.url_policy import A2AUrlPolicyError, validate_base_url

router = APIRouter()
logger = logging.getLogger(__name__)

_broadcast_global: Optional[Callable[[str, dict], Any]] = None

_MAX_NAME_LEN = 200
_MAX_HEADER_NAME_LEN = 200
_MAX_SECRET_LEN = 4096
_MAX_INSTRUCTIONS_LEN = 20000
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,200}$")


def configure(broadcast_global: Callable[[str, dict], Any]) -> None:
    global _broadcast_global
    _broadcast_global = broadcast_global


def _require_configured() -> Callable[[str, dict], Any]:
    if _broadcast_global is None:
        raise HTTPException(status_code=503, detail="A2A API is not configured")
    return _broadcast_global


def _snapshot() -> dict:
    return {"agents": [registry.redact(record) for record in registry.list_all()]}


async def _broadcast_changed() -> None:
    broadcast_global = _require_configured()
    payload = await asyncio.to_thread(_snapshot)
    await broadcast_global("a2a_registry_changed", payload)


class A2AAgentCreate(BaseModel):
    name: str
    base_url: str
    auth_header_name: str = ""
    auth_secret: str = ""


class A2ADelegateRequest(BaseModel):
    app_session_id: str
    instructions: str


@router.get("/api/a2a/agents")
async def list_a2a_agents():
    _require_configured()
    return await asyncio.to_thread(_snapshot)


@router.post("/api/a2a/agents")
async def add_a2a_agent(body: A2AAgentCreate):
    _require_configured()
    name = body.name.strip()
    if not name or len(name) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail="name must be 1-200 characters")
    header_name = body.auth_header_name.strip()
    if header_name and not _HEADER_NAME_RE.fullmatch(header_name):
        raise HTTPException(status_code=400, detail="auth_header_name has invalid characters")
    if len(body.auth_secret) > _MAX_SECRET_LEN:
        raise HTTPException(status_code=400, detail="auth_secret is too long")
    try:
        normalized_url = validate_base_url(body.base_url)
    except A2AUrlPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        agent_card = await fetch_agent_card(normalized_url)
    except AgentCardFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AgentCardValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = await asyncio.to_thread(
        registry.add,
        name=name,
        base_url=normalized_url,
        auth_header_name=header_name,
        auth_secret=body.auth_secret,
        agent_card=agent_card,
    )
    await _broadcast_changed()
    return registry.redact(record)


@router.delete("/api/a2a/agents/{agent_id}")
async def remove_a2a_agent(agent_id: str):
    _require_configured()
    removed = await asyncio.to_thread(registry.remove, agent_id)
    if not removed:
        raise HTTPException(status_code=404, detail="A2A agent not found")
    await _broadcast_changed()
    return {"deleted": True}


@router.post("/api/a2a/agents/{agent_id}/probe")
async def probe_a2a_agent(agent_id: str):
    _require_configured()
    record = await asyncio.to_thread(registry.get, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="A2A agent not found")
    try:
        agent_card = await fetch_agent_card(record["base_url"])
    except (AgentCardFetchError, AgentCardValidationError) as exc:
        updated = await asyncio.to_thread(
            registry.update_probe_result, agent_id, ok=False, error=str(exc),
        )
        await _broadcast_changed()
        return registry.redact(updated or record)
    updated = await asyncio.to_thread(
        registry.update_probe_result, agent_id, ok=True, error=None, agent_card=agent_card,
    )
    await _broadcast_changed()
    return registry.redact(updated or record)


@router.post("/api/a2a/agents/{agent_id}/delegate")
async def delegate_to_a2a_agent(agent_id: str, body: A2ADelegateRequest):
    _require_configured()
    record = await asyncio.to_thread(registry.get, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="A2A agent not found")
    app_session_id = body.app_session_id.strip()
    instructions = body.instructions.strip()
    if not app_session_id:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    if not instructions or len(instructions) > _MAX_INSTRUCTIONS_LEN:
        raise HTTPException(
            status_code=400, detail="instructions must be 1-20000 characters",
        )
    from session_helpers import session_exists
    if not await session_exists(app_session_id):
        raise HTTPException(status_code=404, detail="session not found")
    from a2a.delegation import run_a2a_delegation
    try:
        delegation_id = await run_a2a_delegation(
            app_session_id=app_session_id,
            agent_record=record,
            instructions=instructions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"delegation_id": delegation_id}
