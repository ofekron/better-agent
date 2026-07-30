"""Internal-loopback routes for the sessions an extension owns: the
ownership/permission gates, virtual-session CRUD, synthetic-message
injection, managed and headless runs, render-tree message writes, scoped
session-field read/write, and the session activity snapshot.

Every route here is gated on an extension permission (`session_state`,
`spawn_runs`, `mutates_session_fields`) rather than on a role, so they
stay together behind one pair of gates. The coordinator is injected by
the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException

import config_store
import extension_store
import internal_guards
import synthetic_messages
import ui_selection_projection
import virtual_session_store
from i18n import t
from provider_validation import (
    provider_runner as _provider_runner,
    resolve_provider_id_ref as _resolve_provider_id_ref,
)
from session_helpers import session_exists as _session_exists, session_lite as _session_lite
from session_manager import manager as session_manager

router = APIRouter()
logger = logging.getLogger(__name__)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator this router needs."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(
            status_code=503, detail="internal session-state API is not configured",
        )
    return _coordinator_ref


def _require_extension_session_ownership(
    x_internal_token: str, body: dict,
) -> tuple[str, str]:
    """SDK session-message mutation gate: verify the internal token AND that
    the calling extension owns the session named in the body. The caller's
    identity is derived from its per-extension token (the X-Extension-Id
    header is ignored), so an extension can only mutate sessions it created —
    never arbitrary sessions, and never by spoofing another extension's id.
    Returns (extension_id, session_id)."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    session_id = str((body or {}).get("session_id") or "").strip()
    import extension_session_ownership
    if not extension_session_ownership.is_owner(session_id, extension_id):
        raise HTTPException(status_code=403, detail="extension does not own this session")
    return extension_id, session_id


def _require_extension_permission(x_internal_token: str, permission: str) -> str:
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    record = extension_store.get_extension(extension_id) if extension_id else None
    if (
        record is None
        or not extension_store.is_extension_active(extension_id)
        or not extension_store.has_permission(record, permission)
    ):
        raise HTTPException(status_code=403, detail=f"extension lacks {permission} permission")
    return extension_id


@router.post("/api/internal/virtual-sessions/upsert")
async def internal_virtual_sessions_upsert(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "session_state")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    try:
        existing = await asyncio.to_thread(
            virtual_session_store.get,
            str(body.get("id") or ""),
        )
        session = await asyncio.to_thread(virtual_session_store.upsert, extension_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await _coordinator().broadcast_global(
        "session_metadata_updated" if existing else "session_created",
        {"session_id": session["id"], "patch": session} if existing else {"session": session},
    )
    return {"success": True, "session": session}


@router.post("/api/internal/virtual-sessions/delete")
async def internal_virtual_sessions_delete(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "session_state")
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        deleted = await asyncio.to_thread(virtual_session_store.delete, extension_id, session_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if deleted:
        await _coordinator().broadcast_global("session_deleted", {"session_id": session_id})
        await ui_selection_projection.close_tabs_for_deleted([session_id])
    return {"success": True, "deleted": deleted}


@router.post("/api/internal/virtual-sessions/append-message")
async def internal_virtual_sessions_append_message(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "session_state")
    session_id = str((body or {}).get("session_id") or "").strip()
    message = (body or {}).get("message")
    if not session_id or not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="session_id and message are required")
    try:
        appended = await asyncio.to_thread(
            virtual_session_store.append_message,
            extension_id,
            session_id,
            message,
        )
        session = await asyncio.to_thread(virtual_session_store.get, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        status = 403 if isinstance(exc, PermissionError) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    await _coordinator().broadcast_global(
        "session_metadata_updated",
        {"session_id": session_id, "patch": session or {}},
    )
    return {"success": True, "message": appended, "session": session}


@router.post("/api/internal/synthetic-messages/inject")
async def internal_synthetic_messages_inject(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    session_id = str(body.get("session_id") or "").strip()
    prompt = str(body.get("prompt") or "")
    try:
        return await synthetic_messages.inject(
            _coordinator(),
            session_id,
            prompt=prompt,
            model=str(body.get("model") or ""),
            cwd=str(body.get("cwd") or ""),
            orchestration_mode=str(body.get("orchestration_mode") or ""),
            client_id=str(body.get("client_id") or ""),
            source=str(body.get("source") or "synthetic"),
            display_prompt=str(body.get("display_prompt") or ""),
            capability_contexts=(
                body.get("capability_contexts")
                if isinstance(body.get("capability_contexts"), list)
                else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/internal/managed-runs/run")
async def internal_managed_run(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    managed_session_id = str(body.get("managed_session_id") or "").strip()
    if not managed_session_id:
        raise HTTPException(status_code=400, detail="managed_session_id is required")
    import extension_session_ownership
    if not extension_session_ownership.is_owner(managed_session_id, extension_id):
        raise HTTPException(status_code=403, detail="extension does not own this managed session")
    record = extension_store.get_extension(extension_id)
    declared_env = set(
        ((record or {}).get("manifest") or {}).get("permissions", {}).get("managed_run_env") or []
    )
    raw_env = body.get("extra_env") or {}
    if not isinstance(raw_env, dict):
        raise HTTPException(status_code=400, detail="extra_env must be an object")
    extra_env: dict[str, str] = {}
    for key, value in raw_env.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="invalid extra_env key")
        key = key.strip()
        if not key or "\x00" in key:
            raise HTTPException(status_code=400, detail="invalid extra_env key")
        if key not in declared_env:
            raise HTTPException(
                status_code=403,
                detail=f"extra_env key is not declared in permissions.managed_run_env: {key}",
            )
        value = str(value)
        if "\x00" in value or len(value) > 4096:
            raise HTTPException(status_code=400, detail=f"invalid extra_env value for {key}")
        extra_env[key] = value
    import extension_managed_runs
    try:
        return await extension_managed_runs.run(
            _coordinator(),
            managed_session_id=managed_session_id,
            parent_session_id=str(body.get("parent_session_id") or "").strip(),
            prompt=str(body.get("prompt") or ""),
            model=str(body.get("model") or ""),
            cwd=str(body.get("cwd") or ""),
            init_prompt=str(body.get("init_prompt") or ""),
            agent_sid=str(body.get("agent_sid") or "").strip(),
            event_prefix=str(body.get("event_prefix") or "managed_run").strip(),
            extra_env=extra_env,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/internal/managed-runs/create-session")
async def internal_managed_run_create_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    extension_id = _require_extension_permission(x_internal_token, "spawn_runs")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    parent_session_id = str(body.get("parent_session_id") or "").strip()
    parent = await _session_lite(parent_session_id) if parent_session_id else None
    if parent_session_id and not parent:
        raise HTTPException(status_code=400, detail="parent_session_id does not exist")
    name = str(body.get("name") or "").strip()
    cwd = str(body.get("cwd") or "").strip() or str((parent or {}).get("cwd") or "").strip()
    if not name or not cwd:
        raise HTTPException(status_code=400, detail="name and cwd are required")
    requested_provider_id = await _resolve_provider_id_ref(
        str(body.get("provider_id") or "").strip(),
    )
    provider_id = requested_provider_id or str((parent or {}).get("provider_id") or "").strip()
    provider_id = provider_id or None
    if provider_id and not await asyncio.to_thread(config_store.get_provider, provider_id):
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    runner_input = body.get("runner")
    if not str(runner_input or "").strip() and provider_id == (parent or {}).get("provider_id"):
        runner_input = (parent or {}).get("runner")
    runner = _provider_runner(provider_id, runner_input)
    model = str(body.get("model") or "").strip() or str((parent or {}).get("model") or "").strip()
    if not model:
        model = await asyncio.to_thread(config_store.default_session_model)
    node_id = str(body.get("node_id") or "").strip() or str((parent or {}).get("node_id") or "primary")
    sess = await asyncio.to_thread(
        lambda: session_manager.create(
            name=name,
            cwd=cwd,
            orchestration_mode="native",
            model=model,
            provider_id=provider_id,
            runner=runner,
            node_id=node_id,
            source="extension",
            browser_harness_enabled=False,
        )
    )
    import extension_session_ownership
    extension_session_ownership.claim(sess["id"], extension_id)
    if parent_session_id:
        await _coordinator().emit_session_created_panel(
            sender_session_id=parent_session_id,
            target_session=sess,
        )
    return {
        "success": True,
        "session_id": sess["id"],
        "name": sess.get("name") or name,
        "cwd": sess.get("cwd") or cwd,
        "model": sess.get("model"),
        "provider_id": sess.get("provider_id"),
        "runner": sess.get("runner"),
        "node_id": sess.get("node_id"),
    }


@router.post("/api/internal/headless-run")
async def internal_headless_run(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: one-shot non-interactive provider run (``claude -p`` style) — a
    fresh or forked headless run that returns a raw LLM result, NOT a managed
    turn touching the render tree. Gated by ``spawn_runs`` (same trust level
    as ask-fork / provisioned sessions). Used by externalized lifecycle
    extensions that need a raw result without a session turn."""
    extension_id = _require_extension_permission(x_internal_token, "spawn_runs")
    if type(body) is not dict:
        raise HTTPException(status_code=400, detail="body must be an object")
    allowed = {
        "app_session_id",
        "prompt",
        "fork",
        "resume",
        "no_tools",
        "timeout",
        "expected_provider_id",
        "expected_model",
    }
    if set(body) - allowed:
        raise HTTPException(status_code=400, detail="invalid headless-run fields")
    app_session_id = body.get("app_session_id")
    prompt = body.get("prompt")
    if type(app_session_id) is not str or not app_session_id.strip():
        raise HTTPException(status_code=400, detail="app_session_id is required")
    if type(prompt) is not str or not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    for key in ("fork", "resume", "no_tools"):
        if key in body and type(body[key]) is not bool:
            raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
    if body.get("fork") is True and body.get("resume") is True:
        raise HTTPException(status_code=400, detail="fork and resume are mutually exclusive")
    for key in ("expected_provider_id", "expected_model"):
        if key in body and type(body[key]) is not str:
            raise HTTPException(status_code=400, detail=f"{key} must be a string")
    timeout = body.get("timeout")
    if timeout is not None and type(timeout) not in (int, float):
        raise HTTPException(status_code=400, detail="timeout must be a number")
    from headless_admission import run_session_headless
    try:
        result = await run_session_headless(
            app_session_id.strip(),
            prompt=prompt,
            fork=body.get("fork") is True,
            resume=body.get("resume") is True,
            no_tools=body.get("no_tools") is True,
            timeout=float(timeout) if timeout is not None else None,
            permission_scope="spawn_runs",
            expected_provider_id=str(body.get("expected_provider_id") or ""),
            expected_model=str(body.get("expected_model") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("headless-run failed")
        raise HTTPException(status_code=502, detail="headless run failed") from exc
    logger.info(
        "headless-run by extension %s session=%s fork=%s",
        extension_id,
        app_session_id,
        body.get("fork") is True,
    )
    return result


@router.post("/api/internal/session-messages/append")
async def internal_session_messages_append(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: append a user or assistant message to a session the calling
    extension owns."""
    _extension_id, session_id = _require_extension_session_ownership(x_internal_token, body)
    import uuid
    from datetime import datetime
    role = str(body.get("role") or "").strip()
    content = str(body.get("content") or "")
    if role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'assistant'")
    msg = {
        "id": str(body.get("message_id") or uuid.uuid4().hex),
        "role": role,
        "content": content,
        "timestamp": str(body.get("timestamp") or datetime.now().isoformat()),
    }
    if role == "user":
        result = await asyncio.to_thread(
            session_manager.append_user_msg,
            session_id,
            msg,
        )
    else:
        msg.setdefault("events", [])
        msg["isStreaming"] = bool(body.get("is_streaming", False))
        result = await asyncio.to_thread(
            session_manager.append_assistant_msg,
            session_id,
            msg,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"success": True, "message": msg}


@router.post("/api/internal/session-messages/update-content")
async def internal_session_messages_update_content(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: overwrite the content of a message on an extension-owned session."""
    _extension_id, session_id = _require_extension_session_ownership(x_internal_token, body)
    msg_id = str(body.get("message_id") or "").strip()
    content = str(body.get("content") or "")
    if not msg_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    result = await asyncio.to_thread(
        session_manager.update_running_content,
        session_id,
        msg_id,
        content,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="session or message not found")
    return {"success": True}


@router.post("/api/internal/session-messages/set-streaming")
async def internal_session_messages_set_streaming(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: set the streaming flag on a message of an extension-owned session.

    NOTE: the session_manager ``streaming_set`` change-kind is currently dropped
    by the WS broadcaster (known gap), so this persists the flag but may not
    reach the UI live until that change-kind is mapped."""
    _extension_id, session_id = _require_extension_session_ownership(x_internal_token, body)
    msg_id = str(body.get("message_id") or "").strip()
    if not msg_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    result = await asyncio.to_thread(
        session_manager.set_streaming,
        session_id,
        msg_id,
        bool(body.get("streaming")),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="session or message not found")
    return {"success": True}


@router.post("/api/internal/session-field")
async def internal_session_field(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: scoped mutation of a session-record field the caller does NOT own.

    The acting extension must have declared ``field`` under
    ``permissions.mutates_session_fields``; core enforces that allowlist and
    routes the write to the matching session_manager setter (no raw record
    write, no render-tree/apply_event path). Lets an externalized lifecycle
    extension (e.g. supervisor stamping a verdict) update session metadata it
    didn't create, without a blanket write grant."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    field = str(body.get("field") or "").strip()
    allowed = extension_store.session_field_allowlist(extension_id)
    if not allowed:
        raise HTTPException(status_code=403, detail="extension lacks mutates_session_fields permission")
    if field not in allowed:
        raise HTTPException(status_code=403, detail=f"extension may not mutate session field: {field}")
    session_id = str(body.get("session_id") or "").strip()
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    try:
        updated = await asyncio.to_thread(
            session_manager.apply_session_field,
            session_id,
            field,
            body.get("value"),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="session not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("session-field %s mutated on %s by extension %s", field, session_id, extension_id)
    return {"success": True}


@router.post("/api/internal/session-fields")
async def internal_session_fields(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    session_id = str(body.get("session_id") or "").strip()
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    requested = body.get("fields") or []
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        raise HTTPException(status_code=400, detail="fields must be a string list")
    allowed = set(extension_store.session_field_read_allowlist(extension_id))
    fields = [field for field in requested if field in allowed]
    session = await _session_lite(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"success": True, "fields": {field: session.get(field) for field in fields}}


def _session_activity_snapshot(session_id: str, session: dict) -> dict:
    queued_prompts = session.get("queued_prompts") if isinstance(session, dict) else []
    queued_count = len(queued_prompts) if isinstance(queued_prompts, list) else 0
    if _coordinator().has_queued_prompts(session_id):
        queued_count = max(queued_count, _coordinator().get_queued_count(session_id), 1)
    is_running = _coordinator().turn_manager.is_running_cached(session_id)
    monitoring_state = _coordinator().turn_manager.monitoring_state_cached(session_id)
    return {
        "session_id": session_id,
        "is_running": bool(is_running),
        "monitoring_state": monitoring_state,
        "queued_prompts_count": queued_count,
        "idle": not is_running and queued_count == 0,
    }


@router.post("/api/internal/session-activity")
async def internal_session_activity(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_extension_permission(x_internal_token, "session_state")
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    session = await _session_lite(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"success": True, **await asyncio.to_thread(_session_activity_snapshot, session_id, session)}

