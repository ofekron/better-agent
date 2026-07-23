from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
from typing import Any, Callable

import extension_jobs
import operation_authority
import operation_catalog
import operation_execution
from runtime_principal import RuntimePrincipal
from scoped_runtime_client import ScopedRuntimeClient

_OWNER = "operation-runtime"
_ADMISSION_LOCK = threading.Lock()
_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "expired"})


def _operation_namespace(operation: str) -> str:
    return operation.replace("_", "-")


def _fingerprint(
    *,
    operation: str,
    payload: dict[str, Any],
    principal: RuntimePrincipal,
) -> str:
    data = {
        "operation": operation,
        "payload": payload,
        "principal_scope": principal.idempotency_scope_digest(),
        "grant_generation": principal.grant_generation,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _runner(
    client: ScopedRuntimeClient,
    operation: str,
    deadline_at: float | None,
) -> Callable[..., Any]:
    async def run(request_payload: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        namespace = _operation_namespace(operation)
        try:
            if deadline_at is not None and deadline_at <= time.time():
                extension_jobs.persist_expired(_OWNER, namespace, request_id)
                return {"operation": operation, "expired": True}

            def record_receipt(receipt: str) -> None:
                extension_jobs.persist_owner_receipt(
                    _OWNER,
                    namespace,
                    request_id,
                    receipt,
                )

            with operation_execution.bind(
                operation_execution.OperationExecutionContext(
                    request_id=request_id,
                    operation=operation,
                    deadline_at=deadline_at,
                    record_receipt=record_receipt,
                )
            ):
                result = await client.invoke(operation, request_payload)
            if isinstance(result, dict) and result.get("ready") is False:
                extension_jobs.persist_running(
                    _OWNER,
                    namespace,
                    request_id,
                    recovery_required=True,
                )
                return {"operation": operation, "pending": True}
            outcome = {"operation": operation, "value": result}
            extension_jobs.persist_complete(_OWNER, namespace, request_id, outcome)
            return outcome
        except BaseException as exc:
            record = extension_jobs.read_record_strict(_OWNER, namespace, request_id) or {}
            if isinstance(exc, asyncio.CancelledError) and record.get("owner_receipt"):
                extension_jobs.persist_running(
                    _OWNER,
                    namespace,
                    request_id,
                    recovery_required=True,
                )
                raise
            extension_jobs.persist_failed(
                _OWNER,
                namespace,
                request_id,
                str(exc),
                cancelled=isinstance(exc, asyncio.CancelledError),
            )
            raise

    return run


def admit(
    *,
    client: ScopedRuntimeClient,
    operation: str,
    payload: dict[str, Any],
    idempotency_key: str,
    deadline_at: float | None = None,
) -> dict[str, Any]:
    catalog = operation_catalog.manager().get(client.execution_generation)
    descriptor = catalog.descriptor(operation)
    if not descriptor.policy.durable:
        raise ValueError(f"operation is not durable: {operation}")
    if deadline_at is not None and deadline_at <= time.time():
        raise ValueError("operation deadline already expired")
    operation_authority.verify(client.verified_principal)
    namespace = _operation_namespace(operation)
    digest = _fingerprint(
        operation=operation,
        payload=payload,
        principal=client.principal,
    )

    with _ADMISSION_LOCK:
        existing = extension_jobs.read_record_strict(_OWNER, namespace, idempotency_key)
        if existing is None:
            operation_catalog.manager().pin(client.execution_generation)
        try:
            result = extension_jobs.get_or_fire_idempotent(
                _OWNER,
                namespace,
                idempotency_key,
                payload,
                _runner(client, operation, deadline_at),
                payload_digest=digest,
                caller_extension=client.principal.principal_id,
                metadata={
                    "operation_name": operation,
                    "execution_generation": client.execution_generation,
                    "principal_identity_digest": client.principal.idempotency_scope_digest(),
                    "principal_scope_digest": client.principal.scope_digest(),
                    "principal_reference": client.principal.reference(),
                    "grant_generation": client.principal.grant_generation,
                    "deadline_at": deadline_at,
                    "owner_receipt_at": None,
                },
            )
        except BaseException:
            if existing is None:
                operation_catalog.manager().unpin(client.execution_generation)
            raise
    if isinstance(result, dict):
        return result
    record = extension_jobs.read_record_strict(_OWNER, namespace, idempotency_key)
    return extension_jobs.response_from_record(record or {})


def cancel(
    *,
    client: ScopedRuntimeClient,
    operation: str,
    request_id: str,
) -> dict[str, Any]:
    record = _authorized_record(client, operation, request_id)
    catalog = operation_catalog.manager().get(str(record["execution_generation"]))
    if not catalog.descriptor(operation).policy.cancel_supported:
        raise ValueError(f"operation does not support cancellation: {operation}")
    return extension_jobs.request_cancel(
        _OWNER,
        _operation_namespace(operation),
        request_id,
    )


def get(
    *,
    client: ScopedRuntimeClient,
    operation: str,
    request_id: str,
) -> dict[str, Any] | None:
    namespace = _operation_namespace(operation)
    record = extension_jobs.read_record_strict(_OWNER, namespace, request_id)
    if record is None:
        return None
    _authorized_record(client, operation, request_id, record=record)
    return extension_jobs.response_from_record(record)


def record_owner_receipt(
    *,
    client: ScopedRuntimeClient,
    operation: str,
    request_id: str,
    receipt: str,
) -> dict[str, Any]:
    _authorized_record(client, operation, request_id)
    return extension_jobs.persist_owner_receipt(
        _OWNER,
        _operation_namespace(operation),
        request_id,
        receipt,
    )


def recover_pins() -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in extension_jobs.list_owner_records_strict(_OWNER):
        if record.get("status") in _TERMINAL_STATUSES:
            continue
        generation = str(record.get("execution_generation") or "")
        if not generation:
            raise RuntimeError("durable operation request lacks an execution generation")
        counts[generation] = counts.get(generation, 0) + 1
    operation_catalog.manager().restore_pins(counts)
    return counts


async def recover() -> dict[str, int]:
    counts = recover_pins()
    for record in extension_jobs.list_owner_records_strict(_OWNER):
        if record.get("status") in _TERMINAL_STATUSES:
            continue
        await _recover_record(record)
    return counts


async def _recover_record(record: dict[str, Any]) -> None:
    operation = str(record.get("operation_name") or "")
    request_id = str(record.get("id") or "")
    namespace = _operation_namespace(operation)
    generation = str(record.get("execution_generation") or "")
    deadline_at = record.get("deadline_at")
    if (
        isinstance(deadline_at, (int, float))
        and float(deadline_at) <= time.time()
        and not record.get("owner_receipt")
    ):
        extension_jobs.persist_expired(_OWNER, namespace, request_id)
        return
    try:
        catalog = operation_catalog.manager().get(generation)
        descriptor = catalog.descriptor(operation)
        reference = record.get("principal_reference")
        if not isinstance(reference, dict):
            raise RuntimeError("durable operation request lacks principal reference")
        verified = operation_authority.resolve(
            reference,
            availability_generation=catalog.generation,
        )
        client = ScopedRuntimeClient(verified, catalog)
        _verify_record_owner(client, record)
    except Exception as exc:
        extension_jobs.persist_failed(_OWNER, namespace, request_id, str(exc))
        return

    if descriptor.policy.recovery is operation_catalog.RecoveryPolicy.FAIL:
        extension_jobs.persist_failed(
            _OWNER,
            namespace,
            request_id,
            "operation cannot recover after backend restart",
        )
        return
    receipt = str(record.get("owner_receipt") or "") or None
    if record.get("status") == "cancel_requested" and receipt is None:
        extension_jobs.persist_failed(
            _OWNER,
            namespace,
            request_id,
            "cancelled before owner acknowledgement",
            cancelled=True,
        )
        return
    if descriptor.policy.recovery is operation_catalog.RecoveryPolicy.RESUME:
        extension_jobs.get_or_resume(
            _OWNER,
            namespace,
            request_id,
            _runner(
                client,
                operation,
                float(deadline_at) if isinstance(deadline_at, (int, float)) else None,
            ),
        )
        return
    if descriptor.recovery_handler is None:
        extension_jobs.persist_failed(
            _OWNER,
            namespace,
            request_id,
            "operation has no reconciliation handler",
        )
        return
    try:
        payload = descriptor.request_model.model_validate(record.get("payload"))
        outcome = descriptor.recovery_handler(payload, receipt, request_id)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, dict) and outcome.get("ready") is False:
            extension_jobs.persist_running(
                _OWNER,
                namespace,
                request_id,
                recovery_required=True,
            )
            return
        if isinstance(outcome, dict) and outcome.get("status") == "cancelled":
            extension_jobs.persist_failed(
                _OWNER,
                namespace,
                request_id,
                str(outcome.get("error") or "operation cancelled"),
                cancelled=True,
            )
            return
        if isinstance(outcome, dict) and outcome.get("ready") is True and "result" in outcome:
            outcome = outcome["result"]
        extension_jobs.persist_complete(
            _OWNER,
            namespace,
            request_id,
            {"operation": operation, "value": outcome},
        )
    except BaseException as exc:
        extension_jobs.persist_failed(_OWNER, namespace, request_id, str(exc))


def _authorized_record(
    client: ScopedRuntimeClient,
    operation: str,
    request_id: str,
    *,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation_authority.verify(client.verified_principal)
    record = record or extension_jobs.read_record_strict(
        _OWNER,
        _operation_namespace(operation),
        request_id,
    )
    if record is None:
        raise KeyError(f"unknown operation request: {request_id}")
    if record.get("operation_name") != operation:
        raise PermissionError("operation request identity mismatch")
    _verify_record_owner(client, record)
    return record


def _verify_record_owner(client: ScopedRuntimeClient, record: dict[str, Any]) -> None:
    if record.get("principal_identity_digest") != client.principal.idempotency_scope_digest():
        raise PermissionError("operation request belongs to a different principal")


def _release_terminal_pin(record: dict[str, Any]) -> None:
    if record.get("owner") != _OWNER:
        return
    generation = str(record.get("execution_generation") or "")
    if generation:
        operation_catalog.manager().unpin(generation)


extension_jobs.register_terminal_listener(_release_terminal_pin)
