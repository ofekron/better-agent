"""Composition root: the one place routers are configured and mounted.

`wire` receives the process-wide collaborators the routers need as
arguments, so no router — and not this module — imports `main`.
Adding a router is a change here, not a change to main.py.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI


def wire(
    app: FastAPI,
    *,
    coordinator: Any,
    git_sha: str,
    session_lite: Callable[[str], Awaitable[Optional[dict]]],
    publish_worker_fanout: Callable[..., Awaitable[Any]],
    invalidate_session_list_cache: Callable[[], None],
    notify_projects_changed: Callable[[], Awaitable[None]],
    require_builtin_extension: Callable[[str], None],
    resolve_selector_updates: Callable[[str, dict], Awaitable[dict]],
    record_model_switched_event: Callable[[str, dict, dict, dict], None],
    record_last_model: Callable[[str, str], Awaitable[None]],
    record_last_reasoning_effort: Callable[[str, str], Awaitable[None]],
    broadcast_session_organization_changed: Callable[..., Awaitable[None]],
    delete_session_tree: Callable[[str], Awaitable[bool]],
    extension_daemons_ready: Callable[[], bool],
    cold_recovery_integration_pending: Callable[[], bool],
    frontend_dist: Callable[[], Any],
) -> None:
    """Configure and mount every router on `app`.

    Mount order is preserved from the original inline block: routers
    are mounted before the routes main.py still declares itself, and
    middleware registration continues to happen at main's own call
    site after this returns.
    """
    import auth_routes

    app.include_router(auth_routes.router)

    import misc_api
    misc_api.configure(git_sha=git_sha)
    app.include_router(misc_api.router)

    import providers_api
    providers_api.configure(coordinator.broadcast_global)
    app.include_router(providers_api.router)
    import runtime_profiles_api
    runtime_profiles_api.configure(coordinator.broadcast_global)
    app.include_router(runtime_profiles_api.router)

    import harness_profiles_api
    harness_profiles_api.configure(coordinator.broadcast_global)
    app.include_router(harness_profiles_api.router)

    import internal_extension_api
    internal_extension_api.configure(
        broadcast_global=coordinator.broadcast_global,
        broadcast_session=coordinator.broadcast_session,
        request_principal_async=coordinator.request_principal_async,
        require_builtin_extension=require_builtin_extension,
        resolve_selector_updates=resolve_selector_updates,
        record_model_switched_event=record_model_switched_event,
        record_last_model=record_last_model,
        record_last_reasoning_effort=record_last_reasoning_effort,
        request_immediate_continuation=coordinator.turn_manager.request_immediate_continuation,
        broadcast_session_organization_changed=broadcast_session_organization_changed,
    )
    app.include_router(internal_extension_api.router)

    import worker_pools_api
    import workers_api

    import internal_orchestration_api
    internal_orchestration_api.configure(
        coordinator=coordinator,
        delete_session_tree=delete_session_tree,
        provision_workers=lambda body: workers_api.provision_workers_from_body(body),
    )
    app.include_router(internal_orchestration_api.router)

    # The worker-pool lookups are passed as lambdas rather than as the
    # function objects so patching either side's attribute still reaches
    # the handler.
    import internal_messaging_api
    internal_messaging_api.configure(
        coordinator=coordinator,
        pick_pool_worker_for_sender=lambda *a: worker_pools_api._pick_pool_worker_for_sender(*a),
        find_worker_by_agent_session_id=lambda sid: worker_pools_api._find_worker_by_agent_session_id(sid),
        enqueue_worker_pool_message=lambda **kw: worker_pools_api._enqueue_worker_pool_message(**kw),
        pool_ask_status=lambda ask_id: worker_pools_api._pool_ask_status(ask_id),
        ensure_worker_pool_processor=lambda tag: worker_pools_api._ensure_worker_pool_processor(tag),
        wait_for_pool_ask_result=lambda ask_id, queued: worker_pools_api._wait_for_pool_ask_result(
            ask_id, queued
        ),
    )
    app.include_router(internal_messaging_api.router)

    import internal_session_state_api
    internal_session_state_api.configure(coordinator=coordinator)
    app.include_router(internal_session_state_api.router)

    import user_input_api
    user_input_api.configure(coordinator=coordinator)
    app.include_router(user_input_api.router)

    import schedules_api
    schedules_api.configure(coordinator=coordinator)
    app.include_router(schedules_api.router)

    import tasks_api
    tasks_api.configure(coordinator=coordinator)
    app.include_router(tasks_api.router)

    import session_bridge_api
    session_bridge_api.configure(coordinator=coordinator)
    app.include_router(session_bridge_api.router)

    import coordination_api
    app.include_router(coordination_api.router)

    import capability_api
    app.include_router(capability_api.router)

    # Imported for its registration side effects; it mounts nothing.
    import runtime_operation_api  # noqa: F401

    import extension_api
    app.include_router(extension_api.router)

    import extension_storage_api
    app.include_router(extension_storage_api.router)

    import marketplace_bridge_api
    marketplace_bridge_api.configure(
        lambda: coordinator.internal_token,
        lambda revision: coordinator.broadcast_global(
            "marketplace_bridge_changed",
            {"revision": revision},
        ),
        lambda: extension_api._broadcast_extension_changed(
            *extension_api.EXTENSION_CATALOG_TOPICS
        ),
    )
    app.include_router(marketplace_bridge_api.router)

    import credential_clone_api
    credential_clone_api.configure(coordinator.verify_internal_token)
    app.include_router(credential_clone_api.router)

    import testape_api
    app.include_router(testape_api.router)

    import git_api
    app.include_router(git_api.router)

    import internal_guards
    internal_guards.configure(lambda: coordinator.bound_request_principal())

    import assistant_ui_api
    app.include_router(assistant_ui_api.router)

    import ask_ui_api
    app.include_router(ask_ui_api.router)

    import machine_nodes_api
    app.include_router(machine_nodes_api.router)

    import memory_api
    app.include_router(memory_api.router)

    import requirements_api
    app.include_router(requirements_api.router)

    import prompt_engineer_api
    prompt_engineer_api.configure(
        coordinator.submit_prompt_async,
        coordinator.cancel_session,
        session_lite,
        publish_worker_fanout,
    )
    app.include_router(prompt_engineer_api.router)

    import pending_approvals_api
    pending_approvals_api.configure(
        lambda: coordinator.approval_waiters.keys(),
        coordinator._resolve_approval,
    )
    app.include_router(pending_approvals_api.router)

    import user_prefs_api
    user_prefs_api.configure(coordinator.broadcast_global, invalidate_session_list_cache)
    app.include_router(user_prefs_api.router)

    from session_manager import manager as session_manager

    import project_aggregate_projection
    project_aggregate_projection.bind(coordinator.broadcast_global)

    import projects_api
    projects_api.configure(
        notify_projects_changed,
        coordinator.broadcast_global,
        project_aggregate_projection.snapshot,
    )
    app.include_router(projects_api.router)

    import session_list_cache
    session_list_cache.configure(session_manager.projected_state_version)

    import session_listing_api
    session_listing_api.configure(
        coordinator.broadcast_global,
        session_manager.projected_state_snapshot,
        delete_session_tree,
    )
    app.include_router(session_listing_api.router)

    import session_status_projection
    session_status_projection.bind(
        coordinator.broadcast_global,
        session_manager.projected_state_snapshot,
    )

    # Mounted after session_listing_api so its literal /api/sessions/* paths
    # keep matching ahead of this router's /api/sessions/{session_id}.
    import session_detail_api
    session_detail_api.configure(
        coordinator=coordinator,
        delete_session_tree=delete_session_tree,
        notify_projects_changed=notify_projects_changed,
        record_model_switched_event=record_model_switched_event,
    )
    app.include_router(session_detail_api.router)

    import session_panels_api
    app.include_router(session_panels_api.router)

    import file_editor_api
    file_editor_api.configure(coordinator=coordinator)
    app.include_router(file_editor_api.router)

    import offline_actions_api
    offline_actions_api.configure(coordinator=coordinator)
    app.include_router(offline_actions_api.router)

    import mobile_desktop_api
    mobile_desktop_api.configure(coordinator.broadcast_global)

    import ops_api
    ops_api.configure(
        coordinator=coordinator,
        extension_daemons_ready=extension_daemons_ready,
        cold_recovery_integration_pending=cold_recovery_integration_pending,
        require_mobile_enabled=lambda: mobile_desktop_api.require_mobile_enabled(),
        frontend_dist=frontend_dist,
    )
    app.include_router(ops_api.router)

    import hooks_push_api
    hooks_push_api.configure(coordinator.request_principal_async)
    app.include_router(hooks_push_api.router)

    import file_api
    app.include_router(file_api.router)

    # Imported for their registration side effects; they mount nothing.
    import file_delivery  # noqa: F401
    import native_index_manager  # noqa: F401

    import native_index_api
    app.include_router(native_index_api.router)

    # Mounted last, preserving the precedence the routes had when main.py
    # declared them after this call returned.
    app.include_router(mobile_desktop_api.router)

    worker_pools_api.configure(coordinator=coordinator)
    app.include_router(worker_pools_api.router)

    workers_api.configure(coordinator=coordinator)
    app.include_router(workers_api.router)

    import credential_ui_api
    credential_ui_api.configure(coordinator=coordinator)
    app.include_router(credential_ui_api.router)

    import tool_approvals_api
    tool_approvals_api.configure(coordinator=coordinator)
    app.include_router(tool_approvals_api.router)
