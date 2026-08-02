"""Unit tests for ``provider_fugu`` — Sakana Fugu via the codex CLI.

Fugu only overrides ``codex_config_overrides`` (model-provider selection +
feature gating) and ``build_env`` (SAKANA_API_KEY handling); the rest is
inherited from ``CodexProvider``. These tests pin both overrides plus the
routing class attributes. Records omit ``mode`` so ``require_runtime_credential``
returns early and the Fugu-specific env logic is reached directly.
"""

from __future__ import annotations

import pytest

import provider_fugu


def _provider(**record_overrides) -> "provider_fugu.FuguProvider":
    record = {"id": "fugu-test", "kind": "fugu"}
    record.update(record_overrides)
    return provider_fugu.FuguProvider(record)


# --------------------------------------------------------------------------- #
# routing / catalog class attributes
# --------------------------------------------------------------------------- #

def test_class_attributes_route_to_sakana_via_codex_runner() -> None:
    assert provider_fugu.FuguProvider.KIND == "fugu"
    assert provider_fugu.FuguProvider.RUNNER_KIND == "fugu"
    assert provider_fugu.FuguProvider.CODEX_PROFILE is None
    assert provider_fugu.FuguProvider.CODEX_MODEL_PROVIDER == "sakana"
    assert provider_fugu.FuguProvider.uses_managed_api_key is True


def test_reasoning_effort_dial_advertises_high_and_xhigh_only() -> None:
    cls = provider_fugu.FuguProvider
    assert cls.supports_reasoning_effort is True
    assert cls.reasoning_effort_options == ("high", "xhigh")
    assert cls.default_reasoning_effort == "high"


def test_fugu_models_catalog_is_exactly_two_entries() -> None:
    assert provider_fugu.FUGU_MODELS == ["fugu", "fugu-ultra"]


# --------------------------------------------------------------------------- #
# codex_config_overrides
# --------------------------------------------------------------------------- #

def test_codex_config_overrides_for_fugu() -> None:
    provider = _provider()
    assert provider.codex_config_overrides(model="fugu") == [
        'model_provider="sakana"',
        'model="fugu"',
        "features.image_generation=false",
        "features.shell_snapshot=false",
        'shell_environment_policy.exclude=["SAKANA_API_KEY"]',
    ]


def test_codex_config_overrides_for_fugu_ultra_selects_ultra_model() -> None:
    provider = _provider()
    overrides = provider.codex_config_overrides(model="fugu-ultra")
    assert overrides == [
        'model_provider="sakana"',
        'model="fugu-ultra"',
        "features.image_generation=false",
        "features.shell_snapshot=false",
        'shell_environment_policy.exclude=["SAKANA_API_KEY"]',
    ]


@pytest.mark.parametrize("bad_model", ["gpt-4", "claude", "", "fugu-2"])
def test_codex_config_overrides_rejects_model_outside_catalog(bad_model) -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="configured models"):
        provider.codex_config_overrides(model=bad_model)


def test_codex_config_overrides_rejects_none_model() -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="configured models"):
        provider.codex_config_overrides(model=None)


def test_codex_config_overrides_disables_image_generation_and_shell_snapshot() -> None:
    provider = _provider()
    overrides = provider.codex_config_overrides(model="fugu")
    # Sakana's Responses API rejects image_generation; shell_snapshot is
    # redundant for the multi-agent system. Both must be explicitly disabled.
    assert "features.image_generation=false" in overrides
    assert "features.shell_snapshot=false" in overrides


def test_codex_config_overrides_excludes_sakana_key_from_shell_environment() -> None:
    provider = _provider()
    overrides = provider.codex_config_overrides(model="fugu")
    assert any(
        line.startswith("shell_environment_policy.exclude=")
        and "SAKANA_API_KEY" in line
        for line in overrides
    ), overrides


# --------------------------------------------------------------------------- #
# build_env (SAKANA_API_KEY handling)
# --------------------------------------------------------------------------- #

def test_build_env_sets_sakana_key_from_record() -> None:
    provider = _provider(api_key="sk-from-record")
    assert provider.build_env()["SAKANA_API_KEY"] == "sk-from-record"


def test_build_env_omits_sakana_key_when_record_has_none() -> None:
    provider = _provider()
    assert "SAKANA_API_KEY" not in provider.build_env()


def test_build_env_omits_empty_api_key() -> None:
    provider = _provider(api_key="")
    assert "SAKANA_API_KEY" not in provider.build_env()


def test_build_env_omits_non_string_api_key() -> None:
    provider = _provider(api_key=123456)
    assert "SAKANA_API_KEY" not in provider.build_env()


def test_build_env_strips_ambient_sakana_key_when_record_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An ambient SAKANA_API_KEY inherited from the process env must NOT leak
    # into the runner env when the record carries no key of its own.
    monkeypatch.setenv("SAKANA_API_KEY", "sk-ambient")
    provider = _provider()
    assert "SAKANA_API_KEY" not in provider.build_env()


def test_build_env_record_key_overrides_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAKANA_API_KEY", "sk-ambient")
    provider = _provider(api_key="sk-from-record")
    assert provider.build_env()["SAKANA_API_KEY"] == "sk-from-record"


def test_build_env_never_inherits_other_provider_keys() -> None:
    # The codex base clears Claude/OpenAI credential env so a Fugu run cannot
    # accidentally authenticate as another provider.
    env = _provider(api_key="sk-fugu").build_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
