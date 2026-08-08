#!/usr/bin/env python3
"""Unit owner for runtime_operation_api.py.

The direct owner test_runtime_operation_api.py is a standalone __main__
script (0 collected items); test_node_link.py replaces the module in
sys.modules with a fake. So runtime_operation_api.py is effectively
pytest-ownerless. This file is its pytest owner.

Strategy:
- async handle() driven via asyncio.run (no pytest-asyncio).
- collaborators patched at the module boundary (operation_catalog.published,
  operation_requests, extension_jobs, operation_authority.issue,
  ScopedRuntimeClient, node_store, session_manager.manager, _validate_run).
- _read_run_input / validate_* drive the REAL filesystem under an isolated
  BETTER_AGENT_HOME so the run-id regex and path-traversal guard are
  exercised against real resolve() behavior.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import paths

# conftest engages the isolated BETTER_AGENT_HOME before this module imports
# any backend code; the autouse _ensure_ba_home_dirs fixture owns the home and
# creates `runs/`. fs helpers below write under paths.bc_home() so they land
# exactly where _read_run_input reads.

import node_store
import operation_authority
import operation_catalog
import runtime_operation_api
from runtime_broker import BrokerRequest
from runtime_principal import PrincipalKind, RuntimePrincipal
import session_manager

_RUN = "run-abcdef12"  # valid run id (>=8 alnum/-/_)


# ---------- fake builders ----------


def _policy(side_effect=operation_catalog.SideEffectClass.READ, durable=False):
    return SimpleNamespace(side_effect=side_effect, durable=durable)


def _descriptor(key, side_effect=operation_catalog.SideEffectClass.READ, durable=False):
    return SimpleNamespace(key=key, policy=_policy(side_effect, durable))


def _catalog(monkeypatch, *, descriptors, generation="gen-1", schema=None):
    schema = schema if schema is not None else {k: '{"k":1}' for k in descriptors}
    catalog = SimpleNamespace(
        generation=generation,
        descriptors=descriptors,
        operation_schema_json=schema,
        descriptor=lambda key: descriptors[key],
    )
    monkeypatch.setattr(operation_catalog, "published", lambda: catalog)
    return catalog


def _envelope(*, kind="invoke", operation="runtime_read", generation="gen-1",
              payload=None, request_id="", deadline_at=None, run_id=_RUN,
              app_session_id="sess-one", provider_id="prov-one",
              cwd="/proj", node_id=""):
    request = BrokerRequest(
        version=1,
        kind=kind,
        operation=operation,
        payload=payload,
        request_id=request_id,
        deadline_at=deadline_at,
        generation=generation,
    )
    return {
        "app_session_id": app_session_id,
        "run_id": run_id,
        "provider_id": provider_id,
        "cwd": cwd,
        "node_id": node_id,
        "request": request.model_dump(),
    }


def _patch_handle_collabs(monkeypatch, *, validate=True, issue=None,
                          scoped_client_cls=None, admit=None, get_active=None):
    """Patch everything handle() touches except the catalog filter."""
    if validate:
        monkeypatch.setattr(runtime_operation_api, "_validate_run", lambda *a, **k: None)
    monkeypatch.setattr(operation_authority, "issue", issue or (lambda principal: ("verified", principal)))
    monkeypatch.setattr(runtime_operation_api, "ScopedRuntimeClient", scoped_client_cls or _RecordingClient)
    fake_req = SimpleNamespace(
        admit=admit or (lambda **kw: {"ready": True, "status": "complete", "result": {"value": "done"}}),
        get=lambda **kw: None,
        cancel=lambda **kw: {"cancelled": True},
    )
    monkeypatch.setattr(runtime_operation_api, "operation_requests", fake_req)
    fake_jobs = SimpleNamespace(get_active=get_active or (lambda *a, **k: None))
    monkeypatch.setattr(runtime_operation_api, "extension_jobs", fake_jobs)
    return fake_req


class _RecordingClient:
    """Stand-in for ScopedRuntimeClient; records invokes."""
    instances = []

    def __init__(self, principal, catalog):
        self.principal = principal
        self.catalog = catalog
        self.invoked = []
        type(self).instances.append(self)

    async def invoke(self, operation, payload):
        self.invoked.append((operation, payload))
        return {"invoked": operation, "payload": payload}


# ---------- _principal ----------


def test_principal_agent_run_kind():
    envelope = runtime_operation_api.RuntimeOperationEnvelope(
        **{**{k: v for k, v in _envelope().items() if k != "request"},
           "request": BrokerRequest(version=1, kind="invoke", operation="runtime_read")})
    principal = runtime_operation_api._principal(envelope, "runtime_read", "gen-1")
    assert principal.kind is PrincipalKind.AGENT_RUN
    assert principal.principal_id == _RUN
    assert principal.issuer == "better-agent-runner-broker"
    assert principal.permitted_operations == ("runtime_read",)
    assert principal.grant_generation == "gen-1"
    assert principal.expires_at > principal.issued_at


def test_principal_node_relay_kind():
    envelope = runtime_operation_api.RuntimeOperationEnvelope(
        app_session_id="s", run_id=_RUN, provider_id="p", cwd="/c", node_id="node-1",
        request=BrokerRequest(version=1, kind="invoke", operation="runtime_read"),
    )
    principal = runtime_operation_api._principal(envelope, "coordination_op", "gen-9")
    assert principal.kind is PrincipalKind.NODE_RELAY
    assert principal.principal_id == "node-1"
    assert principal.issuer == "better-agent-node-relay"


# ---------- _operation_response ----------


def test_operation_response_not_ready():
    assert runtime_operation_api._operation_response({"ready": False}) == {
        "success": False, "error": "durable operation is still running"}


def test_operation_response_ready_false_explicit():
    assert runtime_operation_api._operation_response({"ready": False, "status": "complete"}) == {
        "success": False, "error": "durable operation is still running"}


def test_operation_response_failed_status():
    out = runtime_operation_api._operation_response({"ready": True, "status": "error", "error": "boom"})
    assert out == {"success": False, "error": "boom"}


def test_operation_response_failed_no_error_message():
    out = runtime_operation_api._operation_response({"ready": True, "status": "error"})
    assert out == {"success": False, "error": "durable operation failed"}


def test_operation_response_dict_with_value_unwrap():
    out = runtime_operation_api._operation_response(
        {"ready": True, "status": "complete", "result": {"value": 42}})
    assert out == {"success": True, "result": 42}


def test_operation_response_plain_result():
    out = runtime_operation_api._operation_response(
        {"ready": True, "status": "complete", "result": "plain"})
    assert out == {"success": True, "result": "plain"}


# ---------- _read_run_input (real fs) ----------


def _seed_run(run_id=_RUN, *, app_session_id="sess-one", cwd="/proj", body=None):
    root = paths.bc_home() / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    payload = body if body is not None else {
        "app_session_id": app_session_id, "cwd": cwd}
    (root / "input.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_read_run_input_valid():
    _seed_run()
    raw = runtime_operation_api._read_run_input(_RUN)
    assert raw["app_session_id"] == "sess-one"
    assert raw["cwd"] == "/proj"


def test_read_run_input_invalid_id():
    with pytest.raises(ValueError, match="invalid"):
        runtime_operation_api._read_run_input("short")


def test_read_run_input_path_escape_rejected():
    # valid run id whose directory is a symlink pointing outside the state root
    outside = paths.bc_home() / "_outside_target"
    outside.mkdir()
    link = paths.bc_home() / "runs" / "link12345678"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes state root"):
        runtime_operation_api._read_run_input("link12345678")
    link.unlink()


def test_read_run_input_bad_json():
    root = paths.bc_home() / "runs" / "badjson99"
    root.mkdir(parents=True, exist_ok=True)
    (root / "input.json").write_text("not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        runtime_operation_api._read_run_input("badjson99")


def test_read_run_input_non_dict_json():
    root = _seed_run("dict12345", body=[1, 2, 3])
    with pytest.raises(ValueError, match="invalid"):
        runtime_operation_api._read_run_input("dict12345")


# ---------- _validate_run ----------


def _envelope_obj(**kw):
    return runtime_operation_api.RuntimeOperationEnvelope(
        **{**{k: v for k, v in _envelope(**kw).items() if k != "request"},
           "request": BrokerRequest(version=1, kind="invoke", operation="runtime_read")})


def test_validate_run_missing_app_session_id():
    with pytest.raises(PermissionError, match="incomplete"):
        runtime_operation_api._validate_run(
            _envelope_obj(app_session_id=""), available=("runtime_read",))


def test_validate_run_missing_cwd():
    with pytest.raises(PermissionError, match="incomplete"):
        runtime_operation_api._validate_run(
            _envelope_obj(cwd=""), available=("runtime_read",))


def test_validate_run_empty_available():
    with pytest.raises(PermissionError, match="incomplete"):
        runtime_operation_api._validate_run(_envelope_obj(), available=())


def _patch_session_and_node(monkeypatch, *, session=None, connection=object()):
    fake_mgr = SimpleNamespace(get_ref=lambda sid: session)
    monkeypatch.setattr(session_manager, "manager", fake_mgr)
    monkeypatch.setattr(node_store, "get_connection", lambda nid: connection)


def test_validate_run_node_relay_disconnected(monkeypatch):
    _patch_session_and_node(monkeypatch, connection=None)
    with pytest.raises(PermissionError, match="disconnected"):
        runtime_operation_api._validate_run(
            _envelope_obj(node_id="node-1"), available=("runtime_read",))


def test_validate_run_node_relay_session_unavailable(monkeypatch):
    _patch_session_and_node(monkeypatch, session=None, connection=object())
    with pytest.raises(PermissionError, match="unavailable"):
        runtime_operation_api._validate_run(
            _envelope_obj(node_id="node-1"), available=("runtime_read",))


def test_validate_run_node_relay_node_mismatch(monkeypatch):
    _patch_session_and_node(monkeypatch, session={"node_id": "other"}, connection=object())
    with pytest.raises(PermissionError, match="identity mismatch"):
        runtime_operation_api._validate_run(
            _envelope_obj(node_id="node-1"), available=("runtime_read",))


def test_validate_run_node_relay_ok(monkeypatch):
    _patch_session_and_node(monkeypatch, session={"node_id": "node-1"}, connection=object())
    # returns None on success
    assert runtime_operation_api._validate_run(
        _envelope_obj(node_id="node-1"), available=("runtime_read",)) is None


def test_validate_run_agent_session_mismatch(monkeypatch):
    _seed_run(_RUN, app_session_id="sess-one")
    with pytest.raises(PermissionError, match="session identity mismatch"):
        runtime_operation_api._validate_run(
            _envelope_obj(app_session_id="other"), available=("runtime_read",))


def test_validate_run_agent_cwd_mismatch(monkeypatch):
    _seed_run(_RUN, app_session_id="sess-one", cwd="/proj")
    with pytest.raises(PermissionError, match="cwd identity mismatch"):
        runtime_operation_api._validate_run(
            _envelope_obj(cwd="/other"), available=("runtime_read",))


def test_validate_run_agent_session_unavailable(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session=None)
    with pytest.raises(PermissionError, match="unavailable"):
        runtime_operation_api._validate_run(_envelope_obj(), available=("runtime_read",))


def test_validate_run_agent_provider_mismatch(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session={"provider_id": "other-prov"})
    with pytest.raises(PermissionError, match="provider identity mismatch"):
        runtime_operation_api._validate_run(_envelope_obj(), available=("runtime_read",))


def test_validate_run_agent_provider_empty_ok(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session={"provider_id": ""})
    assert runtime_operation_api._validate_run(_envelope_obj(), available=("runtime_read",)) is None


def test_validate_run_agent_ok(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session={"provider_id": "prov-one"})
    assert runtime_operation_api._validate_run(_envelope_obj(), available=("runtime_read",)) is None


# ---------- validate_agent_run ----------


def _principal_agent(run_id=_RUN, app_session_id="sess-one", provider_id="prov-one", cwd="/proj"):
    return RuntimePrincipal(
        kind=PrincipalKind.AGENT_RUN, principal_id=run_id, issuer="broker",
        audience="better-agent-operation-runtime", permitted_operations=("runtime_read",),
        permitted_resources=(), grant_generation="gen-1", availability_generation="gen-1",
        issued_at=1.0, expires_at=2.0, app_session_id=app_session_id, run_id=run_id,
        provider_id=provider_id, cwd=cwd,
    )


def test_validate_agent_run_wrong_kind():
    principal = RuntimePrincipal(
        kind=PrincipalKind.NODE_RELAY, principal_id=_RUN, issuer="broker",
        audience="better-agent-operation-runtime", permitted_operations=("runtime_read",),
        permitted_resources=(), grant_generation="gen-1", availability_generation="gen-1",
        issued_at=1.0, expires_at=2.0, app_session_id="sess-one", run_id=_RUN,
        provider_id="prov-one", cwd="/proj", node_id="node-1")
    assert runtime_operation_api.validate_agent_run(principal) is False


def test_validate_agent_run_read_error(monkeypatch):
    # invalid run id -> ValueError from _read_run_input
    principal = _principal_agent(run_id="short")
    assert runtime_operation_api.validate_agent_run(principal) is False


def test_validate_agent_run_session_mismatch(monkeypatch):
    _seed_run(_RUN, app_session_id="sess-one")
    assert runtime_operation_api.validate_agent_run(
        _principal_agent(app_session_id="other")) is False


def test_validate_agent_run_cwd_mismatch(monkeypatch):
    _seed_run(_RUN, cwd="/proj")
    assert runtime_operation_api.validate_agent_run(
        _principal_agent(cwd="/other")) is False


def test_validate_agent_run_session_none(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session=None)
    assert runtime_operation_api.validate_agent_run(_principal_agent()) is False


def test_validate_agent_run_provider_mismatch(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session={"provider_id": "other"})
    assert runtime_operation_api.validate_agent_run(_principal_agent()) is False


def test_validate_agent_run_provider_empty_ok(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session={"provider_id": ""})
    assert runtime_operation_api.validate_agent_run(_principal_agent()) is True


def test_validate_agent_run_ok(monkeypatch):
    _seed_run(_RUN)
    _patch_session_and_node(monkeypatch, session={"provider_id": "prov-one"})
    assert runtime_operation_api.validate_agent_run(_principal_agent()) is True


# ---------- validate_node_relay ----------


def _principal_node(node_id="node-1", app_session_id="sess-one"):
    return RuntimePrincipal(
        kind=PrincipalKind.NODE_RELAY, principal_id=node_id, issuer="broker",
        audience="better-agent-operation-runtime", permitted_operations=("runtime_read",),
        permitted_resources=(), grant_generation="gen-1", availability_generation="gen-1",
        issued_at=1.0, expires_at=2.0, app_session_id=app_session_id, node_id=node_id, cwd="/proj")


def test_validate_node_relay_wrong_kind():
    principal = RuntimePrincipal(
        kind=PrincipalKind.AGENT_RUN, principal_id=_RUN, issuer="broker",
        audience="better-agent-operation-runtime", permitted_operations=("runtime_read",),
        permitted_resources=(), grant_generation="gen-1", availability_generation="gen-1",
        issued_at=1.0, expires_at=2.0, app_session_id="sess-one", run_id=_RUN, cwd="/proj")
    assert runtime_operation_api.validate_node_relay(principal) is False


def test_validate_node_relay_no_node_id():
    principal = RuntimePrincipal(
        kind=PrincipalKind.NODE_RELAY, principal_id="node-1", issuer="broker",
        audience="better-agent-operation-runtime", permitted_operations=("runtime_read",),
        permitted_resources=(), grant_generation="gen-1", availability_generation="gen-1",
        issued_at=1.0, expires_at=2.0, app_session_id="sess-one", node_id="", cwd="/proj")
    assert runtime_operation_api.validate_node_relay(principal) is False


def test_validate_node_relay_disconnected(monkeypatch):
    _patch_session_and_node(monkeypatch, connection=None)
    assert runtime_operation_api.validate_node_relay(_principal_node()) is False


def test_validate_node_relay_session_none(monkeypatch):
    _patch_session_and_node(monkeypatch, session=None, connection=object())
    assert runtime_operation_api.validate_node_relay(_principal_node()) is False


def test_validate_node_relay_node_mismatch(monkeypatch):
    _patch_session_and_node(monkeypatch, session={"node_id": "other"}, connection=object())
    assert runtime_operation_api.validate_node_relay(_principal_node()) is False


def test_validate_node_relay_default_primary_match(monkeypatch):
    # session without node_id defaults to "primary" -> mismatch with node-1
    _patch_session_and_node(monkeypatch, session={}, connection=object())
    assert runtime_operation_api.validate_node_relay(_principal_node("node-1")) is False


def test_validate_node_relay_ok(monkeypatch):
    _patch_session_and_node(monkeypatch, session={"node_id": "node-1"}, connection=object())
    assert runtime_operation_api.validate_node_relay(_principal_node()) is True


# ---------- handle() ----------


def _catalog_with(monkeypatch, *, ops, generation="gen-1"):
    descriptors = {
        "runtime_read": _descriptor("runtime_read"),
        "runtime_durable": _descriptor("runtime_durable", durable=True),
        "coordination_compat": _descriptor(
            "coordination_compat", side_effect=operation_catalog.SideEffectClass.COMPATIBILITY),
        "custom_op": _descriptor("custom_op"),
    }
    present = {k: descriptors[k] for k in ops}
    return _catalog(monkeypatch, descriptors=present, generation=generation), descriptors


def test_handle_catalog(monkeypatch):
    catalog, _ = _catalog_with(monkeypatch, ops=["runtime_read", "custom_op", "coordination_compat"])
    _patch_handle_collabs(monkeypatch)
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="catalog", operation="", generation="gen-1")))
    assert result["success"] is True
    assert result["generation"] == "gen-1"
    # only the maintained-prefix non-compatibility op is available
    assert set(result["schema"].keys()) == {"runtime_read"}
    assert result["schema"]["runtime_read"] == {"k": 1}


def test_handle_operation_not_available(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    with pytest.raises(PermissionError, match="not available"):
        asyncio.run(runtime_operation_api.handle(
            _envelope(kind="invoke", operation="custom_op", generation="gen-1")))


def test_handle_generation_mismatch(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    with pytest.raises(RuntimeError, match="generation changed"):
        asyncio.run(runtime_operation_api.handle(
            _envelope(kind="invoke", operation="runtime_read", generation="stale")))


def test_handle_invoke_non_durable(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    client = _patch_handle_collabs(monkeypatch, scoped_client_cls=_RecordingClient)
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_read", payload={"x": 1})))
    assert result == {"success": True, "result": {"invoked": "runtime_read", "payload": {"x": 1}}}


def test_handle_invoke_non_durable_payload_default(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch, scoped_client_cls=_RecordingClient)
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_read", payload=None)))
    assert result["result"]["payload"] == {}


def test_handle_invoke_durable_missing_request_id(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_durable"])
    _patch_handle_collabs(monkeypatch)
    with pytest.raises(ValueError, match="request_id is required"):
        asyncio.run(runtime_operation_api.handle(
            _envelope(kind="invoke", operation="runtime_durable", request_id="")))


def test_handle_invoke_durable_complete(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_durable"])
    fake_req = _patch_handle_collabs(monkeypatch)
    fake_req.admit = lambda **kw: {"ready": True, "status": "complete", "result": {"value": "ok"}}
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_durable", request_id="req-1")))
    assert result == {"success": True, "result": "ok"}


def test_handle_invoke_durable_artifact_error(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_durable"])

    def boom(**kw):
        raise operation_catalog.OperationArtifactError("stale")
    _patch_handle_collabs(monkeypatch, admit=boom)
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_durable", request_id="req-1")))
    assert result == {"success": False, "error": "stale"}


def test_handle_invoke_durable_not_ready_await_task(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_durable"])
    awaited = []

    async def fake_task():
        awaited.append(True)

    _patch_handle_collabs(
        monkeypatch,
        admit=lambda **kw: {"ready": False},
        get_active=lambda *a, **k: fake_task(),
    )
    # post-task get() returns a complete response
    monkeypatch.setattr(runtime_operation_api.operation_requests, "get",
                        lambda **kw: {"ready": True, "status": "complete", "result": "late"})
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_durable", request_id="req-1")))
    assert awaited == [True]
    assert result == {"success": True, "result": "late"}


def test_handle_invoke_durable_not_ready_task_raises_swallowed(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_durable"])

    async def boom_task():
        raise RuntimeError("boom")

    _patch_handle_collabs(
        monkeypatch,
        admit=lambda **kw: {"ready": False},
        get_active=lambda *a, **k: boom_task(),
    )
    # get after the awaited-but-failed task still returns not-ready
    monkeypatch.setattr(runtime_operation_api.operation_requests, "get",
                        lambda **kw: {"ready": False})
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_durable", request_id="req-1")))
    # task raised and was swallowed; response stays not-ready -> still running
    assert result == {"success": False, "error": "durable operation is still running"}


def test_handle_invoke_durable_not_ready_no_task(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_durable"])
    _patch_handle_collabs(
        monkeypatch,
        admit=lambda **kw: {"ready": False},
        get_active=lambda *a, **k: None,
    )
    monkeypatch.setattr(runtime_operation_api.operation_requests, "get",
                        lambda **kw: None)
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="invoke", operation="runtime_durable", request_id="req-1")))
    # get() returned None -> response stays {"ready": False} -> still running
    assert result == {"success": False, "error": "durable operation is still running"}


def test_handle_status_missing_request_id(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    with pytest.raises(ValueError, match="request_id is required"):
        asyncio.run(runtime_operation_api.handle(
            _envelope(kind="status", operation="runtime_read", request_id="")))


def test_handle_status_not_found(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    monkeypatch.setattr(runtime_operation_api.operation_requests, "get", lambda **kw: None)
    with pytest.raises(KeyError, match="does not exist"):
        asyncio.run(runtime_operation_api.handle(
            _envelope(kind="status", operation="runtime_read", request_id="req-1")))


def test_handle_status_ok(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    monkeypatch.setattr(runtime_operation_api.operation_requests, "get",
                        lambda **kw: {"ready": True, "status": "complete", "result": "r"})
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="status", operation="runtime_read", request_id="req-1")))
    assert result == {"success": True, "result": {"ready": True, "status": "complete", "result": "r"}}


def test_handle_cancel(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    monkeypatch.setattr(runtime_operation_api.operation_requests, "cancel",
                        lambda **kw: {"cancelled": True})
    result = asyncio.run(runtime_operation_api.handle(
        _envelope(kind="cancel", operation="runtime_read", request_id="req-1")))
    assert result == {"success": True, "result": {"cancelled": True}}


def test_handle_unsupported_kind(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    with pytest.raises(ValueError, match="unsupported"):
        asyncio.run(runtime_operation_api.handle(
            _envelope(kind="bogus", operation="runtime_read", request_id="req-1")))


def test_handle_envelope_extra_forbid_rejected(monkeypatch):
    _catalog_with(monkeypatch, ops=["runtime_read"])
    _patch_handle_collabs(monkeypatch)
    raw = _envelope(kind="catalog")
    raw["unexpected"] = "field"
    with pytest.raises(Exception):
        asyncio.run(runtime_operation_api.handle(raw))


# ---------- module-level validator registration ----------


def test_validators_registered():
    assert operation_authority._VALIDATORS[PrincipalKind.AGENT_RUN] is runtime_operation_api.validate_agent_run
    assert operation_authority._VALIDATORS[PrincipalKind.NODE_RELAY] is runtime_operation_api.validate_node_relay
