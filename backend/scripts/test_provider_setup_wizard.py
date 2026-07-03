from __future__ import annotations

import os
import shutil
import sys
import tempfile
import asyncio
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home
tmp_home = _test_home.isolate("bc-provider-setup-")

import provider_setup  # noqa: E402
import user_prefs  # noqa: E402


def check(label: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise AssertionError(label)


async def main() -> int:
    kinds = provider_setup.supported_provider_kinds()
    check("supported subscription providers are installable", kinds == ["agy", "claude", "codex", "copilot"])

    for kind in kinds:
        installer = provider_setup.installer_for(kind)
        check(f"{kind} install uses argv list", isinstance(installer.install_argv, tuple))
        check(f"{kind} install never uses shell string", "-c" not in installer.install_argv)
        check(f"{kind} install never pipes curl into shell", "curl" not in installer.install_argv)
        check(f"{kind} verifies concrete command", installer.verify_argv[0] == installer.command)
    agy = provider_setup.installer_for("agy")
    check("agy installer downloads allowlisted script", agy.install_script_url.startswith("https://antigravity.google/cli/install."))
    check("agy installer executes downloaded script as argv", "<downloaded-installer>" in agy.install_argv)
    check("agy installer pins downloaded script hash", len(agy.install_script_sha256) == 64)

    try:
        provider_setup.installer_for("custom")
    except ValueError:
        check("unsupported provider kind rejected", True)
    else:
        check("unsupported provider kind rejected", False)

    with mock.patch.object(shutil, "which", return_value=None):
        status = await provider_setup.provider_setup_status("claude", wait_for_cold=True)
        check("missing CLI reports uninstalled", status["installed"] is False)
        check("missing prerequisite captured", status["prerequisite"]["returncode"] == 127)
        check("status exposes prerequisite command", status["prerequisite_command"] == "npm")
        check("status exposes prerequisite installability", status["prerequisite_installable"] is (sys.platform == "win32"))
        check("status exposes prerequisite install command", isinstance(status["prerequisite_install_command"], list))
    provider_setup.clear_status_cache()

    calls: list[tuple[str, ...]] = []

    async def fake_check(argv: tuple[str, ...]) -> dict:
        calls.append(argv)
        return {"ok": True, "stdout": "ok", "stderr": "", "returncode": 0}

    with mock.patch.object(provider_setup, "_check_argv", side_effect=fake_check):
        first = await provider_setup.provider_setup_status("claude", wait_for_cold=True)
        first["verify"]["stdout"] = "mutated"
        second = await provider_setup.provider_setup_status("claude")
        check("provider setup status reuses cached checks", len(calls) == 2)
        check("provider setup status returns isolated copies", second["verify"]["stdout"] == "ok")
    provider_setup.clear_status_cache()

    slow_calls: list[tuple[str, ...]] = []

    async def slow_check(argv: tuple[str, ...]) -> dict:
        slow_calls.append(argv)
        await asyncio.sleep(0.02)
        return {"ok": True, "stdout": "ok", "stderr": "", "returncode": 0}

    with mock.patch.object(provider_setup, "_check_argv", side_effect=slow_check):
        first = await provider_setup.provider_setup_status("claude")
        tasks = list(provider_setup._STATUS_INFLIGHT.values())  # type: ignore[attr-defined]
        check("cold provider setup status returns checking projection", first["checking"] is True)
        check("cold provider setup status does not await checks", first["verify"]["checking"] is True)
        await asyncio.gather(*tasks)
        second = await provider_setup.provider_setup_status("claude")
        check("provider setup background refresh populates cache", second["installed"] is True)
        check("provider setup background refresh probes once", len(slow_calls) == 2)
    provider_setup.clear_status_cache()

    installer = provider_setup.ProviderInstaller(
        kind="test",
        label="Test",
        command="test",
        install_argv=("bash", "<downloaded-installer>"),
        verify_argv=("test", "--version"),
        prerequisite_argv=("bash", "--version"),
        install_script_url="https://antigravity.google/cli/install.sh",
    )
    status = await provider_setup._run_installer_script(installer, timeout=1)  # type: ignore[attr-defined]
    check("remote installer refuses unpinned script", status["returncode"] == 126)

    body = b"echo ok\n"
    with tempfile.TemporaryDirectory(prefix="bc-provider-hash-") as tmp:
        target = Path(tmp) / "install.sh"
        response = mock.Mock()
        response.read.return_value = body
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        with mock.patch.object(provider_setup.urllib.request, "urlopen", return_value=response):
            try:
                provider_setup._download_installer_script(  # type: ignore[attr-defined]
                    "https://antigravity.google/cli/install.sh",
                    "0" * 64,
                    target,
                )
            except RuntimeError:
                check("remote installer refuses hash mismatch", not target.exists())
            else:
                check("remote installer refuses hash mismatch", False)

    check("first-run defaults to not done", user_prefs.get_first_run_wizard_done() is False)
    user_prefs.set_first_run_wizard_done(True)
    check("first-run done persists", user_prefs.get_all()["first_run_wizard_done"] is True)
    check("network bind defaults local-only", user_prefs.get_network_bind_address() == "127.0.0.1")
    user_prefs.set_network_bind_address("0.0.0.0")
    check("network bind persists LAN mode", user_prefs.get_all()["network_bind_address"] == "0.0.0.0")
    try:
        user_prefs.set_network_bind_address("localhost")
    except ValueError:
        check("network bind rejects arbitrary host", True)
    else:
        check("network bind rejects arbitrary host", False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
