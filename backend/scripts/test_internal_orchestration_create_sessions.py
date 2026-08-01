"""Unit-tier coverage of the session-factory routers in
internal_orchestration_api.py:

- internal_create_worker      (/api/internal/create-worker)
- internal_create_session     (/api/internal/create-session)
- internal_create_sub_session (/api/internal/create-sub-session)

These three are the last uncovered chunk of the file. They share a common
shape: validate the request, resolve provider/model/runner/reasoning-effort
through the profile-selector + sender/parent inheritance chain, then mint a
session (via session_manager.create / create_sub_session) or a worker (via the
injected coordinator) and apply folder/tag organization.

The router's own validation/branching/resolution logic is exercised for real;
only the cross-subsystem collaborators it does not own are faked — the
internal-token authority guard (shared stub), the provider/model/runner/effort
resolution helpers, config_store lookups, the terminal
session_manager.create / create_sub_session, the injected coordinator, the
organization hook, and the extension-ownership claim. This keeps tests focused
on this router's logic without spinning a coordinator, a real session, or a
provider runtime.

Isolated BETTER_AGENT_HOME via _test_home.isolate; collaborators are fakes
restored on teardown so nothing bleeds into other test modules in the run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-create-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402

import config_store  # noqa: E402
import extension_store  # noqa: E402
import harness_profile_resolver  # noqa: E402
import internal_guards  # noqa: E402
import internal_orchestration_api  # noqa: E402
import _test_authority  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes + helpers
# --------------------------------------------------------------------------- #


def _noop(*_a, **_k):
    return None


async def _noop_async(*_a, **_k):
    return None


def async_recorder(sink, result):
    """Build an async callable that appends each call's args to `sink` and
    returns `result`."""

    async def _fn(*args, **kwargs):
        sink.append((args, kwargs))
        return result

    return _fn


def _session(sid):
    """A session record shaped like what session_manager.create returns, with
    every key the router's response builders read."""
    return {
        "id": sid,
        "name": "n",
        "cwd": "/c",
        "orchestration_mode": "native",
        "node_id": "primary",
        "provider_id": "prov-a",
        "model": "m",
        "runner": "native",
        "reasoning_effort": "low",
        "bare_config": False,
        "capability_contexts": [],
        "harness_profile_id": "",
        "disallowed_tools": [],
        "disabled_builtin_extensions": [],
        "extra_mcp_servers": [],
    }


class _SyncRecorder:
    """Sync callable that records invocations; used for the terminal
    session_manager.create / create_sub_session which the router runs in a
    thread via asyncio.to_thread."""

    def __init__(self, result):
        self.calls: list = []
        self.result = result
        self.raises = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


class _FakeSessionManager:
    """Stand-in for the session_manager singleton bound into the router."""

    def __init__(self):
        self.create = _SyncRecorder(_session("sess-1"))
        self.create_sub_session = _SyncRecorder(_session("sub-1"))


class _Recorder:
    """Async callable that records every invocation and returns a fixed result."""

    def __init__(self, result):
        self.calls: list = []
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _FakeCoordinator:
    """Coordinator stand-in exposing only what the factory routers use."""

    def __init__(self):
        self.create_worker_for_session = _Recorder(
            {"success": True, "worker_session_id": "worker-1"}
        )
        self.emit_session_created_panel = _Recorder(None)


class _Bindings:
    def __init__(self, coord, sessions):
        self.coord = coord
        self.sessions = sessions


def _merge_factory(runtime_profile_id=None):
    """merge_selector_defaults stand-in: identity over the selector keys the
    routers read, with an optional injected runtime_profile_id pin so the
    create-session profile-pin branch (L849-859) is reachable on demand."""

    def _merge(explicit, _profile_id, _revision=None):
        return {
            "provider_id": explicit.get("provider_id"),
            "model": explicit.get("model"),
            "reasoning_effort": explicit.get("reasoning_effort"),
            "runtime_profile_id": runtime_profile_id,
        }

    return _merge


def _stub_resolution(
    monkeypatch,
    *,
    provider_ref="",
    sender=None,
    provider_rec=None,
    runtime_profile=None,
    default_model="default-m",
    required_model="",
    prefill_model="",
    runner="native",
    provider_effort="medium",
    inherited_effort="low",
    merge=None,
):
    """Stub the provider/model/runner/effort resolution chain + the
    session-organization extraction the routers delegate to. Each collaborator
    belongs to its own module's coverage; stubbing it isolates this router's
    branching for direct assertion."""
    async def _resolve(ref):
        return provider_ref

    async def _lite(_sid):
        return sender

    async def _org(_body):
        return (None, [])

    monkeypatch.setattr(internal_orchestration_api, "_resolve_provider_id_ref", _resolve)
    monkeypatch.setattr(internal_orchestration_api, "_session_lite", _lite)
    monkeypatch.setattr(
        internal_orchestration_api, "_initial_session_organization_from_body", _org
    )
    monkeypatch.setattr(internal_orchestration_api, "_validate_provider_model", lambda *_a: None)
    monkeypatch.setattr(
        internal_orchestration_api,
        "_required_model_from_body_or_provider",
        lambda *_a: required_model,
    )
    monkeypatch.setattr(internal_orchestration_api, "profile_prefill_model", lambda *_a: prefill_model)
    # Echo runner_input so the router's runner-inheritance / runtime-profile-pin
    # branching is reflected in what reaches session_manager; "runner" is the
    # fallback when no input is supplied.
    def _runner(_pid, runner_input=None, **_k):
        return runner_input or runner

    monkeypatch.setattr(internal_orchestration_api, "_provider_runner", _runner)
    monkeypatch.setattr(internal_orchestration_api, "_provider_reasoning_effort", lambda *_a: provider_effort)
    monkeypatch.setattr(internal_orchestration_api, "_inherited_reasoning_effort", lambda *_a: inherited_effort)
    monkeypatch.setattr(harness_profile_resolver, "merge_selector_defaults", merge or _merge_factory())
    monkeypatch.setattr(config_store, "get_provider", lambda _id: provider_rec)
    monkeypatch.setattr(config_store, "get_runtime_profile", lambda _id: runtime_profile)
    monkeypatch.setattr(config_store, "default_session_model", lambda: default_model)


def _stub_worker_chain(monkeypatch, sender=None):
    """Lightweight stubs for the create-worker path: it only needs session-lite
    (the caller), selector merge, and the organization extraction."""
    async def _lite(_sid):
        return sender

    async def _org(_body):
        return (None, [])

    monkeypatch.setattr(internal_orchestration_api, "_session_lite", _lite)
    monkeypatch.setattr(
        internal_orchestration_api, "_initial_session_organization_from_body", _org
    )
    monkeypatch.setattr(harness_profile_resolver, "merge_selector_defaults", _merge_factory())


@pytest.fixture
def configured(monkeypatch):
    """Bind a fake coordinator + fake session_manager, stub the authority guard
    and the builtin-runtime-extension gate, and stub the organization hook to a
    no-op. Restore prior bindings on teardown so this file does not disturb
    other test modules."""
    prior_builtin = internal_guards.require_builtin_runtime_extension
    prior_org = internal_orchestration_api._apply_initial_session_organization
    prior_sm = internal_orchestration_api.session_manager
    prior_coord = internal_orchestration_api._coordinator_ref

    coord = _FakeCoordinator()
    sessions = _FakeSessionManager()
    internal_orchestration_api.configure(
        coordinator=coord,
        delete_session_tree=_noop,
        provision_workers=_noop,
    )
    internal_orchestration_api.session_manager = sessions
    internal_orchestration_api._apply_initial_session_organization = _noop_async
    internal_guards.require_builtin_runtime_extension = lambda *_a, **_k: None
    monkeypatch.setattr(extension_store, "is_extension_active", lambda _id: False)
    with _test_authority.stub_internal_authority():
        yield _Bindings(coord, sessions)
    internal_guards.require_builtin_runtime_extension = prior_builtin
    internal_orchestration_api._apply_initial_session_organization = prior_org
    internal_orchestration_api.session_manager = prior_sm
    internal_orchestration_api._coordinator_ref = prior_coord


# ============================================================ create-worker


def _create_worker(body):
    return internal_orchestration_api.internal_create_worker(body, x_internal_token="x")


class TestCreateWorker:
    def test_unauthenticated_is_403(self):
        # require_builtin_runtime_extension is stubbed away but authority is
        # invalid in the isolated home → 403 before any dispatch.
        original = internal_guards.require_builtin_runtime_extension
        internal_guards.require_builtin_runtime_extension = lambda *_a, **_k: None
        try:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(_create_worker({"app_session_id": "s"}))
            assert exc.value.status_code == 403
        finally:
            internal_guards.require_builtin_runtime_extension = original

    def test_model_pin_validates_against_caller(self, configured, monkeypatch):
        # A pinned model resolves the caller's provider and runs model validation.
        _stub_worker_chain(monkeypatch, sender={"provider_id": "prov-a"})
        validated = []
        monkeypatch.setattr(
            internal_orchestration_api,
            "_validate_provider_model",
            lambda *a: validated.append(a),
        )

        result = asyncio.run(_create_worker({"app_session_id": "mgr", "model": "sonnet"}))

        assert result["success"] is True
        assert validated == [("prov-a", "sonnet")]
        dispatched = configured.coord.create_worker_for_session.calls[0][1]
        assert dispatched["model"] == "sonnet"
        assert dispatched["app_session_id"] == "mgr"
        assert dispatched["worker_description"] == ""

    def test_no_model_skips_validation(self, configured, monkeypatch):
        # No model pin → no caller lookup, no validation; dispatched with "".
        lite = []
        monkeypatch.setattr(
            internal_orchestration_api,
            "_session_lite",
            async_recorder(lite, None),
        )
        async def _org(_body):
            return (None, [])

        monkeypatch.setattr(
            internal_orchestration_api, "_initial_session_organization_from_body", _org
        )
        monkeypatch.setattr(harness_profile_resolver, "merge_selector_defaults", _merge_factory())

        result = asyncio.run(_create_worker({"app_session_id": "mgr"}))

        assert lite == []  # caller never resolved
        dispatched = configured.coord.create_worker_for_session.calls[0][1]
        assert dispatched["model"] == ""

    def test_failed_provision_skips_organization(self, configured, monkeypatch):
        configured.coord.create_worker_for_session.result = {"success": False}
        _stub_worker_chain(monkeypatch)
        org = []
        monkeypatch.setattr(
            internal_orchestration_api,
            "_apply_initial_session_organization",
            async_recorder(org, None),
        )

        result = asyncio.run(_create_worker({"app_session_id": "mgr"}))

        assert result["success"] is False
        assert org == []


# ============================================================ create-session


def _create_session(body):
    return internal_orchestration_api.internal_create_session(body, x_internal_token="x")


class TestCreateSession:
    def test_unauthenticated_is_403(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_session({"name": "n", "cwd": "/c"}))
        assert exc.value.status_code == 403

    def test_missing_name_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_session({"cwd": "/c"}))
        assert exc.value.status_code == 400

    def test_missing_cwd_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_session({"name": "n"}))
        assert exc.value.status_code == 400

    def test_invalid_orchestration_mode_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_session({"name": "n", "cwd": "/c", "orchestration_mode": "weird"}))
        assert exc.value.status_code == 400

    def test_manager_mode_normalizes_to_team(self, configured, monkeypatch):
        # "manager" maps to "team", which passes through require_role_internal
        # (stubbed valid) and produces a team-mode session.
        _stub_resolution(monkeypatch)
        result = asyncio.run(
            _create_session({"name": "n", "cwd": "/c", "orchestration_mode": "manager"})
        )
        assert result["orchestration_mode"] == "team"
        assert configured.sessions.create.calls[0]["orchestration_mode"] == "team"

    def test_bare_config_non_bool_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_session({"name": "n", "cwd": "/c", "bare_config": "yes"}))
        assert exc.value.status_code == 400

    def test_invalid_capability_contexts_is_400(self, configured, monkeypatch):
        def _raise(_v):
            raise ValueError("bad context")

        monkeypatch.setattr(internal_orchestration_api, "normalize_capability_contexts", _raise)
        _stub_resolution(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_session({"name": "n", "cwd": "/c", "capability_contexts": "x"}))
        assert exc.value.status_code == 400
        assert "bad context" in exc.value.detail

    def test_runtime_profile_non_string_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session({"name": "n", "cwd": "/c", "runtime_profile_id": 123})
            )
        assert exc.value.status_code == 400

    def test_runtime_profile_blank_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session({"name": "n", "cwd": "/c", "runtime_profile_id": "   "})
            )
        assert exc.value.status_code == 400

    def test_runtime_profile_unknown_is_400(self, configured, monkeypatch):
        _stub_resolution(monkeypatch, runtime_profile=None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session({"name": "n", "cwd": "/c", "runtime_profile_id": "ghost"})
            )
        assert exc.value.status_code == 400
        assert "unknown or deleted" in exc.value.detail

    def test_runtime_profile_deleted_is_400(self, configured, monkeypatch):
        _stub_resolution(monkeypatch, runtime_profile={"deleted_at": "2026-01-01"})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session({"name": "n", "cwd": "/c", "runtime_profile_id": "dead"})
            )
        assert exc.value.status_code == 400

    def test_runtime_profile_provider_conflict_is_400(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            runtime_profile={"provider_id": "prov-a", "runner": "native"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session(
                    {
                        "name": "n",
                        "cwd": "/c",
                        "runtime_profile_id": "prof",
                        "provider_id": "prov-b",
                    }
                )
            )
        assert exc.value.status_code == 400
        assert "conflicts" in exc.value.detail

    def test_runtime_profile_runner_conflict_is_400(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            runtime_profile={"provider_id": "prov-a", "runner": "native"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session(
                    {
                        "name": "n",
                        "cwd": "/c",
                        "runtime_profile_id": "prof",
                        "runner": "other",
                    }
                )
            )
        assert exc.value.status_code == 400
        assert "conflicts" in exc.value.detail

    def test_runtime_profile_pins_provider_and_runner(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            provider_ref="prov-a",
            provider_rec={"name": "A"},
            runtime_profile={"provider_id": "prov-a", "runner": "better_agent_runner"},
            required_model="m",
        )
        result = asyncio.run(
            _create_session(
                {"name": "n", "cwd": "/c", "runtime_profile_id": "prof", "model": "m"}
            )
        )
        kw = configured.sessions.create.calls[0]
        assert kw["provider_id"] == "prov-a"
        assert kw["runner"] == "better_agent_runner"
        assert kw["runtime_profile_id"] == "prof"
        assert result["success"] is True

    def test_sender_session_missing_is_400(self, configured, monkeypatch):
        _stub_resolution(monkeypatch, sender=None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session({"name": "n", "cwd": "/c", "sender_session_id": "nope"})
            )
        assert exc.value.status_code == 400
        assert "does not exist" in exc.value.detail

    def test_unknown_provider_is_400(self, configured, monkeypatch):
        # provider resolves but config_store.get_provider returns None.
        _stub_resolution(monkeypatch, provider_ref="prov-x", provider_rec=None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_session({"name": "n", "cwd": "/c", "provider_id": "prov-x"})
            )
        assert exc.value.status_code == 400
        assert "does not exist" in exc.value.detail

    def test_requested_model_validates_and_creates(self, configured, monkeypatch):
        _stub_resolution(monkeypatch, provider_ref="prov-a", provider_rec={"name": "A"})
        validated = []
        monkeypatch.setattr(
            internal_orchestration_api,
            "_validate_provider_model",
            lambda *a: validated.append(a),
        )
        result = asyncio.run(
            _create_session({"name": "n", "cwd": "/c", "provider_id": "prov-a", "model": "sonnet"})
        )
        assert validated == [("prov-a", "sonnet")]
        assert configured.sessions.create.calls[0]["model"] == "sonnet"
        assert result["success"] is True

    def test_sender_inherits_provider_model_and_runner(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={
                "provider_id": "prov-a",
                "model": "inherited-m",
                "runner": "inherited-runner",
                "reasoning_effort": "high",
            },
            provider_rec={"name": "A"},
        )
        result = asyncio.run(
            _create_session({"name": "n", "cwd": "/c", "sender_session_id": "s1"})
        )
        kw = configured.sessions.create.calls[0]
        assert kw["provider_id"] == "prov-a"
        assert kw["model"] == "inherited-m"
        assert kw["runner"] == "inherited-runner"
        # No explicit effort → inherited reasoning-effort resolver used.
        assert kw["reasoning_effort"] == "low"  # _inherited_reasoning_effort stub
        # Sender present → session-created panel emitted.
        assert configured.coord.emit_session_created_panel.calls
        assert result["success"] is True

    def test_provider_default_model_prefill(self, configured, monkeypatch):
        # provider pinned, no model anywhere → _required_model_from_body_or_provider
        # supplies the provider's default model.
        _stub_resolution(
            monkeypatch,
            provider_ref="prov-a",
            provider_rec={"name": "A"},
            required_model="prov-default",
        )
        result = asyncio.run(
            _create_session({"name": "n", "cwd": "/c", "provider_id": "prov-a"})
        )
        assert configured.sessions.create.calls[0]["model"] == "prov-default"
        assert result["success"] is True

    def test_final_default_session_model(self, configured, monkeypatch):
        # No provider, no sender, no model → falls through to
        # config_store.default_session_model().
        _stub_resolution(monkeypatch, default_model="global-default")
        result = asyncio.run(_create_session({"name": "n", "cwd": "/c"}))
        assert configured.sessions.create.calls[0]["model"] == "global-default"
        assert result["success"] is True

    def test_provider_pinned_without_model_falls_to_global_default(
        self, configured, monkeypatch
    ):
        # provider pinned, no sender, provider exposes no resolvable model → the
        # router re-derives from the provider (still empty) then falls through to
        # the global default session model.
        _stub_resolution(
            monkeypatch,
            provider_ref="prov-a",
            provider_rec={"name": "A"},
            required_model="",
            default_model="global-default",
        )
        result = asyncio.run(
            _create_session({"name": "n", "cwd": "/c", "provider_id": "prov-a"})
        )
        assert configured.sessions.create.calls[0]["model"] == "global-default"
        assert result["success"] is True

    def test_requested_reasoning_effort(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            provider_ref="prov-a",
            provider_rec={"name": "A"},
            provider_effort="max",
        )
        asyncio.run(
            _create_session(
                {"name": "n", "cwd": "/c", "provider_id": "prov-a", "model": "m", "reasoning_effort": "high"}
            )
        )
        assert configured.sessions.create.calls[0]["reasoning_effort"] == "max"

    def test_profile_selectors_pin_runtime_profile(self, configured, monkeypatch):
        # No explicit runtime_profile_id, but merge_selector_defaults injects one
        # AND no provider/runner were set → the router adopts the pin and reads
        # the profile's runner.
        _stub_resolution(
            monkeypatch,
            merge=_merge_factory(runtime_profile_id="prof-auto"),
            runtime_profile={"provider_id": "prov-a", "runner": "better_agent_runner"},
            required_model="m",
            provider_ref="prov-a",
            provider_rec={"name": "A"},
        )
        asyncio.run(_create_session({"name": "n", "cwd": "/c", "model": "m"}))
        kw = configured.sessions.create.calls[0]
        assert kw["runtime_profile_id"] == "prof-auto"
        assert kw["runner"] == "better_agent_runner"

    def test_profile_runtime_pin_without_record_skips_runner(
        self, configured, monkeypatch
    ):
        # merge injects a runtime_profile_id but the profile record is missing
        # → the runner pin is skipped (pinned is None) and creation proceeds.
        _stub_resolution(
            monkeypatch,
            merge=_merge_factory(runtime_profile_id="ghost-prof"),
            runtime_profile=None,
            provider_ref="prov-a",
            provider_rec={"name": "A"},
            required_model="m",
        )
        asyncio.run(_create_session({"name": "n", "cwd": "/c", "model": "m"}))
        kw = configured.sessions.create.calls[0]
        assert kw["model"] == "m"

    def test_sender_runner_not_inherited_when_provider_differs(
        self, configured, monkeypatch
    ):
        # sender provider differs from the pinned provider → runner is NOT
        # inherited from the sender; _provider_runner supplies the default.
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "sender-runner"},
            provider_ref="prov-x",
            provider_rec={"name": "X"},
        )
        asyncio.run(
            _create_session(
                {
                    "name": "n",
                    "cwd": "/c",
                    "sender_session_id": "s1",
                    "provider_id": "prov-x",
                    "model": "m",
                }
            )
        )
        kw = configured.sessions.create.calls[0]
        assert kw["provider_id"] == "prov-x"
        assert kw["runner"] == "native"

    def test_extension_claims_session(self, configured, monkeypatch):
        # An active owning extension claims the freshly created session.
        _stub_resolution(monkeypatch)
        monkeypatch.setattr(internal_guards, "internal_authority_extension_id", lambda: "ext-1")
        monkeypatch.setattr(extension_store, "is_extension_active", lambda _id: True)
        claimed = []
        import extension_session_ownership

        monkeypatch.setattr(
            extension_session_ownership, "claim", lambda sid, eid: claimed.append((sid, eid))
        )
        asyncio.run(_create_session({"name": "n", "cwd": "/c"}))
        assert claimed and claimed[0][1] == "ext-1"

    def test_response_shape(self, configured, monkeypatch):
        _stub_resolution(monkeypatch)
        result = asyncio.run(_create_session({"name": "n", "cwd": "/c"}))
        for key in (
            "success", "session_id", "name", "cwd", "orchestration_mode",
            "node_id", "provider_id", "model", "runner", "reasoning_effort",
            "bare_config", "capability_contexts", "harness_profile_id",
        ):
            assert key in result


# ============================================================ create-sub-session


def _create_sub_session(body):
    return internal_orchestration_api.internal_create_sub_session(body, x_internal_token="x")


class TestCreateSubSession:
    def test_unauthenticated_is_403(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_sub_session({"sender_session_id": "p"}))
        assert exc.value.status_code == 403

    def test_missing_parent_is_400(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_sub_session({}))
        assert exc.value.status_code == 400
        assert "sender_session_id is required" in exc.value.detail

    def test_parent_missing_is_400(self, configured, monkeypatch):
        _stub_resolution(monkeypatch, sender=None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_sub_session({"sender_session_id": "ghost"}))
        assert exc.value.status_code == 400
        assert "does not exist" in exc.value.detail

    def test_unknown_provider_is_400(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_ref="prov-x",
            provider_rec=None,
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_sub_session({"sender_session_id": "p", "provider_id": "prov-x"})
            )
        assert exc.value.status_code == 400
        assert "does not exist" in exc.value.detail

    def test_pinned_provider_without_default_model_is_400(self, configured, monkeypatch):
        # provider pinned, no model, profile_prefill_model returns "" → 400
        # naming the provider.
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_ref="prov-x",
            provider_rec={"name": "Acme"},
            prefill_model="",
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _create_sub_session({"sender_session_id": "p", "provider_id": "prov-x"})
            )
        assert exc.value.status_code == 400
        assert "Acme" in exc.value.detail

    def test_requested_model_validates(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_ref="prov-a",
            provider_rec={"name": "A"},
        )
        validated = []
        monkeypatch.setattr(
            internal_orchestration_api,
            "_validate_provider_model",
            lambda *a: validated.append(a),
        )
        result = asyncio.run(
            _create_sub_session({"sender_session_id": "p", "model": "sonnet"})
        )
        assert validated == [("prov-a", "sonnet")]
        assert configured.sessions.create_sub_session.calls[0]["model"] == "sonnet"
        assert configured.coord.emit_session_created_panel.calls
        assert result["target_session_id"] == "sub-1"

    def test_inherits_parent_provider_and_model(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "parent-m", "runner": "parent-r"},
            provider_rec={"name": "A"},
        )
        result = asyncio.run(_create_sub_session({"sender_session_id": "p", "description": "child"}))
        kw = configured.sessions.create_sub_session.calls[0]
        assert kw["provider_id"] == "prov-a"
        assert kw["model"] == "parent-m"
        assert kw["runner"] == "parent-r"
        assert kw["name"] == "child"
        assert result["success"] is True

    def test_create_sub_session_value_error_is_400(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_rec={"name": "A"},
        )
        configured.sessions.create_sub_session.raises = ValueError("bad parent state")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_create_sub_session({"sender_session_id": "p"}))
        assert exc.value.status_code == 400
        assert "bad parent state" in exc.value.detail

    def test_requested_provider_prefills_model(self, configured, monkeypatch):
        # Pinned provider (differing from parent) with no model → profile_prefill_model
        # supplies the provider's default; the "no default → 400" branch is skipped.
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_ref="prov-x",
            provider_rec={"name": "X"},
            prefill_model="x-default",
        )
        result = asyncio.run(
            _create_sub_session({"sender_session_id": "p", "provider_id": "prov-x"})
        )
        kw = configured.sessions.create_sub_session.calls[0]
        assert kw["model"] == "x-default"
        assert kw["provider_id"] == "prov-x"
        assert result["success"] is True

    def test_parent_without_model_prefills_from_provider(self, configured, monkeypatch):
        # No requested provider/model, parent exposes no model → derive from the
        # inherited provider's default model.
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "", "runner": "native"},
            provider_rec={"name": "A"},
            prefill_model="provider-default",
        )
        result = asyncio.run(_create_sub_session({"sender_session_id": "p"}))
        assert configured.sessions.create_sub_session.calls[0]["model"] == "provider-default"
        assert result["success"] is True

    def test_extension_claims_session(self, configured, monkeypatch):
        # An active owning extension claims the freshly created sub-session.
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_rec={"name": "A"},
        )
        monkeypatch.setattr(internal_guards, "internal_authority_extension_id", lambda: "ext-1")
        monkeypatch.setattr(extension_store, "is_extension_active", lambda _id: True)
        claimed = []
        import extension_session_ownership

        monkeypatch.setattr(
            extension_session_ownership, "claim", lambda sid, eid: claimed.append((sid, eid))
        )
        asyncio.run(_create_sub_session({"sender_session_id": "p"}))
        assert claimed and claimed[0][1] == "ext-1"

    def test_requested_reasoning_effort(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_ref="prov-a",
            provider_rec={"name": "A"},
            provider_effort="max",
        )
        asyncio.run(
            _create_sub_session(
                {"sender_session_id": "p", "model": "m", "reasoning_effort": "high"}
            )
        )
        assert configured.sessions.create_sub_session.calls[0]["reasoning_effort"] == "max"

    def test_response_shape(self, configured, monkeypatch):
        _stub_resolution(
            monkeypatch,
            sender={"provider_id": "prov-a", "model": "m", "runner": "native"},
            provider_rec={"name": "A"},
        )
        result = asyncio.run(_create_sub_session({"sender_session_id": "p"}))
        for key in (
            "success", "target_session_id", "name", "cwd", "orchestration_mode",
            "node_id", "provider_id", "model", "runner", "reasoning_effort",
            "disallowed_tools", "disabled_builtin_extensions",
            "extra_mcp_servers", "harness_profile_id",
        ):
            assert key in result
