"""Worker registry CRUD and provisioning: the team-orchestration routes
that list workers for a cwd, create one (running the init turn that mints
its agent session id), idempotently provision a whole worker set, adopt an
existing session as a worker, and unregister or reset its forks.

Pool selection and the pool queue live in `worker_pools_api`; this module
consumes its peer-context helpers when building a worker's provisioning
prompt. The coordinator is injected by the composition root (see
`configure`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import extension_store
import harness_profile_resolver
import internal_guards
import session_store
from capability_contexts import normalize_capability_contexts
from harness_profiles_api import harness_profile_selection as _harness_profile_selection
from i18n import t
from prompt_templates import render_prompt
from provider_validation import (
    api_disabled_builtin_extensions as _api_disabled_builtin_extensions,
    api_disallowed_tools as _api_disallowed_tools,
    api_optional_provision_prompt as _api_optional_provision_prompt,
    api_reasoning_effort as _api_reasoning_effort,
    provider_for_required_model as _provider_for_required_model,
    provider_reasoning_effort as _provider_reasoning_effort,
    provider_runner as _provider_runner,
    required_model_from_body_or_provider as _required_model_from_body_or_provider,
)
from session_detail_api import _resolve_session_node_id
from session_helpers import session_lite as _session_lite
from session_listing_api import (
    apply_initial_session_organization as _apply_initial_session_organization,
    initial_session_organization_from_body as _initial_session_organization_from_body,
)
from session_manager import manager as session_manager
from worker_pools_api import (
    _pool_worker_context_for_prompt,
    _pool_worker_specs_for_prompt,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="workers API is not configured")
    return _coordinator_ref


def _internal_list_workers_for_cwd_sync(cwd: str) -> dict:
    import team_orchestration_read

    return team_orchestration_read.list_workers_for_cwd(cwd)


@router.post("/api/internal/workers/list")
async def internal_list_workers_for_cwd(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    cwd = str((body or {}).get("cwd") or "")
    return await asyncio.to_thread(_internal_list_workers_for_cwd_sync, cwd)


@router.post("/api/internal/workers/create")
async def internal_create_worker(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    """Create a new Better Agent session, run a tiny init turn to mint its
    agent_sid, and register it as a worker for the given cwd.

    Body: {cwd, description, orchestration_mode, model}.

    Blocks until the init turn completes (a few seconds). Returns the
    new worker record.
    """
    return await _create_worker_from_body(body or {})


@router.post("/api/internal/workers/provision-ui")
async def internal_provision_workers_ui(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    """Idempotently create/reuse worker sessions for a cwd.

    Body: {cwd, workers:[{role_key, description, orchestration_mode, model}]}.
    Idempotency is by role_key when present, otherwise description. Existing
    workers are matched by the stable session name `worker:<key>`.
    """
    return await provision_workers_from_body(body or {})


@router.post("/api/internal/workers/provision")
async def internal_provision_workers(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('team-orchestration')
    )
    """Internal-token variant for first-party local orchestrators."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    return await provision_workers_from_body(body or {})


# Serializes find-then-create per (name, cwd) so two concurrent provisions
# of the same singleton worker can't both pass the find-None check and
# create duplicates. Single uvicorn worker (assumed everywhere else too)
# means asyncio.Lock is sufficient — the event loop can't context-switch
# inside the synchronous setdefault that creates a new lock.
_PROVISION_LOCKS: dict[str, asyncio.Lock] = {}


def _provision_lock(name: str, cwd: str) -> asyncio.Lock:
    return _PROVISION_LOCKS.setdefault(f"{name}\0{cwd}", asyncio.Lock())


def _worker_provision_prompt_for_body(*, body: dict, bc_session_id: str, description: str) -> str:
    base = _api_optional_provision_prompt(body.get("provision_prompt"))
    if base is None:
        base = _profile_provisioning_prompt_for_body(body=body, bc_session_id=bc_session_id)
    if base is None:
        base = render_prompt("provisioning/worker_prep.md", {"description": description})
    pool_context = _pool_worker_context_for_prompt(
        body=body,
        bc_session_id=bc_session_id,
        description=description,
    )
    if not pool_context:
        return base
    return f"{base}\n\n{pool_context}"


def _profile_provisioning_prompt_for_body(*, body: dict, bc_session_id: str) -> str | None:
    profile_id = str((body or {}).get("harness_profile_id") or "").strip()
    if not profile_id and bc_session_id:
        session = session_manager.get_lite(bc_session_id) or {}
        profile_id = str(session.get("harness_profile_id") or "").strip()
    if not profile_id:
        return None
    try:
        resolved = harness_profile_resolver.resolve_profile(profile_id)
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    prompt = str(resolved.get("provisioning_prompt") or "").strip()
    return prompt or None


def _existing_worker_is_initialized(worker: dict | None) -> bool:
    if not worker:
        return False
    session = session_manager.get_lite(str(worker.get("agent_session_id") or ""))
    if session:
        return bool(session.get("agent_session_id"))
    return bool(worker.get("agent_sid"))


def _remove_stale_uninitialized_worker(worker: dict, cwd: str) -> None:
    from stores import worker_store as _ws

    sid = str(worker.get("agent_session_id") or "").strip()
    if not sid:
        return
    _ws.remove_worker(worker.get("registry_cwd") or worker.get("cwd") or cwd, sid)
    session_manager.delete(sid)


def _provision_parent_session_id(body: dict, spec: dict) -> str:
    parent_id = str(
        spec.get("parent_session_id")
        or body.get("parent_session_id")
        or body.get("app_session_id")
        or ""
    ).strip()
    if parent_id:
        return parent_id
    team_id = str((spec.get("team_instance_id") or body.get("team_instance_id") or "")).strip()
    if not team_id:
        return ""
    import team_store

    team = team_store.get(team_id)
    return str((team or {}).get("root_session_id") or "").strip()


def _worker_working_mode_meta(parent_session_id: str, body: dict, spec: dict, key: str) -> dict:
    from stores import worker_store as _ws

    meta = {
        "parent_session_id": parent_session_id,
        "role_key": key,
    }
    team_id = str((spec.get("team_instance_id") or body.get("team_instance_id") or "")).strip()
    if team_id:
        meta["team_instance_id"] = team_id
    tags = _ws.normalize_tags(spec.get("tags"))
    if tags:
        meta["pool_tags"] = tags
    return meta


def _mark_worker_under_parent(worker_session_id: str, parent_session_id: str, body: dict, spec: dict, key: str) -> None:
    worker_sid = str(worker_session_id or "").strip()
    parent_sid = str(parent_session_id or "").strip()
    if not worker_sid or not parent_sid or worker_sid == parent_sid:
        return
    if not session_manager.get_lite(parent_sid):
        raise HTTPException(status_code=400, detail="parent_session_id does not exist")
    import working_mode

    working_mode.mark_working_mode(
        worker_sid,
        mode="worker_pool",
        meta=_worker_working_mode_meta(parent_sid, body, spec, key),
    )


async def provision_workers_from_body(body: dict):
    import team_store
    from stores import worker_store as _ws

    cwd = str((body or {}).get("cwd") or "").strip()
    specs = (body or {}).get("workers") or []
    # Top-level default lets a first-party orchestrator (TestApe) provision a
    # whole worker set as bare in one call; a per-worker spec can override.
    body_bare = bool((body or {}).get("bare_config", False))
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    if not isinstance(specs, list):
        raise HTTPException(status_code=400, detail="workers must be a list")
    results = []
    created_any = False
    pool_worker_specs = _pool_worker_specs_for_prompt(specs, cwd)
    try:
        for raw in specs:
            spec = raw if isinstance(raw, dict) else {}
            key = str(spec.get("role_key") or spec.get("description") or "").strip()
            if not key:
                raise HTTPException(status_code=400, detail="worker role_key or description required")
            worker_cwd = str(spec.get("cwd") or cwd).strip()
            if not worker_cwd:
                raise HTTPException(status_code=400, detail=t("error.cwd_required"))
            name = f"worker:{key}"
            disallowed_tools = _api_disallowed_tools(spec.get("disallowed_tools"))
            has_disabled_extensions = "disabled_builtin_extensions" in spec
            disabled_extensions = (
                _api_disabled_builtin_extensions(spec.get("disabled_builtin_extensions"))
                if has_disabled_extensions
                else None
            )
            parent_session_id = _provision_parent_session_id(body, spec)
            async with _provision_lock(name, worker_cwd):
                existing = await asyncio.to_thread(_find_worker_by_session_name, worker_cwd, name)
                if existing:
                    replace_uninitialized = bool(
                        spec.get("replace_uninitialized")
                        or body.get("replace_uninitialized")
                    )
                    if replace_uninitialized and not _existing_worker_is_initialized(existing):
                        await asyncio.to_thread(
                            _remove_stale_uninitialized_worker,
                            existing,
                            worker_cwd,
                        )
                        existing = None
                if existing:
                    if disallowed_tools:
                        await asyncio.to_thread(
                            session_manager.set_disallowed_tools,
                            existing["agent_session_id"],
                            disallowed_tools,
                        )
                    if has_disabled_extensions:
                        await asyncio.to_thread(
                            session_manager.set_disabled_builtin_extensions,
                            existing["agent_session_id"],
                            disabled_extensions or [],
                        )
                    existing_cwd = existing.get("cwd") or existing.get("registry_cwd") or worker_cwd
                    requested_tags = spec.get("tags")
                    if requested_tags is not None:
                        existing_tags = _ws.normalize_tags(existing.get("tags"))
                        merged_tags = _ws.normalize_tags([*existing_tags, *requested_tags])
                        if merged_tags != existing_tags:
                            existing = await asyncio.to_thread(
                                _ws.upsert_worker,
                                agent_session_id=existing["agent_session_id"],
                                name=existing.get("name") or name,
                                cwd=existing_cwd,
                                orchestration_mode=(
                                    existing.get("orchestration_mode")
                                    or spec.get("orchestration_mode")
                                    or "native"
                                ),
                                agent_sid=existing.get("agent_sid"),
                                node_id=existing.get("node_id"),
                                role_key=existing.get("role_key") or key,
                                tags=merged_tags,
                            )
                    await asyncio.to_thread(
                        _mark_worker_under_parent,
                        existing["agent_session_id"],
                        parent_session_id,
                        body,
                        spec,
                        key,
                    )
                    result = {
                        **existing,
                        "created": False,
                        "role_key": key,
                        "registry_cwd": existing_cwd,
                        "parent_session_id": parent_session_id or None,
                    }
                    _register_provisioned_team_member(team_store, body, spec, result, key)
                    results.append(result)
                    continue
                # Harness profile is a creation-time property, like
                # provision_prompt: it shapes the worker's instructions, skills
                # and MCP servers at session birth and is ignored when an
                # existing named worker is reused.
                harness_profile_id = _harness_profile_selection(spec)
                profile_selectors = harness_profile_resolver.merge_selector_defaults(
                    {
                        "provider_id": spec.get("provider_id"),
                        "model": spec.get("model"),
                        "reasoning_effort": spec.get("reasoning_effort"),
                    },
                    harness_profile_id,
                )
                create_body = {
                    "cwd": worker_cwd,
                    "name": name,
                    "description": spec.get("description") or name,
                    "orchestration_mode": spec.get("orchestration_mode") or "native",
                    "model": profile_selectors["model"],
                    "provider_id": profile_selectors["provider_id"],
                    "reasoning_effort": profile_selectors["reasoning_effort"],
                    "runner": spec.get("runner"),
                    "harness_profile_id": harness_profile_id,
                    "node_id": spec.get("node_id"),
                    "role_key": key,
                    "tags": spec.get("tags"),
                    "bare_config": bool(spec.get("bare_config", body_bare)),
                    "disallowed_tools": disallowed_tools,
                    "disabled_builtin_extensions": disabled_extensions,
                    "provision_prompt": spec.get("provision_prompt"),
                    "capability_contexts": spec.get("capability_contexts"),
                    "pool_worker_specs": pool_worker_specs,
                }
                folder_id, tag_ids = await _initial_session_organization_from_body(spec)
                if create_body["bare_config"]:
                    created = await asyncio.to_thread(
                        _create_pending_worker_from_body,
                        create_body,
                    )
                else:
                    created = await _create_worker_from_body(create_body, broadcast=False)
                await _apply_initial_session_organization(
                    str(created["agent_session_id"]), folder_id, tag_ids,
                )
                created_any = True
                await asyncio.to_thread(
                    _mark_worker_under_parent,
                    created["agent_session_id"],
                    parent_session_id,
                    body,
                    spec,
                    key,
                )
                result = {
                    **created,
                    "created": True,
                    "role_key": key,
                    "registry_cwd": created.get("cwd") or worker_cwd,
                    "parent_session_id": parent_session_id or None,
                }
                _register_provisioned_team_member(team_store, body, spec, result, key)
                results.append(result)
    finally:
        if created_any:
            await _coordinator().broadcast_workers_changed(None)
    return {"workers": results}


def _register_provisioned_team_member(team_store_module, body: dict, spec: dict, result: dict, key: str) -> None:
    team_id = str((spec.get("team_instance_id") or body.get("team_instance_id") or "")).strip()
    if not team_id:
        return
    member_id = str(spec.get("member_id") or key).strip()
    team_store_module.upsert_member(
        team_id,
        member_id=member_id,
        member_type="worker",
        agent_session_id=result["agent_session_id"],
        role=str(spec.get("role") or key).strip(),
        description=str(spec.get("description") or result.get("name") or key).strip(),
        cwd=str(result.get("registry_cwd") or result.get("cwd") or body.get("cwd") or "").strip(),
        provider_id=str(spec.get("provider_id") or "").strip(),
        model=str(spec.get("model") or "").strip(),
        reasoning_effort=str(spec.get("reasoning_effort") or "").strip(),
        runner=str(spec.get("runner") or "").strip(),
        run_mode=str(spec.get("run_mode") or "").strip(),
        parent_member_id=str(spec.get("parent_member_id") or "").strip(),
        status="active",
    )


def _find_worker_by_session_name(cwd: str, name: str) -> dict | None:
    from stores import worker_store as _ws

    raw = _ws._read()
    for worker in raw.get("workers", []):
        worker_name = str(worker.get("name") or "").strip()
        agent_session_id = worker.get("agent_session_id")
        try:
            bc = session_manager.get_lite(agent_session_id)
        except session_store.SessionProviderNotConfiguredError:
            _ws.remove_worker(worker.get("cwd") or cwd, agent_session_id)
            continue
        worker_cwd = str((worker.get("cwd") or bc.get("cwd")) if bc else "").strip()
        if worker_name and worker_name == name and worker_cwd == str(cwd or "").strip():
            return {
                "agent_session_id": worker.get("agent_session_id"),
                "name": worker_name,
                "display_name": bc.get("name") if bc else worker_name,
                "role_key": worker.get("role_key"),
                "cwd": worker.get("cwd") or (bc.get("cwd") if bc else cwd),
                "registry_cwd": worker.get("cwd") or (bc.get("cwd") if bc else cwd),
                "orchestration_mode": worker.get("orchestration_mode") or (bc.get("orchestration_mode") if bc else None),
                "agent_sid": worker.get("agent_sid"),
                "initialized": bool(bc.get("agent_session_id")) if bc else bool(worker.get("agent_sid")),
                "diverged": False,
                "delegation_count": worker.get("delegation_count", 0),
            }
        if bc and bc.get("name") == name and worker_cwd == str(cwd or "").strip():
            return {
                "agent_session_id": bc["id"],
                "name": worker.get("name") or bc.get("name"),
                "display_name": bc.get("name"),
                "role_key": worker.get("role_key"),
                "cwd": worker.get("cwd") or bc.get("cwd"),
                "registry_cwd": worker.get("cwd") or bc.get("cwd"),
                "orchestration_mode": worker.get("orchestration_mode") or bc.get("orchestration_mode"),
                "agent_sid": worker.get("agent_sid"),
                "initialized": bool(bc.get("agent_session_id")),
                "diverged": False,
                "delegation_count": worker.get("delegation_count", 0),
            }
    return None


def _create_pending_worker_from_body(body: dict):
    from stores import worker_store as _ws

    cwd = body.get("cwd")
    description = body.get("description") or t("worker.default_name")
    session_name = body.get("name") or description
    mode = body.get("orchestration_mode") or "native"
    provider_id = body.get("provider_id")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    harness_profile_id = _harness_profile_selection(body)
    provider_record = _provider_for_required_model(provider_id)
    model = _required_model_from_body_or_provider(body, provider_record)
    runner = _provider_runner(provider_id, body.get("runner"))
    reasoning_effort = _provider_reasoning_effort(
        provider_id,
        _api_reasoning_effort(body.get("reasoning_effort")),
        runner,
        model,
    )
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise HTTPException(status_code=400, detail=t("error.orchestration_mode_must_be_manager_or_native"))
    node_id = _resolve_session_node_id(body)
    bc = session_manager.create(
        name=session_name,
        model=model,
        cwd=cwd,
        orchestration_mode=mode,
        provider_id=provider_id,
        runner=runner,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
        bare_config=True,
        capability_contexts=capability_contexts,
        disallowed_tools=body.get("disallowed_tools"),
        disabled_builtin_extensions=body.get("disabled_builtin_extensions"),
        harness_profile_id=harness_profile_id,
    )
    rec = _ws.upsert_worker(
        cwd=cwd,
        agent_session_id=bc["id"],
        orchestration_mode=mode,
        agent_sid=None,
        node_id=node_id,
        name=body.get("name"),
        role_key=body.get("role_key"),
        tags=body.get("tags"),
    )
    return {
        "agent_session_id": bc["id"],
        "name": body.get("name") or bc["name"],
        "display_name": bc["name"],
        "role_key": body.get("role_key"),
        "cwd": cwd,
        "registry_cwd": cwd,
        "orchestration_mode": mode,
        "agent_sid": None,
        "initialized": False,
        "diverged": False,
        "delegation_count": rec.get("delegation_count", 0),
        "tags": rec.get("tags") or [],
    }


async def _create_worker_from_body(body: dict, broadcast: bool = True):
    from stores import worker_store as _ws

    cwd = body.get("cwd")
    description = body.get("description") or t("worker.default_name")
    session_name = body.get("name") or description
    mode = body.get("orchestration_mode") or "native"
    provider_id = body.get("provider_id")
    try:
        capability_contexts = normalize_capability_contexts(body.get("capability_contexts"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    harness_profile_id = _harness_profile_selection(body)
    provider_record = await asyncio.to_thread(_provider_for_required_model, provider_id)
    model = _required_model_from_body_or_provider(body, provider_record)
    runner = _provider_runner(provider_id, body.get("runner"))
    reasoning_effort = await asyncio.to_thread(
        _provider_reasoning_effort,
        provider_id,
        _api_reasoning_effort(body.get("reasoning_effort")),
        runner,
        model,
    )
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise HTTPException(status_code=400, detail=t("error.orchestration_mode_must_be_manager_or_native"))
    node_id = _resolve_session_node_id(body)

    bc = await asyncio.to_thread(
        lambda: session_manager.create(
            name=session_name, model=model, cwd=cwd, orchestration_mode=mode,
            provider_id=provider_id,
            runner=runner,
            reasoning_effort=reasoning_effort,
            node_id=node_id, bare_config=bool(body.get("bare_config", False)),
            capability_contexts=capability_contexts,
            disallowed_tools=body.get("disallowed_tools"),
            disabled_builtin_extensions=body.get("disabled_builtin_extensions"),
            harness_profile_id=harness_profile_id,
        )
    )
    coordinator = _coordinator()
    cancel_event = asyncio.Event()
    coordinator.init_cancel_events[bc["id"]] = ("__rest_api__", cancel_event)
    try:
        init_sid = await coordinator._init_target_agent_session(
            bc_session=bc, model=model, cwd=cwd,
            description=description, cancel_event=cancel_event,
            provision_prompt=_worker_provision_prompt_for_body(
                body=body,
                bc_session_id=bc["id"],
                description=description,
            ),
        )
    except Exception as e:
        await asyncio.to_thread(session_manager.delete, bc["id"])
        raise HTTPException(status_code=500, detail=t("error.init_turn_failed", e=str(e)))
    finally:
        coordinator.init_cancel_events.pop(bc["id"], None)
    if not init_sid:
        await asyncio.to_thread(session_manager.delete, bc["id"])
        raise HTTPException(status_code=500, detail=t("error.init_turn_no_session_id"))

    rec = await asyncio.to_thread(
        _ws.upsert_worker,
        cwd=cwd,
        agent_session_id=bc["id"],
        orchestration_mode=mode,
        agent_sid=init_sid,
        node_id=node_id,
        name=body.get("name"),
        role_key=body.get("role_key"),
        tags=body.get("tags"),
    )
    if broadcast:
        await coordinator.broadcast_workers_changed(None)
    return {
        "agent_session_id": bc["id"],
        "name": body.get("name") or bc["name"],
        "display_name": bc["name"],
        "role_key": body.get("role_key"),
        "cwd": cwd,
        "registry_cwd": cwd,
        "orchestration_mode": mode,
        "agent_sid": init_sid,
        "initialized": True,
        "diverged": False,
        "delegation_count": rec.get("delegation_count", 0),
        "tags": rec.get("tags") or [],
    }


@router.post("/api/internal/workers/from-session")
async def internal_register_existing_session_as_worker(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    """Register an existing Better Agent session as a worker.

    Body: {cwd, agent_session_id}. If the session already has an
    agent_sid it is registered immediately. Otherwise a one-time init
    turn is run to mint the agent_sid before registration.
    """
    from stores import worker_store as _ws
    cwd = (body or {}).get("cwd")
    bc_sid = (body or {}).get("agent_session_id")
    if not bc_sid:
        raise HTTPException(status_code=400, detail="agent_session_id is required")
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_plus_session_id_required"))
    bc = await _session_lite(bc_sid)
    if not bc:
        raise HTTPException(status_code=404, detail=t("error.bc_session_not_found"))
    worker_cwd = str(bc.get("cwd") or cwd)
    mode = bc.get("orchestration_mode") or "native"
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        mode = "native"
    agent_sid = bc.get("agent_session_id")
    coordinator = _coordinator()

    # No prior turn — run the init turn to mint agent_sid.
    if not agent_sid:
        if bc_sid in coordinator.init_cancel_events:
            raise HTTPException(
                status_code=409,
                detail=t("error.session_already_initializing"),
            )
        cancel_event = asyncio.Event()
        coordinator.init_cancel_events[bc_sid] = ("__rest_api__", cancel_event)
        try:
            model = str(bc.get("model") or "").strip()
            if not model:
                raise HTTPException(status_code=400, detail="session has no model configured")
            init_sid = await coordinator._init_target_agent_session(
                bc_session=bc, model=model,
                cwd=worker_cwd, description=bc.get("name") or "", cancel_event=cancel_event,
                provision_prompt=_worker_provision_prompt_for_body(
                    body=body,
                    bc_session_id=bc_sid,
                    description=bc.get("name") or "",
                ),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=t("error.init_turn_failed", e=str(e)))
        finally:
            coordinator.init_cancel_events.pop(bc_sid, None)
        if not init_sid:
            raise HTTPException(
                status_code=500,
                detail=t("error.init_turn_no_session_id"),
            )
        agent_sid = init_sid

    rec = await asyncio.to_thread(
        _ws.upsert_worker,
        cwd=worker_cwd,
        agent_session_id=bc_sid,
        orchestration_mode=mode,
        agent_sid=agent_sid,
        # The worker runs wherever its Better Agent session lives — the session
        # record is the single source of truth for its node binding.
        node_id=bc.get("node_id") or "primary",
        tags=(body or {}).get("tags"),
    )
    await coordinator.broadcast_workers_changed(None)
    return {
        "agent_session_id": bc_sid,
        "name": bc.get("name"),
        "cwd": worker_cwd,
        "registry_cwd": worker_cwd,
        "orchestration_mode": mode,
        "agent_sid": agent_sid,
        "initialized": True,
        "diverged": False,
        "delegation_count": rec.get("delegation_count", 0),
        "tags": rec.get("tags") or [],
    }


@router.post("/api/internal/workers/unregister")
async def internal_unregister_worker(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    agent_session_id = str((body or {}).get("agent_session_id") or "").strip()
    cwd = str((body or {}).get("cwd") or "").strip()
    if not agent_session_id:
        raise HTTPException(status_code=400, detail="agent_session_id is required")
    if not cwd:
        raise HTTPException(status_code=400, detail=t("error.cwd_required"))
    """Unregister a worker. Does NOT delete the Better Agent session
    itself — only removes it from the worker registry and clears
    any per-pair forks pointing at it as worker. Also cancels an
    in-flight init turn for this Better Agent session if one is still running."""
    from stores import worker_store as _ws
    coordinator = _coordinator()
    init_entry = coordinator.init_cancel_events.get(agent_session_id)
    if init_entry:
        init_entry[1].set()
    removed = await asyncio.to_thread(_ws.remove_worker, cwd, agent_session_id)
    if removed:
        await coordinator.broadcast_workers_changed(None)
    return {"removed": removed}


@router.post("/api/internal/workers/reset-forks")
async def internal_reset_worker_forks(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')
    agent_session_id = str((body or {}).get("agent_session_id") or "").strip()
    if not agent_session_id:
        raise HTTPException(status_code=400, detail="agent_session_id is required")
    """Drop all per-pair forks pointing at `agent_session_id` as worker.

    Used by the Team Orchestration extension reset action when the worker BC
    session has diverged from the manager's view (user typed in it
    directly). Next delegation will re-fork from the current head.
    """
    from stores import worker_store as _ws
    cleared = await asyncio.to_thread(_ws.clear_forks_for_worker_everywhere, agent_session_id)
    for fbsid in cleared:
        try:
            await asyncio.to_thread(session_manager.delete, fbsid)
        except Exception:
            logger.exception("delete delegate-fork BC %s failed during invalidate", fbsid)
    if cleared:
        await _coordinator().broadcast_workers_changed(None)
    return {"forks_cleared": len(cleared)}
