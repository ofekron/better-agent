from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Query

import file_panel_drafts
import app_chat_draft_store
from bff_event_hub import hub


router = APIRouter()
_CHAT_DRAFT_PATH = re.compile(r"^/api/sessions/[A-Za-z0-9_-]+/draft$")


def owns_path(method: str, path: str) -> bool:
    if path == "/api/file/draft":
        return method in {"GET", "POST", "DELETE"}
    return method == "PATCH" and _CHAT_DRAFT_PATH.fullmatch(path) is not None


def chat_draft_session_id(method: str, path: str) -> str | None:
    if method != "PATCH":
        return None
    match = _CHAT_DRAFT_PATH.fullmatch(path)
    if match is None:
        return None
    return path.split("/")[3]


@router.patch("/api/sessions/{session_id}/draft")
async def save_chat_draft(session_id: str, body: dict):
    try:
        result = await asyncio.to_thread(
            app_chat_draft_store.update,
            session_id,
            draft_input=body.get("draft_input"),
            client_seq=body.get("client_seq"),
            draft_images=body.get("draft_images"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("rejected"):
        patch = {
            "draft_input": result["draft_input"],
            "draft_input_seq": result["draft_input_seq"],
            "draft_images": result["draft_images"],
        }
        await hub.publish_session(
            session_id,
            {
                "type": "session_metadata_updated",
                "data": {
                    "session_id": session_id,
                    "patch": patch,
                    "client_id": body.get("client_id"),
                },
            },
        )
    return result


@router.get("/api/file/draft")
async def get_file_draft(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await asyncio.to_thread(file_panel_drafts.read_draft, path, node_id)


@router.post("/api/file/draft")
async def save_file_draft(body: dict):
    try:
        return await asyncio.to_thread(
            file_panel_drafts.write_draft,
            path=body.get("path"),
            node_id=body.get("node_id") or "primary",
            content=body.get("content"),
            base_identity=body.get("base_identity"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/file/draft")
async def delete_file_draft(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await asyncio.to_thread(file_panel_drafts.delete_draft, path, node_id)
