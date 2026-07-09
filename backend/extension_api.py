from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

import extension_store
import personal_harness_extension
import extension_backend_loader
import config_store
import perf

router = APIRouter(prefix="/api/extensions", tags=["extensions"])
logger = logging.getLogger(__name__)

_PROJECTION_RESPONSE_CACHE_TTL_SECONDS = 5.0
_projection_response_cache: dict[tuple[str, tuple[Any, ...]], tuple[float, bytes]] = {}
_projection_response_inflight_by_loop: dict[
    int,
    tuple[
        asyncio.AbstractEventLoop,
        asyncio.Lock,
        dict[tuple[str, tuple[Any, ...]], asyncio.Task[bytes]],
    ],
] = {}
_local_node_id_cache: str | None = None


def _local_node_id_or_primary_cached() -> str:
    global _local_node_id_cache
    cached = _local_node_id_cache
    if cached is not None:
        return cached
    try:
        from topology import local_node_id
        node_id = local_node_id()
    except Exception:
        node_id = "primary"
    _local_node_id_cache = node_id
    return node_id


async def _provider_sync_api_key_ids_from_request(request: Request) -> tuple[str, ...]:
    body_reader = getattr(request, "body", None)
    if body_reader is None:
        return ()
    raw_body = await body_reader()
    if not raw_body:
        return ()
    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="provider sync request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="provider sync request body must be an object")
    include_secrets = payload.get("include_secrets", False)
    if include_secrets is False:
        return ()
    if include_secrets is not True:
        raise HTTPException(status_code=400, detail="include_secrets must be boolean true or false")
    provider_ids = payload.get("provider_ids")
    if not isinstance(provider_ids, list) or not provider_ids:
        raise HTTPException(status_code=400, detail="provider_ids must list credentials to send")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in provider_ids:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(status_code=400, detail="provider_ids must contain non-empty strings")
        provider_id = item.strip()
        if provider_id in seen:
            continue
        seen.add(provider_id)
        cleaned.append(provider_id)
    return tuple(cleaned)


def _assert_node_credential_sync_transport(node_id: str) -> None:
    import node_link
    import node_store

    conn = node_store.get_connection(node_id)
    if conn is None:
        raise HTTPException(status_code=409, detail="node is not connected")
    if node_link.connection_allows_credential_sync(conn):
        return
    raise HTTPException(
        status_code=409,
        detail="provider credential sync requires a WSS or loopback node connection",
    )


class InstallExtensionRequest(BaseModel):
    repo_url: str = ""
    extension_path: str = ""
    ref: str = ""
    entitlement_token: str = ""
    artifact_url: str = ""
    artifact_sha256: str = ""
    artifact_signature: str = ""
    marketplace_metadata_url: str = ""
    marketplace_metadata: dict[str, Any] = Field(default_factory=dict)


class SetEnabledRequest(BaseModel):
    enabled: bool


class SetInstructionEnabledRequest(BaseModel):
    level: str
    enabled: bool
    project_path: str = ""


class SetUiSettingsRequest(BaseModel):
    quick_button_enabled: bool | None = None
    page_enabled: bool | None = None


class SetExtensionSettingRequest(BaseModel):
    key: str
    value: Any


class SetInternalLlmAssignmentsRequest(BaseModel):
    assignments: dict[str, Any] = Field(default_factory=dict)


class SetUserInstructionsRequest(BaseModel):
    instructions: str = ""


class SetMcpEnabledRequest(BaseModel):
    enabled: bool


class SetFrontendModuleEnabledRequest(BaseModel):
    enabled: bool


class SetNativeExposureRequest(BaseModel):
    enabled: bool


class SetPermissionGrantRequest(BaseModel):
    granted: bool


class ResetExtensionSettingsRequest(BaseModel):
    expected_found_schema: int | None = None
    expected_revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


def _extension_error(exc: extension_store.ExtensionError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


async def _broadcast_extensions_changed() -> None:
    from orchestrator import get_active_coordinator

    # Extensions changed: re-project the desired daemon set for the platform
    # daemon host and reconcile backend-lifecycle daemon children.
    try:
        import extension_daemons

        await asyncio.to_thread(extension_daemons.reconcile)
    except Exception:
        logger.exception("extension_daemons.reconcile failed")

    coordinator = get_active_coordinator()
    if coordinator is not None:
        await coordinator.broadcast_global("extensions_changed", {})

    try:
        import node_extension_sync

        node_extension_sync.notify_extensions_changed()
    except Exception:
        logger.exception("node extension sync notify failed")


def _json_projection_response(content: bytes) -> Response:
    return Response(content=content, media_type="application/json")


def _projection_response_cache_get(name: str, key: tuple[Any, ...]) -> Response | None:
    cached = _projection_response_cache.get((name, key))
    if cached is None:
        return None
    if time.monotonic() - cached[0] > _PROJECTION_RESPONSE_CACHE_TTL_SECONDS:
        _projection_response_cache.pop((name, key), None)
        return None
    return _json_projection_response(cached[1])


def _projection_response_cache_put(
    name: str,
    key: tuple[Any, ...],
    value: dict[str, Any],
) -> Response:
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(_projection_response_cache) >= 16:
        oldest = min(
            _projection_response_cache,
            key=lambda item: _projection_response_cache[item][0],
        )
        _projection_response_cache.pop(oldest, None)
    _projection_response_cache[(name, key)] = (time.monotonic(), content)
    return _json_projection_response(content)


def _projection_response_inflight_state() -> tuple[
    asyncio.Lock,
    dict[tuple[str, tuple[Any, ...]], asyncio.Task[bytes]],
]:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    state = _projection_response_inflight_by_loop.get(loop_id)
    if state is not None and state[0] is loop and not loop.is_closed():
        return state[1], state[2]
    stale = [
        key for key, (stored_loop, _lock, _inflight) in _projection_response_inflight_by_loop.items()
        if stored_loop.is_closed()
    ]
    for key in stale:
        _projection_response_inflight_by_loop.pop(key, None)
    lock = asyncio.Lock()
    inflight: dict[tuple[str, tuple[Any, ...]], asyncio.Task[bytes]] = {}
    _projection_response_inflight_by_loop[loop_id] = (loop, lock, inflight)
    return lock, inflight


async def _cleanup_projection_response_inflight(
    cache_key: tuple[str, tuple[Any, ...]],
    task: asyncio.Task[bytes],
) -> None:
    lock, inflight = _projection_response_inflight_state()
    async with lock:
        if inflight.get(cache_key) is task:
            inflight.pop(cache_key, None)


def _cached_json_projection_response(
    name: str,
    key: tuple[Any, ...],
    build: Callable[[], dict[str, Any]],
) -> Response:
    cached = _projection_response_cache_get(name, key)
    if cached is not None:
        return cached
    return _projection_response_cache_put(name, key, build())


async def _cached_json_projection_response_threaded(
    name: str,
    key_fn: Callable[[], tuple[Any, ...]],
    build: Callable[[], dict[str, Any]],
) -> Response:
    key = await asyncio.to_thread(key_fn)
    cached = _projection_response_cache_get(name, key)
    if cached is not None:
        return cached
    cache_key = (name, key)
    lock, inflight = _projection_response_inflight_state()
    async with lock:
        cached = _projection_response_cache_get(name, key)
        if cached is not None:
            return cached
        task = inflight.get(cache_key)
        if task is None:
            async def _build_and_cache() -> bytes:
                value = await asyncio.to_thread(build)
                response = _projection_response_cache_put(name, key, value)
                return bytes(response.body)

            loop = asyncio.get_running_loop()
            task = loop.create_task(_build_and_cache())
            inflight[cache_key] = task
            def _schedule_cleanup(done_task: asyncio.Task[bytes], ck=cache_key) -> None:
                if loop.is_closed():
                    return
                loop.create_task(_cleanup_projection_response_inflight(ck, done_task))

            task.add_done_callback(_schedule_cleanup)
    content = await asyncio.shield(task)
    return _json_projection_response(content)


@router.get("")
async def list_extensions(include_hidden: bool = Query(default=False)):
    cache_key = (extension_store.store_fingerprint(), include_hidden)
    cached = _projection_response_cache_get("list", cache_key)
    if cached is not None:
        return cached
    extensions, changed = await asyncio.to_thread(
        extension_store.list_extensions_with_reconciliation,
        include_hidden=include_hidden,
    )
    if changed:
        await _broadcast_extensions_changed()
    return _projection_response_cache_put("list", cache_key, {"extensions": extensions})


@router.get("/daemons/state")
async def get_daemons_state():
    """Read projection of the extension daemons surface: desired registry,
    host-owned supervisor daemon status, and backend-lifecycle children."""
    import extension_daemons

    return await asyncio.to_thread(extension_daemons.daemons_projection)


@router.get("/builtin-ids")
async def get_builtin_ids():
    """Logical key -> extension id for known builtins. Private ids are present
    only where the private registry is loaded; the frontend fetches this so it
    never hardcodes private/commercial ids."""
    return {"ids": extension_store.builtin_extension_id_map()}


@router.get("/frontend-entrypoints")
async def get_frontend_entrypoints():
    try:
        return await _cached_json_projection_response_threaded(
            "frontend-entrypoints",
            extension_store.frontend_entrypoints_cache_key,
            lambda: {"entrypoints": extension_store.frontend_entrypoints()},
        )
    except extension_store.ExtensionSettingsSchemaError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "extension_settings_incompatible",
                "message": str(exc),
                "found_schema": exc.found,
                "expected_schema": exc.expected,
                "revision": exc.revision,
                "reset_available": True,
            },
        ) from exc


@router.post("/settings/reset")
async def reset_extension_settings(req: ResetExtensionSettingsRequest):
    try:
        result = await asyncio.to_thread(
            extension_store.reset_extension_settings,
            expected_found_schema=req.expected_found_schema,
            expected_revision=req.expected_revision,
        )
    except extension_store.ExtensionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "extension_settings_reset_rejected", "message": str(exc)},
        ) from exc
    await _broadcast_extensions_changed()
    return result


@router.get("/ui-hooks")
async def get_ui_hooks():
    return await _cached_json_projection_response_threaded(
        "ui-hooks",
        extension_store.ui_hooks_cache_key,
        lambda: {"hooks": extension_store.ui_hooks()},
    )


@router.get("/{extension_id}/frontend/{asset_path:path}")
async def get_frontend_asset(extension_id: str, asset_path: str, request: Request):
    try:
        path = extension_store.resolve_frontend_asset(extension_id, asset_path)
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # frontend_entrypoints() stamps the install commit_sha into the URL as
    # `?v=<sha>`. When present, content can't have changed without the URL
    # changing too -> cache forever. When absent (unversioned local-dev
    # install, or legacy cached URL) -> revalidate every load.
    versioned = bool(request.query_params.get("v"))
    cache_control = (
        "public, max-age=31536000, immutable" if versioned else "no-cache"
    )
    return FileResponse(path, headers={"Cache-Control": cache_control})


@router.api_route(
    "/{extension_id}/backend",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def dispatch_backend_extension_base(extension_id: str, request: Request):
    # The bare base (no trailing slash) must dispatch directly to the
    # extension's root handler. Relying on Starlette's redirect-to-slash is
    # unsafe: the frontend StaticFiles mount at "/" preempts the redirect and
    # turns the path into a 404 "Not Found". Delegate with an empty subpath.
    return await dispatch_backend_extension(extension_id, "", request)


@router.api_route(
    "/{extension_id}/backend/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def dispatch_backend_extension(extension_id: str, path: str, request: Request):
    started = time.perf_counter()
    response_source = "unknown"
    try:
        with perf.timed("extension.backend.core_fast"):
            core_response = await _dispatch_core_builtin_backend(
                extension_id,
                path,
                request,
            )
        if core_response is not None:
            response_source = "core_fast"
            return core_response
        with perf.timed("extension.backend.spec"):
            backend_spec = extension_backend_loader.backend_entrypoint_spec_cached(extension_id)
        with perf.timed("extension.backend.core_after_spec"):
            core_response = await _dispatch_core_builtin_backend(
                extension_id,
                path,
                request,
                backend_spec=backend_spec,
            )
        if core_response is not None:
            response_source = "core_after_spec"
            return core_response
        with perf.timed("extension.backend.dispatch"):
            response_source = "extension"
            return await extension_backend_loader.dispatch_extension_backend_request(
                extension_id,
                path,
                request,
                backend_spec=backend_spec,
            )
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= 50:
            logger.info(
                "slow extension backend %s %s/%s source=%s elapsed=%.1fms",
                request.method,
                extension_id,
                path.strip("/"),
                response_source,
                elapsed_ms,
            )


async def _dispatch_core_builtin_backend(
    extension_id: str,
    path: str,
    request: Request,
    *,
    backend_spec: dict[str, Any] | None = None,
) -> JSONResponse | None:
    clean_path = path.strip("/")
    if extension_id != extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID:
        if extension_id == extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID:
            if backend_spec is not None:
                return None
            if not extension_store.is_extension_enabled_cached(extension_id):
                return None
            return await _dispatch_team_orchestration_core_backend(clean_path, request)
        if extension_id == extension_store.BUILTIN_SCHEDULER_EXTENSION_ID:
            if backend_spec is not None:
                return None
            if not extension_store.is_extension_enabled_cached(extension_id):
                return None
            return await _dispatch_scheduler_core_backend(clean_path, request)
        if extension_id == extension_store.BUILTIN_ROUTINES_EXTENSION_ID:
            if backend_spec is not None:
                return None
            if not extension_store.is_extension_enabled_cached(extension_id):
                return None
            return await _dispatch_routines_core_backend(clean_path, request)
        if extension_id != extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID:
            return None
        if backend_spec is not None:
            return None
        if not extension_store.is_extension_enabled_cached(extension_id):
            return None
        return await _dispatch_project_structure_core_backend(clean_path, request)
    if backend_spec is not None:
        return None
    if not extension_store.is_extension_enabled_cached(extension_id):
        return None
    return await _dispatch_machine_nodes_core_backend(clean_path, request)


async def _dispatch_routines_core_backend(
    path: str,
    request: Request,
) -> JSONResponse | None:
    if request.method != "GET" or path != "routines":
        return None

    from stores import task_store

    cwd = str(request.query_params.get("cwd") or "").strip()
    node_id = str(request.query_params.get("node_id") or "primary").strip() or "primary"
    if not cwd:
        return JSONResponse({"detail": "cwd is required"}, status_code=400)
    with perf.timed("extension.routines.list"):
        routines = await asyncio.to_thread(task_store.list_for_project, cwd, node_id)
    return JSONResponse({"routines": routines})


async def _dispatch_scheduler_core_backend(
    path: str,
    request: Request,
) -> JSONResponse | None:
    if request.method != "GET":
        return None
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "sessions" or parts[2] != "schedules":
        return None

    import session_manager
    from stores import schedule_store

    app_session_id = parts[1]
    exists = await asyncio.to_thread(session_manager.manager.exists, app_session_id)
    if not exists:
        raise HTTPException(status_code=404, detail="session not found")
    with perf.timed("extension.scheduler.schedules"):
        schedules = await asyncio.to_thread(
            schedule_store.list_for_session,
            app_session_id,
        )
    return JSONResponse({"schedules": schedules})


async def _dispatch_team_orchestration_core_backend(
    path: str,
    request: Request,
) -> JSONResponse | None:
    if request.method == "GET" and path == "workers":
        import team_orchestration_read

        cwd = str(request.query_params.get("cwd") or "")
        with perf.timed("extension.team_orchestration.workers"):
            return JSONResponse(
                await asyncio.to_thread(team_orchestration_read.list_workers_for_cwd, cwd)
            )
    if request.method == "GET" and path == "pending_approvals":
        from orchestrator import get_active_coordinator
        from stores import pending_approvals

        cwd = request.query_params.get("cwd")
        coordinator = get_active_coordinator()
        active_dids = (
            set(coordinator.approval_waiters.keys())
            if coordinator is not None
            else set()
        )
        with perf.timed("extension.team_orchestration.pending_approvals"):
            pending = await asyncio.to_thread(pending_approvals.list_pending, cwd=cwd)
            return JSONResponse({
                "approvals": [
                    rec for rec in pending
                    if rec.get("delegation_id") in active_dids
                ],
            })
    return None


async def _dispatch_machine_nodes_core_backend(
    path: str,
    request: Request,
) -> JSONResponse | None:
    def sync_results_response(results: list[dict]) -> JSONResponse:
        ok = all(result.get("ok") is True for result in results)
        return JSONResponse({"ok": ok, "results": results})

    if request.method == "GET" and path == "nodes":
        import node_store
        with perf.timed("extension.machine_nodes.nodes"):
            return JSONResponse(await asyncio.to_thread(node_store.snapshot))
    if request.method == "GET" and path == "pending_nodes":
        import node_link
        with perf.timed("extension.machine_nodes.pending_nodes"):
            pending = node_link.public_pending_nodes_cached()
            if pending is None:
                pending = await asyncio.to_thread(node_link.public_pending_nodes)
            return JSONResponse({
                "pending_nodes": pending,
            })
    if request.method == "GET" and path == "local_node_id":
        with perf.timed("extension.machine_nodes.local_node_id"):
            node_id = _local_node_id_or_primary_cached()
            return JSONResponse({"node_id": node_id})
    if request.method == "POST" and path == "nodes/sync-providers":
        import config_store
        import node_store
        from node_rpc_handlers import call_local_or_remote

        provider_api_key_ids = await _provider_sync_api_key_ids_from_request(request)
        if provider_api_key_ids:
            raise HTTPException(
                status_code=400,
                detail="provider credentials can only be synced to one selected node",
            )
        provider_state = await asyncio.to_thread(config_store.export_provider_sync_state)
        snapshot = await asyncio.to_thread(node_store.snapshot)
        results = []
        for node in snapshot:
            node_id = str(node.get("id") or "")
            if (
                not node_id
                or node_id == "primary"
                or node.get("role") != "worker_node"
                or node.get("state") != "connected"
            ):
                continue
            try:
                result = await call_local_or_remote(
                    node_id,
                    "sync_provider_config",
                    {"provider_state": provider_state},
                    secure_transport_required=bool(provider_api_key_ids),
                    version_ready_required=True,
                )
                results.append({"node_id": node_id, "ok": True, **result})
            except Exception as exc:
                results.append({"node_id": node_id, "ok": False, "error": str(exc)})
        return sync_results_response(results)
    if request.method == "POST" and path.startswith("nodes/"):
        parts = path.split("/")
        if len(parts) == 3 and parts[2] == "sync-providers":
            import config_store
            from node_rpc_handlers import call_local_or_remote

            node_id = parts[1]
            provider_api_key_ids = await _provider_sync_api_key_ids_from_request(request)
            if provider_api_key_ids:
                _assert_node_credential_sync_transport(node_id)
            provider_state = await asyncio.to_thread(
                config_store.export_provider_sync_state,
                list(provider_api_key_ids),
            )
            try:
                result = await call_local_or_remote(
                    node_id,
                    "sync_provider_config",
                    {"provider_state": provider_state},
                    secure_transport_required=bool(provider_api_key_ids),
                    version_ready_required=True,
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return JSONResponse({"node_id": node_id, "ok": True, **result})
    if request.method == "POST" and path == "nodes/sync-extensions":
        import extension_store
        import node_store
        from node_rpc_handlers import call_local_or_remote

        extension_state = await asyncio.to_thread(extension_store.export_extension_sync_state)
        snapshot = await asyncio.to_thread(node_store.snapshot)
        results = []
        for node in snapshot:
            node_id = str(node.get("id") or "")
            if (
                not node_id
                or node_id == "primary"
                or node.get("role") != "worker_node"
                or node.get("state") != "connected"
            ):
                continue
            try:
                result = await call_local_or_remote(
                    node_id,
                    "sync_extension_config",
                    {"extension_state": extension_state},
                    timeout=180.0,
                    version_ready_required=True,
                )
                results.append({"node_id": node_id, "ok": True, **result})
            except Exception as exc:
                results.append({"node_id": node_id, "ok": False, "error": str(exc)})
        return sync_results_response(results)
    if request.method == "POST" and path.startswith("nodes/"):
        parts = path.split("/")
        if len(parts) == 3 and parts[2] == "sync-extensions":
            import extension_store
            from node_rpc_handlers import call_local_or_remote

            node_id = parts[1]
            extension_state = await asyncio.to_thread(extension_store.export_extension_sync_state)
            try:
                result = await call_local_or_remote(
                    node_id,
                    "sync_extension_config",
                    {"extension_state": extension_state},
                    timeout=180.0,
                    version_ready_required=True,
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return JSONResponse({"node_id": node_id, "ok": True, **result})
    if request.method == "POST" and path.startswith("pending_nodes/"):
        parts = path.split("/")
        if len(parts) != 3 or parts[2] not in {"approve", "deny"}:
            return None
        import node_link
        node_id = parts[1]
        if parts[2] == "approve":
            rec, reason = await node_link.approve_registration(node_id)
            status = "approved"
        else:
            rec, reason = await node_link.deny_registration(node_id)
            status = "denied"
        if reason == "missing":
            raise HTTPException(status_code=404, detail="node request not found")
        if reason == "expired":
            raise HTTPException(status_code=410, detail="node request expired")
        if reason == "already_resolved":
            status = str(rec.get("status") or status)
        return JSONResponse({
            "status": status,
            "record": node_link._public_rec(rec),
            "idempotent": reason == "already_resolved",
        })
    return None


async def _dispatch_project_structure_core_backend(
    path: str,
    request: Request,
) -> JSONResponse | None:
    if request.method == "GET" and path == "project-updates/total":
        import project_update_store

        with perf.timed("extension.project_updates.total"):
            count = project_update_store.peek_total_unseen()
            if count is None:
                count = await asyncio.to_thread(project_update_store.total_unseen)
            return JSONResponse({"count": count})
    if request.method != "POST" or path != "project-updates/counts-batch":
        return None

    import project_update_store
    from paths import encode_cwd

    with perf.timed("extension.project_updates.counts_batch"):
        body = await request.json()
        cwds = (body or {}).get("cwds") if isinstance(body, dict) else None
        if not isinstance(cwds, list) or any(not isinstance(cwd, str) for cwd in cwds):
            return JSONResponse({"detail": "cwds must be a list of strings"}, status_code=400)
        project_ids = [encode_cwd(cwd) for cwd in cwds]
        counts = project_update_store.peek_unseen_counts(project_ids)
        if counts is None:
            counts = await asyncio.to_thread(project_update_store.unseen_counts, project_ids)
        return JSONResponse(counts)


@router.post("/install")
async def install_extension(req: InstallExtensionRequest):
    try:
        if req.marketplace_metadata_url or req.marketplace_metadata:
            record = extension_store.install_from_marketplace_metadata(
                metadata=req.marketplace_metadata or None,
                metadata_url=req.marketplace_metadata_url,
                entitlement_token=req.entitlement_token,
            )
        elif req.artifact_url:
            record = extension_store.install_from_artifact(
                artifact_url=req.artifact_url,
                artifact_sha256=req.artifact_sha256,
                artifact_signature=req.artifact_signature,
                entitlement_token=req.entitlement_token,
            )
        else:
            record = extension_store.install_from_repo(
                repo_url=req.repo_url,
                extension_path=req.extension_path,
                ref=req.ref,
                entitlement_token=req.entitlement_token,
            )
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"extension": record}


@router.post("/personal-harness")
async def create_personal_harness_extension():
    try:
        record = await asyncio.to_thread(personal_harness_extension.create)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"extension": record}


@router.post("/update")
async def update_extensions():
    try:
        result = extension_store.update_installed_extensions()
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    if result.get("updated"):
        await _broadcast_extensions_changed()
    return result


@router.patch("/{extension_id}/enabled")
async def set_extension_enabled(extension_id: str, req: SetEnabledRequest):
    try:
        record = extension_store.set_enabled(extension_id, req.enabled)
    except extension_store.ExtensionConsentRequired as exc:
        record = extension_store.get_extension(extension_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "consent_required",
                "extension_id": extension_id,
                "message": str(exc),
                "permissions": extension_store.declared_permissions(record) if record else {},
            },
        ) from exc
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"extension": record}


@router.post("/{extension_id}/consent")
async def grant_extension_consent(extension_id: str):
    """Record the user's consent to the extension's declared permission set so
    it can be enabled (trusted-by-install model)."""
    try:
        record = extension_store.grant_consent(extension_id)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"extension": record}


@router.patch("/{extension_id}/instructions/enabled")
async def set_extension_instruction_enabled(extension_id: str, req: SetInstructionEnabledRequest):
    try:
        record = extension_store.set_instruction_enabled(
            extension_id, level=req.level, enabled=req.enabled, project_path=req.project_path
        )
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"extension": record}


@router.get("/{extension_id}/ui-settings")
async def get_extension_ui_settings(extension_id: str):
    try:
        return extension_store.get_ui_settings(extension_id)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc


@router.patch("/{extension_id}/ui-settings")
async def set_extension_ui_settings(extension_id: str, req: SetUiSettingsRequest):
    try:
        settings = extension_store.set_ui_settings(
            extension_id,
            quick_button_enabled=req.quick_button_enabled,
            page_enabled=req.page_enabled,
        )
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return settings


@router.get("/{extension_id}/config")
async def get_extension_config(extension_id: str):
    try:
        return extension_store.extension_config(extension_id)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc


@router.get("/{extension_id}/settings")
async def get_extension_settings(extension_id: str):
    try:
        return extension_store.get_extension_settings(extension_id)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc


@router.patch("/{extension_id}/settings")
async def set_extension_setting(extension_id: str, req: SetExtensionSettingRequest):
    try:
        result = extension_store.set_extension_setting(extension_id, req.key, req.value)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return result


@router.get("/{extension_id}/internal-llm")
async def get_extension_internal_llm(extension_id: str):
    try:
        record = extension_store.get_extension(extension_id)
        if record is None:
            raise extension_store.ExtensionError("Extension not installed")
        task_keys = set(extension_store.extension_internal_llm_tasks(record))
        assignments = config_store.get_internal_llm_assignments()
        labels = extension_store.internal_llm_task_labels()
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    return {
        "tasks": list(task_keys),
        "labels": {key: label for key, label in labels.items() if key in task_keys},
        "assignments": {
            key: value
            for key, value in assignments.items()
            if key in task_keys
        },
    }


@router.put("/{extension_id}/internal-llm")
async def set_extension_internal_llm(extension_id: str, req: SetInternalLlmAssignmentsRequest):
    try:
        record = extension_store.get_extension(extension_id)
        if record is None:
            raise extension_store.ExtensionError("Extension not installed")
        task_keys = set(extension_store.extension_internal_llm_tasks(record))
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    forbidden = sorted(str(key) for key in req.assignments if str(key) not in task_keys)
    if forbidden:
        raise HTTPException(status_code=403, detail="internal LLM task is not owned by this extension")
    current = config_store.get_internal_llm_assignments()
    merged = {
        key: value
        for key, value in current.items()
        if key not in task_keys
    }
    merged.update(req.assignments)
    assignments = config_store.set_internal_llm_assignments(merged)
    await _broadcast_extensions_changed()
    return {
        "assignments": {
            key: value
            for key, value in assignments.items()
            if key in task_keys
        }
    }


@router.get("/{extension_id}/user-instructions")
async def get_extension_user_instructions(extension_id: str):
    try:
        return {"instructions": extension_store.get_user_instructions(extension_id)}
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc


@router.patch("/{extension_id}/user-instructions")
async def set_extension_user_instructions(extension_id: str, req: SetUserInstructionsRequest):
    try:
        instructions = extension_store.set_user_instructions(extension_id, req.instructions)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"instructions": instructions}


@router.get("/{extension_id}/mcp")
async def get_extension_mcp(extension_id: str):
    try:
        return {"servers": extension_store.extension_mcp_servers(extension_id)}
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc


@router.patch("/{extension_id}/mcp/{server_name}/enabled")
async def set_extension_mcp_enabled(extension_id: str, server_name: str, req: SetMcpEnabledRequest):
    try:
        enabled = extension_store.set_mcp_server_enabled(extension_id, server_name, req.enabled)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"server": server_name, "enabled": enabled}


@router.patch("/{extension_id}/frontend-modules/{slot}/{module_id}/enabled")
async def set_extension_frontend_module_enabled(
    extension_id: str,
    slot: str,
    module_id: str,
    req: SetFrontendModuleEnabledRequest,
):
    try:
        enabled = extension_store.set_frontend_module_enabled(
            extension_id,
            slot,
            module_id,
            req.enabled,
        )
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"slot": slot, "id": module_id, "enabled": enabled}


@router.patch("/{extension_id}/harness-additions/{kind}/{name}/native-exposure")
async def set_extension_native_exposure(
    extension_id: str,
    kind: str,
    name: str,
    req: SetNativeExposureRequest,
):
    try:
        exposed = extension_store.set_native_harness_exposed(
            extension_id, kind, name, req.enabled
        )
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"kind": kind, "name": name, "native_exposed": exposed}


@router.patch("/{extension_id}/permissions/{permission}/granted")
async def set_extension_permission_grant(extension_id: str, permission: str, req: SetPermissionGrantRequest):
    try:
        record = extension_store.set_permission_grant(extension_id, permission, req.granted)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"extension": record}


@router.delete("/{extension_id}")
async def uninstall_extension(extension_id: str):
    try:
        extension_store.uninstall(extension_id)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"ok": True}
