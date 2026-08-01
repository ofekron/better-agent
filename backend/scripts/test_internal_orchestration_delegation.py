"""Unit-tier coverage of the delegation + headless-generation routers in
internal_orchestration_api.py:

- internal_headless_generate (one-shot, tool-less text generation)
- internal_delegate_task + _handle_internal_delegate_task (the
  delegate_task smart router: target resolution, runtime-profile pinning,
  provider/model validation, dispatch)

The router's own validation/branching/dispatch logic is exercised for real;
only the cross-subsystem collaborators it does not own are faked — the
internal-token authority guard (shared stub), provider-id resolution
(`_resolve_provider_id_ref`), session existence (`_session_lite`), provider/model
validation + profile prefill (`_validate_provider_model`, `profile_prefill_model`,
config_store lookups), the broadcast hook, and the injected coordinator's
`run_delegate_task`. This keeps tests focused on this router's logic without
spinning a coordinator, a real session, or a provider runtime.

Isolated BETTER_AGENT_HOME via _test_home.isolate; collaborators are fakes
restored on teardown so nothing bleeds into other test modules in the run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-delegation-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402

import config_store  # noqa: E402
import harness_profile_resolver  # noqa: E402
import internal_orchestration_api  # noqa: E402
import internal_extension_api  # noqa: E402
import _test_authority  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Recorder:
    """Async callable that records every invocation and returns a configurable
    result. Used for the injected coordinator dispatch + broadcast hook."""

    def __init__(self, result):
        self.calls: list = []
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _FakeCoordinator:
    """Coordinator stand-in exposing only what the delegation routers use."""

    def __init__(self):
        self.run_delegate_task = _Recorder(
            {"created_session": True, "target_session_id": "new-target"}
        )
        self.broadcasts: list[tuple] = []

    async def broadcast_global(self, event: str, payload):
        self.broadcasts.append((event, payload))


@pytest.fixture
def internal():
    """Stub the authority guard valid (no coordinator needed for headless tests)."""
    with _test_authority.stub_internal_authority():
        yield


@pytest.fixture
def configured():
    """Bind a fake coordinator + broadcast hook and stub the authority guard;
    restore prior bindings on teardown so this file does not disturb other
    test modules."""
    import internal_guards

    prior = (
        internal_orchestration_api._coordinator_ref,
        internal_orchestration_api._delete_session_tree,
        internal_orchestration_api._provision_workers,
        internal_guards.require_builtin_runtime_extension,
        internal_orchestration_api._broadcast_session_organization_changed,
    )
    coord = _FakeCoordinator()
    broadcast = _Recorder(None)
    internal_orchestration_api.configure(
        coordinator=coord,
        delete_session_tree=_noop,
        provision_workers=_noop,
    )
    internal_guards.require_builtin_runtime_extension = lambda *_a, **_k: None
    internal_orchestration_api._broadcast_session_organization_changed = broadcast
    with _test_authority.stub_internal_authority():
        yield _Bindings(coord, broadcast)
    internal_guards.require_builtin_runtime_extension = prior[3]
    internal_orchestration_api._broadcast_session_organization_changed = prior[4]
    internal_orchestration_api._coordinator_ref = prior[0]
    internal_orchestration_api._delete_session_tree = prior[1]
    internal_orchestration_api._provision_workers = prior[2]


class _Bindings:
    def __init__(self, coord, broadcast):
        self.coord = coord
        self.broadcast = broadcast


def _passthrough_selectors(explicit, _profile_id, _revision=None):
    """merge_selector_defaults stand-in: identity over the three selector keys
    so the router's own branching is what's under test, not profile pins."""
    return {
        "provider_id": explicit.get("provider_id"),
        "model": explicit.get("model"),
        "reasoning_effort": explicit.get("reasoning_effort"),
    }


def _stub_resolvers(monkeypatch, *, provider_ref="prov-resolved", sender=None):
    """Stub provider-id resolution, session existence, and model validation."""
    async def _resolve(ref):
        return provider_ref if ref else ""

    async def _lite(_sid):
        return sender

    monkeypatch.setattr(internal_orchestration_api, "_resolve_provider_id_ref", _resolve)
    monkeypatch.setattr(internal_orchestration_api, "_session_lite", _lite)
    monkeypatch.setattr(internal_orchestration_api, "_validate_provider_model", lambda *_a: None)
    monkeypatch.setattr(
        harness_profile_resolver, "merge_selector_defaults", _passthrough_selectors
    )


# ============================================================ headless generate


def _headless(body):
    return internal_orchestration_api.internal_headless_generate(
        body, x_internal_token="x"
    )


class TestHeadlessGenerate:
    def test_unauthenticated_is_403(self):
        # No authority configured in the isolated home → authority_is_valid() is
        # False, so generation is refused before any work.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "s", "prompt": "p"}))
        assert exc.value.status_code == 403

    def test_wrong_body_keys_is_400(self, internal):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "s"}))
        assert exc.value.status_code == 400

    def test_non_string_fields_are_400(self, internal):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": 1, "prompt": "p"}))
        assert exc.value.status_code == 400

    def test_blank_fields_are_400(self, internal):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "  ", "prompt": "p"}))
        assert exc.value.status_code == 400

    def test_oversized_prompt_is_413(self, internal):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "s", "prompt": "x" * 16_001}))
        assert exc.value.status_code == 413

    def test_value_error_from_runner_is_422(self, internal, monkeypatch):
        import headless_admission

        async def _boom(*_a, **_kw):
            raise ValueError("bad session")

        monkeypatch.setattr(headless_admission, "run_session_headless", _boom)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "s", "prompt": "p"}))
        assert exc.value.status_code == 422
        assert "bad session" in exc.value.detail

    def test_generic_failure_is_502(self, internal, monkeypatch):
        import headless_admission

        async def _boom(*_a, **_kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr(headless_admission, "run_session_headless", _boom)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "s", "prompt": "p"}))
        assert exc.value.status_code == 502

    def test_error_result_is_502(self, internal, monkeypatch):
        import headless_admission

        async def _err(*_a, **_kw):
            return {"is_error": True}

        monkeypatch.setattr(headless_admission, "run_session_headless", _err)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_headless({"session_id": "s", "prompt": "p"}))
        assert exc.value.status_code == 502

    def test_happy_returns_text(self, internal, monkeypatch):
        import headless_admission

        async def _ok(*_a, **_kw):
            return {"is_error": False, "result": "hello world"}

        monkeypatch.setattr(headless_admission, "run_session_headless", _ok)
        result = asyncio.run(_headless({"session_id": "s", "prompt": "p"}))
        assert result == {"text": "hello world"}


# ============================================================ delegate-task endpoint


class TestDelegateTaskEndpoint:
    def test_durable_core_job_short_circuits(self, configured, monkeypatch):
        # If a core MCP job handles delegate-task, its result is returned without
        # entering the handler.
        monkeypatch.setattr(
            internal_extension_api,
            "maybe_run_core_mcp_job",
            lambda *_a, **_k: _async_value({"durable": True}),
        )
        result = asyncio.run(
            internal_orchestration_api.internal_delegate_task(
                {"sender_session_id": "s", "task": "do thing"},
                x_internal_token="x",
            )
        )
        assert result == {"durable": True}
        assert not configured.coord.run_delegate_task.calls


async def _async_value(v):
    return v


async def _noop(*_a, **_k):
    return None


# ============================================================ delegate-task handler


def _delegate(body):
    return internal_orchestration_api.internal_delegate_task(
        body, x_internal_token="x"
    )


class TestDelegateTaskHandler:
    def test_requires_sender_and_task(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_delegate({"sender_session_id": "", "task": ""}))
        assert exc.value.status_code == 400

    def test_runtime_profile_id_must_be_string(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _delegate(
                    {
                        "sender_session_id": "s",
                        "task": "t",
                        "runtime_profile_id": 123,
                    }
                )
            )
        assert exc.value.status_code == 400

    def test_runtime_profile_id_blank_is_400(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _delegate(
                    {
                        "sender_session_id": "s",
                        "task": "t",
                        "runtime_profile_id": "   ",
                    }
                )
            )
        assert exc.value.status_code == 400

    def test_unknown_runtime_profile_is_400(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        monkeypatch.setattr(config_store, "get_runtime_profile", lambda _id: None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _delegate(
                    {
                        "sender_session_id": "s",
                        "task": "t",
                        "runtime_profile_id": "ghost",
                    }
                )
            )
        assert exc.value.status_code == 400
        assert "unknown or deleted" in exc.value.detail

    def test_deleted_runtime_profile_is_400(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        monkeypatch.setattr(
            config_store,
            "get_runtime_profile",
            lambda _id: {"deleted_at": "2026-01-01"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _delegate(
                    {
                        "sender_session_id": "s",
                        "task": "t",
                        "runtime_profile_id": "dead",
                    }
                )
            )
        assert exc.value.status_code == 400

    def test_provider_conflicts_with_profile_is_400(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        monkeypatch.setattr(
            config_store,
            "get_runtime_profile",
            lambda _id: {"provider_id": "prov-a", "runner": "native"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _delegate(
                    {
                        "sender_session_id": "s",
                        "task": "t",
                        "runtime_profile_id": "prof",
                        "provider_id": "prov-b",
                    }
                )
            )
        assert exc.value.status_code == 400
        assert "conflicts" in exc.value.detail

    def test_profile_pins_provider_and_runner(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch, provider_ref="prov-a")
        monkeypatch.setattr(
            config_store,
            "get_runtime_profile",
            lambda _id: {"provider_id": "prov-a", "runner": "better_agent_runner"},
        )
        result = asyncio.run(
            _delegate(
                {
                    "sender_session_id": "s",
                    "task": "t",
                    "runtime_profile_id": "prof",
                    "model": "m",
                }
            )
        )
        assert result["created_session"] is True
        dispatched = configured.coord.run_delegate_task.calls[0][1]
        assert dispatched["provider_id"] == "prov-a"
        assert dispatched["runner"] == "better_agent_runner"

    def test_target_null_normalizes_to_none(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        result = asyncio.run(
            _delegate(
                {
                    "sender_session_id": "s",
                    "task": "t",
                    "target_session_id": "null",
                    "model": "m",
                }
            )
        )
        dispatched = configured.coord.run_delegate_task.calls[0][1]
        assert dispatched["target_session_id"] is None

    def test_provider_any_routes_cross_provider(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        result = asyncio.run(
            _delegate(
                {
                    "sender_session_id": "s",
                    "task": "t",
                    "provider_id": "ANY",
                    "model": "m",
                }
            )
        )
        dispatched = configured.coord.run_delegate_task.calls[0][1]
        assert dispatched["provider_id"] == "ANY"

    def test_omitted_provider_searches_globally(self, configured, monkeypatch):
        # No provider/model supplied → requested_provider_id "" (auto-route).
        _stub_resolvers(monkeypatch, provider_ref="")
        result = asyncio.run(
            _delegate({"sender_session_id": "s", "task": "t"})
        )
        dispatched = configured.coord.run_delegate_task.calls[0][1]
        assert dispatched["provider_id"] == ""
        assert dispatched["model"] == ""

    def test_provider_without_model_and_no_default_is_400(self, configured, monkeypatch):
        # A provider is pinned but no model is resolvable and the provider has no
        # default model configured → 400 naming the provider.
        _stub_resolvers(monkeypatch, provider_ref="prov-x")
        monkeypatch.setattr(internal_orchestration_api, "profile_prefill_model", lambda *_a: "")
        monkeypatch.setattr(
            config_store, "get_provider", lambda _id: {"name": "Acme"}
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                _delegate(
                    {
                        "sender_session_id": "s",
                        "task": "t",
                        "provider_id": "prov-x",
                    }
                )
            )
        assert exc.value.status_code == 400
        assert "Acme" in exc.value.detail

    def test_provider_prefills_default_model(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch, provider_ref="prov-x")
        monkeypatch.setattr(
            internal_orchestration_api, "profile_prefill_model", lambda *_a: "default-m"
        )
        result = asyncio.run(
            _delegate(
                {
                    "sender_session_id": "s",
                    "task": "t",
                    "provider_id": "prov-x",
                }
            )
        )
        dispatched = configured.coord.run_delegate_task.calls[0][1]
        assert dispatched["model"] == "default-m"

    def test_happy_dispatches_and_broadcasts(self, configured, monkeypatch):
        _stub_resolvers(
            monkeypatch,
            provider_ref="prov-a",
            sender={"provider_id": "prov-a", "model": "m"},
        )
        result = asyncio.run(
            _delegate(
                {
                    "sender_session_id": "s",
                    "task": "do thing",
                    "target_session_id": "t1",
                    "provider_id": "prov-a",
                    "model": "m",
                }
            )
        )
        assert result["created_session"] is True
        # Created a target → organization-change broadcast fired for it.
        assert configured.broadcast.calls
        broadcast_args = configured.broadcast.calls[0][0]
        assert broadcast_args == (["new-target"],)

    def test_no_broadcast_when_no_session_created(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)
        configured.coord.run_delegate_task.result = {"created_session": False}
        result = asyncio.run(
            _delegate({"sender_session_id": "s", "task": "t", "model": "m"})
        )
        assert result["created_session"] is False
        assert not configured.broadcast.calls

    def test_handler_value_error_is_400(self, configured, monkeypatch):
        _stub_resolvers(monkeypatch)

        async def _fail(**_kw):
            raise ValueError("search corpus unavailable")

        configured.coord.run_delegate_task = _fail
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_delegate({"sender_session_id": "s", "task": "t", "model": "m"}))
        assert exc.value.status_code == 400
        assert "search corpus unavailable" in exc.value.detail
