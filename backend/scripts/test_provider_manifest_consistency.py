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
    # The better_agent_runner override selects the in-process runner module
    # regardless of the kind's own default (claude's default is the native
    # "runner"; the override routes it to runner_better_agent).
    assert pm.runner_module_for("claude", runner="better_agent_runner") == "runner_better_agent"
    assert set(pm.all_kinds()) == set(pm.SPECS)


def test_provider_runner_options_and_default_profile(monkeypatch):
    # Runner selection lives on runtime profiles, not the provider record
    # (see the v3 config-store migration and the frontend FormPayload, which
    # carries no `runner` field). `add_provider` therefore seeds exactly one
    # runtime profile carrying the kind's DEFAULT runner, and the provider
    # view surfaces the runners supported for that kind+mode plus the
    # per-runner profile projections. This locks that contract end to end.
    import config_store
    import provider as _provider
    import pytest
    import runtime_profile

    # api_key-mode providers persist their key through the desktop credential
    # session, which is absent under pytest. Stub an in-memory store (same
    # pattern as test_codex_model_discovery) so credential transactions
    # succeed without touching a real keychain.
    credentials: dict[str, str] = {}

    def _request(action: str, provider_id: str, **kwargs) -> dict:
        current = credentials.get(provider_id, "")
        if action == "status":
            return {"status": "available" if current else "missing"}
        if action == "read":
            return {"status": "available" if current else "missing",
                    **({"value": current} if current else {})}
        if action == "compare_set":
            if current != kwargs["expected_value"]:
                return {"status": "available", "applied": False}
            value = kwargs["value"]
            if value:
                credentials[provider_id] = value
            else:
                credentials.pop(provider_id, None)
            return {"status": "available" if value else "missing", "applied": True}
        raise AssertionError(f"unexpected credential action: {action}")

    monkeypatch.setattr(config_store.credential_session_client, "available", lambda: True)
    monkeypatch.setattr(config_store.credential_session_client, "request", _request)

    def _add(kind: str, mode: str, **extra):
        payload = {"name": f"{kind}-{mode}", "kind": kind, "mode": mode}
        payload.update(extra)
        return config_store.add_provider(payload)

    def _assert_options(view, kind, mode):
        expected = list(runtime_profile.supported_runners({"kind": kind, "mode": mode}))
        assert view["runner_options"] == expected, (kind, mode, view["runner_options"])
        # runner_profiles mirrors supported_runners 1:1; the first is the
        # default runner — the one add_provider seeds a profile for.
        assert [p["runner"] for p in view["runner_profiles"]] == expected, (kind, mode)
        return expected

    # openai has only the better_agent_runner choice, so that is both its
    # only offered runner and its seeded default.
    openai = _add("openai", "api_key", base_url="https://example.test/v1",
                  default_model="model")
    assert _assert_options(openai, "openai", "api_key") == ["better_agent_runner"]

    # claude subscription offers both runners; the seeded default is native.
    claude_sub = _add("claude", "subscription")
    assert _assert_options(claude_sub, "claude", "subscription") == [
        "native", "better_agent_runner"
    ]

    # claude api_key has no better_agent_runner backend (it exists only to
    # speak the subscription OAuth wire format), so the choice is withheld at
    # the options layer — a dead-end config cannot be reached rather than
    # being silently persisted or falling back.
    claude_key = _add("claude", "api_key", api_key="sk-test")
    assert _assert_options(claude_key, "claude", "api_key") == ["native"]

    # fugu subscription collapses to native-only; api_key re-enables the
    # better_agent_runner choice (OpenAI-compatible key auth).
    fugu_sub = _add("fugu", "subscription", default_model="fugu")
    assert _assert_options(fugu_sub, "fugu", "subscription") == ["native"]
    fugu_key = _add("fugu", "api_key", base_url="https://api.sakana.ai/v1",
                    default_model="fugu")
    assert _assert_options(fugu_key, "fugu", "api_key") == [
        "native", "better_agent_runner"
    ]

    # claude is the one kind whose better_agent_runner choice keeps the real
    # "claude" runtime kind (Anthropic's own wire format via the OAuth token)
    # instead of collapsing to "openai" like every other kind does.
    assert _provider._provider_runtime_kind(
        {"kind": "claude", "runner": "better_agent_runner"}) == "claude"
    assert _provider._provider_runtime_kind(
        {"kind": "fugu", "runner": "better_agent_runner"}) == "openai"

    # resolve_runner is the strict gate behind runner selection: an
    # unsupported runner for the kind+mode is rejected outright.
    with pytest.raises(ValueError):
        runtime_profile.resolve_runner(
            {"kind": "claude", "mode": "api_key"}, "better_agent_runner")
    with pytest.raises(ValueError):
        runtime_profile.resolve_runner(
            {"kind": "fugu", "mode": "subscription"}, "better_agent_runner")


if __name__ == "__main__":
    test_resolve_class_matches_manifest()
    test_runner_modules_importable()
    test_recovery_families()
    test_execution_artifact_coverage()
    test_installable_matches_installers()
    test_uses_claude_env_matches()
    test_codex_only_gates()
    test_runner_choices_are_valid()
    test_provider_runner_options_and_default_profile()
    print("ok")
