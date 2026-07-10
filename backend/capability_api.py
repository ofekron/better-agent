from __future__ import annotations

import inspect
import os
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import extension_store
import extension_token_registry


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvokeCapabilityRequest(_StrictPayload):
    capability: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    action: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    payload: dict[str, Any] = Field(default_factory=dict)


class _CwdPayload(_StrictPayload):
    cwd: str = ""


class _SettingsPayload(_CwdPayload):
    capability_id: str = ""


class _ProviderConfigBroadcastPayload(_StrictPayload):
    scope: str = ""
    category: str = ""
    capability_id: str = ""
    path: str = ""
    cwd: str = ""


class _AskSearchPayload(_StrictPayload):
    query: str = Field(min_length=1)
    max_results: int | None = Field(default=None, gt=0)
    timeout: float | None = Field(default=None, gt=0)
    provider_id: str = ""
    model: str = ""
    reasoning_effort: str = ""
    node_id: str = ""


class _SwitchTargetPayload(_StrictPayload):
    target: str = Field(pattern=r"^(dev|main)$")


class _MarketplaceSearchPayload(_StrictPayload):
    query: str = ""
    limit: int = Field(default=20, ge=1, le=100)


class _ExtensionIdPayload(_StrictPayload):
    extension_id: str = Field(min_length=1)


class _MarketplaceInstallPayload(_ExtensionIdPayload):
    entitlement_token: str = ""


class _MarketplaceSetEnabledPayload(_ExtensionIdPayload):
    enabled: bool


class _SessionSelectorsPayload(_StrictPayload):
    app_session_id: str = Field(min_length=1)
    model: str = ""
    provider_id: str = ""
    reasoning_effort: str = ""


class _SessionContinuePayload(_StrictPayload):
    app_session_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    when: str = Field(default="next_turn", pattern=r"^(next_turn|now)$")


class _SessionBridgeSearchPayload(_StrictPayload):
    app_session_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=10)
    provider_id: str = ""
    model: str = ""
    reasoning_effort: str = ""
    node_id: str = ""


class _SessionBridgeDelegatePayload(_StrictPayload):
    app_session_id: str = Field(min_length=1)
    session_id: str = ""
    prompt: str = Field(min_length=1)
    display_prompt: str = ""
    source: str = ""
    client_id: str = ""
    run_mode: str = Field(pattern=r"^(fork|continue|new)$")
    approval: str = Field(pattern=r"^(auto|require)$")
    provider_id: str = ""
    model: str = ""
    reasoning_effort: str = ""


class _SessionBridgeProposePayload(_StrictPayload):
    caller_sid: str = Field(min_length=1)
    session_ids: list[str]
    reasoning: str = ""
    proposed_project_path: str = ""


class _SessionBridgeResolvePayload(_StrictPayload):
    delegation_id: str = Field(min_length=1)
    chosen_session_id: str | None = None


@dataclass(frozen=True)
class _Action:
    schema: type[BaseModel]
    handler: Callable[[BaseModel], Any | Awaitable[Any]]


_ACTIONS: dict[tuple[str, str], _Action] = {}


def register(
    capability: str,
    action: str,
    schema: type[BaseModel],
    handler: Callable[[BaseModel], Any | Awaitable[Any]],
) -> None:
    key = (capability, action)
    if key in _ACTIONS:
        raise RuntimeError(f"duplicate capability action: {capability}.{action}")
    _ACTIONS[key] = _Action(schema=schema, handler=handler)


def _require_grant(token: str, capability: str, action: str) -> str:
    extension_id = extension_token_registry.resolve(token)
    if not extension_id:
        raise HTTPException(status_code=403, detail="capability invocation requires an extension token")
    record = extension_store.get_extension(extension_id)
    if not record or not extension_store.is_extension_active(extension_id):
        raise HTTPException(status_code=403, detail="extension is not active")
    grants = (extension_store.declared_permissions(record).get("capabilities") or [])
    if f"{capability}.{action}" not in grants:
        raise HTTPException(status_code=403, detail="capability action is not granted")
    return extension_id


async def _invoke(request: InvokeCapabilityRequest, token: str) -> Any:
    _require_grant(token, request.capability, request.action)
    registered = _ACTIONS.get((request.capability, request.action))
    if registered is None:
        raise HTTPException(status_code=404, detail="unknown capability action")
    accepted_keys = set(registered.schema.model_fields)
    accepted_keys.update(
        str(field.alias)
        for field in registered.schema.model_fields.values()
        if field.alias
    )
    unknown_keys = sorted(set(request.payload) - accepted_keys)
    if unknown_keys:
        raise HTTPException(
            status_code=422,
            detail=f"unknown capability payload fields: {', '.join(unknown_keys)}",
        )
    try:
        payload = registered.schema.model_validate(request.payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    result = registered.handler(payload)
    if inspect.isawaitable(result):
        result = await result
    return result


router = APIRouter(prefix="/api/internal/capabilities", tags=["capabilities"])


@router.post("/invoke")
async def invoke_capability(
    request: InvokeCapabilityRequest,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
) -> Any:
    return await _invoke(request, x_internal_token)


def _register_ask() -> None:
    import session_search

    async def search_sessions(payload: BaseModel) -> Any:
        values = payload.model_dump(exclude_none=True)
        query = values.pop("query")
        values = {key: value for key, value in values.items() if value != ""}
        result = await session_search.run_search_sessions_session(query, **values)
        return session_search.canonical_search_response(result)

    async def ensure(_payload: BaseModel) -> Any:
        return await session_search.ensure_ask_session()

    register("ask", "sessions.search", _AskSearchPayload, search_sessions)
    register("ask", "ensure", _StrictPayload, ensure)


def _register_provider_config_sync() -> None:
    import provider_config_sync_api as pcs

    async def call(fn: Callable[..., Any], payload: BaseModel, *, unpack: bool = False) -> Any:
        result = fn(**payload.model_dump()) if unpack else fn(payload)
        return await result if inspect.isawaitable(result) else result

    register("provider-config-sync", "state.get", _CwdPayload, lambda p: pcs._discover(p.cwd))
    register(
        "provider-config-sync", "capability-picker.get", _CwdPayload,
        lambda p: {"sources": pcs._capability_picker_sources(p.cwd)},
    )
    register(
        "provider-config-sync", "settings.get", _SettingsPayload,
        lambda p: pcs._standalone_api.get_auto_sync_settings(p.cwd, p.capability_id),
    )
    mappings = {
        "settings.patch": (pcs._standalone_api.AutoSyncSettingsPatch, pcs.patch_provider_config_sync_settings),
        "repository.init": (pcs._standalone_api.RepositoryConfigRequest, pcs.init_provider_config_sync_repository),
        "repository.load": (pcs._standalone_api.RepositoryConfigRequest, pcs.load_provider_config_sync_repository),
        "file.put": (pcs.WriteNativeFileRequest, pcs.write_native_file_route),
        "file.restore": (pcs.RestoreNativeFileRequest, pcs.restore_native_file_route),
        "capability.delete": (pcs.DeleteCapabilityRequest, pcs.delete_capability_route),
        "capability.create": (pcs.CreateCapabilityRequest, pcs.create_capability_route),
        "capability.transfer": (pcs.TransferCapabilityRequest, pcs.transfer_capability_route),
        "apply": (pcs.ApplyNativeFileRequest, pcs.apply_native_file_route),
        "auto-sync": (pcs.AutoSyncRequest, pcs.auto_sync_route),
        "unified-item.upsert": (pcs.UpsertUnifiedCapabilityItemRequest, pcs.upsert_unified_capability_item_route),
        "unified-item.remove": (pcs.RemoveUnifiedCapabilityItemRequest, pcs.remove_unified_capability_item_route),
    }
    for action, (schema, fn) in mappings.items():
        register("provider-config-sync", action, schema, lambda p, fn=fn: call(fn, p))
    register("provider-config-sync", "repository.get", _StrictPayload, lambda _p: pcs.get_provider_config_sync_repository_status())
    register("provider-config-sync", "repository.sync", _StrictPayload, lambda _p: pcs.sync_provider_config_sync_repository())

    async def broadcast(payload: BaseModel) -> Any:
        await pcs._broadcast_better_agent_changed(**payload.model_dump())
        return {"ok": True}

    register("provider-config-sync", "change.broadcast", _ProviderConfigBroadcastPayload, broadcast)


def _register_switch_control() -> None:
    def running_checkout() -> str:
        value = os.environ.get("BETTER_AGENT_ACTIVE_CHECKOUT", "").strip()
        if not value:
            raise HTTPException(status_code=409, detail="launcher did not declare the active checkout")
        return value

    def get_state(_payload: BaseModel) -> Any:
        from daemonhost import switch_control

        return switch_control.state(running_checkout())

    async def request_switch(payload: BaseModel) -> Any:
        from daemonhost import switch_control

        if os.environ.get("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
            raise HTTPException(status_code=409, detail="line switching requires the launcher supervisor")
        request_id = str(uuid.uuid4())
        requested = switch_control.request(running_checkout(), payload.target, request_id)
        try:
            import main

            restarted_nodes = await main._restart_connected_worker_nodes()
            await main._trigger_supervisor_restart(request_id)
        except Exception as exc:
            switch_control.abort(request_id, f"restart trigger failed: {exc}")
            raise HTTPException(status_code=502, detail=f"restart trigger failed: {exc}") from exc
        return {
            **requested,
            "restart": {
                "status": "rebuilding",
                "request_id": request_id,
                "restarted_nodes": restarted_nodes,
            },
        }

    register("switch-control", "state.get", _StrictPayload, get_state)
    register("switch-control", "switch.request", _SwitchTargetPayload, request_switch)


def _legacy_main_handler(
    extension_id: str,
    function_name: str,
    *,
    action: str = "",
) -> Callable[[BaseModel], Awaitable[Any]]:
    async def handler(payload: BaseModel) -> Any:
        import main

        body = payload.model_dump()
        if action:
            body["action"] = action
        fn = getattr(main, function_name)
        result = fn(body, x_internal_token=extension_token_registry.mint(extension_id))
        return await result if inspect.isawaitable(result) else result

    return handler


def _register_marketplace() -> None:
    extension_id = extension_store.MARKETPLACE_EXTENSION_ID
    actions: dict[str, tuple[type[BaseModel], str]] = {
        "search": (_MarketplaceSearchPayload, "search"),
        "installed.list": (_StrictPayload, "list_installed"),
        "installed.get": (_ExtensionIdPayload, "get_installed"),
        "install": (_MarketplaceInstallPayload, "install"),
        "enabled.set": (_MarketplaceSetEnabledPayload, "set_enabled"),
        "uninstall": (_ExtensionIdPayload, "uninstall"),
        "update": (_StrictPayload, "update"),
    }
    for capability_action, (schema, legacy_action) in actions.items():
        register(
            "marketplace",
            capability_action,
            schema,
            _legacy_main_handler(extension_id, "internal_marketplace", action=legacy_action),
        )


def _register_session_control() -> None:
    extension_id = extension_store.BUILTIN_SESSION_CONTROL_EXTENSION_ID
    register(
        "session-control", "selectors.set", _SessionSelectorsPayload,
        _legacy_main_handler(extension_id, "internal_session_control_selectors"),
    )
    register(
        "session-control", "continue-fresh", _SessionContinuePayload,
        _legacy_main_handler(extension_id, "internal_session_control_continue_fresh"),
    )


def _register_session_bridge() -> None:
    extension_id = extension_store.BUILTIN_SESSION_BRIDGE_EXTENSION_ID
    register(
        "session-bridge", "sessions.search", _SessionBridgeSearchPayload,
        _legacy_main_handler(extension_id, "internal_session_bridge_search"),
    )
    register(
        "session-bridge", "delegate", _SessionBridgeDelegatePayload,
        _legacy_main_handler(extension_id, "internal_session_bridge_delegate"),
    )
    register(
        "session-bridge", "sessions.propose", _SessionBridgeProposePayload,
        _legacy_main_handler(extension_id, "internal_ask_propose"),
    )
    register(
        "session-bridge", "delegation.resolve", _SessionBridgeResolvePayload,
        _legacy_main_handler(extension_id, "internal_session_bridge_delegate_resolve"),
    )


_register_ask()
_register_provider_config_sync()
_register_switch_control()
_register_marketplace()
_register_session_control()
_register_session_bridge()
