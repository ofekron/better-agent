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
import config_store
import _runtime_profile_test_helpers as _rp  # noqa: E402
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

    def apply_if_matches(_provider_id, predicate, apply) -> bool:
        if not predicate(record):
            return False
        apply()
        return True

    monkeypatch.setattr(
        provider.config_store,
        "apply_if_provider_matches",
        apply_if_matches,
    )
    provider._PROVIDER_CACHE.clear()
    provider._PROVIDER_KEY_LOCKS.clear()
    try:
        native = provider.get_provider("fugu", "native")
        better_agent = provider.get_provider("fugu", "better_agent_runner")
        assert native is not better_agent
        assert native.KIND == "fugu"
        assert better_agent.KIND == "openai"
        assert provider.get_provider("fugu", "native") is native
        assert provider.get_provider("fugu", "better_agent_runner") is better_agent
    finally:
        for cached in provider._PROVIDER_CACHE.values():
            cached._deactivate_cache()
        provider._PROVIDER_CACHE.clear()
        provider._PROVIDER_KEY_LOCKS.clear()


def test_default_provider_is_resolved_before_orchestration_validation() -> None:
    """A default provider whose kind has no team mode must fail session
    creation on the orchestration axis — proving the default is resolved
    before the orchestration check, not after."""
    # kimi still lacks team-mode support (AGY gained it in 71294b348).
    no_team = config_store.add_provider({
        "name": "No-team default",
        "kind": "kimi",
        "mode": "subscription",
        "default_model": "kimi-test",
    })
    _rp.activate_provider(no_team["id"])
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
        "default_model": "fugu-flash",
    })
    # The installation fixture seeds its own provider as the default; the
    # assertions below read the resolved default, so point it at fugu.
    _rp.activate_provider(fugu["id"])
    ba_profile = config_store.find_live_runtime_profile(
        fugu["id"], "better_agent_runner"
    ) or config_store.add_runtime_profile({
        "provider_id": fugu["id"],
        "runner": "better_agent_runner",
        "default_model": "fugu-flash",
    })
    config_store.set_internal_llm_assignments({
        "default_session": {
            "runtime_profile_id": ba_profile["id"],
            "model": "fugu-flash",
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


def test_supported_runners_gates_subscription_runner_on_mode() -> None:
    # codex's better_agent_runner speaks a subscription OAuth protocol, so it
    # is offered only in subscription mode and hidden in api_key mode.
    codex_sub = {"id": "codex-sub", "kind": "codex", "mode": "subscription"}
    codex_key = {"id": "codex-key", "kind": "codex", "mode": "api_key"}
    assert "better_agent_runner" in runtime_profile.supported_runners(codex_sub)
    filtered = runtime_profile.supported_runners(codex_key)
    assert "better_agent_runner" not in filtered
    assert "native" in filtered


def test_default_runner_prefers_configured_then_first_then_raises(monkeypatch) -> None:
    codex_key = {"id": "codex-key", "kind": "codex", "mode": "api_key"}
    # A configured runner that is supported wins.
    assert runtime_profile.default_runner({**codex_key, "runner": "native"}) == "native"
    # An unsupported configured value falls back to the first supported choice.
    assert runtime_profile.default_runner({**codex_key, "runner": "bogus"}) == "native"
    # When no runner is supported at all, selection fails closed.
    monkeypatch.setattr(runtime_profile, "supported_runners", lambda _record: ())
    try:
        runtime_profile.default_runner(codex_key)
    except ValueError as error:
        assert "no supported runners" in str(error)
    else:
        raise AssertionError("default_runner must raise when no runner is supported")


def test_resolve_runner_falls_back_to_default_when_unspecified_or_lenient() -> None:
    codex_key = {"id": "codex-key", "kind": "codex", "mode": "api_key"}
    # An empty request defers to the default.
    assert runtime_profile.resolve_runner(codex_key, None) == "native"
    assert runtime_profile.resolve_runner(codex_key, "") == "native"
    # In lenient mode an unsupported request falls back to the default rather
    # than raising.
    assert runtime_profile.resolve_runner(codex_key, "bogus", strict=False) == "native"


def test_family_binds_to_kind_matches_own_or_delegated_runtime() -> None:
    # A kind binds to its own family...
    assert runtime_profile.family_binds_to_kind("fugu", "fugu") is True
    # ...and to the family its better_agent_runner choice delegates to. fugu's
    # better_agent_runner resolves to the generic "openai" runtime, not claude.
    assert runtime_profile.family_binds_to_kind("openai", "fugu") is True
    assert runtime_profile.family_binds_to_kind("claude", "fugu") is False
    # claude's better_agent_runner keeps the claude wire format, so claude does
    # NOT delegate to openai.
    assert runtime_profile.family_binds_to_kind("claude", "claude") is True
    assert runtime_profile.family_binds_to_kind("openai", "claude") is False


def test_fit_reasoning_effort_preserves_empty_and_clamps_to_options() -> None:
    # An empty inherited effort means "none chosen" and is preserved.
    assert runtime_profile.fit_reasoning_effort({}, "") == ""
    # An effort the target exposes is kept as-is.
    offers = {"reasoning_effort_options": ["low", "high"]}
    assert runtime_profile.fit_reasoning_effort(offers, "high") == "high"
    # An unsupported effort falls back to the record's own default when set,
    # without consulting the store for a pair-profile default.
    with_default = {
        "reasoning_effort_options": ["low", "high"],
        "default_reasoning_effort": "high",
    }
    assert runtime_profile.fit_reasoning_effort(with_default, "medium") == "high"
    # With no usable default it clamps to the first offered option.
    assert runtime_profile.fit_reasoning_effort(offers, "medium") == "low"
    # And a target that offers no efforts resolves to "".
    assert runtime_profile.fit_reasoning_effort({}, "medium") == ""


def main() -> int:
    test_supported_runner_matrix_is_strict()
    test_supported_runners_gates_subscription_runner_on_mode()
    test_resolve_runner_falls_back_to_default_when_unspecified_or_lenient()
    test_family_binds_to_kind_matches_own_or_delegated_runtime()
    test_fit_reasoning_effort_preserves_empty_and_clamps_to_options()
    test_default_provider_is_resolved_before_orchestration_validation()
    test_internal_profile_and_session_persist_runner()
    try:
        import pytest
    except ImportError:
        print("runtime profile: matrix/adapters OK; cache + default-runner tests require pytest")
        return 0
    test_default_runner_prefers_configured_then_first_then_raises(pytest.MonkeyPatch())
    test_provider_cache_is_runner_scoped(pytest.MonkeyPatch())
    print("runtime profile: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
