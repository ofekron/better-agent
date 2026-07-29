"""Project-structure-edit internal routes, project config scan, hooks
CRUD, and push-token registration/preferences.

Depends on the coordinator only through the request-principal
resolver the project-structure-edit routes need, bound by the
composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

import device_token_store
import extension_api
import hook_store
import internal_guards
import project_structure_edit_session
from bounded_async_executor import AdmissionOverloaded
from i18n import t
from node_op import node_op

router = APIRouter()

_request_principal_async: Optional[Callable[[Request, str], Any]] = None


def configure(request_principal_async: Callable[[Request, str], Any]) -> None:
    """Bind the coordinator capability this router needs."""
    global _request_principal_async
    _request_principal_async = request_principal_async


async def _require_project_structure_internal_async(
    request: Request, x_internal_token: str,
) -> None:
    if _request_principal_async is None:
        raise HTTPException(status_code=503, detail="hooks/push API is not configured")
    principal = await _request_principal_async(request, x_internal_token)
    if principal is None:
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    try:
        role_owner = await extension_api.core_role_owner_async('project-structure')
    except AdmissionOverloaded as exc:
        raise HTTPException(
            status_code=503,
            detail="extension routing is busy; retry shortly",
            headers={"Retry-After": "1"},
        ) from exc
    internal_guards.require_builtin_runtime_extension(role_owner)
    if principal != ("extension", role_owner):
        raise HTTPException(status_code=403, detail="project-structure extension is required")


@router.post("/api/internal/project-structure-edit/status")
async def internal_project_structure_edit_status(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_structure_internal_async(request, x_internal_token)
    cwd = (body or {}).get("cwd") or os.getcwd()
    return await asyncio.to_thread(project_structure_edit_session.get_edit_status, cwd)


@router.post("/api/internal/project-structure-edit/ensure")
async def internal_project_structure_edit_ensure(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await _require_project_structure_internal_async(request, x_internal_token)
    cwd = (body or {}).get("cwd") or os.getcwd()
    prompt_result = await project_structure_edit_session.submit_review_prompt(cwd)
    return {
        "session_id": project_structure_edit_session.EDIT_SINGLETON_ID,
        **prompt_result,
    }


@router.get("/api/project-config")
async def get_project_config(
    cwd: str = Query(...),
    node_id: str = Query("primary"),
):
    result = await node_op(node_id, "scan_project_configs", {"cwd": cwd})
    # rpc handler returns the raw scan dict; legacy endpoint shape
    # wraps it as {"files": ...}. Preserve that envelope.
    return {"files": result}


@router.get("/api/hooks")
async def get_hooks():
    try:
        return {"hooks": await asyncio.to_thread(hook_store.list_hooks)}
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/api/hooks")
async def put_hooks(body: dict):
    hooks = body.get("hooks")
    if not isinstance(hooks, list):
        raise HTTPException(status_code=400, detail="hooks must be a list")
    try:
        return {"hooks": await asyncio.to_thread(hook_store.replace_hooks, hooks)}
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/hooks")
async def post_hook(body: dict):
    try:
        return {"hook": await asyncio.to_thread(hook_store.upsert_hook, body)}
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/hooks/{hook_id}")
async def delete_hook(hook_id: str):
    try:
        deleted = await asyncio.to_thread(hook_store.delete_hook, hook_id)
    except hook_store.HookConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="hook not found")
    return {"ok": True}


_PUSH_TOKEN_MAX_LEN = 4096
_PUSH_DEVICE_ID_MAX_LEN = 256
_PUSH_SESSION_ID_MAX_LEN = 256
_PUSH_PLATFORMS = ("android", "ios")


@router.post("/api/push-tokens")
async def register_push_token(body: dict):
    device_id_raw = body.get("device_id")
    token_raw = body.get("token")
    platform_raw = body.get("platform")
    session_id_raw = body.get("session_id")
    device_id = device_id_raw.strip() if isinstance(device_id_raw, str) else ""
    token = token_raw.strip() if isinstance(token_raw, str) else ""
    platform = platform_raw.strip() if isinstance(platform_raw, str) else ""
    session_id = session_id_raw.strip() if isinstance(session_id_raw, str) else ""
    if not device_id or len(device_id) > _PUSH_DEVICE_ID_MAX_LEN:
        raise HTTPException(status_code=400, detail="device_id is required")
    if not token or len(token) > _PUSH_TOKEN_MAX_LEN:
        raise HTTPException(status_code=400, detail="token is required")
    if platform not in _PUSH_PLATFORMS:
        raise HTTPException(status_code=400, detail="platform must be android or ios")
    if not session_id or len(session_id) > _PUSH_SESSION_ID_MAX_LEN:
        raise HTTPException(status_code=400, detail="session_id is required")
    preferences = body.get("notification_preferences")
    try:
        record = await asyncio.to_thread(
            device_token_store.register_token,
            device_id,
            token,
            platform,
            session_id,
            preferences,
        )
    except device_token_store.NotificationPreferencesError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"device": record}


@router.delete("/api/push-tokens/{device_id}")
async def unregister_push_token(device_id: str):
    if not device_id or len(device_id) > _PUSH_DEVICE_ID_MAX_LEN:
        raise HTTPException(status_code=400, detail="invalid device_id")
    deleted = await asyncio.to_thread(device_token_store.unregister_token, device_id)
    return {"deleted": deleted}


@router.get("/api/push-tokens/{device_id}/notification-preferences")
async def get_push_notification_preferences(device_id: str):
    if not device_id or len(device_id) > _PUSH_DEVICE_ID_MAX_LEN:
        raise HTTPException(status_code=400, detail="invalid device_id")
    preferences = await asyncio.to_thread(
        device_token_store.get_notification_preferences,
        device_id,
    )
    return {"notification_preferences": preferences}


@router.patch("/api/push-tokens/{device_id}/notification-preferences")
async def patch_push_notification_preferences(device_id: str, body: dict):
    if not device_id or len(device_id) > _PUSH_DEVICE_ID_MAX_LEN:
        raise HTTPException(status_code=400, detail="invalid device_id")
    preferences = body.get("notification_preferences")
    try:
        updated = await asyncio.to_thread(
            device_token_store.update_notification_preferences,
            device_id,
            preferences,
        )
    except device_token_store.NotificationPreferencesError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"notification_preferences": updated}
