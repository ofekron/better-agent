from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Query, Request

import app_user_prefs
import file_panel_drafts
import app_chat_draft_store
from bff_event_hub import hub
from bff_runtime_service import (
    RUNTIME_PREFERENCE_KEYS,
    RuntimeServiceError,
    runtime_service,
)
import ui_selection


router = APIRouter()
_CHAT_DRAFT_PATH = re.compile(r"^/api/sessions/[A-Za-z0-9_-]+/draft$")


def owns_path(method: str, path: str) -> bool:
    if path == "/api/file/draft":
        return method in {"GET", "POST", "DELETE"}
    if path == "/api/ui-selection":
        return method in {"GET", "PATCH"}
    if path == "/api/user-prefs":
        return method in {"GET", "PATCH"}
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


@router.get("/api/ui-selection")
async def get_ui_selection():
    return await asyncio.to_thread(ui_selection.get_all)


@router.patch("/api/ui-selection")
async def patch_ui_selection(body: dict):
    def _patch_sync() -> dict:
        if "selected_project" in body:
            selected = body["selected_project"]
            if selected is None:
                ui_selection.set_selected_project("")
            elif isinstance(selected, dict):
                path = selected.get("path")
                if not isinstance(path, str):
                    raise ValueError("selected_project.path must be a string")
                node_id = selected.get("node_id", ui_selection.DEFAULT_NODE_ID)
                if not isinstance(node_id, str):
                    raise ValueError("selected_project.node_id must be a string")
                ui_selection.set_selected_project(path, node_id)
            else:
                raise ValueError("selected_project must be an object or null")
        if "remembered_session" in body:
            remembered = body["remembered_session"]
            if not isinstance(remembered, dict):
                raise ValueError("remembered_session must be an object")
            path = remembered.get("path")
            session_id = remembered.get("session_id")
            node_id = remembered.get("node_id", ui_selection.DEFAULT_NODE_ID)
            if not isinstance(path, str) or not path:
                raise ValueError("remembered_session.path must be a non-empty string")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("remembered_session.session_id must be a non-empty string")
            if not isinstance(node_id, str):
                raise ValueError("remembered_session.node_id must be a string")
            ui_selection.set_remembered_session(path, node_id, session_id)
        if "open_session_tab_ids" in body:
            open_ids = body["open_session_tab_ids"]
            if not isinstance(open_ids, list):
                raise ValueError("open_session_tab_ids must be a list")
            if any(not isinstance(session_id, str) or not session_id for session_id in open_ids):
                raise ValueError("open_session_tab_ids entries must be non-empty strings")
            ui_selection.set_open_session_tab_ids(open_ids)
        if "open_session_tab_joined_at" in body:
            joined_at = body["open_session_tab_joined_at"]
            if not isinstance(joined_at, dict):
                raise ValueError("open_session_tab_joined_at must be an object")
            if any(
                not isinstance(session_id, str)
                or not session_id
                or not isinstance(value, str)
                or not value
                for session_id, value in joined_at.items()
            ):
                raise ValueError("open_session_tab_joined_at entries must be non-empty strings")
            ui_selection.set_open_session_tab_joined_at(joined_at)
        return ui_selection.get_all()

    try:
        snapshot = await asyncio.to_thread(_patch_sync)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await hub.publish_global({"type": "ui_selection_changed", "data": snapshot})
    return snapshot


def _request_identity(request: Request) -> tuple[str | None, list[tuple[bytes, bytes]]]:
    user = getattr(request.state, "auth_user", None)
    username = user.get("username") if isinstance(user, dict) else None
    headers = getattr(request.state, "runtime_headers", None)
    if not isinstance(headers, list):
        raise HTTPException(status_code=503, detail="runtime identity unavailable")
    return username, headers


@router.get("/api/user-prefs")
async def get_user_prefs(request: Request):
    username, headers = _request_identity(request)
    try:
        runtime = await runtime_service.get_preferences(headers)
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    app_prefs = await asyncio.to_thread(app_user_prefs.get_all, username)
    return {**runtime, **app_prefs}


@router.patch("/api/user-prefs")
async def patch_user_prefs(request: Request, body: dict):
    username, headers = _request_identity(request)
    app_patch = {key: value for key, value in body.items() if key in app_user_prefs.APP_PREFERENCE_KEYS}
    runtime_patch = {key: value for key, value in body.items() if key in RUNTIME_PREFERENCE_KEYS}
    try:
        app_user_prefs.validate_patch(app_patch)
        runtime = (
            await runtime_service.patch_preferences(headers, runtime_patch)
            if runtime_patch
            else await runtime_service.get_preferences(headers)
        )
        if app_patch:
            app_prefs = await asyncio.to_thread(app_user_prefs.patch, app_patch, username)
        else:
            app_prefs = await asyncio.to_thread(app_user_prefs.get_all, username)
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    merged = {**runtime, **app_prefs}
    await hub.publish_global({"type": "user_prefs_changed", "data": merged})
    return merged
