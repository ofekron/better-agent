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
from runner_codex import _resolve_codex_cli  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _make_fake_fugu(bin_dir: Path) -> Path:
    """A fake `codex-fugu` that answers `debug models` with a JSON catalog."""
    fugu = bin_dir / "codex-fugu"
    payload = json.dumps({"models": [
        {"slug": "fugu", "visibility": "show"},
        {"slug": "fugu-ultra", "visibility": "show"},
    ]})
    fugu.write_text(
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        "if [ \"$1\" = \"debug\" ] && [ \"$2\" = \"models\" ]; then\n"
        f"  printf '%s\\n' {json.dumps(payload)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fugu.chmod(fugu.stat().st_mode | stat.S_IXUSR)
    return fugu


def test_registry_and_capabilities() -> None:
    cls = _resolve_class("fugu")
    check(cls is FuguProvider, "provider registry resolves fugu")
    check(issubclass(FuguProvider, CodexProvider), "fugu reuses CodexProvider")
    check(FuguProvider.KIND == "fugu", "fugu KIND")
    check(FuguProvider.CODEX_BINARY == "codex-fugu", "fugu drives the codex-fugu launcher")
    check(FuguProvider.RUNNER_KIND == "fugu", "fugu has its own frozen runner dispatch")
    # Fugu IS codex under the hood, so the codex app-server capabilities
    # (fork, steering, subagents, team mode) carry over unchanged.
    check(FuguProvider.supports_fork is True, "fugu inherits codex fork")
    check(FuguProvider.supports_manager_mode is True, "fugu inherits codex team mode")
    check(FuguProvider.supports_steering is True, "fugu inherits codex steering")
    check(FuguProvider.supports_native_subagents is True, "fugu inherits codex subagents")
    # Sakana's catalog advertises exactly high + xhigh for Fugu/Fugu Ultra;
    # the launcher forwards args to codex unchanged, so the effort dial works.
    check(FuguProvider.supports_reasoning_effort is True, "fugu exposes the reasoning-effort dial")
    check(FuguProvider.reasoning_effort_options == ("high", "xhigh"), "fugu offers high + xhigh only")
    check(FuguProvider.default_reasoning_effort == "high", "fugu defaults to high")


def test_models_catalog() -> None:
    check(models._static_cold_start({"kind": "fugu"}) == FUGU_MODELS, "fugu static cold-start")
    fetch = models._resolve_refresh_fetch({"kind": "fugu"})
    check(fetch is fetch_fugu_models, "fugu refresh resolves to fetch_fugu_models")


def test_runner_resolves_fugu_binary() -> None:
    bin_dir = Path(tempfile.mkdtemp(prefix="bc-test-fugu-bin-"))
    old_path = os.environ.get("PATH", "")
    try:
        _make_fake_fugu(bin_dir)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        resolved = _resolve_codex_cli({"codex_binary": "codex-fugu"})
        check(resolved is not None and resolved.endswith("codex-fugu"), "runner honors codex_binary override")
        # Default (no override) does not pick up codex-fugu specifically.
        missing = _resolve_codex_cli({"codex_binary": "definitely-not-real-xyz"})
        check(missing is None, "runner returns None when the named binary is absent")
        check(_resolve_codex_cli() == _resolve_codex_cli({}), "default resolution is stable without an override")
        # The fetched models come from the fugu launcher's debug output.
        check(fetch_fugu_models() == ["fugu", "fugu-ultra"], "fetch_fugu_models parses the launcher catalog")
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
    # With no codex-fugu on PATH, fetch must degrade to the static list
    # rather than raising — the dropdown always needs something.
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
        test_runner_resolves_fugu_binary,
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
