from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_HOME = tempfile.mkdtemp(prefix="ba-install-runtime-")
os.environ["BETTER_AGENT_HOME"] = _HOME
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import config_store
import dependency_plan
from daemonhost.host import DaemonHost
from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import registry_path, state_path
import extension_jobs
import installation_profile
import session_manager


def _reset_config_cache() -> None:
    config_store._state_cache = None


def test_profile_selection_acknowledgement() -> None:
    installation_profile.save(mode=installation_profile.DESKTOP_UI_ONLY, provider="codex")
    assert installation_profile.selection_pending()
    installation_profile.mark_selection_applied()
    assert not installation_profile.selection_pending()
    installation_profile.save(mode=installation_profile.DESKTOP_UI_ONLY, provider="codex")
    assert installation_profile.selection_pending()


def test_selected_provider_is_the_only_active_provider() -> None:
    installation_profile.save(mode=installation_profile.DEFAULT, provider="codex")
    config_path = Path(_HOME) / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "default_provider_id": "claude-id",
                "providers": [
                    {
                        "id": "claude-id",
                        "name": "Claude",
                        "kind": "claude",
                        "mode": "subscription",
                        "default_model": "opus",
                        "suspended": False,
                    },
                    {
                        "id": "codex-id",
                        "name": "Codex",
                        "kind": "codex",
                        "mode": "subscription",
                        "default_model": "gpt-5.5",
                        "suspended": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    _reset_config_cache()

    with patch.object(dependency_plan, "assert_state_transition_supported"):
        state = config_store.apply_installation_profile_selection()

    assert state["default_provider_id"] == "codex-id"
    by_id = {provider["id"]: provider for provider in state["providers"]}
    assert by_id["codex-id"]["suspended"] is False
    assert by_id["claude-id"]["suspended"] is True
    assert not installation_profile.selection_pending()


def test_runtime_plan_uses_pending_selection_then_active_config() -> None:
    installation_profile.save(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    pending = dependency_plan.resolve_plan()
    assert pending["requirements"] == ("requirements.txt",)

    installation_profile.mark_selection_applied()
    config_path = Path(_HOME) / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "default_provider_id": "claude-id",
                "providers": [
                    {
                        "id": "claude-id",
                        "name": "Claude",
                        "kind": "claude",
                        "mode": "subscription",
                        "suspended": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    active = dependency_plan.resolve_plan()
    assert active["requirements"] == (
        "requirements.txt",
        "requirements-claude.txt",
    )


def test_unknown_active_provider_requirement_fails_closed() -> None:
    installation_profile.save(mode=installation_profile.DEFAULT, provider="codex")
    installation_profile.mark_selection_applied()
    config_path = Path(_HOME) / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "default_provider_id": "unknown-id",
                "providers": [
                    {
                        "id": "unknown-id",
                        "name": "Unknown",
                        "kind": "unknown",
                        "mode": "subscription",
                        "suspended": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        dependency_plan.resolve_plan()
    except dependency_plan.DependencyPlanError:
        pass
    else:
        raise AssertionError("unknown active provider requirement must fail closed")


def test_provider_mutation_rejects_missing_runtime_before_persist() -> None:
    installation_profile.save(mode=installation_profile.DEFAULT, provider="codex")
    installation_profile.mark_selection_applied()
    config_path = Path(_HOME) / "config.json"
    config_path.unlink(missing_ok=True)
    _reset_config_cache()
    before = config_store.list_providers()

    with patch.object(dependency_plan, "_module_available", return_value=False):
        try:
            config_store.add_provider(
                {
                    "name": "Claude",
                    "kind": "claude",
                    "mode": "subscription",
                    "suspended": False,
                }
            )
        except dependency_plan.DependencyPlanError:
            pass
        else:
            raise AssertionError("missing provider runtime must reject mutation")

    assert config_store.list_providers() == before


def test_provider_plan_transition_requires_activated_candidate() -> None:
    installation_profile.save(mode=installation_profile.DEFAULT, provider="codex")
    installation_profile.mark_selection_applied()
    current = {
        "default_provider_id": "codex-id",
        "providers": [
            {
                "id": "codex-id",
                "kind": "codex",
                "suspended": False,
            }
        ],
    }
    candidate = {
        "default_provider_id": "claude-id",
        "providers": [
            {
                "id": "claude-id",
                "kind": "claude",
                "suspended": False,
            }
        ],
    }
    (Path(_HOME) / "config.json").write_text(
        json.dumps(current),
        encoding="utf-8",
    )
    candidate_hash = dependency_plan.resolve_plan(candidate)["hash"]
    with patch.object(
        dependency_plan,
        "active_env",
        return_value=Path("/tmp/not-the-candidate"),
    ):
        try:
            dependency_plan.assert_state_transition_supported(candidate)
        except dependency_plan.DependencyPlanError:
            pass
        else:
            raise AssertionError("inactive candidate plan must fail closed")
    with patch.object(
        dependency_plan,
        "active_env",
        return_value=Path("/tmp") / candidate_hash,
    ):
        dependency_plan.assert_state_transition_supported(candidate)


def test_provider_activation_mutations_reject_missing_runtime() -> None:
    installation_profile.save(mode=installation_profile.DEFAULT, provider="codex")
    installation_profile.mark_selection_applied()
    config_path = Path(_HOME) / "config.json"
    config_path.unlink(missing_ok=True)
    _reset_config_cache()
    suspended = config_store.add_provider(
        {
            "name": "Claude",
            "kind": "claude",
            "mode": "subscription",
            "suspended": True,
        }
    )
    active_id = config_store.list_providers()["default_provider_id"]

    with patch.object(dependency_plan, "_module_available", return_value=False):
        for mutation in (
            lambda: config_store.update_provider(
                suspended["id"], {"suspended": False}
            ),
            lambda: config_store.set_provider_suspended(suspended["id"], False),
            lambda: config_store.update_provider(
                active_id, {"kind": "claude"}
            ),
            lambda: config_store.import_provider_sync_state(
                {
                    "default_provider_id": suspended["id"],
                    "providers": [
                        {
                            "id": suspended["id"],
                            "name": "Claude",
                            "kind": "claude",
                            "mode": "subscription",
                            "suspended": False,
                        }
                    ],
                }
            ),
        ):
            try:
                mutation()
            except dependency_plan.DependencyPlanError:
                pass
            else:
                raise AssertionError("provider activation must require its runtime")

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    claude = next(
        provider
        for provider in persisted["providers"]
        if provider["id"] == suspended["id"]
    )
    assert claude["suspended"] is True
    assert persisted["default_provider_id"] != suspended["id"]


def test_failed_dependency_stage_preserves_active_environment() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-dependency-stage-") as tmp:
        root = Path(tmp)
        venv_root = root / ".venvs"
        pointer = root / ".active-venv"
        old_python = venv_root / "old" / "bin" / "python"
        old_python.parent.mkdir(parents=True)
        old_python.write_text("", encoding="utf-8")
        pointer.write_text(".venvs/old", encoding="utf-8")
        plan = {
            "hash": "new",
            "requirements": ("requirements.txt",),
            "probes": ("fastapi",),
        }

        def failed_install(command, **_kwargs):
            if command[1] == "venv":
                stage_python = Path(command[2]) / "bin" / "python"
                stage_python.parent.mkdir(parents=True)
                stage_python.write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)
            raise subprocess.CalledProcessError(1, command)

        with (
            patch.object(dependency_plan, "VENV_ROOT", venv_root),
            patch.object(dependency_plan, "ACTIVE_POINTER", pointer),
            patch.object(dependency_plan, "resolve_plan", return_value=plan),
            patch.object(dependency_plan.subprocess, "run", side_effect=failed_install),
        ):
            try:
                dependency_plan.activate("uv")
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("failed dependency install must abort activation")

        assert pointer.read_text(encoding="utf-8") == ".venvs/old"
        assert old_python.is_file()
        assert not (venv_root / "new").exists()


def test_selection_failure_restores_previous_pointer() -> None:
    installation_profile.save(mode=installation_profile.DEFAULT, provider="codex")
    with tempfile.TemporaryDirectory(prefix="ba-dependency-selection-") as tmp:
        root = Path(tmp)
        venv_root = root / ".venvs"
        pointer = root / ".active-venv"
        python = venv_root / "new" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        pointer.write_text(".venvs/old", encoding="utf-8")
        plan = {
            "hash": "new",
            "requirements": ("requirements.txt",),
            "probes": ("fastapi",),
        }

        def write_candidate(_env: Path) -> None:
            pointer.write_text(".venvs/new", encoding="utf-8")

        with (
            patch.object(dependency_plan, "VENV_ROOT", venv_root),
            patch.object(dependency_plan, "ACTIVE_POINTER", pointer),
            patch.object(dependency_plan, "resolve_plan", return_value=plan),
            patch.object(dependency_plan, "_write_pointer", side_effect=write_candidate),
            patch.object(
                dependency_plan,
                "_apply_pending_selection",
                side_effect=RuntimeError("config commit failed"),
            ),
        ):
            try:
                dependency_plan.activate("uv")
            except RuntimeError:
                pass
            else:
                raise AssertionError("failed config commit must abort activation")

        assert pointer.read_text(encoding="utf-8") == ".venvs/old"


def test_desktop_profile_excludes_native_dependencies() -> None:
    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    mobile = json.loads(
        (ROOT / "frontend" / "mobile-dependencies.json").read_text(encoding="utf-8")
    )
    native_packages = set(mobile)
    assert native_packages
    assert native_packages.isdisjoint(package["dependencies"])
    assert "claude-agent-sdk" not in (
        BACKEND / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert "firebase-admin" not in (
        BACKEND / "requirements.txt"
    ).read_text(encoding="utf-8")
    run_source = (ROOT / "run.sh").read_text(encoding="utf-8")
    installer_source = (
        ROOT / "frontend" / "scripts" / "install-frontend-deps.mjs"
    ).read_text(encoding="utf-8")
    assert "npm run install:desktop-deps" in run_source
    assert (
        "dependency_files=(package.json mobile-dependencies.json "
        "package-lock.mobile.json)"
    ) in run_source
    assert 'npm_project_hash "$project_dir" "${dependency_files[@]}"' in run_source
    assert "package-lock.mobile.json" in installer_source


def test_ui_only_rejects_team_session_creation() -> None:
    installation_profile.save(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    try:
        session_manager.manager.create(
            name="blocked",
            cwd=str(ROOT),
            orchestration_mode="team",
        )
    except session_manager.IncompatibleOrchestrationMode:
        pass
    else:
        raise AssertionError("UI-only must reject persisted team sessions")


def test_ui_only_ignores_stale_supervisor_registry() -> None:
    installation_profile.save(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    write_json(
        registry_path(),
        {
            "daemons": {
                "stale:worker": {
                    "extension_id": "stale",
                    "name": "worker",
                    "module": "stale.worker",
                    "lifecycle": "supervisor",
                    "restart_policy": {},
                    "env_allowlist": [],
                    "ports": [],
                    "source_root": str(ROOT),
                }
            }
        },
    )
    host = DaemonHost(poll_interval=0.01)
    host.reconcile_once()
    assert read_json(state_path()).get("daemons") == {}


def test_ui_only_quiesces_durable_jobs() -> None:
    installation_profile.save(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    extension_jobs.persist_running("example", "work", "job-1", phase="running")
    asyncio.run(extension_jobs.quiesce_for_ui_only())
    record = extension_jobs.read_record("example", "work", "job-1")
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == "cancelled by UI-only installation mode"


def test_ui_only_cleanup_failure_is_not_ignored() -> None:
    installation_profile.save(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    invalid = Path(_HOME) / "extension_jobs" / "example" / "work" / "bad.json"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text("{", encoding="utf-8")
    try:
        asyncio.run(extension_jobs.quiesce_for_ui_only())
    except RuntimeError:
        pass
    else:
        raise AssertionError("invalid durable job state must fail UI-only cleanup")
    invalid.unlink()


if __name__ == "__main__":
    test_profile_selection_acknowledgement()
    test_selected_provider_is_the_only_active_provider()
    test_runtime_plan_uses_pending_selection_then_active_config()
    test_unknown_active_provider_requirement_fails_closed()
    test_provider_mutation_rejects_missing_runtime_before_persist()
    test_provider_plan_transition_requires_activated_candidate()
    test_provider_activation_mutations_reject_missing_runtime()
    test_failed_dependency_stage_preserves_active_environment()
    test_selection_failure_restores_previous_pointer()
    test_desktop_profile_excludes_native_dependencies()
    test_ui_only_rejects_team_session_creation()
    test_ui_only_ignores_stale_supervisor_registry()
    test_ui_only_quiesces_durable_jobs()
    test_ui_only_cleanup_failure_is_not_ignored()
    print("installation runtime policy tests passed")
