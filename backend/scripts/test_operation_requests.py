#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

from pydantic import BaseModel

_STATE_HOME = tempfile.mkdtemp(prefix="better-agent-operation-requests-")
os.environ["BETTER_AGENT_HOME"] = _STATE_HOME

import extension_jobs
import installation_profile
import operation_authority
import operation_catalog
import operation_requests
from runtime_principal import PrincipalKind, RuntimePrincipal
from scoped_runtime_client import ScopedRuntimeClient


class Request(BaseModel):
    value: str
    wait: bool = False


def _principal(operation: str, *, principal_id: str = "run-1", availability: str = "one"):
    now = time.time()
    return RuntimePrincipal(
        kind=PrincipalKind.AGENT_RUN,
        principal_id=principal_id,
        issuer="test",
        audience="better-agent-operation-runtime",
        permitted_operations=(operation,),
        permitted_resources=("session:one",),
        grant_generation="grant-1",
        availability_generation=availability,
        issued_at=now,
        expires_at=now + 60,
        app_session_id="session-one",
        run_id=principal_id,
        provider_id="provider-one",
        node_id="primary",
        cwd="/tmp/project",
    )


async def _scenario() -> None:
    original_manager = operation_catalog._MANAGER
    manager = operation_catalog.CatalogManager()
    operation_catalog._MANAGER = manager
    release = asyncio.Event()
    executions: list[str] = []

    async def handler(request: Request):
        executions.append(request.value)
        if request.wait:
            await release.wait()
        return {"value": request.value}

    async def reconcile(request: Request, receipt: str | None, request_id: str):
        return {"value": request.value, "receipt": receipt, "request_id": request_id}

    previous_validator = operation_authority.register_validator(
        PrincipalKind.AGENT_RUN,
        lambda principal: principal.principal_id in {"run-1", "run-2"},
    )
    previous_integrations_enabled = installation_profile.integrations_enabled
    installation_profile.integrations_enabled = lambda: True
    try:
        descriptor = manager.register_capability(
            "example",
            "mutate",
            Request,
            handler,
            policy=operation_catalog.OperationPolicy(
                side_effect=operation_catalog.SideEffectClass.MUTATION,
                owner=operation_catalog.ExecutionOwner.PRIMARY,
                recovery=operation_catalog.RecoveryPolicy.RECONCILE,
                durable=True,
                cancel_supported=True,
                context_required=True,
            ),
            recovery_handler=reconcile,
        )
        catalog = manager.publish()
        principal = _principal(descriptor.key)
        client = ScopedRuntimeClient(operation_authority.issue(principal), catalog)
        admitted = operation_requests.admit(
            client=client,
            operation=descriptor.key,
            payload={"value": "first"},
            idempotency_key="request-one",
        )
        assert admitted["status"] == "running"
        assert manager.pin_count(catalog.generation) == 1
        task = extension_jobs.get_active(
            "operation-runtime",
            descriptor.key.replace("_", "-"),
            "request-one",
        )
        assert task is not None
        await task
        completed = operation_requests.get(
            client=client,
            operation=descriptor.key,
            request_id="request-one",
        )
        assert completed and completed["result"] == {
            "operation": descriptor.key,
            "value": {"value": "first"},
        }
        assert manager.pin_count(catalog.generation) == 0

        manager.register_capability("example", "read", Request, handler)
        changed_catalog = manager.publish()
        assert changed_catalog.generation != catalog.generation
        changed_client = ScopedRuntimeClient(
            operation_authority.issue(_principal(descriptor.key, availability="two")),
            changed_catalog,
        )
        duplicate = operation_requests.admit(
            client=changed_client,
            operation=descriptor.key,
            payload={"value": "first"},
            idempotency_key="request-one",
        )
        assert duplicate["ready"] is True
        assert executions == ["first"]
        try:
            operation_requests.admit(
                client=changed_client,
                operation=descriptor.key,
                payload={"value": "different"},
                idempotency_key="request-one",
            )
        except ValueError as exc:
            assert "different payload" in str(exc)
        else:
            raise AssertionError("idempotency conflict was accepted")

        operation_requests.admit(
            client=changed_client,
            operation=descriptor.key,
            payload={"value": "cancel", "wait": True},
            idempotency_key="request-cancel",
        )
        requested = operation_requests.cancel(
            client=changed_client,
            operation=descriptor.key,
            request_id="request-cancel",
        )
        assert requested["status"] == "cancel_requested"
        cancel_task = extension_jobs.get_active(
            "operation-runtime",
            descriptor.key.replace("_", "-"),
            "request-cancel",
        )
        assert cancel_task is not None
        await asyncio.sleep(0)
        assert not cancel_task.done()
        release.set()
        await cancel_task
        completed_after_cancel_request = operation_requests.get(
            client=changed_client,
            operation=descriptor.key,
            request_id="request-cancel",
        )
        assert completed_after_cancel_request
        assert completed_after_cancel_request["status"] == "complete"

        other_client = ScopedRuntimeClient(
            operation_authority.issue(_principal(descriptor.key, principal_id="run-2")),
            changed_catalog,
        )
        try:
            operation_requests.get(
                client=other_client,
                operation=descriptor.key,
                request_id="request-one",
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("cross-principal request access was accepted")

        recovery_id = "request-recover"
        recovery_namespace = descriptor.key.replace("_", "-")
        recovery_record = {
            "id": recovery_id,
            "owner": "operation-runtime",
            "operation": recovery_namespace,
            "operation_name": descriptor.key,
            "payload": {"value": "recover"},
            "payload_digest": "stored",
            "caller_extension": principal.principal_id,
            "status": "running",
            "created_at": time.time(),
            "execution_generation": changed_catalog.generation,
            "principal_identity_digest": changed_client.principal.idempotency_scope_digest(),
            "principal_scope_digest": changed_client.principal.scope_digest(),
            "principal_reference": changed_client.principal.reference(),
            "grant_generation": changed_client.principal.grant_generation,
            "deadline_at": None,
            "owner_receipt": "receipt-1",
        }
        extension_jobs.job_path(
            "operation-runtime",
            recovery_namespace,
            recovery_id,
        ).parent.mkdir(parents=True, exist_ok=True)
        extension_jobs.job_path(
            "operation-runtime",
            recovery_namespace,
            recovery_id,
        ).write_text(json.dumps(recovery_record), encoding="utf-8")
        restarted_manager = operation_catalog.CatalogManager()
        restarted_manager.register_capability(
            "example",
            "mutate",
            Request,
            handler,
            policy=descriptor.policy,
            recovery_handler=reconcile,
        )
        restarted_manager.register_capability("example", "read", Request, handler)
        assert restarted_manager.publish().generation == changed_catalog.generation
        operation_catalog._MANAGER = restarted_manager
        restarted_manager.restore_pins({})
        assert await operation_requests.recover() == {changed_catalog.generation: 1}
        recovered = operation_requests.get(
            client=changed_client,
            operation=descriptor.key,
            request_id=recovery_id,
        )
        assert recovered and recovered["result"]["value"] == {
            "value": "recover",
            "receipt": "receipt-1",
            "request_id": recovery_id,
        }
        assert restarted_manager.pin_count(changed_catalog.generation) == 0

        corrupt_path = extension_jobs.job_path(
            "operation-runtime",
            recovery_namespace,
            "request-corrupt",
        )
        corrupt_path.write_text("{", encoding="utf-8")
        try:
            operation_requests.recover_pins()
        except RuntimeError as exc:
            assert "corrupt" in str(exc)
        else:
            raise AssertionError("corrupt durable state was ignored")
        assert executions.count("first") == 1
        assert "recover" not in executions
    finally:
        installation_profile.integrations_enabled = previous_integrations_enabled
        operation_authority.restore_validator(PrincipalKind.AGENT_RUN, previous_validator)
        operation_catalog._MANAGER = original_manager


def main() -> None:
    try:
        asyncio.run(_scenario())
        print("operation request tests passed")
    finally:
        import shutil

        shutil.rmtree(Path(_STATE_HOME))


if __name__ == "__main__":
    main()
