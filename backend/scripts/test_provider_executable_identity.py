from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import cli_paths
import installation_profile
import provider_setup


def _launcher(directory: Path, content: bytes) -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    path = directory / f"codex{suffix}"
    path.write_bytes(content)
    path.chmod(0o700)
    return path


def _profile(identity: dict) -> dict:
    return {
        "schema_version": installation_profile.SCHEMA_VERSION,
        "status": "active",
        "generation": "test",
        "mode": installation_profile.DEFAULT,
        "provider": "codex",
        "provider_identity": identity,
    }


def test_selected_provider_is_pinned_across_path_reordering() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-provider-identity-") as tmp:
        root = Path(tmp)
        first = root / "first"
        second = root / "second"
        first.mkdir()
        second.mkdir()
        selected = _launcher(first, b"selected")
        _launcher(second, b"other")
        identity = provider_setup.executable_identity(str(selected.absolute()))

        with (
            patch.object(installation_profile, "load", return_value=_profile(identity)),
            patch.object(installation_profile, "_activation_ready", return_value=True),
            patch.dict(os.environ, {"PATH": os.pathsep.join((str(second), str(first)))}),
        ):
            assert cli_paths.resolve_cli_binary("codex") == str(selected.absolute())


def test_replacement_and_in_place_mutation_fail_drift_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-provider-drift-") as tmp:
        launcher = _launcher(Path(tmp), b"before")
        identity = provider_setup.executable_identity(str(launcher.absolute()))
        profile = _profile(identity)

        with (
            patch.object(installation_profile, "load", return_value=profile),
            patch.object(installation_profile, "_activation_ready", return_value=True),
        ):
            launcher.write_bytes(b"after")
            assert cli_paths.resolve_cli_binary("codex") is None


def test_symlink_target_swap_fails_drift_validation() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="ba-provider-symlink-") as tmp:
        root = Path(tmp)
        first = root / "first"
        second = root / "second"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        first.chmod(0o700)
        second.chmod(0o700)
        launcher = root / "codex"
        launcher.symlink_to(first)
        identity = provider_setup.executable_identity(str(launcher.absolute()))
        profile = _profile(identity)

        with (
            patch.object(installation_profile, "load", return_value=profile),
            patch.object(installation_profile, "_activation_ready", return_value=True),
        ):
            launcher.unlink()
            launcher.symlink_to(second)
            assert cli_paths.resolve_cli_binary("codex") is None


def test_verification_detects_mutation_before_activation() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="ba-provider-verify-") as tmp:
            launcher = _launcher(Path(tmp), b"before")

            async def mutate(_argv, timeout):
                assert timeout == 10
                launcher.write_bytes(b"after")
                return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

            with (
                patch.object(
                    provider_setup,
                    "resolve_cli_binary",
                    return_value=str(launcher.absolute()),
                ),
                patch.object(provider_setup, "_run_argv", side_effect=mutate),
            ):
                try:
                    await provider_setup.verified_provider_identity("codex")
                except RuntimeError as exc:
                    assert "changed during verification" in str(exc)
                else:
                    raise AssertionError("mutated executable must fail verification")

    asyncio.run(run())


if __name__ == "__main__":
    test_selected_provider_is_pinned_across_path_reordering()
    test_replacement_and_in_place_mutation_fail_drift_validation()
    test_symlink_target_swap_fails_drift_validation()
    test_verification_detects_mutation_before_activation()
    print("provider executable identity tests passed")
