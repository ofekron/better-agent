from __future__ import annotations

import os
import sys

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-runtime-profile-")

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


def test_gemini_better_agent_adapter() -> None:
    gemini = {
        "id": "gemini",
        "kind": "gemini",
        "mode": "api_key",
        "runner": "native",
        "api_key": "secret",
        "base_url": "",
    }
    assert runtime_profile.supported_runners(gemini) == ("native", "better_agent_runner")
    adapted = runtime_profile.provider_record_for_runner(gemini, "better_agent_runner")
    assert adapted["base_url"] == runtime_profile.GEMINI_OPENAI_BASE_URL
    assert adapted["api_key"] == "secret"
    assert gemini["base_url"] == ""
    assert runtime_profile.reasoning_efforts(
        gemini, "better_agent_runner", model="gemini-2.5-flash",
    ) == ("none", "minimal", "low", "medium", "high")
    assert runtime_profile.reasoning_efforts(
        gemini, "better_agent_runner", model="gemini-3.5-flash",
    ) == ("minimal", "low", "medium", "high")


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


def test_internal_profile_and_session_persist_runner() -> None:
    gemini = config_store.add_provider({
        "name": "Gemini OpenAI runtime",
        "kind": "gemini",
        "mode": "api_key",
        "runner": "native",
        "default_model": "gemini-2.5-flash",
    })
    config_store.set_internal_llm_assignments({
        "default_session": {
            "provider_id": gemini["id"],
            "model": "gemini-2.5-flash",
            "reasoning_effort": "minimal",
            "runner": "better_agent_runner",
        },
    })
    resolved = config_store.resolve_internal_llm("default_session")
    assert resolved["runner"] == "better_agent_runner"
    assert resolved["reasoning_effort"] == "minimal"

    try:
        session_manager.create(name="invalid default orchestration", cwd="/tmp")
    except IncompatibleOrchestrationMode as error:
        assert "does not support team mode" in str(error)
    else:
        raise AssertionError("default provider must be resolved before orchestration validation")

    session = session_manager.create(
        name="runner profile", cwd="/tmp", orchestration_mode="native",
    )
    assert session["provider_id"] == gemini["id"]
    assert session["runner"] == "better_agent_runner"
    assert session["last_active_runner"] is None

    try:
        session_manager.create(
            name="invalid runner profile",
            cwd="/tmp",
            provider_id=gemini["id"],
            model="gemini-2.5-flash",
            runner="better_agent_runner",
            reasoning_effort="xhigh",
            orchestration_mode="native",
        )
    except ValueError as error:
        assert "not supported" in str(error)
    else:
        raise AssertionError("unsupported provider/model/effort/runner tuple must be rejected")


def main() -> int:
    test_supported_runner_matrix_is_strict()
    test_gemini_better_agent_adapter()
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
