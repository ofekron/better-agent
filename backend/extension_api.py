from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import extension_store
import extension_backend_loader

router = APIRouter(prefix="/api/extensions", tags=["extensions"])


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


class SetMcpEnabledRequest(BaseModel):
    enabled: bool


class SetHarnessDeliveryRequest(BaseModel):
    mode: str


class SetPermissionGrantRequest(BaseModel):
    granted: bool


def _extension_error(exc: extension_store.ExtensionError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


async def _broadcast_extensions_changed() -> None:
    from orchestrator import get_active_coordinator

    coordinator = get_active_coordinator()
    if coordinator is not None:
        await coordinator.broadcast_global("extensions_changed", {})


@router.get("")
async def list_extensions(include_hidden: bool = Query(default=False)):
    extensions, changed = extension_store.list_extensions_with_reconciliation(include_hidden=include_hidden)
    if changed:
        await _broadcast_extensions_changed()
    return {"extensions": extensions}


@router.get("/builtin-ids")
async def get_builtin_ids():
    """Logical key -> extension id for known builtins. Private ids are present
    only where the private registry is loaded; the frontend fetches this so it
    never hardcodes private/commercial ids."""
    return {"ids": extension_store.builtin_extension_id_map()}


@router.get("/frontend-entrypoints")
async def get_frontend_entrypoints():
    return {"entrypoints": extension_store.frontend_entrypoints()}


@router.get("/ui-hooks")
async def get_ui_hooks():
    return {"hooks": extension_store.ui_hooks()}


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
    core_response = await _dispatch_core_builtin_backend(
        extension_id,
        path,
        request,
    )
    if core_response is not None:
        return core_response
    backend_spec = extension_backend_loader.backend_entrypoint_spec_cached(extension_id)
    core_response = await _dispatch_core_builtin_backend(
        extension_id,
        path,
        request,
        backend_spec=backend_spec,
    )
    if core_response is not None:
        return core_response
    return await extension_backend_loader.dispatch_extension_backend_request(
        extension_id,
        path,
        request,
        backend_spec=backend_spec,
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
        return None
    if backend_spec is not None:
        return None
    record = extension_store.get_extension(extension_id)
    if not record or record.get("enabled") is not True:
        return None
    return await _dispatch_machine_nodes_core_backend(clean_path, request)


async def _dispatch_machine_nodes_core_backend(
    path: str,
    request: Request,
) -> JSONResponse | None:
    if request.method == "GET" and path == "nodes":
        import node_store
        return JSONResponse(node_store.snapshot())
    if request.method == "GET" and path == "pending_nodes":
        from stores import pending_node_registrations
        import node_link
        return JSONResponse({
            "pending_nodes": [
                node_link._public_rec(rec)
                for rec in pending_node_registrations.list_pending()
            ],
        })
    if request.method == "GET" and path == "local_node_id":
        try:
            from topology import local_node_id
            node_id = local_node_id()
        except Exception:
            node_id = "primary"
        return JSONResponse({"node_id": node_id})
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


@router.patch("/{extension_id}/harness-delivery")
async def set_extension_harness_delivery(extension_id: str, req: SetHarnessDeliveryRequest):
    try:
        mode = extension_store.set_harness_delivery_mode(extension_id, req.mode)
    except extension_store.ExtensionError as exc:
        raise _extension_error(exc) from exc
    await _broadcast_extensions_changed()
    return {"mode": mode}


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
