#!/usr/bin/env python3
"""Dedicated unit coverage for headless_admission.py.

The indirect tests (test_headless_caller_admission, test_internal_orchestration_delegation,
test_task_pipeline) monkeypatch the four module-level collaborator hooks
(_session_snapshot / _provider_record / _provider_capabilities / _session_executor),
which means the hook BODIES and the executor node-routing branches never execute under
them. This file drives those bodies plus the validation/admit/result branches directly.

Collaborator strategy:
- session_manager / extension_store / provider_remote are heavy and import-fragile on
  some hosts, so lightweight stand-ins are installed into sys.modules; the helper code
  under test resolves them via `from X import Y` at call time and picks up the stand-in.
- topology / provider / config_store / runtime_profile import cleanly and are patched
  in place via monkeypatch.
Every test asserts the real observable outcome of the branch, never just execution.
"""
from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.anyio

_TEST_HOME = tempfile.mkdtemp(prefix="ba-headless-admission-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(_TEST_HOME)

import headless_admission  # noqa: E402
import config_store  # noqa: E402
import provider as provider_module  # noqa: E402
import runtime_profile  # noqa: E402
import topology  # noqa: E402
from headless_request_contract import HeadlessAdmissionError  # noqa: E402

_PERMISSION_SCOPE = "internal_generation"


def _provider_record() -> dict:
    return {
        "id": "provider-1",
        "kind": "claude",
        "generation": str(uuid.uuid4()),
        "revision": 3,
        "execution_revision": 2,
    }


def _session_record(node_id: str = "primary") -> dict:
    return {
        "id": "session-1",
        "provider_id": "provider-1",
        "model": "claude-model",
        "reasoning_effort": "high",
        "runner": "native",
        "node_id": node_id,
        "cwd": "/tmp/project",
        "agent_session_id": "agent-parent",
    }


class _FakeExecutor:
    supports_fork = True
    supports_headless_no_tools = True
    suspended = False
    defunct = False

    def __init__(self, result: object = {"result": "ok"}) -> None:
        self._result = result
        self.calls: list = []

    async def run_admitted_headless(self, admitted) -> object:
        self.calls.append(admitted)
        return self._result


def _install_session_manager(monkeypatch, record: dict | None) -> SimpleNamespace:
    """Inject a fake session_manager module exposing `manager.get_fields`."""
    captured = SimpleNamespace(fields=None)

    class _Manager:
        def get_fields(self, _sid: str, fields) -> dict | None:
            captured.fields = tuple(fields)
            return None if record is None else dict(record)

    fake_module = SimpleNamespace(manager=_Manager())
    monkeypatch.setitem(sys.modules, "session_manager", fake_module)
    return captured


def _install_extension_stores(monkeypatch, *, not_ready_message) -> SimpleNamespace:
    """Inject fake extension_store + provider_remote for the remote-node path."""
    captured = SimpleNamespace(role=None, proxy_node=None, proxy_return=object())

    def _extension_id_for_role(role: str) -> str:
        captured.role = role
        return "ext-machine-nodes"

    def _get_proxy(node_id: str):
        captured.proxy_node = node_id
        return captured.proxy_return

    fake_ext = SimpleNamespace(
        runtime_not_ready_message=lambda _ext_id: not_ready_message,
        extension_id_for_role=_extension_id_for_role,
    )
    fake_remote = SimpleNamespace(get_proxy=_get_proxy)
    monkeypatch.setitem(sys.modules, "extension_store", fake_ext)
    monkeypatch.setitem(sys.modules, "provider_remote", fake_remote)
    return captured


# ---------------------------------------------------------------------------
# _session_snapshot / _provider_record / _provider_capabilities bodies
# ---------------------------------------------------------------------------


def test_session_snapshot_reads_exact_field_set(monkeypatch):
    record = _session_record()
    captured = _install_session_manager(monkeypatch, record)

    result = headless_admission._session_snapshot("session-1")

    assert result == record
    assert set(captured.fields) == set(headless_admission._SESSION_FIELDS)


def test_session_snapshot_returns_none_when_missing(monkeypatch):
    _install_session_manager(monkeypatch, None)

    assert headless_admission._session_snapshot("nope") is None


def test_provider_record_delegates_to_config_store(monkeypatch):
    record = _provider_record()
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: dict(record))

    assert headless_admission._provider_record("provider-1") == record


def test_provider_record_returns_none_when_unknown(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: None)

    assert headless_admission._provider_record("unknown") is None


def test_provider_capabilities_returns_resolved_executor(monkeypatch):
    executor = _FakeExecutor()
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: executor)

    assert headless_admission._provider_capabilities("provider-1", "native") is executor


# ---------------------------------------------------------------------------
# _session_executor — node routing + provider liveness branches
# ---------------------------------------------------------------------------


def test_executor_local_node_returns_live_provider(monkeypatch):
    executor = _FakeExecutor()
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: executor)

    resolved = headless_admission._session_executor(_session_record(), "provider-1", "native")

    assert resolved is executor


def test_executor_survives_topology_failure_and_falls_back_to_primary(monkeypatch):
    executor = _FakeExecutor()

    def _boom() -> str:
        raise OSError("topology unreachable")

    monkeypatch.setattr(topology, "local_node_id", _boom)
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: executor)

    # local_node_id() raises -> local_id becomes "primary"; primary session is local.
    resolved = headless_admission._session_executor(
        _session_record(node_id="primary"), "provider-1", "native"
    )

    assert resolved is executor


def test_executor_rejects_suspended_provider(monkeypatch):
    executor = _FakeExecutor()
    executor.suspended = True
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: executor)

    with pytest.raises(RuntimeError, match="provider provider-1 is suspended"):
        headless_admission._session_executor(_session_record(), "provider-1", "native")


def test_executor_rejects_defunct_provider(monkeypatch):
    executor = _FakeExecutor()
    executor.defunct = True
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: executor)

    with pytest.raises(RuntimeError, match="provider provider-1 is defunct"):
        headless_admission._session_executor(_session_record(), "provider-1", "native")


def test_executor_remote_node_proxies_when_ready(monkeypatch):
    captured = _install_extension_stores(monkeypatch, not_ready_message=None)
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")

    resolved = headless_admission._session_executor(
        _session_record(node_id="node-2"), "provider-1", "native"
    )

    assert resolved is captured.proxy_return
    assert captured.role == "machine-nodes"
    assert captured.proxy_node == "node-2"


def test_executor_remote_node_blocks_when_extension_not_ready(monkeypatch):
    _install_extension_stores(monkeypatch, not_ready_message="machine-nodes offline")
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")

    with pytest.raises(RuntimeError, match="machine-nodes offline"):
        headless_admission._session_executor(
            _session_record(node_id="node-2"), "provider-1", "native"
        )


# ---------------------------------------------------------------------------
# run_session_headless — validation, admission, and result branches
# ---------------------------------------------------------------------------


def _wire_run_path(monkeypatch, *, executor: _FakeExecutor) -> None:
    """Install every collaborator run_session_headless touches (happy path)."""
    _install_session_manager(monkeypatch, _session_record())
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: _provider_record())
    monkeypatch.setattr(runtime_profile, "resolve_runner", lambda _record, _runner: "native")
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: executor)


def _run(monkeypatch, **overrides):
    defaults = dict(
        prompt="prompt",
        fork=False,
        resume=False,
        no_tools=False,
        timeout=30,
        permission_scope=_PERMISSION_SCOPE,
    )
    defaults.update(overrides)
    return headless_admission.run_session_headless("session-1", **defaults)


async def test_run_happy_path_admits_and_executes(monkeypatch):
    executor = _FakeExecutor(result={"result": "ok"})
    _wire_run_path(monkeypatch, executor=executor)

    result = await _run(monkeypatch)

    assert result == {"result": "ok"}
    assert len(executor.calls) == 1
    admitted = executor.calls[0].to_dict()
    assert admitted["owner"] == {"kind": "session", "id": "session-1"}
    assert admitted["provider"]["execution_revision"] == 2
    assert "revision" not in admitted["provider"]
    assert admitted["fork"] is False


async def test_run_rejects_invalid_permission_scope(monkeypatch):
    _wire_run_path(monkeypatch, executor=_FakeExecutor())

    with pytest.raises(ValueError, match="permission scope is invalid"):
        await _run(monkeypatch, permission_scope="not-a-real-scope")


async def test_run_rejects_non_bool_execution_flags(monkeypatch):
    _wire_run_path(monkeypatch, executor=_FakeExecutor())

    with pytest.raises(ValueError, match="execution flags must be booleans"):
        await _run(monkeypatch, fork=1)  # type: ignore[arg-type]


async def test_run_rejects_fork_and_resume_together(monkeypatch):
    _wire_run_path(monkeypatch, executor=_FakeExecutor())

    with pytest.raises(ValueError, match="fork and resume are mutually exclusive"):
        await _run(monkeypatch, fork=True, resume=True)


async def test_run_rejects_missing_session(monkeypatch):
    _install_session_manager(monkeypatch, None)
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: _provider_record())

    with pytest.raises(ValueError, match="session is unavailable"):
        await _run(monkeypatch)


async def test_run_rejects_missing_provider_authority(monkeypatch):
    _install_session_manager(monkeypatch, _session_record())
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: None)

    with pytest.raises(ValueError, match="provider authority is unavailable"):
        await _run(monkeypatch)


async def test_run_rejects_stale_provider_selector(monkeypatch):
    _wire_run_path(monkeypatch, executor=_FakeExecutor())

    with pytest.raises(ValueError, match="provider selector is stale"):
        await _run(monkeypatch, expected_provider_id="wrong-provider")


async def test_run_rejects_stale_model_selector(monkeypatch):
    _wire_run_path(monkeypatch, executor=_FakeExecutor())

    with pytest.raises(ValueError, match="model selector is stale"):
        await _run(monkeypatch, expected_model="wrong-model")


async def test_run_requires_provider_session_id_for_fork(monkeypatch):
    record = _session_record()
    record["agent_session_id"] = ""
    _install_session_manager(monkeypatch, record)
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: _provider_record())
    monkeypatch.setattr(runtime_profile, "resolve_runner", lambda _r, _rr: "native")
    monkeypatch.setattr(provider_module, "get_provider", lambda *_a: _FakeExecutor())

    with pytest.raises(ValueError, match="provider session"):
        await _run(monkeypatch, fork=True)


async def test_run_rejects_session_with_empty_required_field(monkeypatch):
    record = _session_record()
    record["model"] = ""
    _install_session_manager(monkeypatch, record)
    monkeypatch.setattr(config_store, "get_provider", lambda _pid: _provider_record())

    with pytest.raises(ValueError, match="headless session model is unavailable"):
        await _run(monkeypatch)


async def test_run_translates_admission_error_to_value_error(monkeypatch):
    _wire_run_path(monkeypatch, executor=_FakeExecutor())

    def _deny(*_args, **_kwargs):
        raise HeadlessAdmissionError("scope not permitted for owner")

    monkeypatch.setattr(headless_admission, "admit_headless_request", _deny)

    with pytest.raises(ValueError, match="scope not permitted for owner"):
        await _run(monkeypatch)


async def test_run_rejects_non_dict_provider_result(monkeypatch):
    executor = _FakeExecutor(result="not-a-dict")
    _wire_run_path(monkeypatch, executor=executor)

    with pytest.raises(RuntimeError, match="provider result is invalid"):
        await _run(monkeypatch)
