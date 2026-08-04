"""Internal-loopback routes for: session capability load/release,
session-control (model/provider selectors + fresh-context
continuation), project updates, inter-extension calls, the core MCP
job lifecycle, the extension marketplace, and auto-tagging (including
the tag-substrate CRUD the auto-tagging extension owns).

Depends on the coordinator and on a handful of callables main.py still
owns (selector resolution/recording and the debounced
session-organization broadcaster), bound by the composition root (see
`configure`).
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Request

import config_store
import extension_api
import extension_jobs
import extension_store
import global_events
import harness_profile_resolver
import internal_guards
import perf
import project_update_store
import runtime_operation_api
import session_organization_store
from bounded_async_executor import AdmissionOverloaded
from i18n import t
from session_helpers import session_exists
from session_manager import manager as session_manager

router = APIRouter()

_CORE_MCP_JOB_OWNER = "core-mcp"
_CORE_MCP_JOB_ID_KEY = "_mcp_job_id"
_CORE_MCP_JOB_WAIT_KEY = "_mcp_job_wait"
_CORE_MCP_JOB_FIELDS = frozenset({_CORE_MCP_JOB_ID_KEY, _CORE_MCP_JOB_WAIT_KEY})
_CORE_MCP_RESUMABLE_OPERATIONS = frozenset({
    "ask",
    "ask-fork",
    "session-bridge-search",
    "testape-qa-ask",
})

_broadcast_global: Optional[Callable[[str, dict], Any]] = None
_broadcast_session: Optional[Callable[..., Awaitable[Any]]] = None
_request_principal_async: Optional[Callable[[Request, str], Any]] = None
_require_builtin_extension: Optional[Callable[[str], None]] = None
_resolve_selector_updates: Optional[Callable[[str, dict], Awaitable[dict]]] = None
_transition_selectors: Optional[Callable[..., Awaitable[Optional[dict]]]] = None
_record_model_switched_event: Optional[Callable[[str, dict, dict, dict], None]] = None
_record_last_model: Optional[Callable[[str, str], Awaitable[None]]] = None
_record_last_reasoning_effort: Optional[Callable[[str, str], Awaitable[None]]] = None
_request_immediate_continuation: Optional[Callable[[str, str], bool]] = None
_broadcast_session_organization_changed: Optional[Callable[..., Awaitable[None]]] = None


def configure(
    *,
    broadcast_global: Callable[[str, dict], Any],
    broadcast_session: Callable[..., Awaitable[Any]],
    request_principal_async: Callable[[Request, str], Any],
    require_builtin_extension: Callable[[str], None],
    resolve_selector_updates: Callable[[str, dict], Awaitable[dict]],
    transition_selectors: Callable[..., Awaitable[Optional[dict]]],
    record_model_switched_event: Callable[[str, dict, dict, dict], None],
    record_last_model: Callable[[str, str], Awaitable[None]],
    record_last_reasoning_effort: Callable[[str, str], Awaitable[None]],
    request_immediate_continuation: Callable[[str, str], bool],
    broadcast_session_organization_changed: Callable[..., Awaitable[None]],
) -> None:
    """Bind the coordinator/main.py capabilities this router needs."""
    global _broadcast_global, _broadcast_session
    global _request_principal_async, _require_builtin_extension
    global _resolve_selector_updates, _transition_selectors
    global _record_model_switched_event
    global _record_last_model, _record_last_reasoning_effort
    global _request_immediate_continuation
    global _broadcast_session_organization_changed
    _broadcast_global = broadcast_global
    _broadcast_session = broadcast_session
    _request_principal_async = request_principal_async
    _require_builtin_extension = require_builtin_extension
    _resolve_selector_updates = resolve_selector_updates
    _transition_selectors = transition_selectors
    _record_model_switched_event = record_model_switched_event
    _record_last_model = record_last_model
    _record_last_reasoning_effort = record_last_reasoning_effort
    _request_immediate_continuation = request_immediate_continuation
    _broadcast_session_organization_changed = broadcast_session_organization_changed


def _configured(value: Any, name: str) -> Any:
    if value is None:
        raise HTTPException(status_code=503, detail=f"internal extension API is not configured ({name})")
    return value


# ── Project-updates authority ──────────────────────────────────


async def require_project_updates_internal_async(request: Request, x_internal_token: str) -> None:
    principal = await _configured(_request_principal_async, "request_principal_async")(
        request, x_internal_token,
    )
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
    _configured(_require_builtin_extension, "require_builtin_extension")(role_owner)
    if principal != ("extension", role_owner):
        raise HTTPException(status_code=403, detail="project-structure extension is required")


# ── Session capabilities ──────────────────────────────────


def _require_capabilities_internal(x_internal_token: str) -> None:
    """Capabilities are managed by the Better Agent builtin MCP that runs inside
    the runner and calls back over the internal loopback. Gate on a valid
    internal token only."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))


def _capabilities_snapshot(sess: dict) -> dict:
    active = [
        str(c) for c in (sess.get("active_capability_ids") or []) if str(c or "").strip()
    ]
    catalog = []
    for cap_id, descriptor in extension_store.capability_catalog().items():
        catalog.append({
            "id": cap_id,
            "scope": descriptor.get("scope"),
            "bare_allowed": bool(descriptor.get("bare_allowed")),
            "scope_gate": descriptor.get("scope_gate", "internal"),
            "release": descriptor.get("release") or {},
            "loaded": cap_id in active,
        })
    catalog.sort(key=lambda c: c["id"])
    return {"capabilities": catalog, "active_capability_ids": active}


@router.post("/api/internal/sessions/{sid}/capabilities")
async def internal_session_capabilities(
    sid: str,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """List/load/release scoped capabilities for a session. Core owns the write
    (session_manager.active_capability_ids); callers are internal loopback only
    (the Better Agent capabilities builtin MCP). Delivery follows on the next
    turn — the capability's MCP self-gates on the active set, skills merge at
    assembly."""
    _require_capabilities_internal(x_internal_token)
    action = str((body or {}).get("action") or "").strip()
    if action not in ("list", "load", "release"):
        raise HTTPException(status_code=400, detail="action must be list, load or release")
    sess = await asyncio.to_thread(session_manager.get, sid)
    if not sess:
        raise HTTPException(status_code=404, detail="unknown session")
    if action == "list":
        return {"ok": True, **_capabilities_snapshot(sess)}
    capability_id = str((body or {}).get("capability_id") or "").strip()
    if not capability_id:
        raise HTTPException(status_code=400, detail="capability_id is required")
    if not extension_store.get_capability(capability_id):
        raise HTTPException(status_code=404, detail="unknown capability")
    if action == "load":
        updated = session_manager.add_active_capability(sid, capability_id)
    else:
        updated = session_manager.remove_active_capability(sid, capability_id)
    return {
        "ok": True,
        "active_capability_ids": (updated or {}).get("active_capability_ids") or [],
    }


# ── Session control (model/provider selectors, fresh continuation) ──────


@router.post("/api/internal/session-control/selectors")
async def internal_session_control_selectors(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """`switch_model` tool backing. Applies model / provider_id /
    reasoning_effort to the CALLER'S OWN session (`app_session_id` from the
    loopback) through the SAME validation path as the public /selectors
    endpoint. The change takes effect on the next turn via the
    selector-change continuation (fresh provider subprocess, same session).

    Provider/model/runner transitions persist a continuation handoff and
    invalidate incompatible native SID authority before the next launch."""
    internal_guards.require_builtin_runtime_extension(extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID)
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    sid = str((body or {}).get("app_session_id") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    resolve_selector_updates = _configured(_resolve_selector_updates, "resolve_selector_updates")
    updates = await resolve_selector_updates(sid, body or {})
    before = await asyncio.to_thread(session_manager.get, sid)
    session = await _configured(
        _transition_selectors,
        "transition_selectors",
    )(
        sid,
        updates=updates,
    )
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    _configured(_record_model_switched_event, "record_model_switched_event")(sid, before or {}, session, updates)
    import runtime_profiles_api

    session_profile_id = runtime_profiles_api.runtime_profile_id_for_session(session)
    if "model" in updates:
        await _configured(_record_last_model, "record_last_model")(
            session_profile_id, updates["model"]
        )
    if updates.get("reasoning_effort"):
        await _configured(_record_last_reasoning_effort, "record_last_reasoning_effort")(
            session_profile_id, updates["reasoning_effort"],
        )
    return {"id": sid, "updates": updates}


@router.post("/api/internal/session-control/continue-fresh")
async def internal_session_control_continue_fresh(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """`continue_in_fresh_context` tool backing. Sets the agent-requested
    continuation flag on the CALLER'S OWN session.

    `when="next_turn"` (default): the current turn completes normally, then a
    fresh provider subprocess starts under the SAME session running the queued
    prompt. `when="now"`: abort the in-flight run immediately and start that
    fresh subprocess right away (same session, continuation_chain extended).

    The agent can only continue its own session."""
    internal_guards.require_builtin_runtime_extension(extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID)
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    sid = str((body or {}).get("app_session_id") or "").strip()
    prompt = str((body or {}).get("prompt") or "").strip()
    when = str((body or {}).get("when") or "next_turn").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if when == "now":
        request_immediate_continuation = _configured(
            _request_immediate_continuation, "request_immediate_continuation",
        )
        landed = request_immediate_continuation(sid, prompt)
        # If no live turn is running to abort, fall back to next-turn semantics
        # so the request is never silently lost.
        if landed:
            return {"ok": True, "session_id": sid, "when": "now"}
        when = "next_turn"
    if when == "next_turn":
        session_manager.set_continuation_requested(sid, prompt, when="next_turn")
        return {"ok": True, "session_id": sid, "when": "next_turn"}
    raise HTTPException(status_code=400, detail="when must be 'next_turn' or 'now'")


# ── Project updates ──────────────────────────────────


@router.post("/api/internal/project-updates/count")
async def internal_project_update_count(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    import os

    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    count = await asyncio.to_thread(project_update_store.unseen_count, project_id)
    return {"project_id": project_id, "count": count}


@router.post("/api/internal/project-updates/total")
async def internal_project_update_total(
    request: Request,
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    with perf.timed("internal.project_updates.total"):
        count = project_update_store.peek_total_unseen()
        if count is None:
            count = await asyncio.to_thread(project_update_store.total_unseen)
        return {"count": count}


@router.post("/api/internal/project-updates/counts-batch")
async def internal_project_update_counts_batch(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    cwds = (body or {}).get("cwds")
    if not isinstance(cwds, list) or any(not isinstance(cwd, str) for cwd in cwds):
        raise HTTPException(status_code=400, detail="cwds must be a list of strings")
    from paths import encode_cwd

    with perf.timed("internal.project_updates.counts_batch"):
        project_ids = [encode_cwd(cwd) for cwd in cwds]
        counts = project_update_store.peek_unseen_counts(project_ids)
        if counts is None:
            counts = await asyncio.to_thread(project_update_store.unseen_counts, project_ids)
        return counts


@router.post("/api/internal/project-updates/unseen")
async def internal_project_updates_unseen(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    import os

    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    return await asyncio.to_thread(project_update_store.list_unseen, project_id)


@router.post("/api/internal/project-updates/capture")
async def capture_project_update(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    import os

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    from paths import encode_cwd
    project_id = encode_cwd(body.get("cwd", "") or os.getcwd())
    entry, unseen_count = await asyncio.to_thread(
        lambda: (
            project_update_store.append(project_id, text),
            project_update_store.unseen_count(project_id),
        )
    )
    await _configured(_broadcast_global, "broadcast_global")(
        "project_updates_changed",
        {"project_id": project_id, "unseen_count": unseen_count},
    )
    return entry


@router.post("/api/internal/project-updates/list")
async def internal_project_updates_list(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    import os

    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    unseen_count, unseen_updates = await asyncio.to_thread(
        lambda: (
            project_update_store.unseen_count(project_id),
            project_update_store.list_unseen(project_id),
        )
    )
    return {
        "project_id": project_id,
        "unseen_count": unseen_count,
        "unseen_updates": unseen_updates,
    }


@router.post("/api/internal/project-updates/mark-seen")
async def internal_project_updates_mark_seen(
    request: Request,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    await require_project_updates_internal_async(request, x_internal_token)
    import os

    from paths import encode_cwd

    project_id = encode_cwd((body or {}).get("cwd") or os.getcwd())
    entry_ids = body.get("entry_ids") or []
    if not isinstance(entry_ids, list) or not all(isinstance(x, str) for x in entry_ids):
        raise HTTPException(status_code=400, detail="entry_ids must be a list of strings")
    marked, unseen_count = await asyncio.to_thread(
        lambda: (
            project_update_store.mark_seen(project_id, entry_ids),
            project_update_store.unseen_count(project_id),
        )
    )
    await _configured(_broadcast_global, "broadcast_global")(
        "project_updates_changed",
        {"project_id": project_id, "unseen_count": unseen_count},
    )
    return {"success": True, "marked": marked}


# ── Inter-extension calls ──────────────────────────────────


_EXTENSION_CALL_MAX_JSON_DEPTH = 32


def _validate_extension_call_path(path: str) -> None:
    import unicodedata

    if (
        not path.startswith("/")
        or "\\" in path
        or "?" in path
        or "#" in path
        or "%" in path
        or unicodedata.normalize("NFC", path) != path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise HTTPException(status_code=400, detail="path must be canonical and extension-local")
    segments = path[1:].split("/")
    if not segments or any(
        not segment or segment in {".", ".."} or segment != segment.strip()
        for segment in segments
    ):
        raise HTTPException(status_code=400, detail="path must be canonical and extension-local")


def _validate_extension_call_json_depth(value: object) -> None:
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth > _EXTENSION_CALL_MAX_JSON_DEPTH:
            raise HTTPException(status_code=400, detail="body nesting is too deep")
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth + 1) for child in children)


def _encode_extension_call_body(value: dict) -> bytes:
    import extension_backend_loader

    chunks: list[bytes] = []
    total = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        total += len(encoded)
        if total >= extension_backend_loader.REQUEST_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Extension request body is too large")
        chunks.append(encoded)
    return b"".join(chunks)


async def _read_extension_call_body(request: Request) -> dict:
    import extension_backend_loader

    def reject_nonstandard_constant(_value: str) -> None:
        raise ValueError("non-standard JSON constant")

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=400, detail="content-type must be application/json")
    raw = await extension_backend_loader._read_limited_body(request)
    if not raw:
        raise HTTPException(status_code=400, detail="request body must be valid JSON")
    try:
        body = json.loads(
            raw,
            parse_constant=reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _validate_extension_call_json_depth(body)
    return body


@router.post("/api/internal/extension-call")
async def internal_extension_call(
    request: Request,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK inter-extension call: let one active extension invoke another active
    extension's exposed backend surface. Extensions expose their own SDKs
    (feature-specific capabilities live in per-extension surfaces); core only
    routes the call and never bakes in feature logic."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller = internal_guards.internal_authority_extension_id() or ""
    if not caller or not extension_store.is_extension_active(caller):
        raise HTTPException(status_code=403, detail="calling extension is not active")
    body = await _read_extension_call_body(request)
    allowed_fields = {"target_extension_id", "path", "method", "body"}
    if set(body) - allowed_fields:
        raise HTTPException(status_code=400, detail="request has unexpected fields")
    target = str(body.get("target_extension_id") or "").strip()
    raw_path = body.get("path")
    path = raw_path if isinstance(raw_path, str) else ""
    method = str(body.get("method") or "POST").strip().upper()
    inner = body.get("body")
    if not target or not path:
        raise HTTPException(status_code=400, detail="target_extension_id and path are required")
    _validate_extension_call_path(path)
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise HTTPException(status_code=400, detail="unsupported method")
    if inner is not None and not isinstance(inner, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _validate_extension_call_json_depth(inner or {})
    if target == caller:
        raise HTTPException(status_code=400, detail="inter-extension call is for other extensions")
    caller_record = extension_store.get_extension(caller)
    dependencies = (
        ((caller_record or {}).get("manifest") or {}).get("dependencies") or []
    )
    if target not in dependencies:
        raise HTTPException(status_code=403, detail="target is not a declared dependency")
    if not extension_store.is_extension_active(target):
        raise HTTPException(status_code=404, detail="target extension is not active")
    import extension_backend_loader
    body_bytes = _encode_extension_call_body(inner) if inner is not None else b""
    return await extension_backend_loader.invoke_extension_backend(
        target,
        path,
        method=method,
        body_bytes=body_bytes,
        base_url=str(request.base_url).rstrip("/"),
    )


# ── Core MCP job lifecycle ──────────────────────────────────
#
# `maybe_run_core_mcp_job` is called by the routes owning each operation
# to fire an idempotent, resumable job before running the handler
# inline. This module owns the operation → handler table because it owns
# the job lifecycle; the handlers themselves live with their routes.


def _core_mcp_job_payload(body: dict) -> dict[str, Any]:
    return {
        key: value
        for key, value in (body or {}).items()
        if key not in _CORE_MCP_JOB_FIELDS
    }


def _core_mcp_job_id(body: dict) -> str:
    job_id = str((body or {}).get(_CORE_MCP_JOB_ID_KEY) or "").strip()
    if not job_id:
        return ""
    if len(job_id) > 128 or any(not (ch.isalnum() or ch in ("-", "_")) for ch in job_id):
        raise HTTPException(status_code=400, detail="invalid MCP job id")
    return job_id


def _core_mcp_job_wait(body: dict) -> float:
    wait = (body or {}).get(_CORE_MCP_JOB_WAIT_KEY, 0.0)
    if not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0:
        raise HTTPException(status_code=400, detail="MCP job wait must be a non-negative number")
    return float(wait)


def _core_mcp_payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _core_mcp_job_handler(operation: str):
    # Imported at call time: every owning router imports this module, so a
    # module-level import here would close the cycle.
    import internal_messaging_api
    import internal_orchestration_api
    import session_bridge_api

    handlers = {
        "ask": internal_messaging_api._handle_internal_ask,
        "testape-qa-ask": internal_messaging_api._handle_internal_testape_qa_ask,
        "mssg": internal_messaging_api._handle_internal_mssg,
        "ask-fork": internal_orchestration_api._handle_internal_ask_fork,
        "delegate-task": internal_orchestration_api._handle_internal_delegate_task,
        "session-bridge-search": session_bridge_api._handle_internal_session_bridge_search,
        "session-bridge-delegate": session_bridge_api._handle_internal_session_bridge_delegate,
    }
    handler = handlers.get(operation)
    if handler is None:
        raise HTTPException(status_code=404, detail="unknown MCP job operation")
    return handler


async def _run_core_mcp_job_payload(
    payload: dict[str, Any],
    *,
    request_id: str = "",
    operation: str,
) -> dict[str, Any]:
    handler = _core_mcp_job_handler(operation)
    await asyncio.to_thread(
        extension_jobs.persist_running,
        _CORE_MCP_JOB_OWNER,
        operation,
        request_id,
        phase="running",
        message="MCP job is running",
    )
    return await handler(payload)


def _core_mcp_nonresumable_response(operation: str, request_id: str) -> dict[str, Any]:
    record = extension_jobs.read_record(_CORE_MCP_JOB_OWNER, operation, request_id)
    if isinstance(record, dict) and record.get("status") in ("complete", "failed"):
        return extension_jobs.response_from_record(record)
    return {
        "success": False,
        "id": request_id,
        "status": "failed",
        "ready": True,
        "error": "MCP job cannot be resumed after backend restart",
    }


async def maybe_run_core_mcp_job(
    operation: str,
    body: dict,
) -> dict[str, Any] | None:
    job_id = _core_mcp_job_id(body)
    if not job_id:
        return None
    payload = _core_mcp_job_payload(body)
    extension_jobs.cleanup(_CORE_MCP_JOB_OWNER, operation)
    try:
        found = extension_jobs.get_or_fire_idempotent(
            _CORE_MCP_JOB_OWNER,
            operation,
            job_id,
            payload,
            functools.partial(_run_core_mcp_job_payload, operation=operation),
            payload_digest=_core_mcp_payload_digest(payload),
            caller_extension=_CORE_MCP_JOB_OWNER,
            metadata={
                "phase": "created",
                "message": "MCP job created",
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(found, dict):
        if (
            operation not in _CORE_MCP_RESUMABLE_OPERATIONS
            and found.get("status") == "running"
            and extension_jobs.get_active(_CORE_MCP_JOB_OWNER, operation, job_id) is None
        ):
            return _core_mcp_nonresumable_response(operation, job_id)
        return found
    wait = _core_mcp_job_wait(body)
    if wait <= 0:
        record = extension_jobs.read_record(_CORE_MCP_JOB_OWNER, operation, job_id)
        if isinstance(record, dict):
            return extension_jobs.response_from_record(record)
        return {"success": True, "id": job_id, "status": "running", "ready": False}
    try:
        result = await asyncio.wait_for(asyncio.shield(found), timeout=wait)
    except asyncio.TimeoutError:
        record = extension_jobs.read_record(_CORE_MCP_JOB_OWNER, operation, job_id)
        if isinstance(record, dict):
            return extension_jobs.response_from_record(record)
        return {"success": True, "id": job_id, "status": "running", "ready": False}
    except Exception as exc:
        return {"success": False, "id": job_id, "status": "failed", "ready": True, "error": str(exc)}
    return {"success": True, "id": job_id, "status": "complete", "ready": True, "result": result}


@router.post("/api/internal/mcp-jobs/results")
async def internal_mcp_job_results(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    operation = str((body or {}).get("operation") or "").strip()
    request_id = str((body or {}).get("id") or "").strip()
    if not operation or not request_id:
        raise HTTPException(status_code=400, detail="operation and id are required")
    try:
        extension_jobs.job_path(_CORE_MCP_JOB_OWNER, operation, request_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    extension_jobs.cleanup(_CORE_MCP_JOB_OWNER, operation)
    if operation not in _CORE_MCP_RESUMABLE_OPERATIONS:
        task = extension_jobs.get_active(_CORE_MCP_JOB_OWNER, operation, request_id)
        if task is None:
            return _core_mcp_nonresumable_response(operation, request_id)
        found = task
    else:
        found = extension_jobs.get_or_resume(
            _CORE_MCP_JOB_OWNER,
            operation,
            request_id,
            functools.partial(_run_core_mcp_job_payload, operation=operation),
        )
        if found is None:
            return {"success": False, "error": "unknown id"}
    if isinstance(found, dict):
        return found
    try:
        result = await asyncio.wait_for(asyncio.shield(found), timeout=_core_mcp_job_wait(body))
    except asyncio.TimeoutError:
        record = extension_jobs.read_record(_CORE_MCP_JOB_OWNER, operation, request_id)
        if isinstance(record, dict):
            return extension_jobs.response_from_record(record)
        return {"success": True, "id": request_id, "status": "running", "ready": False}
    except Exception as exc:
        return {"success": False, "id": request_id, "status": "failed", "ready": True, "error": str(exc)}
    return {"success": True, "id": request_id, "status": "complete", "ready": True, "result": result}


# ── Auto-tagging (and the tag substrate the extension owns) ──────────────


def _latest_user_task_text(session: dict) -> str:
    return _latest_message_text(session, "user")


def _latest_assistant_response_text(session: dict) -> str:
    return _latest_message_text(session, "assistant")


def _latest_message_text(session: dict, role: str) -> str:
    """Latest message text for `role`, read from an already-fetched session
    snapshot. Callers MUST pass a session dict (e.g. one fetched off-loop via
    `get_lite`) — this MUST NOT fetch on the event loop: a full `get()`
    deepcopy here blocked the loop for ~1.8s on large sessions (auto-tagging
    `current-task`), and `internal_auto_tagging` already fetches the session
    off-loop before calling this."""
    for msg in reversed((session or {}).get("messages") or []):
        if msg.get("role") != role:
            continue
        return _message_text(msg)
    return ""


def _message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    text = msg.get("text")
    return text.strip() if isinstance(text, str) else ""


def _session_auto_tagging_eligible(session: dict) -> bool:
    if not session:
        return False
    if session.get("parent_session_id") or session.get("bare_config"):
        return False
    return session.get("source") not in {"internal", "provisioning", "subprocess_agent"}


def _auto_tagging_selector_module():
    """Lazy-load the auto-tagging extension's provisioned tag-selector
    package (tagging_selector) so the worker spec registers in-process."""
    import extension_package_loader
    extension_package_loader.ensure_package_importable(
        extension_store.BUILTIN_AUTO_TAGGING_EXTENSION_ID, "tagging_selector"
    )
    import importlib
    return importlib.import_module("tagging_selector")


@router.post("/api/internal/auto-tagging")
async def internal_auto_tagging(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if internal_guards.internal_authority_extension_id() != extension_store.BUILTIN_AUTO_TAGGING_EXTENSION_ID:
        raise HTTPException(status_code=403, detail="auto-tagging extension is required")
    not_ready = extension_store.runtime_not_ready_message(extension_store.BUILTIN_AUTO_TAGGING_EXTENSION_ID)
    if not_ready:
        raise HTTPException(status_code=403, detail=not_ready)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    broadcast_session_organization_changed = _configured(
        _broadcast_session_organization_changed, "broadcast_session_organization_changed",
    )
    action = str(body.get("action") or "").strip()
    try:
        if action == "current-task":
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("session_id is required")
            session = await asyncio.to_thread(
                session_manager.get_lite, session_id,
            ) or {}
            return {
                "success": True,
                "task": _latest_user_task_text(session),
                "last_response": _latest_assistant_response_text(session),
                "cwd": str(session.get("cwd") or ""),
                "eligible": _session_auto_tagging_eligible(session),
            }
        if action == "select-tags":
            session_id = str(body.get("session_id") or "").strip()
            session = await asyncio.to_thread(session_manager.get_lite, session_id) or {}
            if not session or not _session_auto_tagging_eligible(session):
                raise HTTPException(status_code=403, detail="session is not eligible for auto tagging")
            task = str(body.get("task") or "").strip()
            if not task:
                raise ValueError("task is required")
            evidence = body.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("evidence must be a non-empty list")
            existing_tags = body.get("existing_tags")
            if not isinstance(existing_tags, list):
                raise ValueError("existing_tags must be a list")
            max_tags = body.get("max_tags")
            if not isinstance(max_tags, int) or isinstance(max_tags, bool) or max_tags <= 0:
                raise ValueError("max_tags must be a positive integer")
            cwd = body.get("cwd") or ""
            if not isinstance(cwd, str):
                raise ValueError("cwd must be a string")
            if cwd != str(session.get("cwd") or ""):
                raise HTTPException(status_code=403, detail="cwd must match the session cwd")
            selector = _auto_tagging_selector_module()
            try:
                labels = await asyncio.to_thread(
                    selector.select_labels,
                    task=task,
                    evidence=evidence,
                    existing_tags=existing_tags,
                    max_tags=max_tags,
                    cwd=cwd,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"tag-selector worker failed: {exc}"
                )
            return {"success": True, "labels": labels}
        if action == "snapshot":
            return {
                "success": True,
                "organization": await asyncio.to_thread(
                    session_organization_store.snapshot,
                    body.get("project_id"),
                ),
            }
        if action == "ensure-tag":
            tag = await asyncio.to_thread(
                session_organization_store.ensure_tag,
                name=body.get("name"),
                project_id=body.get("project_id"),
                color=body.get("color"),
            )
            await broadcast_session_organization_changed()
            return {"success": True, "tag": tag}
        if action == "update-tag":
            try:
                tag = await asyncio.to_thread(
                    session_organization_store.update_tag,
                    body.get("tag_id"),
                    body.get("patch") or {},
                )
            except KeyError:
                raise HTTPException(status_code=404, detail="tag not found")
            await broadcast_session_organization_changed()
            return {"success": True, "tag": tag}
        if action == "delete-tag":
            deleted = await asyncio.to_thread(
                session_organization_store.delete_tag,
                body.get("tag_id"),
            )
            if not deleted:
                raise HTTPException(status_code=404, detail="tag not found")
            await broadcast_session_organization_changed()
            return {"success": True, "deleted": True}
        if action == "sync-session-tags":
            session_id = str(body.get("session_id") or "").strip()
            if not await session_exists(session_id):
                raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
            source = str(body.get("source") or session_organization_store.TAG_SOURCE_AUTO_TAGGING).strip()
            if source != session_organization_store.TAG_SOURCE_AUTO_TAGGING:
                raise ValueError("sync-session-tags source must be auto_tagging")
            merge = body.get("merge", False)
            if not isinstance(merge, bool):
                raise ValueError("merge must be a boolean")
            org = await asyncio.to_thread(
                session_organization_store.sync_session_tags_by_source,
                session_id,
                tag_ids=body.get("tag_ids"),
                source=source,
                merge=merge,
            )
            await broadcast_session_organization_changed([session_id])
            return {"success": True, "session_id": session_id, "organization": org}
        if action == "tags-sql":
            sql = body.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                raise ValueError("sql must be a non-empty string")
            result = await asyncio.to_thread(session_organization_store.run_tags_readonly_sql, sql)
            return {"success": "error" not in result, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=400, detail="unknown auto-tagging action")


# ── Extension settings, provisioned sessions, session broadcast,
#    runtime operations ──────────────────────────────────


def _extension_settings_harness_inputs(extension_id: str, body: dict) -> dict[str, Any] | None:
    raw = body.get("harness_launcher_projection") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        projection = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid harness launcher projection") from exc
    if not isinstance(projection, dict):
        raise HTTPException(status_code=400, detail="invalid harness launcher projection")
    revisions = projection.get("extension_revisions")
    if not isinstance(revisions, dict) or extension_id not in revisions:
        raise HTTPException(status_code=403, detail="harness profile does not select this extension")
    record = extension_store.get_extension(extension_id)
    if record is None:
        raise HTTPException(status_code=403, detail="extension not active")
    expected = str(revisions.get(extension_id) or "")
    actual = harness_profile_resolver._extension_revision(record)
    if expected and actual and expected != actual:
        raise HTTPException(status_code=409, detail="extension revision changed")
    return {"resolved_harness_run_config": {"launcher_projection": projection, **projection}}


@router.post("/api/internal/provisioned-sessions")
async def internal_provisioned_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Extension-facing primitive (Better Agent SDK): run one provisioned-session
    fork for a registered spec or a validated extension-owned inline spec."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    record = extension_store.get_extension(extension_id) if extension_id else None
    if (
        record is None
        or not extension_store.is_extension_active(extension_id)
        or not extension_store.has_permission(record, "spawn_runs")
    ):
        raise HTTPException(status_code=403, detail="extension lacks spawn_runs permission")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    raw_spec_key = body.get("spec_key")
    if raw_spec_key is None:
        spec_key = ""
    elif isinstance(raw_spec_key, str):
        spec_key = raw_spec_key.strip()
    else:
        raise HTTPException(status_code=400, detail="spec_key must be a string")
    inline_spec = body.get("inline_spec")
    query = body.get("query", "")
    if query is None:
        query = ""
    ctx = body.get("ctx", {})
    if ctx is None:
        ctx = {}
    if bool(spec_key) == (inline_spec is not None):
        raise HTTPException(status_code=400, detail="exactly one of spec_key or inline_spec is required")
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query must be a string")
    if not isinstance(ctx, dict):
        raise HTTPException(status_code=400, detail="ctx must be an object")
    import provisioning

    if inline_spec is not None:
        try:
            spec = provisioning.inline_spec_from_payload(
                inline_spec,
                extension_id=extension_id,
                allowed_task_keys=set(extension_store.extension_provisioned_internal_llm_tasks(record)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            spec = provisioning.get(spec_key)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown provisioned-session spec: {spec_key}")
    try:
        result = await provisioning.run(spec, query, ctx)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "text": result.text,
        "value": result.value,
        "base_session_id": result.base_session_id,
    }


@router.post("/api/internal/extension-settings")
async def internal_extension_settings(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Better Agent SDK: read an extension's own declared settings. Secrets
    are resolved from the OS keychain server-side and never placed in the
    subprocess environment. The caller may read only its own settings."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension not active")
    key = str(body.get("key") or "").strip() if isinstance(body, dict) else ""
    try:
        resolved = extension_store.resolve_all_settings(
            extension_id,
            inputs=_extension_settings_harness_inputs(extension_id, body or {}),
        )
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if key:
        return {"success": True, "value": resolved.get(key)}
    return {"success": True, "settings": resolved}


@router.post("/api/internal/extension-internal-llm/resolve")
async def internal_extension_internal_llm_resolve(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    record = extension_store.get_extension(extension_id) if extension_id else None
    if record is None or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension not active")
    task_key = str((body or {}).get("task_key") or "").strip()
    allowed = {
        *extension_store.extension_internal_llm_tasks(record),
        *extension_store.extension_provisioned_internal_llm_tasks(record),
    }
    if task_key not in allowed:
        raise HTTPException(status_code=403, detail="internal LLM task is not owned by this extension")
    resolved = await asyncio.to_thread(config_store.resolve_internal_llm, task_key)
    return {"success": True, "task_key": task_key, "resolved": resolved}


@router.get("/api/internal/provisioned-sessions/specs")
async def internal_provisioned_specs(
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: list the provisioned-session spec types an extension may invoke."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import provisioning

    specs = [
        {"key": spec.key, "name": spec.name, "version": spec.version}
        for spec in provisioning.all_specs()
    ]
    return {"specs": specs}


@router.post("/api/internal/sessions/{sid}/broadcast")
async def internal_broadcast_session(
    sid: str,
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """SDK: let an active extension emit a per-session WebSocket event.

    The event is persisted to events.jsonl via ``coordinator.broadcast_session``
    and fanned to the session's WS subscribers by the tailer. Core owns the
    outer ``extension_event`` type; the extension chooses only the inner event
    name and data. ``source`` is pinned to the calling extension id so one
    extension cannot impersonate another."""
    return await broadcast_session_for_internal_authority(sid, body)


async def broadcast_session_for_internal_authority(sid: str, body: dict):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    extension_id = internal_guards.internal_authority_extension_id() or ""
    if not extension_id or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    app_session_id = sid.strip()
    event_name = str(body.get("event_name") or "").strip()
    data = body.get("data", {})
    if not app_session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="data must be an object")
    try:
        event_type, event_data = global_events.extension_event(
            extension_id, event_name, data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _configured(_broadcast_session, "broadcast_session")(
        app_session_id,
        event_type,
        event_data,
        source=f"extension:{extension_id}",
    )
    return {
        "success": True,
        "event_type": event_type,
        "event_name": event_name,
        "source": f"extension:{extension_id}",
    }


@router.post("/api/internal/runtime-operations")
async def internal_runtime_operations(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    return await runtime_operation_api.handle(body)


def _require_marketplace_internal(x_internal_token: str) -> None:
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    internal_guards.require_builtin_runtime_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    if internal_guards.internal_authority_extension_id() != extension_store.MARKETPLACE_EXTENSION_ID:
        raise HTTPException(status_code=403, detail="marketplace extension is required")


@router.post("/api/internal/marketplace")
async def internal_marketplace(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_marketplace_internal(x_internal_token)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    action = str(body.get("action") or "").strip()
    changed = False
    try:
        if action == "search":
            result = await asyncio.to_thread(
                extension_store.search_marketplace_catalog,
                query=str(body.get("query") or ""),
                limit=body.get("limit") or 20,
            )
        elif action == "install":
            extension_id = str(body.get("extension_id") or "").strip()
            result = {
                "extension": await extension_api.marketplace_service.install_direct(
                    extension_id,
                    entitlement_token=str(body.get("entitlement_token") or ""),
                )
            }
            changed = True
        elif action == "list_installed":
            result = {"extensions": await asyncio.to_thread(extension_store.list_extensions)}
        elif action == "get_installed":
            extension_id = str(body.get("extension_id") or "").strip()
            record = await asyncio.to_thread(extension_store.get_extension, extension_id)
            if not record or extension_id in extension_store.PUBLIC_EXTENSION_LIST_HIDDEN_IDS:
                raise HTTPException(status_code=404, detail="Extension not installed")
            result = {"extension": record}
        elif action == "set_enabled":
            extension_id = str(body.get("extension_id") or "").strip()
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise extension_store.ExtensionError("enabled must be a boolean")
            result = {
                "extension": await asyncio.to_thread(
                    extension_store.set_enabled,
                    extension_id,
                    enabled,
                )
            }
            changed = True
        elif action == "uninstall":
            extension_id = str(body.get("extension_id") or "").strip()
            await asyncio.to_thread(extension_store.uninstall, extension_id)
            result = {"ok": True}
            changed = True
        elif action == "update":
            result = await asyncio.to_thread(extension_store.update_installed_extensions)
            changed = bool(result.get("updated"))
        else:
            raise extension_store.ExtensionError("unknown marketplace action")
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if changed:
        await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
    return result
