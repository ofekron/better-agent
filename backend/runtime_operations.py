from __future__ import annotations

import asyncio
import inspect
import importlib.util
from pathlib import Path
import sys
import uuid
from typing import Any, Callable

from pydantic import BaseModel

import chat_store
import inbox_store
import operation_authority
import operation_catalog
import operation_execution
from better_agent_sdk.surfaces import request_model_for_callable

Register = Callable[..., None]


def register_operations(register: Register) -> None:
    import capabilities_mcp
    import communicate_mcp
    import open_config_panel_mcp
    import open_file_panel_mcp
    coordination_server = _load_bundled_server("coordination")

    _register(
        register,
        "runtime",
        "capabilities.list",
        capabilities_mcp.list_capabilities_response,
        _capability_handler("list"),
        _read_policy(),
    )
    for action, source in (
        ("load", capabilities_mcp.load_capability_response),
        ("release", capabilities_mcp.release_capability_response),
    ):
        _register(
            register,
            "runtime",
            f"capabilities.{action}",
            source,
            _capability_handler(action),
            _mutation_policy(),
        )

    sources = {
        "mssg": communicate_mcp.mssg_response,
        "stop_turn": communicate_mcp.stop_turn_response,
        "list_available_provider_models": communicate_mcp.available_provider_models_response,
        "chat": communicate_mcp.chat_response,
        "inbox": communicate_mcp.inbox_response,
        "read_inbox_history": communicate_mcp.read_inbox_history_response,
        "read_chat_history": communicate_mcp.read_chat_history_response,
        "create_chat": communicate_mcp.create_chat_response,
        "set_chat_sender_policy": communicate_mcp.set_chat_sender_policy_response,
        "delete_chat": communicate_mcp.delete_chat_response,
        "delegate_task": communicate_mcp.delegate_task_response,
        "create_session": communicate_mcp.create_session_response,
        "create_sub_session": communicate_mcp.create_sub_session_surface_response,
        "ask": communicate_mcp.ask_response,
        "create_worker": communicate_mcp.create_worker_response,
        "ensure_named_worker": communicate_mcp.ensure_named_worker_response,
    }
    reads = {
        "list_available_provider_models",
        "read_inbox_history",
        "read_chat_history",
    }
    durable_jobs = {
        "mssg": "mssg",
        "delegate_task": "delegate-task",
        "ask": "ask",
    }
    for name, source in sources.items():
        durable_operation = durable_jobs.get(name)
        _register(
            register,
            "runtime",
            f"communication.{name}",
            source,
            _communication_handler(name),
            (
                _read_policy()
                if name in reads
                else _mutation_policy(durable=durable_operation is not None)
            ),
            recovery_handler=(
                _mcp_job_recovery(durable_operation)
                if durable_operation is not None
                else None
            ),
        )

    ui_sources = {
        "open-file-panel": open_file_panel_mcp.open_file_panel_response,
        "request-user-input": open_file_panel_mcp.request_user_input_response,
        "request-user-approval": open_file_panel_mcp.request_user_approval_response,
        "start-file-discussion": open_file_panel_mcp.start_file_discussion_response,
        "open-config-panel": open_config_panel_mcp.open_config_panel_response,
    }
    for action, source in ui_sources.items():
        _register(
            register,
            "runtime",
            f"ui.{action}",
            source,
            _ui_handler(action),
            _control_policy(),
        )

    _register(
        register,
        "coordination",
        "lock-ops",
        coordination_server.lock_ops_response,
        _route_handler("/api/internal/coordination/lock-ops"),
        _control_policy(),
    )
    _register_provider_config_sync_tools(register)
    _register_marketplace_tools(register)
    _register_session_control_tools(register)
    _register_session_bridge_tools(register)


def _register(
    register: Register,
    capability: str,
    action: str,
    source: Callable[..., Any],
    handler: Callable[[BaseModel], Any],
    policy: operation_catalog.OperationPolicy,
    *,
    recovery_handler: operation_catalog.RecoveryHandler | None = None,
) -> None:
    register(
        capability,
        action,
        request_model_for_callable(
            operation_catalog.operation_key(capability, action),
            source,
        ),
        handler,
        policy=policy,
        recovery_handler=recovery_handler,
    )


def _register_provider_config_sync_tools(register: Register) -> None:
    from provider_config_sync_backend.mcp_server import create_server

    server = create_server()
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    if not isinstance(tools, dict):
        raise RuntimeError("Provider Config Sync tool registry is unavailable")
    read_prefixes = ("get_", "list_", "read_")
    for name, tool in sorted(tools.items()):
        is_read = str(name).startswith(read_prefixes)
        _register(
            register,
            "provider-config-sync-tools",
            str(name),
            tool.fn,
            _callable_handler(tool.fn),
            _read_policy() if is_read else _mutation_policy(),
        )


def _callable_handler(source: Callable[..., Any]):
    async def handler(payload: BaseModel) -> Any:
        result = source(**payload.model_dump(by_alias=True))
        return await result if inspect.isawaitable(result) else result

    return handler


def _register_marketplace_tools(register: Register) -> None:
    server = _load_bundled_server("marketplace")

    actions = {
        "search_extensions": "search",
        "list_installed_extensions": "list_installed",
        "get_installed_extension": "get_installed",
        "install_extension": "install",
        "set_extension_enabled": "set_enabled",
        "uninstall_extension": "uninstall",
        "update_installed_extensions": "update",
    }
    reads = {
        "search_extensions",
        "list_installed_extensions",
        "get_installed_extension",
    }
    for spec in server._specs():
        action = actions[spec.name]

        async def handler(
            payload: BaseModel,
            action: str = action,
        ) -> Any:
            values = payload.model_dump(by_alias=True)
            values["action"] = action
            return await _call_route(
                "/api/internal/marketplace",
                values,
                extension_id="ofek-dev.marketplace",
            )

        _register(
            register,
            "runtime-marketplace",
            spec.name,
            spec.handler,
            handler,
            _read_policy() if spec.name in reads else _mutation_policy(),
        )


def _register_session_control_tools(register: Register) -> None:
    server = _load_bundled_server("session-control")

    paths = {
        "switch_model": "/api/internal/session-control/selectors",
        "continue_in_fresh_context": "/api/internal/session-control/continue-fresh",
    }
    for spec in server._specs():
        path = paths[spec.name]

        async def handler(
            payload: BaseModel,
            path: str = path,
        ) -> Any:
            values = payload.model_dump(by_alias=True)
            values["app_session_id"] = operation_authority.current_principal().app_session_id
            return await _call_route(
                path,
                values,
                extension_id="ofek-dev.session-control",
            )

        _register(
            register,
            "runtime-session-control",
            spec.name,
            spec.handler,
            handler,
            _control_policy(),
        )


def _register_session_bridge_tools(register: Register) -> None:
    server = _load_bundled_server("session-bridge")

    routes = {
        "search_sessions": (
            "/api/internal/session-bridge/search",
            "session-bridge-search",
            True,
        ),
        "delegate_to_session": (
            "/api/internal/session-bridge/delegate",
            "session-bridge-delegate",
            True,
        ),
        "propose_sessions": (
            "/api/internal/ask-propose",
            "",
            False,
        ),
    }
    for spec in server._specs():
        path, durable_operation, durable = routes[spec.name]

        async def handler(
            payload: BaseModel,
            path: str = path,
            durable: bool = durable,
        ) -> Any:
            principal = operation_authority.current_principal()
            values = payload.model_dump(by_alias=True)
            if path == "/api/internal/ask-propose":
                values["caller_sid"] = principal.app_session_id
            else:
                values["app_session_id"] = principal.app_session_id
            if durable:
                execution = operation_execution.current()
                values["_mcp_job_id"] = execution.request_id
                values["_mcp_job_wait"] = 0
            result = await _call_route(
                path,
                values,
                extension_id="ofek-dev.session-bridge",
            )
            if durable:
                execution.record_receipt(execution.request_id)
                result = await _await_mcp_job_result(
                    durable_operation,
                    execution.request_id,
                    result,
                )
            return result

        _register(
            register,
            "runtime-session-bridge",
            spec.name,
            spec.handler,
            handler,
            (
                _read_policy(durable=True)
                if spec.name == "search_sessions"
                else _mutation_policy(durable=durable)
                if durable
                else _control_policy()
            ),
            recovery_handler=(
                _mcp_job_recovery(durable_operation)
                if durable
                else None
            ),
        )


def _capability_handler(action: str):
    async def handler(payload: BaseModel) -> Any:
        principal = operation_authority.current_principal()
        body = payload.model_dump()
        body["action"] = action
        return await _call_route(
            "/api/internal/sessions/{sid}/capabilities",
            body,
            sid=principal.app_session_id,
        )

    return handler


def _communication_handler(name: str):
    async def handler(payload: BaseModel) -> Any:
        principal = operation_authority.current_principal()
        values = payload.model_dump()
        session_id = principal.app_session_id
        if name == "list_available_provider_models":
            from provider_catalog_mcp import available_provider_models_response

            return available_provider_models_response(**values)
        if name == "chat":
            return chat_store.post_and_read(reader_id=session_id, **values)
        if name == "inbox":
            return inbox_store.post_or_read(caller_session_id=session_id, **values)
        if name == "read_inbox_history":
            return inbox_store.read_history(recipient_session_id=session_id, **values)
        if name == "read_chat_history":
            return chat_store.read_history(**values)
        if name == "create_chat":
            return chat_store.create_chat(created_by=session_id, **values)
        if name == "set_chat_sender_policy":
            return chat_store.set_sender_policy(owner_id=session_id, **values)
        if name == "delete_chat":
            return chat_store.delete_chat(**values)
        if name == "ensure_named_worker":
            return await _ensure_named_worker(values, principal.cwd)
        values["cwd"] = str(values.get("cwd") or "").strip() or principal.cwd
        if name in {"mssg", "delegate_task", "create_session", "create_sub_session", "ask"}:
            values["sender_session_id"] = session_id
        elif name == "stop_turn":
            values["caller_session_id"] = session_id
        elif name == "create_worker":
            values["app_session_id"] = session_id
            values["client_request_id"] = f"operation_{uuid.uuid4().hex}"
        path = {
            "mssg": "/api/internal/mssg",
            "stop_turn": "/api/internal/stop-turn",
            "delegate_task": "/api/internal/delegate-task",
            "create_session": "/api/internal/create-session",
            "create_sub_session": "/api/internal/create-sub-session",
            "ask": "/api/internal/ask",
            "create_worker": "/api/internal/create-worker",
        }[name]
        durable_operation = {
            "mssg": "mssg",
            "delegate_task": "delegate-task",
            "ask": "ask",
        }.get(name)
        if durable_operation is not None:
            execution = operation_execution.current()
            values["_mcp_job_id"] = execution.request_id
            values["_mcp_job_wait"] = 0
        result = await _call_route(path, values)
        if durable_operation is not None:
            execution.record_receipt(execution.request_id)
            result = await _await_mcp_job_result(
                durable_operation,
                execution.request_id,
                result,
            )
        return result

    return handler


def _load_bundled_server(extension_dir: str):
    module_name = "better_agent_bundled_" + extension_dir.replace("-", "_") + "_mcp"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = (
        Path(__file__).resolve().parents[1]
        / "extensions"
        / extension_dir
        / "mcp"
        / "server.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load bundled MCP server: {extension_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


async def _ensure_named_worker(values: dict[str, Any], inherited_cwd: str) -> dict[str, Any]:
    name = str(values.pop("name") or "").strip()
    mode = str(values.pop("orchestration_mode") or "").strip()
    if mode == "manager":
        mode = "team"
    cwd = str(values.pop("cwd") or "").strip() or inherited_cwd
    spec = {
        "role_key": name,
        "description": str(values.pop("description") or "").strip() or f"worker:{name}",
        "orchestration_mode": mode,
        "node_id": str(values.pop("node_id") or "").strip() or None,
        "tags": [name],
        "folder_id": str(values.pop("folder_id") or "").strip() or None,
        "tag_ids": values.pop("tag_ids") or [],
    }
    for field in ("provision_prompt", "provider_id", "model", "reasoning_effort", "runner"):
        value = str(values.pop(field) or "").strip()
        if value:
            spec[field] = value
    result = await _call_route(
        "/api/internal/workers/provision",
        {"cwd": cwd, "workers": [spec]},
    )
    workers = result.get("workers") or []
    if not workers:
        raise RuntimeError("provision returned no worker")
    worker = workers[0]
    return {
        "success": True,
        "agent_session_id": worker.get("agent_session_id"),
        "name": worker.get("name"),
        "created": bool(worker.get("created")),
        "orchestration_mode": worker.get("orchestration_mode"),
        "registry_cwd": worker.get("registry_cwd") or worker.get("cwd"),
    }


def _ui_handler(action: str):
    async def handler(payload: BaseModel) -> Any:
        values = payload.model_dump()
        values["app_session_id"] = operation_authority.current_principal().app_session_id
        path = {
            "open-file-panel": "/api/internal/open-file-panel",
            "request-user-input": "/api/internal/user-input/request",
            "request-user-approval": "/api/internal/user-input/request",
            "start-file-discussion": "/api/internal/file-editor/start-discussion",
            "open-config-panel": "/api/internal/open-config-panel",
        }[action]
        if action == "request-user-input":
            values["kind"] = "input"
        elif action == "request-user-approval":
            values["kind"] = "approval"
        return await _call_route(path, values)

    return handler


def _route_handler(path: str):
    async def handler(payload: BaseModel) -> Any:
        return await _call_route(path, payload.model_dump())

    return handler


def _mcp_job_recovery(operation: str):
    async def recover(
        _payload: BaseModel,
        _receipt: str | None,
        request_id: str,
    ) -> Any:
        return await _call_route(
            "/api/internal/mcp-jobs/results",
            {
                "operation": operation,
                "id": request_id,
                "_mcp_job_wait": 0,
            },
        )

    return recover


async def _await_mcp_job_result(
    operation: str,
    request_id: str,
    initial: Any,
) -> Any:
    response = initial
    if isinstance(response, dict) and response.get("ready") is False:
        import extension_jobs
        import main

        task = extension_jobs.get_active(
            main._CORE_MCP_JOB_OWNER,
            operation,
            request_id,
        )
        if task is not None:
            await asyncio.shield(task)
        response = await _call_route(
            "/api/internal/mcp-jobs/results",
            {
                "operation": operation,
                "id": request_id,
                "_mcp_job_wait": 0,
            },
        )
    if not isinstance(response, dict) or response.get("ready") is not True:
        return response
    if response.get("status") != "complete":
        raise RuntimeError(str(response.get("error") or "MCP job failed"))
    return response.get("result")


async def _call_route(
    path: str,
    body: dict[str, Any],
    *,
    extension_id: str = "",
    **path_values: Any,
) -> Any:
    import main

    route = next(
        (
            item
            for item in main.app.routes
            if getattr(item, "path", None) == path and "POST" in getattr(item, "methods", ())
        ),
        None,
    )
    if route is None:
        raise RuntimeError(f"internal operation route is unavailable: {path}")
    token = main.coordinator.internal_token
    binding = None
    if extension_id:
        import extension_token_registry

        token = extension_token_registry.mint(extension_id)
        principal = main.coordinator.resolve_principal(token)
        if principal is None:
            raise PermissionError("runtime extension principal is unavailable")
        binding = main.coordinator.bind_principal(
            token,
            principal,
            allow_downstream=True,
        )
    kwargs = {
        **path_values,
        "body": body,
        "x_internal_token": token,
    }
    if binding is None:
        result = route.endpoint(**kwargs)
        return await result if inspect.isawaitable(result) else result
    with binding:
        result = route.endpoint(**kwargs)
        return await result if inspect.isawaitable(result) else result


def _read_policy(*, durable: bool = False) -> operation_catalog.OperationPolicy:
    return operation_catalog.OperationPolicy(
        side_effect=operation_catalog.SideEffectClass.READ,
        owner=operation_catalog.ExecutionOwner.PRIMARY,
        recovery=(
            operation_catalog.RecoveryPolicy.RECONCILE
            if durable
            else operation_catalog.RecoveryPolicy.FAIL
        ),
        durable=durable,
        cancel_supported=False,
        context_required=True,
    )


def _mutation_policy(*, durable: bool = False) -> operation_catalog.OperationPolicy:
    return operation_catalog.OperationPolicy(
        side_effect=operation_catalog.SideEffectClass.MUTATION,
        owner=operation_catalog.ExecutionOwner.PRIMARY,
        recovery=(
            operation_catalog.RecoveryPolicy.RECONCILE
            if durable
            else operation_catalog.RecoveryPolicy.FAIL
        ),
        durable=durable,
        cancel_supported=False,
        context_required=True,
    )


def _control_policy() -> operation_catalog.OperationPolicy:
    return operation_catalog.OperationPolicy(
        side_effect=operation_catalog.SideEffectClass.CONTROL,
        owner=operation_catalog.ExecutionOwner.PRIMARY,
        recovery=operation_catalog.RecoveryPolicy.FAIL,
        durable=False,
        cancel_supported=False,
        context_required=True,
    )
