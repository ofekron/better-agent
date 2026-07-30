"""Locks the canonical provider manifest against every consumer so adding a
provider can't drift one site out of sync.

Phase 1 (this file at introduction): asserts the manifest equals the CURRENT
hardcoded sources of truth — a migration lock proving the table faithfully
encodes today's behavior before consumers are repointed at it.

After consumers are repointed (P2+), the assertions that compared against the
old constants become behavioral (every kind resolves; runner modules import;
app_entry choices == runner_kinds; installable == installer-bearing).

Uses a temp BETTER_AGENT_HOME so no real session state is touched.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="manifest_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import provider_manifest as pm  # noqa: E402


def test_resolve_class_matches_manifest():
    import provider
    for kind, spec in pm.SPECS.items():
        if spec.virtual:
            continue
        cls = provider._resolve_class(kind)
        assert cls.__name__ == spec.cls, (kind, cls.__name__, spec.cls)
        assert cls.KIND == kind, (kind, cls.KIND)


def test_runner_modules_importable():
    for kind in pm.runner_kinds():
        mod = pm.runner_module_for(kind)
        assert importlib.util.find_spec(mod) is not None, (kind, mod)


def test_copilot_dispatchable_in_frozen_app():
    # Regression: copilot used to be missing from app_entry's --runner-kind
    # choices, so a frozen-app copilot run died at argparse. Now app_entry
    # derives choices from runner_kinds(); copilot must be present and route
    # to its own runner, not the default claude runner.
    assert "copilot" in pm.runner_kinds()
    assert pm.runner_module_for("copilot") == "runner_copilot"


def test_recovery_families():
    # Lock the recovery-reader mapping. session-events family = runners
    # writing a Claude-shaped session_events.jsonl; codex family = rollout
    # reader, including Fugu's runner_codex execution path.
    assert pm.session_events_family_kinds() == frozenset({
        "agy", "copilot", "openai",
        "pi", "qwen", "cursor", "kimi", "amp", "opencode",
    })
    assert {
        kind for kind, spec in pm.SPECS.items()
        if spec.recovery_family == "codex"
    } == {"codex", "fugu"}
    assert pm.runner_module_for("fugu") == "runner_codex"


def test_execution_artifact_coverage():
    from execution_artifact_io import requires_execution_artifact

    local = {
        kind
        for kind, spec in pm.SPECS.items()
        if not spec.virtual
    }
    assert pm.artifact_family_kinds() == local - {"codex", "fugu"}
    assert {
        kind
        for kind in local
        if requires_execution_artifact(kind)
    } == local


def test_installable_matches_installers():
    import provider_setup
    assert pm.installable_kinds() == sorted(provider_setup.INSTALLERS)


def test_uses_claude_env_matches():
    import config_store
    for kind, spec in pm.SPECS.items():
        assert config_store._uses_claude_env({"kind": kind}) == spec.uses_claude_env, kind
    # missing kind defaults to claude env (True); unknown non-empty is False
    assert config_store._uses_claude_env({}) is True
    assert config_store._uses_claude_env({"kind": "totally-unknown"}) is False


def test_codex_only_gates():
    # Locks the current literal `== "codex"` semantics for the preempt and
    # ui-mcp gates (codex is the only context-continuation kind; codex is the
    # only kind WITHOUT the ui mcp server).
    ctx = {k for k, s in pm.SPECS.items() if s.context_continuation}
    no_ui = {k for k, s in pm.SPECS.items() if not s.hosts_ui_mcp}
    assert ctx == {"codex"}, ctx
    assert no_ui == {"codex"}, no_ui


def test_runner_choices_are_valid():
    for kind, spec in pm.SPECS.items():
        assert spec.runner_choices, kind
        assert set(spec.runner_choices).issubset({"native", "better_agent_runner"}), kind
        if spec.runner_choices == ("better_agent_runner",):
            assert pm.runner_module_for(kind) == "runner_better_agent", kind
    assert pm.default_runner_for("claude") == "native"
    assert pm.default_runner_for("openai") == "better_agent_runner"
    assert pm.runner_choices_for("fugu") == ("native", "better_agent_runner")


def test_provider_runner_round_trips():
    import config_store
    config_store._keyring_blocked = True
    openai = config_store.add_provider({
        "name": "OpenAI-compatible",
        "kind": "openai",
        "mode": "api_key",
        "base_url": "https://example.test/v1",
        "default_model": "model",
        "runner": "better_agent_runner",
    })
    assert openai["runner"] == "better_agent_runner"
    assert openai["runner_options"] == ["better_agent_runner"]

    claude_native = config_store.add_provider({
        "name": "Claude native",
        "kind": "claude",
        "mode": "subscription",
    })
    assert claude_native["runner"] == "native"
    assert claude_native["runner_options"] == ["native", "better_agent_runner"]

    # kind=="claude" is the one kind whose better_agent_runner choice keeps
    # the real "claude" runtime kind (it speaks Anthropic's own wire format
    # via the subscription OAuth token) instead of collapsing to "openai"
    # like every other kind's better_agent_runner choice does.
    claude_ba = config_store.add_provider({
        "name": "Claude BA",
        "kind": "claude",
        "mode": "subscription",
        "runner": "better_agent_runner",
    })
    assert claude_ba["runner"] == "better_agent_runner"
    assert claude_ba["runner_options"] == ["native", "better_agent_runner"]
    import provider as _provider
    assert _provider._provider_runtime_kind({"kind": "claude", "runner": "better_agent_runner"}) == "claude"

    # api_key mode has no better_agent_runner backend for claude — the
    # config layer rejects it outright (a dead-end configuration) rather
    # than silently persisting or silently falling back.
    try:
        config_store.add_provider({
            "name": "Claude API key BA",
            "kind": "claude",
            "mode": "api_key",
            "api_key": "sk-test",
            "runner": "better_agent_runner",
        })
        raise AssertionError("claude api_key + better_agent_runner should be rejected")
    except ValueError:
        pass

    fugu_native = config_store.add_provider({
        "name": "Fugu native",
        "kind": "fugu",
        "mode": "subscription",
        "default_model": "fugu",
    })
    assert fugu_native["runner"] == "native"
    assert fugu_native["runner_options"] == ["native"]

    fugu_ba = config_store.add_provider({
        "name": "Fugu BA",
        "kind": "fugu",
        "mode": "api_key",
        "base_url": "https://api.sakana.ai/v1",
        "default_model": "fugu",
        "runner": "better_agent_runner",
    })
    assert fugu_ba["kind"] == "fugu"
    assert fugu_ba["runner"] == "better_agent_runner"
    assert fugu_ba["runner_options"] == ["native", "better_agent_runner"]
    assert fugu_ba["permission_options"] == {"mode": ["default", "bypassPermissions"]}
    assert fugu_ba["reasoning_effort_options"] == ["high", "xhigh"]

    import provider
    assert provider._provider_runtime_kind({"kind": "fugu", "runner": "better_agent_runner"}) == "openai"

    try:
        config_store.add_provider({
            "name": "Invalid Fugu BA",
            "kind": "fugu",
            "mode": "subscription",
            "runner": "better_agent_runner",
        })
    except ValueError:
        pass
    else:
        raise AssertionError("fugu Better Agent runner must require api_key mode")


if __name__ == "__main__":
    test_resolve_class_matches_manifest()
    test_runner_modules_importable()
    test_recovery_families()
    test_execution_artifact_coverage()
    test_installable_matches_installers()
    test_uses_claude_env_matches()
    test_codex_only_gates()
    test_runner_choices_are_valid()
    test_provider_runner_round_trips()
    print("ok")
