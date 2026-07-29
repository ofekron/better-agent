"""Internal HTTP surface for the assistant-UI session.

Every route is gated on internal authority plus the assistant
extension's runtime readiness, and delegates to `assistant_ui`.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Header, HTTPException

import assistant_ui
from internal_guards import require_role_internal

router = APIRouter(tags=["assistant-ui"])

_ROLE = "assistant"


def _board_preamble(body: dict | None) -> str | None:
    if isinstance(body, dict) and "board_preamble" in body:
        return str(body.get("board_preamble") or "")
    return None


def _session_summary(sess: dict) -> dict:
    return {"id": sess["id"], "name": sess.get("name"), "cwd": sess.get("cwd")}


@router.post("/api/internal/assistant-ui/ensure")
async def internal_assistant_ui_ensure(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    sess = await asyncio.to_thread(assistant_ui.ensure_singleton, _board_preamble(body))
    return _session_summary(sess)


@router.post("/api/internal/assistant-ui/ensure-monitor")
async def internal_assistant_ui_ensure_monitor(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    sess = await asyncio.to_thread(assistant_ui.ensure_monitor, _board_preamble(body))
    return _session_summary(sess)


@router.post("/api/internal/assistant-ui/search")
async def internal_assistant_ui_search(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    return await assistant_ui.search(
        str(body.get("query") or ""),
        max_results=int(body.get("max_results") or 10),
    )


@router.post("/api/internal/assistant-ui/resolve-ba-session")
async def internal_assistant_ui_resolve_ba_session(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    return await assistant_ui.resolve_ba_session(str(body.get("session_id") or ""))


@router.post("/api/internal/assistant-ui/adopt-native-session")
async def internal_assistant_ui_adopt_native_session(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    return await assistant_ui.adopt_native_session(
        str(body.get("session_id") or ""),
        transcript_path=str(body.get("transcript_path") or ""),
    )


@router.post("/api/internal/assistant-ui/delegate")
async def internal_assistant_ui_delegate(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    target = str(body.get("target_session_id") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    if not target or not prompt:
        raise HTTPException(status_code=400, detail="target_session_id and prompt are required")
    return await assistant_ui.delegate(target, prompt)


@router.post("/api/internal/assistant-ui/last-turn")
async def internal_assistant_ui_last_turn(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    return await asyncio.to_thread(assistant_ui.last_turn, sid)
