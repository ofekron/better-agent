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
    session_lite: Callable[[str], Awaitable[Optional[dict]]],
    publish_worker_fanout: Callable[..., Awaitable[Any]],
) -> None:
    """Configure and mount every router on `app`.

    Mount order is preserved from the original inline block: routers
    are mounted before the routes main.py still declares itself, and
    middleware registration continues to happen at main's own call
    site after this returns.
    """
    import auth_routes

    app.include_router(auth_routes.router)

    import providers_api
    providers_api.configure(coordinator.broadcast_global)
    app.include_router(providers_api.router)

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

    import file_api
    app.include_router(file_api.router)

    # Imported for their registration side effects; they mount nothing.
    import file_delivery  # noqa: F401
    import native_index_manager  # noqa: F401

    import native_index_api
    app.include_router(native_index_api.router)
