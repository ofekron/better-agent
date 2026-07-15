"""BFF chat-tree read path: rendering cache → formal chat tree.

Serves the formal chat tree (`chat-panel.md` grammar, the shape
`frontend/src/chat/parseProjection.ts` accepts) from the BFF's own
rendering cache: stored canonical facts adapt through
`chat_canonical_adapter` into `chat_projector.project_chat`, and the
result serializes via `chat_tree_wire`.

The runtime stays authoritative: the session snapshot (and provider
identity) comes from `projection-source`, and a root missing from the
cache is marked dirty on the feed client so the next read finds it.
Failures are typed — no stale success, no silent fallbacks.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

import re

import bff_chat_feed
import chat_projection_ingestion
from bff_runtime_service import RuntimeServiceError, runtime_service
from bff_runtime_upstream import RuntimeUpstreamUnavailable
from chat_canonical_adapter import ChatAdapterError, adapt_chat_inputs
from chat_models import CHAT_SCHEMA_VERSION
from chat_projector import ChatProjectionInputError, project_chat
from chat_projection_service import ProjectionServiceError
from chat_tree_wire import chat_to_wire

router = APIRouter()

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_READ_PAGE = 1000
_PROVIDER_KINDS = {"claude", "codex", "gemini"}


def _read_stored_facts(root_id: str, provider: str) -> list[dict[str, Any]]:
    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider=provider, session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    facts: list[dict[str, Any]] = []
    after = 0
    while True:
        page = service.read_facts(authority, after=after, limit=_READ_PAGE)
        facts.extend(dict(stored.canonical_fact) for stored in page)
        if len(page) < _READ_PAGE:
            return facts
        after = page[-1].fact_sequence


@router.get("/api/chat-tree/{session_id}")
async def get_chat_tree(session_id: str):
    if not _SESSION_ID.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    try:
        source = await runtime_service.projection_source(
            session_id, after_seq=0, limit=1,
        )
    except (RuntimeServiceError, RuntimeUpstreamUnavailable) as exc:
        raise HTTPException(status_code=503, detail="runtime unavailable") from exc
    if source.get("found") is not True:
        raise HTTPException(status_code=404, detail="session not found")
    session = source.get("session")
    provider = source.get("provider_kind")
    if not isinstance(session, dict) or provider not in _PROVIDER_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"code": "provider_identity_unresolvable",
                    "message": "session provider identity is unavailable"},
        )
    try:
        facts = await asyncio.to_thread(_read_stored_facts, session_id, provider)
    except ProjectionServiceError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": exc.detail},
        ) from exc
    if not facts:
        # Cache miss (e.g. root created while the feed was offline): ask
        # the feed to pull; the client retries after the cache warms.
        bff_chat_feed.feed_client.mark_dirty(session_id)
        raise HTTPException(
            status_code=503,
            detail={"code": "chat_tree_rebuilding",
                    "message": "rendering cache is warming for this session"},
            headers={"Retry-After": "2"},
        )
    try:
        adapted = await asyncio.to_thread(adapt_chat_inputs, facts, session)
        chat = await asyncio.to_thread(
            project_chat, adapted.messages, adapted.events,
            schema_version=CHAT_SCHEMA_VERSION,
        )
    except (ChatAdapterError, ChatProjectionInputError) as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return {
        "session_id": session_id,
        "schema_version": CHAT_SCHEMA_VERSION,
        "items": chat_to_wire(chat),
        "dropped": list(adapted.dropped),
    }
