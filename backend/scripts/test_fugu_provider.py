import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-fugu-")
_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

import models  # noqa: E402
from provider import _resolve_class  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from provider_fugu import FUGU_MODELS, FuguProvider, fetch_fugu_models  # noqa: E402
from runner_codex import _build_app_server_argv, _resolve_codex_cli  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _make_fake_codex(bin_dir: Path) -> Path:
    """A fake `codex` that rejects profiles and accepts Sakana config."""
    codex = bin_dir / "codex"
    payload = json.dumps({"models": [
        {"slug": "gpt-5.5", "visibility": "show"},
        {"slug": "fugu", "visibility": "show"},
        {"slug": "fugu-ultra", "visibility": "show"},
    ]})
    codex.write_text(
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        'if [ "${1:-}" = "-p" ]; then\n'
        "  exit 2\n"
        "fi\n"
        'if [ "$1" = "-c" ] && [ "$2" = "model_provider=\\"sakana\\"" ] && [ "$3" = "-c" ] && [ "$4" = "model=\\"fugu\\"" ] && [ "$5" = "debug" ] && [ "$6" = "models" ]; then\n'
        f"  printf '%s\\n' {json.dumps(payload)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    return codex


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
    # Fugu IS codex under the hood, so the codex app-server capabilities
    # (fork, steering, subagents, team mode) carry over unchanged.
    check(FuguProvider.supports_fork is True, "fugu inherits codex fork")
    check(FuguProvider.supports_manager_mode is True, "fugu inherits codex team mode")
    check(FuguProvider.supports_steering is True, "fugu inherits codex steering")
    check(FuguProvider.supports_native_subagents is True, "fugu inherits codex subagents")
    # Sakana's catalog advertises exactly high + xhigh for Fugu/Fugu Ultra;
    # the model-provider override routes the call to Fugu, so the effort dial works.
    check(FuguProvider.supports_reasoning_effort is True, "fugu exposes the reasoning-effort dial")
    check(FuguProvider.reasoning_effort_options == ("high", "xhigh"), "fugu offers high + xhigh only")
    check(FuguProvider.default_reasoning_effort == "high", "fugu defaults to high")


def test_models_catalog() -> None:
    check(models._static_cold_start({"kind": "fugu"}) == FUGU_MODELS, "fugu static cold-start")
    fetch = models._resolve_refresh_fetch({"kind": "fugu"})
    check(fetch is fetch_fugu_models, "fugu refresh resolves to fetch_fugu_models")


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
        "-c", "model_reasoning_effort=\"high\"",
        "app-server",
    ], "fugu argv selects Sakana via config overrides before app-server")
    check("-p" not in fugu_argv, "fugu app-server argv never uses unsupported profile flag")


def test_fugu_disables_image_generation_tool() -> None:
    # Sakana's Responses API rejects codex's built-in image_generation tool
    # type (only `function`/`custom` allowed). Codex enables that feature by
    # default, so Fugu runs must turn it off via a config override.
    provider = FuguProvider({"id": "fugu-test", "kind": "fugu"})
    for model in ("fugu", "fugu-ultra", None, "bogus"):
        overrides = provider.codex_config_overrides(model=model)
        check("features.image_generation=false" in overrides,
              f"fugu overrides disable image_generation for model={model!r}")
    # Global flags still precede the subcommand for generic profile users.
    check(_build_app_server_argv("codex", "other") == ["codex", "-p", "other", "app-server"],
          "arbitrary profile is forwarded")


def test_fetch_uses_codex_with_sakana_overrides() -> None:
    bin_dir = Path(tempfile.mkdtemp(prefix="bc-test-fugu-bin-"))
    old_path = os.environ.get("PATH", "")
    try:
        _make_fake_codex(bin_dir)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        # The fetched models come from Codex with Sakana model-provider config.
        check(fetch_fugu_models() == ["fugu", "fugu-ultra"], "fetch_fugu_models parses the fugu catalog")
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(bin_dir, ignore_errors=True)


def test_codex_binary_override_mechanism() -> None:
    # The codex_binary override still works generically (independent of fugu),
    # and defaults to plain `codex`.
    bin_dir = Path(tempfile.mkdtemp(prefix="bc-test-fugu-bin2-"))
    old_path = os.environ.get("PATH", "")
    try:
        _make_fake_codex(bin_dir)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        resolved = _resolve_codex_cli({"codex_binary": "codex"})
        check(resolved is not None and resolved.endswith("codex"), "runner honors codex_binary override")
        missing = _resolve_codex_cli({"codex_binary": "definitely-not-real-xyz"})
        check(missing is None, "runner returns None when the named binary is absent")
        check(_resolve_codex_cli() == _resolve_codex_cli({}), "default resolution is stable without an override")
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(bin_dir, ignore_errors=True)


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


def test_fugu_models_fallback_without_binary() -> None:
    # With no codex on PATH (or profile not installed), fetch must degrade to
    # the static list rather than raising — the dropdown always needs something.
    bin_dir = Path(tempfile.mkdtemp(prefix="bc-test-fugu-empty-"))
    old_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}"
        check(fetch_fugu_models() == FUGU_MODELS, "fetch falls back to static list without the binary")
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(bin_dir, ignore_errors=True)


def main() -> int:
    tests = [
        test_registry_and_capabilities,
        test_models_catalog,
        test_argv_builder_selects_fugu_sakana_overrides,
        test_fugu_disables_image_generation_tool,
        test_fetch_uses_codex_with_sakana_overrides,
        test_codex_binary_override_mechanism,
        test_fugu_not_auto_installable,
        test_fugu_models_fallback_without_binary,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
