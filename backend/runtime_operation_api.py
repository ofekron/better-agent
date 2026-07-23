from __future__ import annotations

import json
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict

import extension_jobs
import operation_authority
import operation_catalog
import operation_requests
from paths import bc_home
from runtime_broker import BrokerRequest
from runtime_principal import PrincipalKind, RuntimePrincipal
from scoped_runtime_client import ScopedRuntimeClient

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_MAINTAINED_PREFIXES = (
    "runtime_",
    "coordination_",
    "marketplace_",
    "provider_config_sync_",
    "session_bridge_",
    "session_control_",
    "agent_board_",
)


class RuntimeOperationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_session_id: str
    run_id: str
    provider_id: str
    cwd: str
    node_id: str = ""
    request: BrokerRequest


async def handle(raw: dict[str, Any]) -> dict[str, Any]:
    envelope = RuntimeOperationEnvelope.model_validate(raw)
    catalog = operation_catalog.current()
    available = tuple(
        key
        for key, descriptor in catalog.descriptors.items()
        if key.startswith(_MAINTAINED_PREFIXES)
        and descriptor.policy.side_effect
        is not operation_catalog.SideEffectClass.COMPATIBILITY
    )
    _validate_run(envelope, available)
    request = envelope.request
    if request.kind == "catalog":
        return {
            "success": True,
            "generation": catalog.generation,
            "schema": {
                key: {
                    "request": catalog.snapshot.get(key).request_schema(),
                    "response": catalog.snapshot.get(key).response_schema(),
                }
                for key in available
            },
        }
    if request.operation not in available:
        raise PermissionError("runtime operation is not available to this run")
    if request.generation != catalog.generation:
        raise RuntimeError("runtime operation generation changed")
    client = ScopedRuntimeClient(
        operation_authority.issue(_principal(envelope, request.operation, catalog.generation)),
        catalog,
    )
    descriptor = catalog.descriptor(request.operation)
    if request.kind == "invoke":
        payload = request.payload or {}
        if not descriptor.policy.durable:
            return {
                "success": True,
                "result": await client.invoke(request.operation, payload),
            }
        if not request.request_id:
            raise ValueError("durable operation request_id is required")
        response = operation_requests.admit(
            client=client,
            operation=request.operation,
            payload=payload,
            idempotency_key=request.request_id,
            deadline_at=request.deadline_at,
        )
        if response.get("ready") is False:
            task = extension_jobs.get_active(
                "operation-runtime",
                request.operation.replace("_", "-"),
                request.request_id,
            )
            if task is not None:
                try:
                    await task
                except Exception:
                    pass
            response = operation_requests.get(
                client=client,
                operation=request.operation,
                request_id=request.request_id,
            ) or response
        return _operation_response(response)
    if not request.request_id:
        raise ValueError("operation request_id is required")
    if request.kind == "status":
        response = operation_requests.get(
            client=client,
            operation=request.operation,
            request_id=request.request_id,
        )
        if response is None:
            raise KeyError("operation request does not exist")
        return {"success": True, "result": response}
    if request.kind == "cancel":
        return {
            "success": True,
            "result": operation_requests.cancel(
                client=client,
                operation=request.operation,
                request_id=request.request_id,
            ),
        }
    raise ValueError("unsupported runtime operation request")


def validate_agent_run(principal: RuntimePrincipal) -> bool:
    if principal.kind is not PrincipalKind.AGENT_RUN:
        return False
    try:
        raw = _read_run_input(principal.run_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if str(raw.get("app_session_id") or "") != principal.app_session_id:
        return False
    if str(raw.get("cwd") or "") != principal.cwd:
        return False
    from session_manager import manager as session_manager

    session = session_manager.get_ref(principal.app_session_id)
    if session is None:
        return False
    provider_id = str(session.get("provider_id") or "")
    return not provider_id or provider_id == principal.provider_id


def validate_node_relay(principal: RuntimePrincipal) -> bool:
    if principal.kind is not PrincipalKind.NODE_RELAY or not principal.node_id:
        return False
    import node_store
    from session_manager import manager as session_manager

    if node_store.get_connection(principal.node_id) is None:
        return False
    session = session_manager.get_ref(principal.app_session_id)
    return bool(session and str(session.get("node_id") or "primary") == principal.node_id)


def _validate_run(
    envelope: RuntimeOperationEnvelope,
    available: tuple[str, ...],
) -> None:
    if not envelope.app_session_id or not envelope.cwd or not available:
        raise PermissionError("runtime operation context is incomplete")
    if envelope.node_id:
        import node_store
        from session_manager import manager as session_manager

        if node_store.get_connection(envelope.node_id) is None:
            raise PermissionError("runtime node relay is disconnected")
        session = session_manager.get_ref(envelope.app_session_id)
        if session is None:
            raise PermissionError("runtime session is unavailable")
        if str(session.get("node_id") or "primary") != envelope.node_id:
            raise PermissionError("runtime run/node identity mismatch")
        return
    raw = _read_run_input(envelope.run_id)
    if str(raw.get("app_session_id") or "") != envelope.app_session_id:
        raise PermissionError("runtime run/session identity mismatch")
    if str(raw.get("cwd") or "") != envelope.cwd:
        raise PermissionError("runtime run/cwd identity mismatch")
    from session_manager import manager as session_manager

    session = session_manager.get_ref(envelope.app_session_id)
    if session is None:
        raise PermissionError("runtime session is unavailable")
    provider_id = str(session.get("provider_id") or "")
    if provider_id and provider_id != envelope.provider_id:
        raise PermissionError("runtime run/provider identity mismatch")


def _read_run_input(run_id: str) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("runtime run id is invalid")
    root = (bc_home() / "runs").resolve()
    path = (root / run_id / "input.json").resolve()
    if not path.is_relative_to(root):
        raise ValueError("runtime run path escapes state root")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runtime run input is invalid")
    return raw


def _principal(
    envelope: RuntimeOperationEnvelope,
    operation: str,
    generation: str,
) -> RuntimePrincipal:
    now = time.time()
    return RuntimePrincipal(
        kind=PrincipalKind.NODE_RELAY if envelope.node_id else PrincipalKind.AGENT_RUN,
        principal_id=envelope.node_id or envelope.run_id,
        issuer=(
            "better-agent-node-relay"
            if envelope.node_id
            else "better-agent-runner-broker"
        ),
        audience="better-agent-operation-runtime",
        permitted_operations=(operation,),
        permitted_resources=(),
        grant_generation=generation,
        availability_generation=generation,
        issued_at=now,
        expires_at=now + 60.0,
        app_session_id=envelope.app_session_id,
        run_id=envelope.run_id,
        provider_id=envelope.provider_id,
        node_id=envelope.node_id,
        cwd=envelope.cwd,
    )


def _operation_response(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("ready") is not True:
        return {"success": False, "error": "durable operation is still running"}
    if response.get("status") != "complete":
        return {
            "success": False,
            "error": str(response.get("error") or "durable operation failed"),
        }
    result = response.get("result")
    if isinstance(result, dict) and "value" in result:
        result = result["value"]
    return {"success": True, "result": result}


operation_authority.register_validator(PrincipalKind.AGENT_RUN, validate_agent_run)
operation_authority.register_validator(PrincipalKind.NODE_RELAY, validate_node_relay)
