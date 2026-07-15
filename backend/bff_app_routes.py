from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Query, Request

import app_user_prefs
import project_mapping_store
import project_store
import file_panel_drafts
import app_chat_draft_store
from bff_event_hub import hub
from bff_runtime_service import (
    RUNTIME_PREFERENCE_KEYS,
    RuntimeServiceError,
    runtime_service,
)
from bff_runtime_contract import project_candidate_from_session
import ui_selection


router = APIRouter()
_CHAT_DRAFT_PATH = re.compile(r"^/api/sessions/[A-Za-z0-9_-]+/draft$")
_PROJECT_MAPPING_PATH = re.compile(r"^/api/project-mappings/[^/]+$")
_CHAT_FEED_STATUS_PATH = re.compile(r"^/api/chat-feed/[A-Za-z0-9_-]+/status$")


def owns_path(method: str, path: str) -> bool:
    if _CHAT_FEED_STATUS_PATH.fullmatch(path):
        return method == "GET"
    if path == "/api/file/draft":
        return method in {"GET", "POST", "DELETE"}
    if path == "/api/ui-selection":
        return method in {"GET", "PATCH"}
    if path == "/api/user-prefs":
        return method in {"GET", "PATCH"}
    if path == "/api/projects":
        return method in {"GET", "POST", "DELETE"}
    if path == "/api/projects/touch":
        return method == "POST"
    if path == "/api/project-mappings":
        return method == "GET"
    if path == "/api/project-mappings/rebuild":
        return method == "POST"
    if _PROJECT_MAPPING_PATH.fullmatch(path):
        return method in {"PATCH", "DELETE"}
    if path == "/api/sessions":
        return method == "POST"
    return method == "PATCH" and _CHAT_DRAFT_PATH.fullmatch(path) is not None


@router.get("/api/chat-feed/{session_id}/status")
async def chat_feed_status(session_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    import bff_chat_feed

    return bff_chat_feed.feed_client.status(session_id)


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
            base_content=body.get("base_content"),
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


def _request_username(request: Request) -> str | None:
    user = getattr(request.state, "auth_user", None)
    return user.get("username") if isinstance(user, dict) else None


@router.get("/api/user-prefs")
async def get_user_prefs(request: Request):
    username = _request_username(request)
    try:
        runtime = await runtime_service.get_preferences()
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    app_prefs = await asyncio.to_thread(app_user_prefs.get_all, username)
    return {**runtime, **app_prefs}


@router.patch("/api/user-prefs")
async def patch_user_prefs(request: Request, body: dict):
    username = _request_username(request)
    app_patch = {key: value for key, value in body.items() if key in app_user_prefs.APP_PREFERENCE_KEYS}
    runtime_patch = {key: value for key, value in body.items() if key in RUNTIME_PREFERENCE_KEYS}
    try:
        app_user_prefs.validate_patch(app_patch)
        runtime = (
            await runtime_service.patch_preferences(runtime_patch)
            if runtime_patch
            else await runtime_service.get_preferences()
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


async def _sync_project_catalog(*, publish: bool) -> list[dict]:
    projects = await asyncio.to_thread(project_store.list_projects)
    try:
        await runtime_service.sync_project_catalog(projects)
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await asyncio.to_thread(project_mapping_store.rebuild_and_save, projects)
    if publish:
        await hub.publish_global({"type": "projects_changed", "data": {}})
        await hub.publish_global({"type": "project_mappings_changed", "data": {}})
    return projects


async def initialize_app_projects() -> None:
    facts = await _runtime_project_facts()
    await asyncio.to_thread(
        project_store.seed_from_session_candidates,
        facts.get("candidates") or [],
    )
    await asyncio.to_thread(project_store.backfill_git_remotes)
    await _sync_project_catalog(publish=False)


async def _runtime_project_facts() -> dict:
    try:
        return await runtime_service.project_facts()
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/api/projects")
async def get_projects():
    facts = await _runtime_project_facts()
    aggregates = {
        (item.get("path") or "", item.get("node_id") or "primary"): item
        for item in facts.get("aggregates") or []
        if isinstance(item, dict)
    }
    projects = await asyncio.to_thread(project_store.list_projects)
    return {
        "projects": [
            {
                **project,
                "running_count": aggregates.get(
                    (project.get("path") or "", project.get("node_id") or "primary"),
                    {},
                ).get("running_count", 0),
                "unread_session_count": aggregates.get(
                    (project.get("path") or "", project.get("node_id") or "primary"),
                    {},
                ).get("unread_session_count", 0),
            }
            for project in projects
        ]
    }


@router.get("/api/projects/status")
async def get_project_status():
    try:
        status = await runtime_service.project_status()
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"projects": status.get("aggregates") or []}


@router.post("/api/projects")
async def create_project(body: dict):
    record = await asyncio.to_thread(
        project_store.add_project,
        path=body.get("path", ""),
        name=body.get("name") or None,
        node_id=body.get("node_id") or "primary",
    )
    if not record:
        raise HTTPException(status_code=400, detail="Invalid project path")
    await _sync_project_catalog(publish=True)
    return record


@router.delete("/api/projects")
async def delete_project(path: str = Query(...), node_id: str = Query("primary")):
    deleted = await asyncio.to_thread(
        project_store.remove_project, path, node_id=node_id
    )
    if deleted:
        await _sync_project_catalog(publish=True)
    return {"deleted": deleted}


@router.post("/api/projects/touch")
async def touch_project(body: dict):
    await asyncio.to_thread(
        project_store.touch_project,
        body.get("path", ""),
        node_id=body.get("node_id") or "primary",
    )
    await _sync_project_catalog(publish=True)
    return {"status": "ok"}


@router.get("/api/project-mappings")
async def get_project_mappings():
    return {"groups": await asyncio.to_thread(project_mapping_store.list_mappings)}


@router.post("/api/project-mappings/rebuild")
async def rebuild_project_mappings():
    projects = await asyncio.to_thread(project_store.list_projects)
    groups = await asyncio.to_thread(project_mapping_store.rebuild_and_save, projects)
    await hub.publish_global({"type": "project_mappings_changed", "data": {}})
    return {"groups": groups}


@router.patch("/api/project-mappings/{group_id}")
async def update_project_mapping(group_id: str, body: dict):
    result = await asyncio.to_thread(
        project_mapping_store.update_group,
        group_id,
        label=body.get("label"),
        members=body.get("members"),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Mapping group not found")
    await hub.publish_global({"type": "project_mappings_changed", "data": {}})
    return result


@router.delete("/api/project-mappings/{group_id}")
async def delete_project_mapping(group_id: str):
    deleted = await asyncio.to_thread(project_mapping_store.remove_group, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Mapping group not found")
    await hub.publish_global({"type": "project_mappings_changed", "data": {}})
    return {"deleted": True}


@router.post("/api/sessions")
async def create_session(body: dict):
    try:
        session = await runtime_service.create_session(body)
    except RuntimeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    candidate = project_candidate_from_session(session)
    if candidate is not None:
        record = await asyncio.to_thread(
            project_store.add_project,
            candidate["path"],
            name=candidate.get("name") or None,
            node_id=candidate.get("node_id") or "primary",
        )
        if record is not None:
            await _sync_project_catalog(publish=True)
    return session
