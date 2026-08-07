"""Unit-tier coverage of the ask-fork router in internal_orchestration_api.py.

``internal_ask_fork`` is the fork engine behind ``ask(run_mode='fork')``: it
spawns a target run on a per-(caller, target) fork and returns the aggregate
result payload. The router's own logic is exercised for real — the authority
gate (including the defensive re-check), the durable core-mcp-job short-circuit,
input validation (missing worker_session_id, unknown session, bad ask_mode),
ask-mode normalization, the full ``run_delegation`` kwarg mapping, and the
``DelegateForkParentMissing`` race → 409 mapping.

Cross-subsystem collaborators the router does not own are faked: the internal
authority guard, the durable job dispatch (``internal_extension_api``), provider-id
resolution, session existence, the provisioned-profile / provision-prompt adapters,
and the injected coordinator's ``run_delegation``. Isolated ``BETTER_AGENT_HOME``
via ``_test_home.isolate``; fakes are restored on teardown so nothing bleeds into
other test modules in the shared pytest session.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-ask-fork-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402

import internal_orchestration_api  # noqa: E402
import internal_extension_api  # noqa: E402
import internal_guards  # noqa: E402
import _test_authority  # noqa: E402
from session_manager import DelegateForkParentMissing  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _AsyncFake:
    """Async callable that records every invocation, returns a configurable
    result, and optionally raises. One shape covers the coordinator dispatch,
    the durable-job short-circuit, and the resolver stubs."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls: list = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeCoordinator:
    """Coordinator stand-in exposing only what the fork router uses."""

    def __init__(self):
        self.run_delegation = _AsyncFake({"jsonl_path": "/run.jsonl", "ready": True})


async def _noop(*_a, **_k):
    return None


class _Bindings:
    def __init__(self, coord, durable, session_exists, resolve):
        self.coord = coord
        self.durable = durable
        self.session_exists = session_exists
        self.resolve = resolve


@pytest.fixture
def configured(monkeypatch):
    """Bind a fake coordinator, stub durable-job dispatch + provider/session
    resolvers + the authority guard, and yield controllable fakes. Prior
    bindings are restored on teardown so this file does not disturb other
    test modules."""
    coord = _FakeCoordinator()
    prior_coord = internal_orchestration_api._coordinator_ref
    internal_orchestration_api.configure(
        coordinator=coord,
        delete_session_tree=_noop,
        provision_workers=_noop,
    )

    session_exists = _AsyncFake(True)
    resolve = _AsyncFake("prov-resolved")
    durable = _AsyncFake(None)

    monkeypatch.setattr(internal_orchestration_api, "_session_exists", session_exists)
    monkeypatch.setattr(internal_orchestration_api, "_resolve_provider_id_ref", resolve)
    monkeypatch.setattr(
        internal_orchestration_api,
        "_api_optional_provisioned_tool_profile",
        lambda value, body: ("PROFILE", value),
    )
    monkeypatch.setattr(
        internal_orchestration_api,
        "_api_optional_provision_prompt",
        lambda value: ("PROMPT", value),
    )
    monkeypatch.setattr(internal_extension_api, "maybe_run_core_mcp_job", durable)

    with _test_authority.stub_internal_authority():
        yield _Bindings(coord, durable, session_exists, resolve)

    internal_orchestration_api._coordinator_ref = prior_coord


def _fork(body):
    return internal_orchestration_api.internal_ask_fork(body, x_internal_token="x")


def _full_body(**overrides):
    body = {
        "worker_session_id": "worker-1",
        "app_session_id": "app-1",
        "instructions": "do the thing",
        "worker_description": "a worker",
        "provider_id": "prov",
        "model": "m",
        "reasoning_effort": "high",
        "runner": "native",
        "cwd": "/cwd",
        "justification": "because",
        "proposed_orchestration_mode": "team",
        "client_delegation_id": "cdel",
        "node_id": "n1",
        "run_mode": "fork",
        "worker_registry_cwd": "/reg",
        "ephemeral": True,
        "machine_completion": True,
        "provision_prompt": "pp",
        "provisioned_tool_profile": "prof",
        "include_events": True,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Authority gate
# --------------------------------------------------------------------------- #


class TestAskForkAuthority:
    def test_unauthenticated_is_403(self):
        # No authority configured in the isolated home → require_role_internal
        # refuses before any work. Exercises the guard path, not the handler.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_fork({"worker_session_id": "w"}))
        assert exc.value.status_code == 403

    def test_defensive_authority_recheck_refuses_403(self, monkeypatch):
        # require_role_internal is bypassed but authority is still invalid in
        # the isolated home → the route's explicit authority re-check must
        # still refuse. Pins the defense-in-depth line independently of the
        # composed role guard.
        monkeypatch.setattr(internal_guards, "require_role_internal", lambda *_a, **_k: None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_fork({"worker_session_id": "w"}))
        assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# Durable core-mcp-job short-circuit
# --------------------------------------------------------------------------- #


class TestAskForkDurableShortCircuit:
    def test_durable_job_result_returned_directly(self, configured):
        # When the durable job layer has a result, the router returns it
        # without invoking the coordinator's fork path.
        configured.durable.result = {"success": True, "id": "job-1", "status": "complete"}
        result = asyncio.run(_fork(_full_body()))
        assert result == {"success": True, "id": "job-1", "status": "complete"}
        assert configured.coord.run_delegation.calls == []


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


class TestAskForkValidation:
    def test_missing_worker_session_id_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_fork(_full_body(worker_session_id=None)))
        assert exc.value.status_code == 400
        assert "worker_session_id" in exc.value.detail

    def test_whitespace_worker_session_id_is_stripped(self, configured):
        # The missing-id guard checks truthiness BEFORE stripping, so a
        # whitespace-only id is not rejected at the guard; it is stripped to
        # "" and resolved. Pins the strip ordering.
        asyncio.run(_fork(_full_body(worker_session_id="   worker-1   ")))
        assert configured.session_exists.calls[0][0][0] == "worker-1"
        kwargs = configured.coord.run_delegation.calls[0][1]
        assert kwargs["worker_session_id"] == "worker-1"

    def test_unknown_worker_session_is_404(self, configured):
        configured.session_exists.result = False
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_fork(_full_body()))
        assert exc.value.status_code == 404

    def test_bad_ask_mode_is_400(self, configured, monkeypatch):
        def _raise(mode, run_mode):
            raise ValueError(f"bad ask_mode {mode!r}")

        monkeypatch.setattr(internal_orchestration_api, "normalize_ask_execution", _raise)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_fork(_full_body(ask_mode="nope")))
        assert exc.value.status_code == 400
        assert "nope" in exc.value.detail


# --------------------------------------------------------------------------- #
# Dispatch + full kwarg mapping
# --------------------------------------------------------------------------- #


class TestAskForkDispatch:
    def test_full_kwarg_mapping_no_ask_mode(self, configured):
        result = asyncio.run(_fork(_full_body()))
        assert result == {"jsonl_path": "/run.jsonl", "ready": True}

        kwargs = configured.coord.run_delegation.calls[0][1]
        assert kwargs["app_session_id"] == "app-1"
        assert kwargs["instructions"] == "do the thing"
        assert kwargs["worker_session_id"] == "worker-1"
        assert kwargs["worker_description"] == "a worker"
        assert kwargs["provider_id"] == "prov-resolved"
        assert kwargs["model"] == "m"
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["runner"] == "native"
        assert kwargs["cwd"] == "/cwd"
        assert kwargs["justification"] == "because"
        assert kwargs["proposed_orchestration_mode"] == "team"
        assert kwargs["client_delegation_id"] == "cdel"
        assert kwargs["node_id"] == "n1"
        assert kwargs["run_mode"] == "fork"
        assert kwargs["worker_registry_cwd"] == "/reg"
        assert kwargs["ephemeral"] is True
        assert kwargs["machine_completion"] is True
        assert kwargs["include_events"] is True
        assert kwargs["ask_mode"] == ""
        assert kwargs["provision_prompt"] == ("PROMPT", "pp")
        assert kwargs["provisioned_tool_profile"] == ("PROFILE", "prof")

        # worker_session_id is resolved against the stripped id actually present.
        assert configured.session_exists.calls[0][0][0] == "worker-1"
        # provider_id ref is the raw (pre-resolution) value from the body.
        assert configured.resolve.calls[0][0][0] == "prov"

    def test_empty_optional_fields_default_and_coerce(self, configured):
        # Omit every optional field: defaults apply, strict `is True` coercions
        # fall to False for non-True values, run_mode falls back to "fork".
        body = {
            "worker_session_id": "worker-1",
            "app_session_id": "app-1",
            "instructions": "i",
            "model": "m",
            "cwd": "/c",
            "provider_id": "",
            "ephemeral": "yes",
            "machine_completion": 1,
            "include_events": "true",
            "run_mode": None,
        }
        asyncio.run(_fork(body))
        kwargs = configured.coord.run_delegation.calls[0][1]
        assert kwargs["run_mode"] == "fork"
        assert kwargs["worker_description"] == ""
        assert kwargs["reasoning_effort"] == ""
        assert kwargs["runner"] == ""
        assert kwargs["justification"] is None
        assert kwargs["proposed_orchestration_mode"] is None
        assert kwargs["client_delegation_id"] is None
        assert kwargs["node_id"] is None
        assert kwargs["worker_registry_cwd"] is None
        assert kwargs["ephemeral"] is False
        assert kwargs["machine_completion"] is False
        assert kwargs["include_events"] is False
        assert kwargs["ask_mode"] == ""

    def test_valid_ask_mode_is_normalized(self, configured, monkeypatch):
        monkeypatch.setattr(
            internal_orchestration_api,
            "normalize_ask_execution",
            lambda mode, run_mode: ("wait", "fork"),
        )
        asyncio.run(_fork(_full_body(ask_mode="wait")))
        kwargs = configured.coord.run_delegation.calls[0][1]
        assert kwargs["ask_mode"] == "wait"

    def test_parent_missing_race_is_409(self, configured):
        # The parent agent session vanished between the existence check and
        # fork creation → the typed DelegateForkParentMissing is mapped to 409
        # rather than surfacing as a bare 500.
        configured.coord.run_delegation.exc = DelegateForkParentMissing("parent-1")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_fork(_full_body()))
        assert exc.value.status_code == 409
        assert "no longer available" in exc.value.detail

    def test_unrelated_keyerror_propagates(self, configured):
        # Only the typed DelegateForkParentMissing subclass is mapped to 409;
        # a plain KeyError must still propagate as a real error, not be swallowed.
        configured.coord.run_delegation.exc = KeyError("something-else")
        with pytest.raises(KeyError):
            asyncio.run(_fork(_full_body()))
