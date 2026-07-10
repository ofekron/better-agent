from __future__ import annotations

import base64
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


class _RequirementsQueryPayload(_StrictPayload):
    query: str = Field(min_length=1)
    cwd: str = ""
    cwds: list[str] = Field(default_factory=list)
    all_projects: bool = False


class _RequirementsFirePayload(_RequirementsQueryPayload):
    wait: bool = False


class _RequirementsResultsPayload(_StrictPayload):
    id: str = Field(min_length=1)
    wait: float = Field(default=0.0, ge=0)


class _RequirementsUnitPayload(_RequirementsQueryPayload):
    fields: list[str] | None = None
    include_all_fields: bool = False


class _RequirementsRgPayload(_StrictPayload):
    rg_args: list[str] | None = None
    query: str = ""
    cwd: str = ""
    cwds: list[str] = Field(default_factory=list)
    all_projects: bool = False
    fields: list[str] | None = None
    include_all_fields: bool = False
    include_unprocessed_prompts: bool = False
    provider_native_only: bool = False
    compare: bool = False


class _RequirementsSqlPayload(_StrictPayload):
    sql: str = Field(min_length=1)


class _SessionIdPayload(_StrictPayload):
    session_id: str = Field(min_length=1)


class _SupervisorTogglePayload(_SessionIdPayload):
    enabled: bool
    custom_prompt: str | None = None


class _PromptEngineerStartPayload(_SessionIdPayload):
    draft: str = ""
    mode: str = Field(default="fork", pattern=r"^(fork|new)$")
    client_id: str = ""


class _PromptEngineerCommentPayload(_SessionIdPayload):
    file_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_col: int = Field(ge=0)
    comment: str = Field(min_length=1)
    client_id: str = ""


class _AutoTaggingCurrentTaskPayload(_StrictPayload):
    session_id: str = Field(min_length=1)


class _AutoTaggingSnapshotPayload(_StrictPayload):
    project_id: str = ""


class _AutoTaggingSelectPayload(_StrictPayload):
    task: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    existing_tags: list[str] = Field(default_factory=list)
    max_tags: int = Field(default=5, ge=1)
    cwd: str = ""


class _AutoTaggingEnsurePayload(_StrictPayload):
    name: str = Field(min_length=1)
    project_id: str = ""
    color: str | None = None


class _AutoTaggingSyncPayload(_StrictPayload):
    session_id: str = Field(min_length=1)
    tag_ids: list[str] = Field(default_factory=list)
    source: str = ""
    merge: bool = False


class _AutoTaggingSqlPayload(_StrictPayload):
    sql: str = Field(min_length=1)


class _ScheduleCreatePayload(_StrictPayload):
    app_session_id: str = ""
    prompt: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    delay_seconds: int | None = Field(default=None, ge=0)
    fire_at: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)


class _ScheduleListPayload(_StrictPayload):
    app_session_id: str = ""


class _ScheduleDeletePayload(_ScheduleListPayload):
    schedule_id: str = Field(min_length=1)


class _RoutineCreatePayload(_StrictPayload):
    cwd: str = Field(min_length=1)
    node_id: str = "primary"
    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    description: str = ""
    orchestration_mode: str = "native"
    worker_creation_policy: str = "approve"
    session_type: str = "normal"
    model: str | None = None
    provider_id: str | None = None
    reasoning_effort: str | None = None
    singleton: bool = False
    goal: str = ""
    trigger: dict[str, Any] | None = None
    scripts: dict[str, Any] | None = None
    assessment: dict[str, Any] | None = None


class _RoutineListPayload(_StrictPayload):
    cwd: str = Field(min_length=1)
    node_id: str = "primary"


class _RoutineIdPayload(_StrictPayload):
    task_id: str = Field(min_length=1)


class _RoutineUpdatePayload(_RoutineIdPayload):
    patch: dict[str, Any]


class _RoutineRunPayload(_RoutineIdPayload):
    prompt: str | None = None
    client_id: str | None = None


class _RoutineOutputsListPayload(_RoutineIdPayload):
    limit: int = Field(default=50, ge=1, le=100)


class _RoutineOutputsPublishPayload(_RoutineIdPayload):
    title: str = Field(min_length=1)
    file_path: str = ""
    content: str = ""
    content_type: str = "text/html"
    kind: str = "artifact"
    session_id: str = ""


class _RoutineOutputsContentPayload(_RoutineIdPayload):
    output_id: str = Field(min_length=1)


class _AssistantPreamblePayload(_StrictPayload):
    board_preamble: str = ""


class _AssistantSessionPayload(_StrictPayload):
    session_id: str = Field(min_length=1)


class _AssistantSearchPayload(_StrictPayload):
    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1)


class _AssistantAdoptPayload(_StrictPayload):
    session_id: str = ""
    transcript_path: str = ""


class _AssistantDelegatePayload(_StrictPayload):
    target_session_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    requirement_ref: str = ""


class _AssistantMessagePayload(_StrictPayload):
    sender_session_id: str = Field(min_length=1)
    target_session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    collapse_key: str = ""
    collapse_policy: str = ""


class _AssistantHeadlessPayload(_StrictPayload):
    session_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


class _AssistantAskPayload(_AssistantMessagePayload):
    target_worker_id: str = ""
    target_worker_pool: str = ""
    pool_affinity_key: str = ""
    ask_id: str = ""
    provider_id: str = ""
    model: str = ""
    reasoning_effort: str = ""
    mode: str = ""


class _AgentBoardPromptPayload(_StrictPayload):
    session_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)


class _CredentialRequestPayload(_StrictPayload):
    app_session_id: str = Field(min_length=1)
    descriptor: dict[str, Any]


class _CredentialExecutePayload(_StrictPayload):
    consent_id: str = Field(min_length=1)
    proof: str = ""


class _CredentialPendingPayload(_StrictPayload):
    app_session_id: str = ""


class _CredentialConsentPayload(_StrictPayload):
    consent_id: str = Field(min_length=1)


class _CredentialApprovePayload(_CredentialConsentPayload):
    secret: str | None = None
    secrets: dict[str, Any] | None = None


class _PasswordManagerStorePayload(_StrictPayload):
    service: str = Field(min_length=1, max_length=128)
    account: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=65536)


class _PasswordManagerDeletePayload(_StrictPayload):
    service: str = Field(min_length=1, max_length=128)
    account: str = Field(min_length=1, max_length=256)


class _NodeIdPayload(_StrictPayload):
    node_id: str = Field(min_length=1)


class _ProjectCwdPayload(_StrictPayload):
    cwd: str = ""


class _ProjectCwdsPayload(_StrictPayload):
    cwds: list[str]


class _ProjectCapturePayload(_StrictPayload):
    text: str = Field(min_length=1)
    cwd: str = ""


class _ProjectMarkSeenPayload(_ProjectCwdPayload):
    entry_ids: list[str]


class _GitBasePayload(_StrictPayload):
    actor_session_id: str = Field(min_length=1)
    cwd: str = Field(min_length=1)


class _GitDiffPayload(_GitBasePayload):
    staged: bool = True


class _GitLogPayload(_GitBasePayload):
    limit: int = Field(default=20, ge=1, le=100)


class _GitAddPayload(_GitBasePayload):
    paths: list[str] = Field(min_length=1, max_length=200)


class _GitCommitPayload(_GitBasePayload):
    message: str = Field(min_length=1, max_length=10000)


class _GitBranchPayload(_GitBasePayload):
    name: str = Field(min_length=1, max_length=255)
    create: bool = False


class _GitPushPayload(_GitBasePayload):
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9._-]+$")
    ref: str = Field(default="", max_length=255)


class _DelegateTaskPolicyPayload(_StrictPayload):
    policy: str = Field(min_length=1)


class _TeamDefinitionPlanPayload(_StrictPayload):
    source_id: str = Field(min_length=1)
    profile: str = ""
    team_instance_id: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)


class _WorkersListPayload(_StrictPayload):
    cwd: str = ""


class _WorkerCreatePayload(_StrictPayload):
    cwd: str = Field(min_length=1)
    description: str = ""
    name: str = ""
    role_key: str = ""
    orchestration_mode: str = "team"
    model: str = ""
    provider_id: str = ""
    reasoning_effort: str = ""
    node_id: str = "primary"
    provision_prompt: str = ""
    parent_session_id: str = ""
    team_instance_id: str = ""
    tags: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    disabled_builtin_extensions: list[str] = Field(default_factory=list)
    bare_config: dict[str, Any] = Field(default_factory=dict)
    capability_contexts: dict[str, Any] = Field(default_factory=dict)
    pool_worker_specs: list[dict[str, Any]] = Field(default_factory=list)


class _WorkerProvisionSpec(_WorkerCreatePayload):
    cwd: str = ""


class _WorkersProvisionPayload(_StrictPayload):
    cwd: str = Field(min_length=1)
    workers: list[_WorkerProvisionSpec]
    parent_session_id: str = ""
    team_instance_id: str = ""


class _WorkerPoolEnqueuePayload(_StrictPayload):
    tag: str = Field(min_length=1)
    sender_session_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    pool_affinity_key: str = ""
    expect_mssg_response: bool = False


class _WorkerFromSessionPayload(_StrictPayload):
    cwd: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class _WorkerIdentityPayload(_StrictPayload):
    agent_session_id: str = Field(min_length=1)


class _WorkerUnregisterPayload(_WorkerIdentityPayload):
    cwd: str = Field(min_length=1)


class _PendingApprovalsListPayload(_StrictPayload):
    cwd: str = ""


class _PendingApprovalPayload(_StrictPayload):
    delegation_id: str = Field(min_length=1)


class _PendingApprovalApprovePayload(_PendingApprovalPayload):
    description: str | None = None
    orchestration_mode: str | None = None


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


def _role_main_handler(
    role: str,
    function_name: str,
    *,
    action: str = "",
) -> Callable[[BaseModel], Awaitable[Any]]:
    async def handler(payload: BaseModel) -> Any:
        import main

        extension_id = extension_store.extension_id_for_role(role)
        if not extension_id:
            raise HTTPException(status_code=503, detail=f"{role} extension is unavailable")
        body = payload.model_dump()
        if action:
            body["action"] = action
        result = getattr(main, function_name)(
            body,
            x_internal_token=extension_token_registry.mint(extension_id),
        )
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


def _register_requirements() -> None:
    def handler(function_name: str) -> Callable[[BaseModel], Awaitable[Any]]:
        async def invoke(payload: BaseModel) -> Any:
            import main

            extension_id = extension_store.extension_id_for_role("requirements")
            if not extension_id:
                raise HTTPException(status_code=503, detail="requirements extension is unavailable")
            fn = getattr(main, function_name)
            result = fn(
                payload.model_dump(),
                x_internal_token=extension_token_registry.mint(extension_id),
            )
            return await result if inspect.isawaitable(result) else result

        return invoke

    actions = {
        "fire": (_RequirementsFirePayload, "internal_fire_get_requirements"),
        "results": (_RequirementsResultsPayload, "internal_get_requirements_results"),
        "processed": (_RequirementsQueryPayload, "internal_get_requirements"),
        "unit-rg": (_RequirementsRgPayload, "internal_search_requirements"),
        "unit-fts": (_RequirementsUnitPayload, "internal_requirements_unit_fts"),
        "unit-vector": (_RequirementsUnitPayload, "internal_requirements_unit_vector"),
        "index-sql": (_RequirementsSqlPayload, "internal_requirements_index_sql"),
    }
    for action, (schema, function_name) in actions.items():
        register("requirements", action, schema, handler(function_name))


def _main_action(
    function_name: str,
    *,
    extension_role: str = "",
) -> Callable[[BaseModel], Awaitable[Any]]:
    async def invoke(payload: BaseModel) -> Any:
        import main

        fn = getattr(main, function_name)
        kwargs: dict[str, Any] = {}
        if extension_role:
            extension_id = extension_store.extension_id_for_role(extension_role)
            if not extension_id:
                raise HTTPException(status_code=503, detail=f"{extension_role} extension is unavailable")
            kwargs["x_internal_token"] = extension_token_registry.mint(extension_id)
        result = fn(payload.model_dump(), **kwargs)
        return await result if inspect.isawaitable(result) else result

    return invoke


def _register_supervisor() -> None:
    register(
        "supervisor", "default-prompt.get", _StrictPayload,
        _main_action("internal_supervisor_default_prompt"),
    )
    register(
        "supervisor", "separate", _SessionIdPayload,
        _main_action("internal_supervisor_separate"),
    )
    register(
        "supervisor", "toggle", _SupervisorTogglePayload,
        _main_action("internal_supervisor_toggle"),
    )
    register(
        "supervisor", "review-last-work", _SessionIdPayload,
        _main_action("internal_supervisor_review_last_work"),
    )


def _register_prompt_engineer() -> None:
    mappings = {
        "start": (_PromptEngineerStartPayload, "internal_prompt_engineering_start"),
        "get": (_SessionIdPayload, "internal_prompt_engineering_get"),
        "comment": (_PromptEngineerCommentPayload, "internal_prompt_engineering_comment"),
        "result": (_SessionIdPayload, "internal_prompt_engineering_result"),
        "cleanup": (_SessionIdPayload, "internal_prompt_engineering_cleanup"),
    }
    for action, (schema, function_name) in mappings.items():
        register(
            "prompt-engineer", action, schema,
            _main_action(function_name, extension_role="prompt-engineer"),
        )


def _register_private_workflows() -> None:
    assistant_actions = {
        "ensure": (_AssistantPreamblePayload, "internal_assistant_ui_ensure"),
        "ensure-monitor": (_AssistantPreamblePayload, "internal_assistant_ui_ensure_monitor"),
        "search": (_AssistantSearchPayload, "internal_assistant_ui_search"),
        "resolve-ba-session": (_AssistantSessionPayload, "internal_assistant_ui_resolve_ba_session"),
        "adopt-native-session": (_AssistantAdoptPayload, "internal_assistant_ui_adopt_native_session"),
        "delegate": (_AssistantDelegatePayload, "internal_assistant_ui_delegate"),
        "last-turn": (_AssistantSessionPayload, "internal_assistant_ui_last_turn"),
        "session-activity": (_AssistantSessionPayload, "internal_session_activity"),
        "message.send": (_AssistantMessagePayload, "internal_mssg"),
        "headless-generate": (_AssistantHeadlessPayload, "internal_headless_generate"),
        "ask": (_AssistantAskPayload, "internal_ask"),
    }
    for action, (schema, function_name) in assistant_actions.items():
        register(
            "assistant", action, schema,
            _main_action(function_name, extension_role="assistant"),
        )
    auto_tagging_actions = {
        "current-task": _AutoTaggingCurrentTaskPayload,
        "snapshot": _AutoTaggingSnapshotPayload,
        "select-tags": _AutoTaggingSelectPayload,
        "ensure-tag": _AutoTaggingEnsurePayload,
        "sync-session-tags": _AutoTaggingSyncPayload,
        "tags-sql": _AutoTaggingSqlPayload,
    }
    for action, schema in auto_tagging_actions.items():
        register(
            "auto-tagging", action, schema,
            _role_main_handler("auto-tagging", "internal_auto_tagging", action=action),
        )
    schedule_actions = {
        "create": _ScheduleCreatePayload,
        "list": _ScheduleListPayload,
        "delete": _ScheduleDeletePayload,
    }
    for action, schema in schedule_actions.items():
        register(
            "scheduler", action, schema,
            _role_main_handler("scheduler", "internal_schedules", action=action),
        )
    routine_actions = {
        "create": _RoutineCreatePayload,
        "list": _RoutineListPayload,
        "get": _RoutineIdPayload,
        "update": _RoutineUpdatePayload,
        "delete": _RoutineIdPayload,
        "run": _RoutineRunPayload,
        "stop": _RoutineIdPayload,
    }
    for action, schema in routine_actions.items():
        register(
            "routines", action, schema,
            _role_main_handler("routines", "internal_tasks", action=action),
        )
    output_actions = {
        "outputs.list": _RoutineOutputsListPayload,
        "outputs.publish": _RoutineOutputsPublishPayload,
    }
    for action, schema in output_actions.items():
        register(
            "routines", action, schema,
            _role_main_handler(
                "routines",
                "internal_task_outputs",
                action=action.rsplit(".", 1)[1],
            ),
        )

    async def output_content(payload: BaseModel) -> Any:
        from stores import task_output_store

        try:
            path, content_type = await __import__("asyncio").to_thread(
                task_output_store.content_path, payload.task_id, payload.output_id
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown output") from exc
        raw = await __import__("asyncio").to_thread(path.read_bytes)
        return {"content_base64": base64.b64encode(raw).decode("ascii"), "content_type": content_type}

    register("routines", "outputs.content", _RoutineOutputsContentPayload, output_content)
    team_actions = {
        "policy.get": (_StrictPayload, "internal_get_delegate_task_policy_endpoint"),
        "policy.set": (_DelegateTaskPolicyPayload, "internal_set_delegate_task_policy_endpoint"),
        "definitions.list": (_StrictPayload, "internal_list_extension_team_definitions"),
        "definitions.plan": (_TeamDefinitionPlanPayload, "internal_plan_team_definition"),
        "workers.list": (_WorkersListPayload, "internal_list_workers_for_cwd"),
        "workers.create": (_WorkerCreatePayload, "internal_create_worker"),
        "workers.provision": (_WorkersProvisionPayload, "internal_provision_workers_ui"),
        "worker-pool.enqueue": (_WorkerPoolEnqueuePayload, "internal_enqueue_worker_pool_prompt"),
        "workers.from-session": (_WorkerFromSessionPayload, "internal_register_existing_session_as_worker"),
        "workers.unregister": (_WorkerUnregisterPayload, "internal_unregister_worker"),
        "workers.reset-forks": (_WorkerIdentityPayload, "internal_reset_worker_forks"),
        "approvals.list": (_PendingApprovalsListPayload, "internal_list_pending_approvals"),
        "approvals.approve": (_PendingApprovalApprovePayload, "internal_approve_pending_approval"),
        "approvals.deny": (_PendingApprovalPayload, "internal_deny_pending_approval"),
    }
    for action, (schema, function_name) in team_actions.items():
        register(
            "team-orchestration", action, schema,
            _main_action(function_name, extension_role="team-orchestration"),
        )


def _register_agent_board() -> None:
    register(
        "agent-board", "prompt.run", _AgentBoardPromptPayload,
        _main_action("internal_agent_board_run_prompt", extension_role="agent-board"),
    )


def _register_credential_broker() -> None:
    mappings = {
        "request": (_CredentialRequestPayload, "internal_credential_request"),
        "execute": (_CredentialExecutePayload, "internal_credential_execute"),
        "ui.pending": (_CredentialPendingPayload, "internal_list_pending_credentials"),
        "ui.approve": (_CredentialApprovePayload, "internal_approve_credential_consent"),
        "ui.deny": (_CredentialConsentPayload, "internal_deny_credential_consent"),
        "ui.revoke": (_CredentialConsentPayload, "internal_revoke_credential_consent"),
        "password-manager.list": (_StrictPayload, "internal_list_password_manager_secrets"),
        "password-manager.store": (_PasswordManagerStorePayload, "internal_store_password_manager_secret"),
        "password-manager.delete": (_PasswordManagerDeletePayload, "internal_delete_password_manager_secret"),
    }
    for action, (schema, function_name) in mappings.items():
        register(
            "credential-broker", action, schema,
            _main_action(function_name, extension_role="credential-broker"),
        )


def _register_machine_nodes() -> None:
    mappings = {
        "list": (_StrictPayload, "internal_get_nodes"),
        "local-node-id": (_StrictPayload, "internal_get_local_node_id"),
        "pending": (_StrictPayload, "internal_list_pending_nodes"),
        "approve": (_NodeIdPayload, "internal_approve_pending_node"),
        "deny": (_NodeIdPayload, "internal_deny_pending_node"),
        "revoke": (_NodeIdPayload, "internal_revoke_node"),
        "restart": (_NodeIdPayload, "internal_restart_node"),
    }
    for action, (schema, function_name) in mappings.items():
        register(
            "machine-nodes", action, schema,
            _main_action(function_name, extension_role="machine-nodes"),
        )


def _register_project_structure() -> None:
    mappings = {
        "updates.count": (_ProjectCwdPayload, "internal_project_update_count"),
        "updates.total": (_StrictPayload, "internal_project_update_total"),
        "updates.counts-batch": (_ProjectCwdsPayload, "internal_project_update_counts_batch"),
        "updates.unseen": (_ProjectCwdPayload, "internal_project_updates_unseen"),
        "updates.capture": (_ProjectCapturePayload, "capture_project_update"),
        "updates.mark-seen": (_ProjectMarkSeenPayload, "internal_project_updates_mark_seen"),
        "edit.status": (_ProjectCwdPayload, "internal_project_structure_edit_status"),
        "edit.ensure": (_ProjectCwdPayload, "internal_project_structure_edit_ensure"),
    }
    for action, (schema, function_name) in mappings.items():
        register(
            "project-structure", action, schema,
            _main_action(function_name, extension_role="project-structure"),
        )


def _register_git() -> None:
    import git_capability

    mappings = {
        "status": _GitBasePayload,
        "diff": _GitDiffPayload,
        "log": _GitLogPayload,
        "add": _GitAddPayload,
        "commit": _GitCommitPayload,
        "branch": _GitBranchPayload,
        "push": _GitPushPayload,
    }
    for action, schema in mappings.items():
        register(
            "git", action, schema,
            lambda payload, action=action: git_capability.execute(action, payload.model_dump()),
        )


_register_ask()
_register_provider_config_sync()
_register_switch_control()
_register_marketplace()
_register_session_control()
_register_session_bridge()
_register_requirements()
_register_supervisor()
_register_prompt_engineer()
_register_private_workflows()
_register_agent_board()
_register_credential_broker()
_register_machine_nodes()
_register_project_structure()
_register_git()
