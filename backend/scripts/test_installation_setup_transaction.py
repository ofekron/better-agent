from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import dependency_plan
import installation_profile
import provider_setup

_SPEC = importlib.util.spec_from_file_location(
    "better_agent_install_script",
    ROOT / "scripts" / "install.py",
)
assert _SPEC is not None and _SPEC.loader is not None
install_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install_script)


def _identity(provider: str = "codex", digest: str = "a") -> dict:
    command = provider_setup.installer_for(provider).command
    path = str((Path(tempfile.gettempdir()) / command).absolute())
    return {
        "command": command,
        "launcher_path": path,
        "launcher_sha256": digest * 64,
        "target_path": path,
        "target_sha256": digest * 64,
        "size": 1,
        "mtime_ns": 1,
    }


def _write_existing_profile(root: Path) -> bytes:
    path = root / "installation.json"
    path.write_text(
        json.dumps({
            "schema_version": 3,
            "status": "active",
            "generation": "existing",
            "mode": "desktop-ui-only",
            "provider": "codex",
            "provider_identity": _identity(),
        }),
        encoding="utf-8",
    )
    return path.read_bytes()


def _assert_configure_failure_preserves_profile(
    *,
    identities,
    install_result: dict | None = None,
    prepare_error: BaseException | None = None,
    activate_error: BaseException | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="ba-install-transaction-") as tmp:
        root = Path(tmp)
        previous_home = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = tmp
        before = _write_existing_profile(root)

        prepare = (
            AsyncMock()
            if False
            else patch.object(
                dependency_plan,
                "prepare_installation",
                side_effect=prepare_error,
                return_value=root / "env",
            )
        )
        activate = patch.object(
            dependency_plan,
            "activate_prepared_installation",
            side_effect=activate_error,
        )
        try:
            with (
                patch.object(install_script.shutil, "which", return_value="/usr/bin/uv"),
                patch.object(dependency_plan, "activation_lock", return_value=nullcontext()),
                patch.object(
                    provider_setup,
                    "verified_provider_identity",
                    AsyncMock(side_effect=identities),
                ),
                patch.object(
                    provider_setup,
                    "install_if_missing",
                    AsyncMock(return_value=install_result),
                ),
                prepare,
                activate,
            ):
                try:
                    asyncio.run(
                        install_script._configure(
                            installation_profile.DEFAULT,
                            "codex",
                        )
                    )
                except BaseException:
                    pass
                else:
                    raise AssertionError("the configured failure stage must abort")
            assert (root / "installation.json").read_bytes() == before
        finally:
            if previous_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous_home


def test_failure_and_cancellation_before_commit_preserve_previous_profile() -> None:
    failed_install = {"state": "failed", "message": "install failed"}
    succeeded_install = {"state": "succeeded"}
    _assert_configure_failure_preserves_profile(
        identities=[RuntimeError("verify failed")],
    )
    _assert_configure_failure_preserves_profile(
        identities=[asyncio.CancelledError()],
    )
    _assert_configure_failure_preserves_profile(
        identities=[None],
        install_result=failed_install,
    )
    _assert_configure_failure_preserves_profile(
        identities=[None, None],
        install_result=succeeded_install,
    )
    _assert_configure_failure_preserves_profile(
        identities=[_identity(), _identity(digest="b")],
    )
    _assert_configure_failure_preserves_profile(
        identities=[_identity(), _identity()],
        prepare_error=RuntimeError("dependency preparation failed"),
    )


def test_successful_setup_orders_verification_environment_and_activation() -> None:
    events: list[str] = []
    identity = _identity()

    async def verify(_provider: str):
        events.append("verify")
        return identity

    def prepare(_uv: str, _profile: dict):
        events.append("prepare")
        return Path("/prepared")

    def activate(_environment: Path, _profile: dict):
        events.append("activate")

    with (
        patch.object(install_script.shutil, "which", return_value="/usr/bin/uv"),
        patch.object(dependency_plan, "activation_lock", return_value=nullcontext()),
        patch.object(provider_setup, "verified_provider_identity", verify),
        patch.object(dependency_plan, "prepare_installation", side_effect=prepare),
        patch.object(
            dependency_plan,
            "activate_prepared_installation",
            side_effect=activate,
        ),
    ):
        asyncio.run(
            install_script._configure(
                installation_profile.DEFAULT,
                "codex",
            )
        )
    assert events == ["verify", "verify", "prepare", "activate"]


def test_interrupted_commit_restores_pointer_and_leaves_setup_incomplete() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-install-commit-") as tmp:
        root = Path(tmp)
        backend = root / "backend"
        old_env = backend / ".venvs" / "old"
        new_env = backend / ".venvs" / "new"
        for environment, plan_hash in ((old_env, "old"), (new_env, "new")):
            environment.mkdir(parents=True)
            (environment / ".dependency-plan.json").write_text(
                json.dumps({"schema_version": 1, "hash": plan_hash}),
                encoding="utf-8",
            )
        pointer = backend / ".active-venv"
        pointer.write_text(".venvs/old", encoding="utf-8")
        profile = installation_profile.new_active_profile(
            mode=installation_profile.DEFAULT,
            provider="codex",
            provider_identity=_identity(),
        )
        previous_home = os.environ.get("BETTER_AGENT_HOME")
        previous_backend = installation_profile.BACKEND_ROOT
        os.environ["BETTER_AGENT_HOME"] = tmp
        installation_profile.BACKEND_ROOT = backend
        try:
            with (
                patch.object(dependency_plan, "BACKEND", backend),
                patch.object(dependency_plan, "ACTIVE_POINTER", pointer),
                patch.object(dependency_plan, "VENV_ROOT", backend / ".venvs"),
                patch.object(
                    dependency_plan,
                    "resolve_plan",
                    return_value={
                        "mode": installation_profile.DEFAULT,
                        "provider_kinds": ("codex",),
                        "requirements": ("requirements.txt",),
                        "probes": (),
                        "hash": "new",
                    },
                ),
                patch.object(
                    dependency_plan,
                    "_apply_pending_selection",
                    side_effect=RuntimeError("interrupted"),
                ),
            ):
                try:
                    dependency_plan.activate_prepared_installation(new_env, profile)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("interrupted activation must abort")

            assert pointer.read_text(encoding="utf-8") == ".venvs/old"
            assert installation_profile.capabilities()["setup_required"] is True
        finally:
            installation_profile.BACKEND_ROOT = previous_backend
            if previous_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous_home


if __name__ == "__main__":
    test_failure_and_cancellation_before_commit_preserve_previous_profile()
    test_successful_setup_orders_verification_environment_and_activation()
    test_interrupted_commit_restores_pointer_and_leaves_setup_incomplete()
    print("installation setup transaction tests passed")
