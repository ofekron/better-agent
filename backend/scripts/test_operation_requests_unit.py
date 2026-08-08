"""Pytest owner for backend/operation_requests.py.

The direct owner (test_operation_requests.py) is a standalone __main__
script and therefore pytest-invisible, so this module is the authoritative
unit-tier owner. It exercises every branch through the REAL collaborators
(operation_catalog, extension_jobs, operation_authority, ScopedRuntimeClient)
against an isolated BETTER_AGENT_HOME, driving each scenario through
asyncio.run. The only forced branches are the rare error paths that cannot
be reached deterministically through the public API (admit's fire-failure
unpin, the runner's deadline-at-execution and cancelled-with-receipt paths,
and the defensive _authorized_record identity-mismatch) — those use targeted
monkeypatching or direct internal calls.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import shutil
import tempfile
import time
from typing import Any

import pytest
from pydantic import BaseModel

import paths

_TEST_HOME = tempfile.mkdtemp(prefix="better-agent-op-requests-unit-")
paths.engage_test_home(_TEST_HOME)
atexit.register(lambda: shutil.rmtree(_TEST_HOME, ignore_errors=True))

import extension_jobs  # noqa: E402
import installation_profile  # noqa: E402
import operation_authority  # noqa: E402
import operation_catalog  # noqa: E402
import operation_execution  # noqa: E402
import operation_requests  # noqa: E402
from runtime_principal import PrincipalKind, RuntimePrincipal  # noqa: E402
from scoped_runtime_client import ScopedRuntimeClient  # noqa: E402

OWNER = operation_requests._OWNER
AUDIENCE = "better-agent-operation-runtime"


class Payload(BaseModel):
    value: str = ""
    wait: bool = False


def _principal(operation: str, *, principal_id: str = "run-1") -> RuntimePrincipal:
    now = time.time()
    return RuntimePrincipal(
        kind=PrincipalKind.AGENT_RUN,
        principal_id=principal_id,
        issuer="test",
        audience=AUDIENCE,
        permitted_operations=(operation,),
        permitted_resources=("session:one",),
        grant_generation="grant-1",
        availability_generation="one",
        issued_at=now,
        expires_at=now + 3600,
        app_session_id="session-one",
        run_id=principal_id,
        provider_id="provider-one",
        node_id="primary",
        cwd="/tmp/project",
    )


def _client(catalog: operation_catalog.PublishedCatalog, operation: str, *, principal_id: str = "run-1") -> ScopedRuntimeClient:
    return ScopedRuntimeClient(operation_authority.issue(_principal(operation, principal_id=principal_id)), catalog)


def _policy(
    *,
    durable: bool = True,
    cancel_supported: bool = True,
    recovery: operation_catalog.RecoveryPolicy = operation_catalog.RecoveryPolicy.RECONCILE,
) -> operation_catalog.OperationPolicy:
    return operation_catalog.OperationPolicy(
        side_effect=operation_catalog.SideEffectClass.MUTATION,
        owner=operation_catalog.ExecutionOwner.PRIMARY,
        recovery=recovery,
        durable=durable,
        cancel_supported=cancel_supported,
        context_required=True,
    )


def _register(operation: str, handler: Any, *, policy: operation_catalog.OperationPolicy, recovery_handler: Any = None) -> operation_catalog.PublishedCatalog:
    capability, action = operation.split("_", 1)
    operation_catalog._MANAGER.register_capability(capability, action, Payload, handler, policy=policy, recovery_handler=recovery_handler)
    return operation_catalog._MANAGER.publish()


def _seed(operation: str, request_id: str, **fields: Any) -> dict[str, Any]:
    namespace = operation_requests._operation_namespace(operation)
    record: dict[str, Any] = {
        "id": request_id,
        "owner": OWNER,
        "operation": namespace,
        "operation_name": fields.get("operation_name", operation),
        "status": fields.get("status", "running"),
        "created_at": time.time(),
    }
    record.update(fields)
    path = extension_jobs.job_path(OWNER, namespace, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


async def _await_jobs() -> None:
    for task in list(extension_jobs._JOBS.values()):
        if not task.done():
            try:
                await task
            except BaseException:
                pass


def _record(operation: str, request_id: str) -> dict[str, Any] | None:
    return extension_jobs.read_record_strict(OWNER, operation_requests._operation_namespace(operation), request_id)


@pytest.fixture(autouse=True)
def _env():
    home = tempfile.mkdtemp(prefix="better-agent-op-requests-unit-")
    paths.engage_test_home(home)
    paths.reset_home_cache()
    prev_manager = operation_catalog._MANAGER
    operation_catalog._MANAGER = operation_catalog.CatalogManager()
    prev_validator = operation_authority.register_validator(PrincipalKind.AGENT_RUN, lambda principal: True)
    prev_integ = installation_profile.integrations_enabled
    installation_profile.integrations_enabled = lambda: True  # type: ignore[assignment]
    extension_jobs._JOBS.clear()
    extension_jobs._COMPLETED_AT.clear()
    try:
        yield
    finally:
        installation_profile.integrations_enabled = prev_integ  # type: ignore[assignment]
        operation_authority.restore_validator(PrincipalKind.AGENT_RUN, prev_validator)
        operation_catalog._MANAGER = prev_manager
        paths.reset_home_cache()
        shutil.rmtree(home, ignore_errors=True)


def test_namespace_and_fingerprint_are_deterministic():
    assert operation_requests._operation_namespace("example_mutate") == "example-mutate"
    principal = _principal("example_mutate")
    digest_a = operation_requests._fingerprint(operation="example_mutate", payload={"value": "x"}, principal=principal)
    digest_b = operation_requests._fingerprint(operation="example_mutate", payload={"value": "x"}, principal=principal)
    assert digest_a == digest_b and len(digest_a) == 64
    assert operation_requests._fingerprint(operation="example_mutate", payload={"value": "y"}, principal=principal) != digest_a


def test_admit_runs_to_complete_and_releases_pin():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        admitted = operation_requests.admit(client=client, operation="example_mutate", payload={"value": "first"}, idempotency_key="r1")
        assert admitted["status"] == "running"
        assert operation_catalog._MANAGER.pin_count(catalog.generation) == 1
        await _await_jobs()

    asyncio.run(drive())
    completed = operation_requests.get(client=client, operation="example_mutate", request_id="r1")
    assert completed and completed["ready"] is True
    assert completed["result"] == {"operation": "example_mutate", "value": {"value": "first"}}
    assert operation_catalog._MANAGER.pin_count(catalog.generation) == 0


def test_admit_duplicate_after_restart_returns_cached_response():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "first"}, idempotency_key="r1")
        await _await_jobs()
        extension_jobs._JOBS.clear()  # simulate restart: record on disk, no in-memory task
        duplicate = operation_requests.admit(client=client, operation="example_mutate", payload={"value": "first"}, idempotency_key="r1")
        assert isinstance(duplicate, dict)
        assert duplicate["ready"] is True
        assert duplicate["status"] == "complete"

    asyncio.run(drive())


def test_admit_rejects_non_durable_operation():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_read", handler, policy=_policy(durable=False))
    client = _client(catalog, "example_read")
    with pytest.raises(ValueError, match="not durable"):
        operation_requests.admit(client=client, operation="example_read", payload={"value": "x"}, idempotency_key="r1")


def test_admit_rejects_already_expired_deadline():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    with pytest.raises(ValueError, match="deadline already expired"):
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1", deadline_at=time.time() - 1)


def test_admit_unpins_when_fire_raises():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    original = extension_jobs.get_or_fire_idempotent
    extension_jobs.get_or_fire_idempotent = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fire failed"))  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="fire failed"):
            operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        assert operation_catalog._MANAGER.pin_count(catalog.generation) == 0
    finally:
        extension_jobs.get_or_fire_idempotent = original  # type: ignore[assignment]


def test_admit_skips_unpin_when_fire_raises_on_existing_record():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed("example_mutate", "r1", payload_digest="stored", caller_extension="run-1")
    original = extension_jobs.get_or_fire_idempotent
    extension_jobs.get_or_fire_idempotent = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fire failed"))  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="fire failed"):
            operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        # existing record present -> no pin was taken, so nothing to unpin
        assert operation_catalog._MANAGER.pin_count(catalog.generation) == 0
    finally:
        extension_jobs.get_or_fire_idempotent = original  # type: ignore[assignment]


def test_runner_pends_when_handler_reports_not_ready():
    async def handler(request: Payload):
        return {"ready": False}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        await _await_jobs()

    asyncio.run(drive())
    record = _record("example_mutate", "r1")
    # A handler that reports not-ready and then returns still completes its
    # task, so extension_jobs' done-callback finalizes the record; the proof
    # that the runner took the not-ready branch is the pending outcome it
    # produced.
    assert record and record["result"] == {"operation": "example_mutate", "pending": True}


def test_runner_records_failure_on_handler_exception():
    async def handler(request: Payload):
        raise RuntimeError("handler broke")

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        await _await_jobs()

    asyncio.run(drive())
    record = _record("example_mutate", "r1")
    assert record and record["status"] == "failed"
    assert "handler broke" in record.get("error", "")


def test_runner_expires_when_deadline_passes_before_execution():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed("example_mutate", "expired-run")

    async def drive():
        run = operation_requests._runner(client, "example_mutate", time.time() - 1)
        return await run({"value": "x"}, request_id="expired-run")

    result = asyncio.run(drive())
    assert result == {"operation": "example_mutate", "expired": True}
    record = _record("example_mutate", "expired-run")
    assert record and record["status"] == "expired"


def test_runner_records_owner_receipt_from_handler_context():
    async def handler(request: Payload):
        operation_execution.current().record_receipt("mid-receipt")
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        await _await_jobs()

    asyncio.run(drive())
    record = _record("example_mutate", "r1")
    assert record and record["owner_receipt"] == "mid-receipt"


def test_runner_reruns_cancelled_with_owner_receipt():
    def handler(request: Payload):
        raise asyncio.CancelledError()

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed("example_mutate", "cancelled-run", owner_receipt="receipt-1")

    async def drive():
        run = operation_requests._runner(client, "example_mutate", None)
        with pytest.raises(asyncio.CancelledError):
            await run({"value": "x"}, request_id="cancelled-run")

    asyncio.run(drive())
    record = _record("example_mutate", "cancelled-run")
    assert record and record["status"] == "running"
    assert record.get("recovery_required") is True


def test_cancel_rejects_unsupported_operation():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy(cancel_supported=False))
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        await _await_jobs()
        with pytest.raises(ValueError, match="does not support cancellation"):
            operation_requests.cancel(client=client, operation="example_mutate", request_id="r1")

    asyncio.run(drive())


def test_cancel_requests_cancellation():
    release = asyncio.Event()

    async def handler(request: Payload):
        if request.wait:
            await release.wait()
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x", "wait": True}, idempotency_key="r1")
        requested = operation_requests.cancel(client=client, operation="example_mutate", request_id="r1")
        assert requested["status"] == "cancel_requested"
        release.set()
        await _await_jobs()

    asyncio.run(drive())
    record = _record("example_mutate", "r1")
    assert record and record["status"] == "complete"


def test_get_returns_none_for_unknown_request():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    assert operation_requests.get(client=client, operation="example_mutate", request_id="missing") is None


def test_get_returns_response_for_existing_request():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")

    async def drive():
        operation_requests.admit(client=client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        await _await_jobs()

    asyncio.run(drive())
    response = operation_requests.get(client=client, operation="example_mutate", request_id="r1")
    assert response and response["ready"] is True


def test_get_rejects_cross_principal_access():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    owner_client = _client(catalog, "example_mutate", principal_id="run-1")

    async def drive():
        operation_requests.admit(client=owner_client, operation="example_mutate", payload={"value": "x"}, idempotency_key="r1")
        await _await_jobs()

    asyncio.run(drive())
    intruder = _client(catalog, "example_mutate", principal_id="run-2")
    with pytest.raises(PermissionError, match="different principal"):
        operation_requests.get(client=intruder, operation="example_mutate", request_id="r1")


def test_cancel_unknown_request_raises_keyerror():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    with pytest.raises(KeyError, match="unknown operation request"):
        operation_requests.cancel(client=client, operation="example_mutate", request_id="missing")


def test_authorized_record_rejects_identity_mismatch():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed("example_mutate", "mismatch", operation_name="other_operation")
    with pytest.raises(PermissionError, match="identity mismatch"):
        operation_requests._authorized_record(client, "example_mutate", "mismatch")


def test_record_owner_receipt_persists():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed("example_mutate", "receipt-run", principal_identity_digest=client.principal.idempotency_scope_digest())
    response = operation_requests.record_owner_receipt(client=client, operation="example_mutate", request_id="receipt-run", receipt="rec-9")
    record = _record("example_mutate", "receipt-run")
    assert record and record["owner_receipt"] == "rec-9"
    assert response["status"] == "running"


def test_release_terminal_pin_ignores_other_owners():
    operation_requests._release_terminal_pin({"owner": "not-operation-runtime", "execution_generation": "g1"})


def test_release_terminal_pin_skips_empty_generation():
    operation_requests._release_terminal_pin({"owner": OWNER, "execution_generation": ""})


def test_recover_pins_skips_terminal_and_restores_running():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    _seed("example_mutate", "done", status="complete", execution_generation=catalog.generation)
    _seed("example_mutate", "live", execution_generation=catalog.generation)
    counts = operation_requests.recover_pins()
    assert counts == {catalog.generation: 1}
    assert operation_catalog._MANAGER.pin_count(catalog.generation) == 1


def test_recover_pins_raises_on_missing_generation():
    _seed("example_mutate", "nogen", execution_generation="")
    with pytest.raises(RuntimeError, match="execution generation"):
        operation_requests.recover_pins()


def test_recover_pins_propagates_corrupt_record():
    path = extension_jobs.job_path(OWNER, "example-mutate", "corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        operation_requests.recover_pins()


def _seed_recovery_record(operation: str, request_id: str, catalog: operation_catalog.PublishedCatalog, client: ScopedRuntimeClient, **fields: Any) -> None:
    principal = client.principal
    base: dict[str, Any] = dict(
        execution_generation=catalog.generation,
        payload={"value": "recover"},
        principal_identity_digest=principal.idempotency_scope_digest(),
        principal_scope_digest=principal.scope_digest(),
        principal_reference=principal.reference(),
        grant_generation=principal.grant_generation,
    )
    base.update(fields)
    _seed(operation, request_id, **base)


def test_recover_record_expires_when_deadline_passed_without_receipt():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed_recovery_record("example_mutate", "rec1", catalog, client, deadline_at=time.time() - 1, owner_receipt=None)

    async def drive():
        await operation_requests._recover_record(_record("example_mutate", "rec1"))

    asyncio.run(drive())
    record = _record("example_mutate", "rec1")
    assert record and record["status"] == "expired"


def test_recover_record_fails_when_principal_reference_invalid():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed_recovery_record("example_mutate", "rec2", catalog, client, principal_reference="not-a-dict")

    async def drive():
        await operation_requests._recover_record(_record("example_mutate", "rec2"))

    asyncio.run(drive())
    record = _record("example_mutate", "rec2")
    assert record and record["status"] == "failed"
    assert "principal reference" in record.get("error", "")


def test_recover_record_fail_policy_persists_failure():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_fail", handler, policy=_policy(recovery=operation_catalog.RecoveryPolicy.FAIL))
    client = _client(catalog, "example_fail")
    _seed_recovery_record("example_fail", "rec3", catalog, client)

    async def drive():
        await operation_requests._recover_record(_record("example_fail", "rec3"))

    asyncio.run(drive())
    record = _record("example_fail", "rec3")
    assert record and record["status"] == "failed"
    assert "cannot recover" in record.get("error", "")


def test_recover_record_cancel_requested_without_receipt_fails_cancelled():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_mutate", handler, policy=_policy())
    client = _client(catalog, "example_mutate")
    _seed_recovery_record("example_mutate", "rec4", catalog, client, status="cancel_requested", owner_receipt=None)

    async def drive():
        await operation_requests._recover_record(_record("example_mutate", "rec4"))

    asyncio.run(drive())
    record = _record("example_mutate", "rec4")
    assert record and record["status"] == "cancelled"


def test_recover_record_resume_policy_reruns_and_completes():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_resume", handler, policy=_policy(recovery=operation_catalog.RecoveryPolicy.RESUME))
    client = _client(catalog, "example_resume")
    _seed("example_done", "terminal", status="complete", execution_generation=catalog.generation)  # skipped by recover()
    _seed_recovery_record("example_resume", "rec5", catalog, client, owner_receipt="receipt-1")

    async def drive():
        counts = await operation_requests.recover()
        assert counts == {catalog.generation: 1}
        await _await_jobs()

    asyncio.run(drive())
    record = _record("example_resume", "rec5")
    assert record and record["status"] == "complete"


def test_recover_record_reconcile_without_handler_fails():
    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_recon", handler, policy=_policy(), recovery_handler=None)
    client = _client(catalog, "example_recon")
    _seed_recovery_record("example_recon", "rec6", catalog, client)

    async def drive():
        await operation_requests._recover_record(_record("example_recon", "rec6"))

    asyncio.run(drive())
    record = _record("example_recon", "rec6")
    assert record and record["status"] == "failed"
    assert "reconciliation handler" in record.get("error", "")


def test_recover_record_reconcile_not_ready_stays_running():
    def reconcile(request: Payload, receipt: str | None, request_id: str):
        return {"ready": False}

    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_recon", handler, policy=_policy(), recovery_handler=reconcile)
    client = _client(catalog, "example_recon")
    _seed_recovery_record("example_recon", "rec7", catalog, client, owner_receipt="receipt-1")

    async def drive():
        await operation_requests._recover_record(_record("example_recon", "rec7"))

    asyncio.run(drive())
    record = _record("example_recon", "rec7")
    assert record and record["status"] == "running"
    assert record.get("recovery_required") is True


def test_recover_record_reconcile_cancelled_status_fails_cancelled():
    def reconcile(request: Payload, receipt: str | None, request_id: str):
        return {"status": "cancelled", "error": "owner cancelled"}

    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_recon", handler, policy=_policy(), recovery_handler=reconcile)
    client = _client(catalog, "example_recon")
    _seed_recovery_record("example_recon", "rec8", catalog, client, owner_receipt="receipt-1")

    async def drive():
        await operation_requests._recover_record(_record("example_recon", "rec8"))

    asyncio.run(drive())
    record = _record("example_recon", "rec8")
    assert record and record["status"] == "cancelled"


def test_recover_record_reconcile_ready_result_unwraps_and_completes():
    async def reconcile(request: Payload, receipt: str | None, request_id: str):
        return {"ready": True, "result": {"value": request.value, "receipt": receipt}}

    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_recon", handler, policy=_policy(), recovery_handler=reconcile)
    client = _client(catalog, "example_recon")
    _seed_recovery_record("example_recon", "rec9", catalog, client, owner_receipt="receipt-1")

    async def drive():
        await operation_requests._recover_record(_record("example_recon", "rec9"))

    asyncio.run(drive())
    record = _record("example_recon", "rec9")
    assert record and record["status"] == "complete"
    assert record["result"]["value"] == {"value": "recover", "receipt": "receipt-1"}


def test_recover_record_reconcile_plain_outcome_completes():
    def reconcile(request: Payload, receipt: str | None, request_id: str):
        return {"value": request.value, "request_id": request_id}

    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_recon", handler, policy=_policy(), recovery_handler=reconcile)
    client = _client(catalog, "example_recon")
    _seed_recovery_record("example_recon", "rec10", catalog, client, owner_receipt="receipt-1")

    async def drive():
        await operation_requests._recover_record(_record("example_recon", "rec10"))

    asyncio.run(drive())
    record = _record("example_recon", "rec10")
    assert record and record["status"] == "complete"
    assert record["result"]["value"] == {"value": "recover", "request_id": "rec10"}


def test_recover_record_reconcile_handler_raises_fails():
    def reconcile(request: Payload, receipt: str | None, request_id: str):
        raise RuntimeError("reconcile broke")

    async def handler(request: Payload):
        return {"value": request.value}

    catalog = _register("example_recon", handler, policy=_policy(), recovery_handler=reconcile)
    client = _client(catalog, "example_recon")
    _seed_recovery_record("example_recon", "rec11", catalog, client, owner_receipt="receipt-1")

    async def drive():
        await operation_requests._recover_record(_record("example_recon", "rec11"))

    asyncio.run(drive())
    record = _record("example_recon", "rec11")
    assert record and record["status"] == "failed"
    assert "reconcile broke" in record.get("error", "")
