"""run.sh's fresh-home adoption block, exercised as the shell runs it.

A state home with no installation profile can serve nothing, so run.sh adopts
an already-installed provider CLI before it decides what to provision. The
block runs on the bootstrap interpreter, before backend dependencies are
synced, and its failure is non-fatal — so the commands it depends on are
pinned here rather than discovered at a user's cold start.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
RUN_SH = ROOT / "run.sh"


def _run(code: str, home: Path, path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env["BETTER_AGENT_HOME"] = str(home)
    env["BETTER_CLAUDE_HOME"] = str(home)
    env["BETTER_AGENT_TEST_MODE"] = "1"
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def _stub_cli(directory: Path, command: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / command
    stub.write_bytes(b"#!/bin/sh\nexit 0\n")
    stub.chmod(0o700)
    return stub


def test_run_sh_still_contains_the_adoption_block() -> None:
    script = RUN_SH.read_text(encoding="utf-8")
    for fragment in (
        "installation_bootstrap.adoptable_provider_kind()",
        "--yes --adopt",
        "capability_requested(p.MOBILE)",
        "capability_requested(p.INTEGRATIONS)",
        'BETTER_AGENT_INSTALL_MODE:-default',
    ):
        assert fragment in script, f"run.sh no longer runs: {fragment}"


def test_a_profile_less_home_reports_setup_required_and_desktop_provisioning() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-adopt-") as tmp:
        home = Path(tmp)
        probe = _run(
            'import installation_profile;'
            ' raise SystemExit(0 if installation_profile.load()["status"] == "active" else 1)',
            home,
        )
        assert probe.returncode == 1, probe.stderr

        # The provisioning question must still answer on a profile-less home:
        # run.sh reads it before anything has been adopted.
        mobile = _run(
            'import installation_profile as p;'
            ' raise SystemExit(0 if p.capability_requested(p.MOBILE) else 1)',
            home,
        )
        assert mobile.returncode == 1, mobile.stderr
        assert not mobile.stderr.strip(), mobile.stderr


def test_adoption_picks_an_installed_cli_and_prints_a_bare_kind() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-adopt-") as tmp:
        home = Path(tmp)
        (home / "config.json").write_text(
            json.dumps({
                "default_provider_id": "codex-id",
                "providers": [{"id": "codex-id", "kind": "codex", "suspended": False}],
            }),
            encoding="utf-8",
        )
        bin_dir = home / "bin"
        _stub_cli(bin_dir, "codex")
        result = _run(
            "import installation_bootstrap;"
            ' print(installation_bootstrap.adoptable_provider_kind() or "")',
            home,
            path_prefix=bin_dir,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "codex", result.stdout


def test_adopt_refuses_to_install_a_missing_cli() -> None:
    """`--adopt` exists so a cold start never installs a CLI over the network
    behind the user's back. The refusal must land before any environment work,
    so nothing is built for a provider that is not there.

    Runs in-process with the provider probe stubbed: invoking the real setup
    would build and activate a dependency environment inside this checkout.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    sys.path.insert(0, str(ROOT / "scripts"))
    import install as install_script
    import dependency_plan
    import provider_setup

    with (
        patch.object(shutil, "which", return_value="/usr/bin/uv"),
        patch.object(
            provider_setup, "verified_provider_identity", AsyncMock(return_value=None)
        ),
        patch.object(provider_setup, "install_if_missing", AsyncMock()) as installed,
        patch.object(dependency_plan, "prepare_installation") as prepared,
        patch.object(dependency_plan, "activation_lock"),
    ):
        try:
            asyncio.run(install_script._configure("default", "codex", adopt=True))
        except RuntimeError as exc:
            assert "--adopt" in str(exc), str(exc)
        else:
            raise AssertionError("adoption must refuse a provider that is not installed")
        assert not installed.called, "--adopt must never install a CLI"
        assert not prepared.called, "a refused adoption must not build an environment"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
