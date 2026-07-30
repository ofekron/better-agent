"""Internal HTTP surface for the Ask UI session."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Header, HTTPException

import extension_store
import session_search
from internal_guards import require_builtin_runtime_extension, require_internal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask-ui"])


def _require_ask_internal(x_internal_token: str) -> None:
    require_internal()
    require_builtin_runtime_extension(extension_store.BUILTIN_ASK_EXTENSION_ID)

@router.post("/api/internal/ask-ui/search")
async def internal_ask_ui_search(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_ask_internal(x_internal_token)
    body = body or {}
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(
            status_code=400, detail="query must be a non-empty string",
        )
    max_results = body.get("max_results")
    timeout = body.get("timeout")
    kwargs: dict = {}
    if isinstance(max_results, int) and max_results > 0:
        kwargs["max_results"] = max_results
    if isinstance(timeout, (int, float)) and timeout > 0:
        kwargs["timeout"] = float(timeout)
    for key in ("provider_id", "model", "reasoning_effort", "node_id"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            kwargs[key] = val.strip()
    return await session_search.search(query, **kwargs)

@router.post("/api/internal/ask-ui/search-sessions")
async def internal_ask_ui_search_sessions(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_ask_internal(x_internal_token)
    body = body or {}
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(
            status_code=400, detail="query must be a non-empty string",
        )
    max_results = body.get("max_results")
    timeout = body.get("timeout")
    kwargs: dict = {}
    if isinstance(max_results, int) and max_results > 0:
        kwargs["max_results"] = max_results
    if isinstance(timeout, (int, float)) and timeout > 0:
        kwargs["timeout"] = float(timeout)
    for key in ("provider_id", "model", "reasoning_effort", "node_id", "cwd"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            kwargs[key] = val.strip()
    folder = body.get("folder")
    if isinstance(folder, str) and folder.strip():
        kwargs["folder_id"] = folder.strip()
    raw_tags = body.get("tags")
    if isinstance(raw_tags, list):
        cleaned_tags = [t for t in raw_tags if isinstance(t, str) and t.strip()]
        if cleaned_tags:
            kwargs["tag_ids"] = cleaned_tags
    result = await session_search.run_search_sessions_session(query, **kwargs)
    return await asyncio.to_thread(
        session_search.canonical_search_response, result,
    )

@router.post("/api/internal/ask-ui/ensure")
async def internal_ask_ui_ensure(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_ask_internal(x_internal_token)
    return await session_search.ensure_ask_session()
