import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-fugu-")
_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

import models  # noqa: E402
import config_store  # noqa: E402
from provider import ProviderCredentialError, _resolve_class  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from provider_fugu import FuguProvider  # noqa: E402
from provider_openai import OpenAIProvider  # noqa: E402
from provider_runtime import _preserved_env_keys  # noqa: E402
from runner_codex import _build_app_server_argv  # noqa: E402
from run_recovery import _recovery_family  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_registry_and_capabilities() -> None:
    cls = _resolve_class("fugu")
    check(cls is FuguProvider, "provider registry resolves fugu")
    check(issubclass(FuguProvider, CodexProvider), "fugu reuses CodexProvider")
    check(FuguProvider.KIND == "fugu", "fugu KIND")
    check(FuguProvider.RUNNER_KIND == "fugu", "fugu has its own frozen runner dispatch")
    # Fugu drives the SAME `codex` binary as generic Codex; only the config
    # overrides differ. No separate launcher binary.
    check(FuguProvider.CODEX_BINARY == "codex", "fugu reuses the regular codex binary")
    check(FuguProvider.CODEX_PROFILE is None, "fugu does not use Codex profiles for app-server")
    check(CodexProvider.CODEX_PROFILE is None, "generic codex has no profile override")
    check(FuguProvider.uses_managed_api_key is True, "fugu requires a broker-managed API key")
    # Fugu IS codex under the hood, so the codex app-server capabilities
    # (fork, steering, subagents, team mode) carry over unchanged.
    check(FuguProvider.supports_fork is True, "fugu inherits codex fork")
    check(
        FuguProvider.supports_headless_no_tools is False,
        "fugu does not misrepresent read-only sandboxing as no-tools",
    )
    check(FuguProvider.supports_manager_mode is True, "fugu inherits codex team mode")
    check(FuguProvider.supports_steering is True, "fugu inherits codex steering")
    check(FuguProvider.supports_native_subagents is True, "fugu inherits codex subagents")
    # Sakana's catalog advertises exactly high + xhigh for Fugu/Fugu Ultra;
    # the model-provider override routes the call to Fugu, so the effort dial works.
    check(FuguProvider.supports_reasoning_effort is True, "fugu exposes the reasoning-effort dial")
    check(FuguProvider.reasoning_effort_options == ("high", "xhigh"), "fugu offers high + xhigh only")
    check(FuguProvider.default_reasoning_effort == "high", "fugu defaults to high")


def test_models_catalog() -> None:
    check(models._static_cold_start({"kind": "fugu"}) == [], "fugu has no legacy cold-start authority")
    check(models._resolve_refresh_fetch({"kind": "fugu"}) is None, "fugu has no legacy refresh authority")


def test_argv_builder_selects_fugu_sakana_overrides() -> None:
    # Generic codex: no profile flag, just the subcommand.
    check(_build_app_server_argv("codex", None) == ["codex", "app-server"], "generic argv has no -p")
    provider = FuguProvider({"id": "fugu-test", "kind": "fugu"})
    fugu_overrides = [
        *provider.codex_config_overrides(model="fugu-ultra"),
        "model_reasoning_effort=\"high\"",
    ]
    fugu_argv = _build_app_server_argv("codex", FuguProvider.CODEX_PROFILE, fugu_overrides)
    check(fugu_argv == [
        "codex",
        "-c", "model_provider=\"sakana\"",
        "-c", "model=\"fugu-ultra\"",
        "-c", "features.image_generation=false",
        "-c", "features.shell_snapshot=false",
        "-c", "shell_environment_policy.exclude=[\"SAKANA_API_KEY\"]",
        "-c", "model_reasoning_effort=\"high\"",
        "app-server",
    ], "fugu argv selects Sakana via config overrides before app-server")
    check("-p" not in fugu_argv, "fugu app-server argv never uses unsupported profile flag")


def test_fugu_disables_image_generation_tool() -> None:
    # Sakana's Responses API rejects codex's built-in image_generation tool
    # type (only `function`/`custom` allowed). Codex enables that feature by
    # default, so Fugu runs must turn it off via a config override.
    provider = FuguProvider({"id": "fugu-test", "kind": "fugu"})
    for model in ("fugu", "fugu-ultra"):
        overrides = provider.codex_config_overrides(model=model)
        check("features.image_generation=false" in overrides,
              f"fugu overrides disable image_generation for model={model!r}")
        check("features.shell_snapshot=false" in overrides,
              f"fugu overrides disable shell snapshots for model={model!r}")
        check("shell_environment_policy.exclude=[\"SAKANA_API_KEY\"]" in overrides,
              f"fugu excludes its API key from spawned commands for model={model!r}")
    for model in (None, "bogus"):
        try:
            provider.codex_config_overrides(model=model)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid Fugu model must fail closed")
    # Global flags still precede the subcommand for generic profile users.
    check(_build_app_server_argv("codex", "other") == ["codex", "-p", "other", "app-server"],
          "arbitrary profile is forwarded")


def test_fugu_runner_uses_only_broker_authoritative_key() -> None:
    record = {
        "id": "fugu-test",
        "kind": "fugu",
        "mode": "api_key",
        "api_key": "broker-key",
        "_credential_authoritative": True,
    }
    provider = FuguProvider(record)

    real_status = config_store.provider_credential_status
    old_key = os.environ.get("SAKANA_API_KEY")
    config_store.provider_credential_status = lambda provider_id: (
        "available" if provider_id == "fugu-test" else "missing"
    )
    os.environ["SAKANA_API_KEY"] = "ambient-key"
    try:
        env = provider.build_env()
        check(env.get("SAKANA_API_KEY") == "broker-key", "broker key replaces ambient key")
        check("SAKANA_API_KEY" in _preserved_env_keys(env), "isolated runtime preserves Sakana key")
    finally:
        config_store.provider_credential_status = real_status
        if old_key is None:
            os.environ.pop("SAKANA_API_KEY", None)
        else:
            os.environ["SAKANA_API_KEY"] = old_key


def test_fugu_runner_rejects_non_authoritative_key() -> None:
    provider = FuguProvider({
        "id": "fugu-test",
        "kind": "fugu",
        "mode": "api_key",
        "api_key": "ambient-key",
    })
    try:
        provider.build_env()
    except ProviderCredentialError as exc:
        check("not supervisor-authoritative" in str(exc), "runner rejects unmanaged Fugu key")
        return
    raise AssertionError("Fugu runner must reject unmanaged keys")


def test_runner_backend_state_projects_recovery_family() -> None:
    def state(run_dir: Path, runner: str) -> SimpleNamespace:
        run_dir.mkdir(parents=True)
        return SimpleNamespace(
            run_id=run_dir.name,
            run_dir=run_dir,
            popen=SimpleNamespace(pid=os.getpid()),
            mode="native",
            app_session_id="app-session",
            persist_to="app-session",
            started_at="2026-07-29T00:00:00Z",
            session_id=None,
            jsonl_path=run_dir / "events.jsonl",
            processed_line=0,
            processed_byte_offset=0,
            cancelled=False,
            turn_cancelled=False,
            target_message_id=None,
            turn_run_id=None,
            child_sources={},
            runner=runner,
        )

    native_dir = Path(_TMP_HOME) / "native-fugu"
    FuguProvider({
        "id": "fugu-provider",
        "kind": "fugu",
        "runner": "native",
    })._write_backend_state(state(native_dir, "native"))
    native = json.loads(
        (native_dir / "backend_state.json").read_text(encoding="utf-8"),
    )
    check(native["provider_kind"] == "fugu", "native Fugu persists its runtime kind")
    check(native["runner"] == "native", "native Fugu persists its frozen runner")
    check(_recovery_family(native) == "codex", "native Fugu replays Codex rollout")

    better_agent_dir = Path(_TMP_HOME) / "better-agent-fugu"
    better_agent_provider = OpenAIProvider({
        "id": "fugu-provider",
        "kind": "fugu",
        "runner": "better_agent_runner",
    })
    better_agent_state = state(better_agent_dir, "better_agent_runner")
    better_agent_provider._record["runner"] = "native"
    better_agent_provider._write_backend_state(better_agent_state)
    better_agent = json.loads(
        (better_agent_dir / "backend_state.json").read_text(encoding="utf-8"),
    )
    check(
        better_agent["provider_kind"] == "openai",
        "Better Agent Fugu persists its runtime kind",
    )
    check(
        better_agent["runner"] == "better_agent_runner",
        "Better Agent Fugu persists its frozen runner",
    )
    check(
        _recovery_family(better_agent) == "session_events",
        "Better Agent Fugu replays normalized session events",
    )


def test_fugu_not_auto_installable() -> None:
    # The fugu installer is a `git clone HEAD | bash` bootstrap that is not
    # hash-pinnable, so it MUST NOT be wired into the setup wizard's
    # auto-installer registry (security: fail closed on un-pinnable scripts).
    import provider_setup
    check("fugu" not in provider_setup.supported_provider_kinds(), "fugu is not auto-installable")
    try:
        provider_setup.installer_for("fugu")
    except ValueError:
        return
    raise AssertionError("installer_for(fugu) must raise — no safe installer exists")


def main() -> int:
    tests = [
        test_registry_and_capabilities,
        test_models_catalog,
        test_argv_builder_selects_fugu_sakana_overrides,
        test_fugu_disables_image_generation_tool,
        test_fugu_runner_uses_only_broker_authoritative_key,
        test_fugu_runner_rejects_non_authoritative_key,
        test_runner_backend_state_projects_recovery_family,
        test_fugu_not_auto_installable,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
