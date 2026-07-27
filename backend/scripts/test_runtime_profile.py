from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP_HOME = _test_home.isolate("bc-test-runtime-profile-")

import _test_installation  # noqa: E402

# session_manager.create runs behind the installation gate, so the isolated
# home needs an activated installation before any session is created.
_test_installation.activate(Path(_TMP_HOME))

import provider  # noqa: E402
import runtime_profile  # noqa: E402
import config_store  # noqa: E402
from session_manager import IncompatibleOrchestrationMode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def test_supported_runner_matrix_is_strict() -> None:
    fugu = {"id": "fugu", "kind": "fugu", "mode": "api_key", "runner": "native"}
    assert runtime_profile.supported_runners(fugu) == ("native", "better_agent_runner")
    assert runtime_profile.resolve_runner(fugu, "better_agent_runner") == "better_agent_runner"

    claude = {"id": "claude", "kind": "claude", "mode": "api_key", "runner": "native"}
    try:
        runtime_profile.resolve_runner(claude, "better_agent_runner")
    except ValueError as error:
        assert "not supported" in str(error)
    else:
        raise AssertionError("unsupported runner must be rejected")

    fugu_subscription = {"id": "fugu-sub", "kind": "fugu", "mode": "subscription", "runner": "native"}
    assert runtime_profile.supported_runners(fugu_subscription) == ("native",)


def test_provider_cache_is_runner_scoped(monkeypatch) -> None:
    record = {
        "id": "fugu",
        "kind": "fugu",
        "mode": "api_key",
        "runner": "native",
        "base_url": "https://api.sakana.ai/v1",
        "api_key": "secret",
    }
    monkeypatch.setattr(provider.config_store, "get_provider_with_key", lambda _provider_id: dict(record))
    provider._PROVIDER_CACHE.clear()
    native = provider.get_provider("fugu", "native")
    better_agent = provider.get_provider("fugu", "better_agent_runner")
    assert native is not better_agent
    assert native.KIND == "fugu"
    assert better_agent.KIND == "openai"
    assert provider.get_provider("fugu", "native") is native
    assert provider.get_provider("fugu", "better_agent_runner") is better_agent


def test_default_provider_is_resolved_before_orchestration_validation() -> None:
    """A default provider whose kind has no team mode must fail session
    creation on the orchestration axis — proving the default is resolved
    before the orchestration check, not after."""
    agy = config_store.add_provider({
        "name": "AGY default",
        "kind": "agy",
        "mode": "subscription",
        "runner": "native",
        "default_model": "Gemini 3.1 Pro (High)",
    })
    config_store.set_default_provider(agy["id"])
    try:
        session_manager.create(name="invalid default orchestration", cwd="/tmp")
    except IncompatibleOrchestrationMode as error:
        assert "does not support team mode" in str(error)
    else:
        raise AssertionError("default provider must be resolved before orchestration validation")


def test_internal_profile_and_session_persist_runner() -> None:
    fugu = config_store.add_provider({
        "name": "Fugu OpenAI runtime",
        "kind": "fugu",
        "mode": "api_key",
        "runner": "native",
        "default_model": "fugu-flash",
    })
    # The installation fixture seeds its own provider as the default; the
    # assertions below read the resolved default, so point it at fugu.
    config_store.set_default_provider(fugu["id"])
    config_store.set_internal_llm_assignments({
        "default_session": {
            "provider_id": fugu["id"],
            "model": "fugu-flash",
            "runner": "better_agent_runner",
        },
    })
    resolved = config_store.resolve_internal_llm("default_session")
    assert resolved["runner"] == "better_agent_runner"

    session = session_manager.create(
        name="runner profile", cwd="/tmp", orchestration_mode="native",
    )
    assert session["provider_id"] == fugu["id"]
    assert session["runner"] == "better_agent_runner"
    assert session["last_active_runner"] is None

    try:
        session_manager.create(
            name="invalid runner profile",
            cwd="/tmp",
            provider_id=fugu["id"],
            model="fugu-flash",
            runner="better_agent_runner",
            reasoning_effort="minimal",
            orchestration_mode="native",
        )
    except ValueError as error:
        assert "not supported" in str(error)
    else:
        raise AssertionError("unsupported provider/model/effort/runner tuple must be rejected")


def main() -> int:
    test_supported_runner_matrix_is_strict()
    test_default_provider_is_resolved_before_orchestration_validation()
    test_internal_profile_and_session_persist_runner()
    try:
        import pytest
    except ImportError:
        print("runtime profile: matrix/adapters OK; cache test requires pytest")
        return 0
    test_provider_cache_is_runner_scoped(pytest.MonkeyPatch())
    print("runtime profile: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
