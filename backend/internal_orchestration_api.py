"""Internal-loopback orchestration routes: ask-fork delegation, the
delegate-task router and its policy, internal-LLM task assignments, team
definition planning/activation plus team roster registration, and the
worker / session / sub-session creation primitives agents call.

The coordinator, main.py's session-tree deletion, and worker
provisioning are injected by the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Body, Header, HTTPException

import config_store
import extension_store
import harness_profile_resolver
import internal_extension_api
import internal_guards
import perf
from capability_contexts import normalize_capability_contexts
from communication_modes import normalize_ask_execution
from harness_profiles_api import harness_profile_selection as _harness_profile_selection
from i18n import t
from provider_validation import (
    api_disabled_builtin_extensions as _api_disabled_builtin_extensions,
    api_disallowed_tools as _api_disallowed_tools,
    api_extra_mcp_servers as _api_extra_mcp_servers,
    api_optional_provision_prompt as _api_optional_provision_prompt,
    api_optional_provisioned_tool_profile as _api_optional_provisioned_tool_profile,
    api_reasoning_effort as _api_reasoning_effort,
    inherited_reasoning_effort as _inherited_reasoning_effort,
    profile_prefill_model,
    provider_reasoning_effort as _provider_reasoning_effort,
    provider_runner as _provider_runner,
    required_model_from_body_or_provider as _required_model_from_body_or_provider,
    resolve_provider_id_ref as _resolve_provider_id_ref,
    validate_provider_model as _validate_provider_model,
)
from session_helpers import session_exists as _session_exists, session_lite as _session_lite
from session_listing_api import (
    apply_initial_session_organization as _apply_initial_session_organization,
    broadcast_session_organization_changed as _broadcast_session_organization_changed,
    initial_session_organization_from_body as _initial_session_organization_from_body,
    session_organization_input_from_body as _session_organization_input_from_body,
)
from session_manager import DelegateForkParentMissing, manager as session_manager

router = APIRouter()
logger = logging.getLogger(__name__)

_coordinator_ref: Any = None
_delete_session_tree: Optional[Callable[[str], Awaitable[bool]]] = None
_provision_workers: Optional[Callable[[dict], Awaitable[dict]]] = None


def configure(
    *,
    coordinator: Any,
    delete_session_tree: Callable[[str], Awaitable[bool]],
    provision_workers: Callable[[dict], Awaitable[dict]],
) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref, _delete_session_tree, _provision_workers
    _coordinator_ref = coordinator
    _delete_session_tree = delete_session_tree
    _provision_workers = provision_workers


def _configured(value: Any, name: str) -> Any:
    if value is None:
        raise HTTPException(
            status_code=503,
            detail=f"internal orchestration API is not configured ({name})",
        )
    return value


def _coordinator() -> Any:
    return _configured(_coordinator_ref, "coordinator")


@router.post("/api/internal/ask-fork")
async def internal_ask_fork(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """The fork engine behind `ask(run_mode='fork')`. Spawns a target run on
    a per-(caller, target) fork, streams its events back over the originating
    app session's WebSocket, and returns the aggregate result payload
    (jsonl_path + byte offsets) the caller samples to verify the outcome.
    """
    with perf.timed("ask_fork.route"):
        internal_guards.require_role_internal('team-orchestration')
        if not internal_guards.authority_is_valid():
            raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
        durable = await internal_extension_api.maybe_run_core_mcp_job("ask-fork", body)
        if durable is not None:
            return durable
        return await _handle_internal_ask_fork(body)


async def _handle_internal_ask_fork(body: dict) -> dict[str, Any]:
        if not body.get("worker_session_id"):
            raise HTTPException(
                status_code=400,
                detail="ask-fork requires worker_session_id",
            )
        worker_session_id = str(body.get("worker_session_id") or "").strip()
        if not await _session_exists(worker_session_id):
            raise HTTPException(status_code=404, detail=t("error.session_not_found"))
        requested_provider_id = await _resolve_provider_id_ref(
            str(body.get("provider_id") or "").strip(),
        )
        provisioned_tool_profile = _api_optional_provisioned_tool_profile(
            body.get("provisioned_tool_profile"),
            body,
        )
        ask_mode = str(body.get("ask_mode") or "").strip()
        if ask_mode:
            try:
                ask_mode, _ = normalize_ask_execution(ask_mode, "fork")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return await _coordinator().run_delegation(
                app_session_id=body["app_session_id"],
                instructions=body["instructions"],
                worker_session_id=worker_session_id,
                worker_description=str(body.get("worker_description") or ""),
                provider_id=requested_provider_id,
                model=body["model"],
                reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
                runner=str(body.get("runner") or "").strip(),
                cwd=body["cwd"],
                justification=body.get("justification"),
                proposed_orchestration_mode=body.get("proposed_orchestration_mode"),
                client_delegation_id=body.get("client_delegation_id"),
                node_id=body.get("node_id"),
                run_mode=body.get("run_mode") or "fork",
                worker_registry_cwd=body.get("worker_registry_cwd"),
                ephemeral=body.get("ephemeral") is True,
                machine_completion=body.get("machine_completion") is True,
                provision_prompt=_api_optional_provision_prompt(body.get("provision_prompt")),
                provisioned_tool_profile=provisioned_tool_profile,
                include_events=body.get("include_events") is True,
                ask_mode=ask_mode,
            )
        except DelegateForkParentMissing as exc:
            # Race: the parent agent session vanished between the
            # worker_session existence check above and fork creation
            # (delete/eviction, or a stale/unknown agent session id).
            # Map to 409 instead of letting the strict-mode KeyError
            # surface as a bare 500. Catches ONLY this typed subclass so
            # unrelated KeyErrors still propagate as real errors.
            raise HTTPException(
                status_code=409,
                detail="parent agent session no longer available for fork",
            ) from exc


# Max chars accepted for a headless-generate prompt. Bounds the blast
# radius of an over-large client-supplied prompt; generous enough for a
# composer draft plus the extension's wrapping instruction.
_HEADLESS_GENERATE_MAX_PROMPT = 16_000
_HEADLESS_GENERATE_TIMEOUT = 60.0


@router.post("/api/internal/headless-generate")
async def internal_headless_generate(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """One-shot, tool-less, render-tree-invisible text generation seeded
    with a session's conversation.

    Forks the session's provider sid (`--fork-session`) so the user's real
    conversation is never mutated, runs with EVERY built-in tool disabled
    (`no_tools=True`) so a generation can only produce text, and returns
    `{text}` synchronously. Leaves zero footprint in the session render
    tree / events.jsonl. Backs the
    composer-fill extension; internal-token callers only.
    """
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if set(body) != {"session_id", "prompt"}:
        raise HTTPException(status_code=400, detail="invalid headless generation request")
    if type(body.get("session_id")) is not str or type(body.get("prompt")) is not str:
        raise HTTPException(status_code=400, detail="invalid headless generation request")
    session_id = body["session_id"].strip()
    prompt = body["prompt"].strip()
    if not session_id or not prompt:
        raise HTTPException(status_code=400, detail="session_id and prompt are required")
    if len(prompt) > _HEADLESS_GENERATE_MAX_PROMPT:
        raise HTTPException(status_code=413, detail="prompt too long")
    from headless_admission import run_session_headless
    try:
        result = await run_session_headless(
            session_id,
            prompt=prompt,
            fork=True,
            resume=False,
            no_tools=True,
            timeout=_HEADLESS_GENERATE_TIMEOUT,
            permission_scope="internal_generation",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("headless generation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="generation failed") from exc
    if not result or result.get("is_error"):
        raise HTTPException(status_code=502, detail="generation failed")
    return {"text": str(result.get("result") or "")}


@router.post("/api/internal/delegate-task")
async def internal_delegate_task(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """The `delegate_task` tool's backend: smart router. Per the global
    delegate_task_policy, resolves a target (caller-supplied → search first
    suggestion → create new), optionally gates on user approval, then dispatches
    the task detached (does NOT join the sender's turn). Generic — available to
    any session."""
    internal_guards.require_role_internal('team-orchestration')
    durable = await internal_extension_api.maybe_run_core_mcp_job("delegate-task", body)
    if durable is not None:
        return durable
    return await _handle_internal_delegate_task(body)


async def _handle_internal_delegate_task(body: dict) -> dict[str, Any]:
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    task = str(body.get("task") or "").strip()
    if not sender_session_id or not task:
        raise HTTPException(
            status_code=400,
            detail="sender_session_id and task are required",
        )
    requested_profile_id = body.get("runtime_profile_id")
    if requested_profile_id is not None:
        if not isinstance(requested_profile_id, str) or not requested_profile_id.strip():
            raise HTTPException(
                status_code=400, detail="runtime_profile_id must be a string"
            )
        delegation_profile = await asyncio.to_thread(
            config_store.get_runtime_profile, requested_profile_id
        )
        if delegation_profile is None or delegation_profile.get("deleted_at"):
            raise HTTPException(
                status_code=400, detail="runtime profile is unknown or deleted"
            )
        if (
            str(body.get("provider_id") or "").strip()
            and body["provider_id"] != delegation_profile["provider_id"]
        ):
            raise HTTPException(
                status_code=400,
                detail="provider_id conflicts with runtime_profile_id",
            )
        body = {
            **body,
            "provider_id": delegation_profile["provider_id"],
            "runner": delegation_profile["runner"],
        }
    folder_id, tag_ids = _session_organization_input_from_body(body)
    harness_profile_id = _harness_profile_selection(body)
    # A profile's provider/model/effort pins fill in only what the caller left
    # out, so an auto-created delegate target runs the profile's intended stack.
    profile_selectors = harness_profile_resolver.merge_selector_defaults(
        {
            "provider_id": str(body.get("provider_id") or "").strip() or None,
            "model": str(body.get("model") or "").strip() or None,
            "reasoning_effort": body.get("reasoning_effort"),
        },
        harness_profile_id,
    )
    target = body.get("target_session_id")
    if target in ("", "null"):
        target = None
    raw_provider_id = str(profile_selectors["provider_id"] or "").strip()
    if target:
        requested_provider_id = await _resolve_provider_id_ref(raw_provider_id) if raw_provider_id else ""
    elif raw_provider_id.upper() == "ANY":
        requested_provider_id = "ANY"
    elif raw_provider_id:
        requested_provider_id = await _resolve_provider_id_ref(raw_provider_id)
    else:
        # Omitted provider means delegate auto-routing searches the same global
        # corpus the session-list AI search uses. If no existing target fits,
        # the coordinator still creates a fallback target from the sender's
        # provider/model.
        requested_provider_id = ""
    requested_model = str(profile_selectors["model"] or "").strip()
    model = requested_model
    run_provider_id = "" if requested_provider_id.upper() == "ANY" else requested_provider_id
    if requested_model or run_provider_id:
        sender = await _session_lite(sender_session_id)
        provider_id = run_provider_id or str((sender or {}).get("provider_id") or "").strip() or None
        if not model and run_provider_id:
            model = await asyncio.to_thread(profile_prefill_model, provider_id)
            if not model:
                provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
                name = provider.get("name") or provider_id
                raise HTTPException(status_code=400, detail=f"{name} has no default model configured")
        if not model:
            model = str((sender or {}).get("model") or "").strip()
        await asyncio.to_thread(_validate_provider_model, provider_id, model)
    try:
        raw_search_tags = body.get("search_tags")
        search_tags = (
            [t for t in raw_search_tags if isinstance(t, str) and t.strip()]
            if isinstance(raw_search_tags, list) else None
        )
        result = await _coordinator().run_delegate_task(
            sender_session_id=sender_session_id,
            task=task,
            target_session_id=target,
            provider_id=requested_provider_id,
            model=model,
            reasoning_effort=str(profile_selectors["reasoning_effort"] or "").strip(),
            runner=str(body.get("runner") or "").strip(),
            sub_session=body.get("sub_session") is not False,
            harness_profile_id=harness_profile_id,
            cwd=str(body.get("cwd") or ""),
            run_mode=str(body.get("run_mode") or "direct").strip() or "direct",
            folder_id=folder_id,
            tag_ids=tag_ids,
            search_cwd=str(body.get("search_cwd") or "").strip() or None,
            search_folder=str(body.get("search_folder") or "").strip() or None,
            search_tags=search_tags,
        )
        if result.get("created_session") and result.get("target_session_id"):
            await _broadcast_session_organization_changed(
                [str(result["target_session_id"])],
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/internal/delegate-task-policy/get")
async def internal_get_delegate_task_policy_endpoint(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    policy = await asyncio.to_thread(config_store.get_delegate_task_policy)
    return {"policy": policy}


@router.post("/api/internal/delegate-task-policy/set")
async def internal_set_delegate_task_policy_endpoint(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    policy = await asyncio.to_thread(
        config_store.set_delegate_task_policy,
        str(body.get("policy") or ""),
    )
    return {"policy": policy}


@router.get("/api/settings/internal-llm")
async def get_internal_llm_endpoint():
    """Tasks: the known internal-LLM task keys. Assignments: the stored
    {task → {provider_id?, model?, reasoning_effort?}} map (missing fields
    mean "inherit active provider")."""
    assignments = await asyncio.to_thread(config_store.get_internal_llm_assignments)
    extension_tasks = await asyncio.to_thread(extension_store.extension_internal_llm_task_keys)
    tasks = await asyncio.to_thread(config_store.internal_llm_tasks)
    labels = extension_store.internal_llm_task_labels()
    return {
        "tasks": [task for task in tasks if task not in extension_tasks],
        "labels": {key: label for key, label in labels.items() if key not in extension_tasks},
        "assignments": {
            key: value
            for key, value in assignments.items()
            if key not in extension_tasks
        },
    }


@router.put("/api/settings/internal-llm")
async def set_internal_llm_endpoint(body: dict):
    raw_assignments = body.get("assignments") or {}
    if not isinstance(raw_assignments, dict):
        raise HTTPException(status_code=400, detail="assignments must be an object")
    extension_tasks = await asyncio.to_thread(extension_store.extension_internal_llm_task_keys)
    forbidden = sorted(str(key) for key in raw_assignments if str(key) in extension_tasks)
    if forbidden:
        raise HTTPException(status_code=403, detail="extension-owned internal LLM tasks must be edited in extension settings")
    current = await asyncio.to_thread(config_store.get_internal_llm_assignments)
    merged = {
        key: value
        for key, value in current.items()
        if key in extension_tasks
    }
    merged.update(raw_assignments)
    assignments = await asyncio.to_thread(
        config_store.set_internal_llm_assignments,
        merged,
    )
    await _coordinator().broadcast_global("internal_llm_changed", {})
    return {
        "assignments": {
            key: value
            for key, value in assignments.items()
            if key not in extension_tasks
        }
    }


@router.post("/api/internal/team-definitions/list")
async def internal_list_extension_team_definitions(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    try:
        return {"team_definitions": extension_store.team_definition_sources()}
    except extension_store.ExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/internal/team-definitions/plan")
async def internal_plan_team_definition(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    import team_definitions

    try:
        return {
            "plan": team_definitions.build_plan(
                source_id=str((body or {}).get("source_id") or ""),
                profile=str((body or {}).get("profile") or ""),
                team_instance_id=str((body or {}).get("team_instance_id") or ""),
                variables=(body or {}).get("variables") or {},
            )
        }
    except (extension_store.ExtensionError, team_definitions.TeamDefinitionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/internal/teams/create")
async def internal_create_team(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_store

    root_session_id = str(body.get("root_session_id") or "").strip()
    if not root_session_id:
        raise HTTPException(status_code=400, detail="root_session_id is required")
    if not await _session_lite(root_session_id):
        raise HTTPException(status_code=400, detail="root_session_id does not exist")
    try:
        team = team_store.create(
            root_session_id=root_session_id,
            definition_ref=str(body.get("definition_ref") or "").strip(),
            profile=str(body.get("profile") or "").strip(),
            team_id=str(body.get("team_id") or "").strip() or None,
        )
    except team_store.TeamStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "team": team}


@router.post("/api/internal/teams/register-member")
async def internal_register_team_member(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_store

    provider_id = await _resolve_provider_id_ref(str(body.get("provider_id") or ""))
    try:
        member = team_store.upsert_member(
            str(body.get("team_instance_id") or ""),
            member_id=str(body.get("member_id") or ""),
            member_type=str(body.get("member_type") or ""),
            agent_session_id=str(body.get("agent_session_id") or ""),
            role=str(body.get("role") or ""),
            description=str(body.get("description") or ""),
            cwd=str(body.get("cwd") or ""),
            provider_id=provider_id,
            model=str(body.get("model") or ""),
            reasoning_effort=str(body.get("reasoning_effort") or ""),
            runner=str(body.get("runner") or ""),
            run_mode=str(body.get("run_mode") or ""),
            parent_member_id=str(body.get("parent_member_id") or ""),
            status=str(body.get("status") or "active"),
            nested_team_id=str(body.get("nested_team_id") or ""),
        )
    except team_store.TeamStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "member": member}


@router.post("/api/internal/team-definitions/activate")
async def internal_activate_team_definition(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_activation_store
    import team_definitions

    root_session_id = str(body.get("root_session_id") or "").strip()
    if not root_session_id:
        raise HTTPException(status_code=400, detail="root_session_id is required")
    if not await _session_lite(root_session_id):
        raise HTTPException(status_code=400, detail="root_session_id does not exist")
    raw_plan = body.get("plan")
    if raw_plan is not None and not isinstance(raw_plan, dict):
        raise HTTPException(status_code=400, detail="plan must be an object")
    team_instance_id = str(
        (raw_plan or {}).get("team_instance_id")
        or body.get("team_instance_id")
        or f"team-{uuid.uuid4().hex}"
    ).strip()
    if raw_plan is None:
        try:
            raw_plan = team_definitions.build_plan(
                source_id=str(body.get("source_id") or ""),
                profile=str(body.get("profile") or ""),
                team_instance_id=team_instance_id,
                variables=body.get("variables") if isinstance(body.get("variables"), dict) else {},
            )
        except (extension_store.ExtensionError, team_definitions.TeamDefinitionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif not str(raw_plan.get("team_instance_id") or "").strip():
        raw_plan = {**raw_plan, "team_instance_id": team_instance_id}
    activation = team_activation_store.create(
        root_session_id=root_session_id,
        team_instance_id=team_instance_id,
        source_id=str(raw_plan.get("source_id") or body.get("source_id") or ""),
        profile=str(raw_plan.get("profile") or body.get("profile") or ""),
    )
    asyncio.create_task(
        _run_team_definition_activation(
            activation["id"],
            root_session_id=root_session_id,
            plan=raw_plan,
            default_cwd=str(body.get("cwd") or ""),
            bare_config=body.get("bare_config") is True,
        ),
        name=f"team-activation-{activation['id']}",
    )
    return {"success": True, "activation": activation}


@router.get("/api/internal/team-definitions/activate/{activation_id}")
async def internal_get_team_definition_activation(
    activation_id: str,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_activation_store

    try:
        activation = team_activation_store.get(activation_id)
    except team_activation_store.TeamActivationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if activation is None:
        raise HTTPException(status_code=404, detail="activation_id does not exist")
    return {"success": True, "activation": activation}


@router.post("/api/internal/team-definitions/finalize")
async def internal_finalize_team_definition_member(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import team_store

    team_id = str(body.get("team_instance_id") or "").strip()
    member_id = str(body.get("member_id") or "").strip()
    if not team_id:
        raise HTTPException(status_code=400, detail="team_instance_id is required")
    if not member_id:
        raise HTTPException(status_code=400, detail="member_id is required")
    if team_store.get(team_id) is None:
        raise HTTPException(status_code=404, detail="team_instance_id does not exist")
    spec = team_store.pop_pending_member(team_id, member_id)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail="member_id is not a pending finalize_with worker for this team",
        )
    default_cwd = str(body.get("cwd") or spec.get("cwd") or "").strip()
    bare_config = bool(body.get("bare_config") is True or spec.get("bare_config") is True)
    result = await _configured(_provision_workers, "provision_workers")(
        {
            "cwd": default_cwd,
            "team_instance_id": team_id,
            "bare_config": bare_config,
            "workers": [spec],
        }
    )
    return {"success": True, "workers": result.get("workers") or []}


async def _rollback_team_activation(
    team_id: str,
    created_worker_session_ids: list[str],
) -> list[str]:
    import team_store

    rolled_back: list[str] = []
    for sid in created_worker_session_ids:
        try:
            if await _configured(_delete_session_tree, "delete_session_tree")(sid):
                rolled_back.append(sid)
        except Exception:
            logger.exception(
                "failed to roll back worker session %s during team activation failure", sid,
            )
    if team_id:
        try:
            team_store.delete(team_id)
        except Exception:
            logger.exception(
                "failed to delete team %s during team activation rollback", team_id,
            )
    return rolled_back


async def _run_team_definition_activation(
    activation_id: str,
    *,
    root_session_id: str,
    plan: dict,
    default_cwd: str,
    bare_config: bool,
) -> None:
    import team_activation_store
    import team_store

    team_id = str(plan.get("team_instance_id") or "").strip()
    created_worker_session_ids: list[str] = []
    try:
        profile = str(plan.get("profile") or "").strip()
        source_id = str(plan.get("source_id") or "").strip()
        team_activation_store.append_step(activation_id, "create runtime team")
        team = team_store.create(
            root_session_id=root_session_id,
            definition_ref=source_id,
            profile=profile,
            team_id=team_id,
        )
        manager = plan.get("manager") if isinstance(plan.get("manager"), dict) else {}
        team_activation_store.append_step(activation_id, "register manager")
        team_store.upsert_member(
            team_id,
            member_id="manager",
            member_type="manager",
            agent_session_id=root_session_id,
            role="manager",
            description=str(manager.get("id") or "manager"),
            cwd=str(manager.get("cwd") or default_cwd),
            provider_id=str(manager.get("provider_id") or ""),
            model=str(manager.get("model") or ""),
            reasoning_effort=str(manager.get("reasoning_effort") or ""),
            runner=str(manager.get("runner") or ""),
            run_mode=str(manager.get("run_mode") or "direct"),
        )
        finalize_specs = plan.get("finalize_with")
        if isinstance(finalize_specs, list) and finalize_specs:
            team_activation_store.append_step(activation_id, "register deferred workers")
            team_store.set_pending_members(team_id, finalize_specs)
        workers = plan.get("activate")
        if not isinstance(workers, list):
            raise ValueError("plan.activate must be a list")
        for worker in workers:
            if not isinstance(worker, dict):
                raise ValueError("plan.activate items must be objects")
            team_activation_store.append_step(
                activation_id,
                f"provision {worker.get('member_id') or worker.get('role_key') or 'worker'}",
                status="running",
            )
            result = await _configured(_provision_workers, "provision_workers")(
                {
                    "cwd": default_cwd,
                    "team_instance_id": team_id,
                    "bare_config": bare_config,
                    "workers": [worker],
                }
            )
            for provisioned in result.get("workers") or []:
                if provisioned.get("created") and provisioned.get("agent_session_id"):
                    created_worker_session_ids.append(str(provisioned["agent_session_id"]))
            team_activation_store.append_step(
                activation_id,
                f"provisioned {worker.get('member_id') or worker.get('role_key') or 'worker'}",
                data=result,
            )
        team_activation_store.complete(
            activation_id,
            {"team": team_store.get(team_id) or team, "plan": plan},
        )
    except Exception as exc:
        rolled_back = await _rollback_team_activation(team_id, created_worker_session_ids)
        team_activation_store.fail(activation_id, str(exc), rolled_back_worker_ids=rolled_back)


@router.post("/api/internal/create-worker")
async def internal_create_worker(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(extension_store.extension_id_for_role('team-orchestration'))
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    app_session_id = str(body.get("app_session_id") or "")
    folder_id, tag_ids = await _initial_session_organization_from_body(body)
    harness_profile_id = _harness_profile_selection(body)
    # Only the model pin applies here: a fresh worker always inherits the
    # calling manager's provider (its CLI session branches off the manager's).
    profile_selectors = harness_profile_resolver.merge_selector_defaults(
        {"model": str(body.get("model") or "").strip() or None},
        harness_profile_id,
    )
    requested_model = str(profile_selectors["model"] or "").strip()
    if requested_model:
        caller = await _session_lite(app_session_id)
        provider_id = str((caller or {}).get("provider_id") or "").strip() or None
        _validate_provider_model(provider_id, requested_model)
    result = await _coordinator().create_worker_for_session(
        app_session_id=app_session_id,
        worker_description=str(body.get("worker_description") or ""),
        justification=str(body.get("justification") or ""),
        proposed_orchestration_mode=str(body.get("orchestration_mode") or ""),
        model=requested_model,
        cwd=str(body.get("cwd") or ""),
        client_request_id=str(body.get("client_request_id") or "") or None,
        node_id=str(body.get("node_id") or "") or None,
        harness_profile_id=harness_profile_id,
    )
    if result.get("success") and result.get("worker_session_id"):
        await _apply_initial_session_organization(
            str(result["worker_session_id"]), folder_id, tag_ids,
        )
    return result


@router.post("/api/internal/create-session")
async def internal_create_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Low-level session creation exposed to agents: mint a standalone BC
    session (NOT a team worker — no roster registration, no approval, no init
    turn). Pairs with delegate/mssg/ask to spin up a fresh session to hand
    work off to. For a session that joins the team's worker roster, use
    /api/internal/create-worker instead."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    name = str(body.get("name") or "").strip()
    cwd = str(body.get("cwd") or "").strip()
    if not name or not cwd:
        raise HTTPException(status_code=400, detail="name and cwd are required")
    folder_id, tag_ids = await _initial_session_organization_from_body(body)
    mode = str(body.get("orchestration_mode") or "native").strip() or "native"
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise HTTPException(status_code=400, detail="orchestration_mode must be 'team' or 'native'")
    if mode == "team":
        internal_guards.require_role_internal('team-orchestration')
    bare_config = body.get("bare_config", False)
    if not isinstance(bare_config, bool):
        raise HTTPException(status_code=400, detail="bare_config must be a boolean")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    requested_profile_id = body.get("runtime_profile_id")
    if requested_profile_id is not None:
        if not isinstance(requested_profile_id, str) or not requested_profile_id.strip():
            raise HTTPException(
                status_code=400, detail="runtime_profile_id must be a string"
            )
        runtime_profile_record = await asyncio.to_thread(
            config_store.get_runtime_profile, requested_profile_id
        )
        if runtime_profile_record is None or runtime_profile_record.get("deleted_at"):
            raise HTTPException(
                status_code=400, detail="runtime profile is unknown or deleted"
            )
        if (
            str(body.get("provider_id") or "").strip()
            and body["provider_id"] != runtime_profile_record["provider_id"]
        ):
            raise HTTPException(
                status_code=400,
                detail="provider_id conflicts with runtime_profile_id",
            )
        if (
            str(body.get("runner") or "").strip()
            and body["runner"] != runtime_profile_record["runner"]
        ):
            raise HTTPException(
                status_code=400, detail="runner conflicts with runtime_profile_id"
            )
        body = {
            **body,
            "provider_id": runtime_profile_record["provider_id"],
            "runner": runtime_profile_record["runner"],
        }
    harness_profile_id = _harness_profile_selection(body)
    # Profile provider/model/effort pins fill in whatever the caller omitted,
    # outranking sender-session inheritance but never an explicit body value.
    profile_selectors = harness_profile_resolver.merge_selector_defaults(
        {
            "provider_id": str(body.get("provider_id") or "").strip() or None,
            "model": str(body.get("model") or "").strip() or None,
            "reasoning_effort": body.get("reasoning_effort"),
        },
        harness_profile_id,
    )
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    sender_session = await _session_lite(sender_session_id) if sender_session_id else None
    if sender_session_id and not sender_session:
        raise HTTPException(status_code=400, detail="sender_session_id does not exist")
    requested_provider_id = await _resolve_provider_id_ref(
        str(profile_selectors["provider_id"] or "").strip(),
    )
    provider_id = requested_provider_id
    if not provider_id and sender_session:
        provider_id = str(sender_session.get("provider_id") or "").strip()
    provider_id = provider_id or None
    if provider_id and not await asyncio.to_thread(config_store.get_provider, provider_id):
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    runner_input = body.get("runner")
    if not str(runner_input or "").strip() and sender_session:
        if provider_id == sender_session.get("provider_id"):
            runner_input = sender_session.get("runner")
    runner = _provider_runner(provider_id, runner_input)
    requested_model = str(profile_selectors["model"] or "").strip()
    model = ""
    if requested_model:
        model = requested_model
    elif requested_provider_id and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        model = await asyncio.to_thread(_required_model_from_body_or_provider, {}, provider)
    elif sender_session:
        model = str(sender_session.get("model") or "").strip()
    if not model and provider_id:
        provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
        model = await asyncio.to_thread(_required_model_from_body_or_provider, {}, provider)
    if requested_model or requested_provider_id:
        await asyncio.to_thread(_validate_provider_model, provider_id, model)
    requested_effort = profile_selectors["reasoning_effort"]
    reasoning_effort: object
    if requested_effort is not None and str(requested_effort).strip():
        reasoning_effort = await asyncio.to_thread(
            _provider_reasoning_effort,
            provider_id,
            _api_reasoning_effort(requested_effort),
            runner,
            model,
        )
    else:
        reasoning_effort = await asyncio.to_thread(
            _inherited_reasoning_effort,
            provider_id,
            str((sender_session or {}).get("reasoning_effort") or "").strip(),
            runner,
            model,
        )
    node_id = str(body.get("node_id") or "").strip() or "primary"
    extra_mcp_servers = _api_extra_mcp_servers(body.get("mcp_servers"))
    if not model:
        model = await asyncio.to_thread(config_store.default_session_model)
    sess = await asyncio.to_thread(
        lambda: session_manager.create(
            name=name,
            cwd=cwd,
            orchestration_mode=mode,
            model=model,
            provider_id=provider_id,
            runner=runner,
            reasoning_effort=reasoning_effort,
            node_id=node_id,
            source="cli",
            bare_config=bare_config,
            capability_contexts=capability_contexts,
            extra_mcp_servers=extra_mcp_servers,
            harness_profile_id=harness_profile_id,
        )
    )
    await _apply_initial_session_organization(sess["id"], folder_id, tag_ids)
    if sender_session_id:
        await _coordinator().emit_session_created_panel(
            sender_session_id=sender_session_id,
            target_session=sess,
        )
    _ext_id = internal_guards.internal_authority_extension_id() or ""
    if _ext_id and extension_store.is_extension_active(_ext_id):
        import extension_session_ownership
        extension_session_ownership.claim(sess["id"], _ext_id)
    return {
        "success": True,
        "session_id": sess["id"],
        "name": sess.get("name") or name,
        "cwd": sess.get("cwd") or cwd,
        "orchestration_mode": mode,
        "node_id": node_id,
        "provider_id": sess.get("provider_id"),
        "model": sess.get("model"),
        "runner": sess.get("runner"),
        "reasoning_effort": sess.get("reasoning_effort"),
        "bare_config": sess.get("bare_config"),
        "capability_contexts": sess.get("capability_contexts") or [],
        "harness_profile_id": sess.get("harness_profile_id") or "",
    }


@router.post("/api/internal/create-sub-session")
async def internal_create_sub_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    parent_session_id = str(body.get("sender_session_id") or "").strip()
    description = str(body.get("description") or "").strip()
    if not parent_session_id:
        raise HTTPException(status_code=400, detail="sender_session_id is required")
    folder_id, tag_ids = await _initial_session_organization_from_body(body)
    parent = await _session_lite(parent_session_id)
    if not parent:
        raise HTTPException(status_code=400, detail="sender_session_id does not exist")

    harness_profile_id = _harness_profile_selection(body)
    # Profile provider/model/effort pins fill in whatever the caller omitted,
    # outranking parent-session inheritance but never an explicit body value.
    profile_selectors = harness_profile_resolver.merge_selector_defaults(
        {
            "provider_id": str(body.get("provider_id") or "").strip() or None,
            "model": str(body.get("model") or "").strip() or None,
            "reasoning_effort": body.get("reasoning_effort"),
        },
        harness_profile_id,
    )
    requested_provider_id = await _resolve_provider_id_ref(
        str(profile_selectors["provider_id"] or "").strip(),
    )
    provider_id = requested_provider_id or str(parent.get("provider_id") or "").strip()
    provider_id = provider_id or None
    if provider_id and not await asyncio.to_thread(config_store.get_provider, provider_id):
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    runner_input = body.get("runner")
    if not str(runner_input or "").strip() and provider_id == parent.get("provider_id"):
        runner_input = parent.get("runner")
    runner = _provider_runner(provider_id, runner_input)
    requested_model = str(profile_selectors["model"] or "").strip()
    model = requested_model
    if not model and requested_provider_id and provider_id:
        model = await asyncio.to_thread(profile_prefill_model, provider_id, runner)
        if not model:
            provider = await asyncio.to_thread(config_store.get_provider, provider_id) or {}
            name = provider.get("name") or provider_id
            raise HTTPException(status_code=400, detail=f"{name} has no default model configured")
    if not model:
        model = str(parent.get("model") or "").strip()
    if not model and provider_id:
        model = await asyncio.to_thread(profile_prefill_model, provider_id, runner)
    if requested_model or requested_provider_id:
        await asyncio.to_thread(_validate_provider_model, provider_id, model)
    requested_effort = profile_selectors["reasoning_effort"]
    reasoning_effort: object
    if requested_effort is not None and str(requested_effort).strip():
        reasoning_effort = await asyncio.to_thread(
            _provider_reasoning_effort,
            provider_id,
            _api_reasoning_effort(requested_effort),
            runner,
            model,
        )
    else:
        reasoning_effort = await asyncio.to_thread(
            _inherited_reasoning_effort,
            provider_id,
            str(parent.get("reasoning_effort") or "").strip(),
            runner,
            model,
        )
    cwd = str(body.get("cwd") or "").strip() or str(parent.get("cwd") or "").strip()
    node_id = str(body.get("node_id") or "").strip() or str(parent.get("node_id") or "primary")
    disallowed_tools = _api_disallowed_tools(body.get("disallowed_tools"))
    disabled_builtin_extensions = _api_disabled_builtin_extensions(body.get("disabled_builtin_extensions"))
    extra_mcp_servers = _api_extra_mcp_servers(body.get("mcp_servers"))
    name = description or "sub-session"

    try:
        sub = await asyncio.to_thread(
            lambda: session_manager.create_sub_session(
                parent_session_id=parent_session_id,
                name=name,
                model=model,
                provider_id=provider_id,
                runner=runner,
                reasoning_effort=reasoning_effort,
                cwd=cwd,
                node_id=node_id,
                disallowed_tools=disallowed_tools,
                disabled_builtin_extensions=disabled_builtin_extensions,
                extra_mcp_servers=extra_mcp_servers,
                harness_profile_id=harness_profile_id,
            )
        )
        await _apply_initial_session_organization(sub["id"], folder_id, tag_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _coordinator().emit_session_created_panel(
        sender_session_id=parent_session_id,
        target_session=sub,
    )
    _ext_id = internal_guards.internal_authority_extension_id() or ""
    if _ext_id and extension_store.is_extension_active(_ext_id):
        import extension_session_ownership
        extension_session_ownership.claim(sub["id"], _ext_id)
    return {
        "success": True,
        "target_session_id": sub["id"],
        "name": sub.get("name") or name,
        "cwd": sub.get("cwd") or cwd,
        "orchestration_mode": sub.get("orchestration_mode") or "native",
        "node_id": sub.get("node_id") or node_id,
        "provider_id": sub.get("provider_id"),
        "model": sub.get("model"),
        "runner": sub.get("runner"),
        "reasoning_effort": sub.get("reasoning_effort"),
        "disallowed_tools": sub.get("disallowed_tools") or [],
        "disabled_builtin_extensions": sub.get("disabled_builtin_extensions") or [],
        "extra_mcp_servers": sub.get("extra_mcp_servers") or [],
        "harness_profile_id": sub.get("harness_profile_id") or "",
    }
