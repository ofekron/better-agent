from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from contextlib import contextmanager
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
import provider_setup
import session_manager


def _reset_config_cache() -> None:
    config_store._state_cache = None


def _stage_installation_profile(*, mode: str, provider: str) -> dict:
    command = provider_setup.installer_for(provider).command
    executable = str((Path(_HOME) / command).absolute())
    profile = installation_profile.new_active_profile(
        mode=mode,
        provider=provider,
        provider_identity={
            "command": command,
            "launcher_path": executable,
            "launcher_sha256": "0" * 64,
            "target_path": executable,
            "target_sha256": "0" * 64,
            "size": 0,
            "mtime_ns": 0,
        },
    )
    return installation_profile.stage_activation(profile)


def _ack_profile_for_dependency_tests() -> None:
    profile = installation_profile.require_active()
    receipt = {
        "schema_version": installation_profile.RECEIPT_SCHEMA_VERSION,
        "generation": profile["generation"],
        "profile_sha256": installation_profile._canonical_hash(profile),
        "provider_selection_sha256": "0" * 64,
        **installation_profile._active_environment_receipt(),
    }
    (Path(_HOME) / "installation-activation.json").write_text(
        json.dumps(receipt),
        encoding="utf-8",
    )


def _write_probe_wheel(root: Path) -> Path:
    wheel = root / "runtime_probe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("runtime_probe/__init__.py", "")
        archive.writestr(
            "runtime_probe/cli.py",
            "def main():\n    print('probe-ok')\n",
        )
        archive.writestr(
            "runtime_probe-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: runtime-probe\nVersion: 1.0\n",
        )
        archive.writestr(
            "runtime_probe-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: Better Agent test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "runtime_probe-1.0.dist-info/entry_points.txt",
            "[console_scripts]\nruntime-probe = runtime_probe.cli:main\n",
        )
        archive.writestr("runtime_probe-1.0.dist-info/RECORD", "")
    return wheel


def test_profile_selection_acknowledgement() -> None:
    _stage_installation_profile(mode=installation_profile.DESKTOP_UI_ONLY, provider="codex")
    assert installation_profile.selection_pending()
    try:
        installation_profile.mark_selection_applied()
    except installation_profile.InstallationProfileError:
        pass
    else:
        raise AssertionError("selection receipt must require the applied provider config")
    assert installation_profile.selection_pending()


def test_selected_provider_is_the_only_active_provider() -> None:
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
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
    _stage_installation_profile(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    pending = dependency_plan.resolve_plan()
    assert pending["requirements"] == ("requirements.txt",)

    _ack_profile_for_dependency_tests()
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
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
    _ack_profile_for_dependency_tests()
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


def test_suspended_provider_requirement_is_included() -> None:
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
    _ack_profile_for_dependency_tests()
    config_path = Path(_HOME) / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "default_provider_id": "codex-id",
                "providers": [
                    {
                        "id": "codex-id",
                        "kind": "codex",
                        "suspended": False,
                    },
                    {
                        "id": "claude-id",
                        "kind": "claude",
                        "suspended": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    plan = dependency_plan.resolve_plan()

    assert plan["provider_kinds"] == ("claude", "codex")
    assert plan["requirements"] == (
        "requirements.txt",
        "requirements-mobile.txt",
        "requirements-claude.txt",
    )


def test_unknown_suspended_provider_requirement_fails_closed() -> None:
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
    _ack_profile_for_dependency_tests()
    state = {
        "default_provider_id": "codex-id",
        "providers": [
            {
                "id": "codex-id",
                "kind": "codex",
                "suspended": False,
            },
            {
                "id": "unknown-id",
                "kind": "unknown",
                "suspended": True,
            },
        ],
    }
    config_path = Path(_HOME) / "config.json"
    config_path.write_text(json.dumps(state), encoding="utf-8")

    for operation in (
        dependency_plan.resolve_plan,
        lambda: dependency_plan.assert_state_supported(state),
    ):
        try:
            operation()
        except dependency_plan.DependencyPlanError:
            pass
        else:
            raise AssertionError("unknown suspended provider must fail closed")


def test_provider_mutation_rejects_missing_runtime_before_persist() -> None:
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
    _ack_profile_for_dependency_tests()
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
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
    _ack_profile_for_dependency_tests()
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
        "_assert_environment",
    ):
        dependency_plan.assert_state_transition_supported(candidate)


def test_provider_activation_mutations_reject_missing_runtime() -> None:
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
    _ack_profile_for_dependency_tests()
    config_path = Path(_HOME) / "config.json"
    config_path.unlink(missing_ok=True)
    _reset_config_cache()
    with patch.object(dependency_plan, "assert_state_transition_supported"):
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


def _write_credential_test_state() -> Path:
    config_path = Path(_HOME) / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "default_provider_id": "codex-id",
                "providers": [
                    {
                        "id": "codex-id",
                        "name": "Codex",
                        "kind": "codex",
                        "mode": "subscription",
                        "suspended": False,
                    },
                    {
                        "id": "api-id",
                        "name": "Claude API",
                        "kind": "claude",
                        "mode": "api_key",
                        "suspended": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    _reset_config_cache()
    return config_path


def _credential_mutations() -> tuple:
    return (
        lambda: config_store.add_provider(
            {
                "name": "New API",
                "kind": "claude",
                "mode": "api_key",
                "api_key": "new-secret",
                "suspended": True,
            }
        ),
        lambda: config_store.update_provider(
            "api-id",
            {"api_key": "new-secret"},
        ),
        lambda: config_store.delete_provider("api-id"),
        lambda: config_store.import_provider_sync_state(
            {
                "default_provider_id": "codex-id",
                "providers": [
                    {
                        "id": "codex-id",
                        "name": "Codex",
                        "kind": "codex",
                        "mode": "subscription",
                        "suspended": False,
                    },
                    {
                        "id": "api-id",
                        "name": "Claude API",
                        "kind": "claude",
                        "mode": "api_key",
                        "suspended": False,
                    },
                ],
                "provider_api_keys": [
                    {"provider_id": "api-id", "api_key": "new-secret"}
                ],
            }
        ),
    )


def test_rejected_provider_transitions_do_not_touch_credentials() -> None:
    config_path = _write_credential_test_state()
    before_config = config_path.read_text(encoding="utf-8")
    credentials = {"api-id": "old-secret"}

    def write_credential(provider_id: str, value: str) -> None:
        if value:
            credentials[provider_id] = value
        else:
            credentials.pop(provider_id, None)

    for mutation in _credential_mutations():
        with (
            patch.object(
                config_store,
                "_read_api_key_authoritative",
                side_effect=lambda pid: credentials.get(pid, ""),
            ),
            patch.object(config_store, "_write_api_key", side_effect=write_credential) as write,
            patch.object(dependency_plan, "assert_provider_supported"),
            patch.object(dependency_plan, "assert_state_supported"),
            patch.object(
                config_store,
                "_validate_state_for_save",
                side_effect=dependency_plan.DependencyPlanError("rejected"),
            ),
        ):
            try:
                mutation()
            except dependency_plan.DependencyPlanError:
                pass
            else:
                raise AssertionError("provider transition must be rejected")
        assert write.call_count == 0
        assert credentials == {"api-id": "old-secret"}
        assert config_path.read_text(encoding="utf-8") == before_config


def test_failed_provider_persist_rolls_back_credentials() -> None:
    config_path = _write_credential_test_state()
    before_config = config_path.read_text(encoding="utf-8")
    credentials = {"api-id": "old-secret"}

    def write_credential(provider_id: str, value: str) -> None:
        if value:
            credentials[provider_id] = value
        else:
            credentials.pop(provider_id, None)

    for mutation in _credential_mutations():
        with (
            patch.object(
                config_store,
                "_read_api_key_authoritative",
                side_effect=lambda pid: credentials.get(pid, ""),
            ),
            patch.object(config_store, "_write_api_key", side_effect=write_credential),
            patch.object(dependency_plan, "assert_provider_supported"),
            patch.object(dependency_plan, "assert_state_supported"),
            patch.object(config_store, "_validate_state_for_save"),
            patch.object(config_store, "_save_state", side_effect=OSError("disk failed")),
        ):
            try:
                mutation()
            except OSError:
                pass
            else:
                raise AssertionError("failed provider save must abort")
        assert credentials == {"api-id": "old-secret"}
        assert config_path.read_text(encoding="utf-8") == before_config


def test_credential_transaction_requires_authoritative_snapshot() -> None:
    with (
        patch.object(
            config_store.credential_session_client,
            "available",
            return_value=True,
        ),
        patch.object(
            config_store.credential_session_client,
            "request",
            return_value={"status": "blocked"},
        ),
        patch.object(config_store, "_write_api_key") as write,
    ):
        try:
            with config_store._credential_transaction(
                [("api-id", "new-secret")]
            ):
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError("blocked credential reads must fail closed")
    write.assert_not_called()


def test_credential_write_failure_rolls_back_attempted_mutation() -> None:
    credentials = {"api-id": "old-secret"}
    calls = 0

    def write_credential(provider_id: str, value: str) -> None:
        nonlocal calls
        calls += 1
        credentials[provider_id] = value
        if calls == 1:
            raise RuntimeError("verification failed after write")

    with (
        patch.object(
            config_store,
            "_read_api_key_authoritative",
            return_value="old-secret",
        ),
        patch.object(
            config_store,
            "_write_api_key",
            side_effect=write_credential,
        ),
    ):
        try:
            with config_store._credential_transaction(
                [("api-id", "new-secret")]
            ):
                pass
        except RuntimeError as exc:
            assert str(exc) == "verification failed after write"
        else:
            raise AssertionError("failed credential write must abort")
    assert calls == 2
    assert credentials == {"api-id": "old-secret"}


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
                stage_python = Path(command[3]) / "bin" / "python"
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
        assert not any((venv_root / "new").iterdir())


def test_selection_failure_restores_previous_pointer() -> None:
    _stage_installation_profile(mode=installation_profile.DEFAULT, provider="codex")
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
            patch.object(
                dependency_plan,
                "_resolve_or_build_environment",
                return_value=python.parent.parent,
            ),
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


def test_pending_selection_does_not_write_activation_stdout() -> None:
    with (
        patch.object(
            installation_profile,
            "selection_pending",
            return_value=True,
        ),
        patch.object(
            dependency_plan.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run,
    ):
        dependency_plan._apply_pending_selection(Path(sys.executable))
    assert run.call_args.kwargs["stdout"] is subprocess.DEVNULL


def test_activation_stdout_is_one_path_in_isolated_process() -> None:
    code = """
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
temporary = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "backend"))
import dependency_plan

selection = temporary / "selection.py"
selection.write_text("print('selection-noise')\\n", encoding="utf-8")
environment = temporary / "env"
plan = {"hash": "plan", "requirements": (), "probes": ()}
dependency_plan.__file__ = str(selection)
dependency_plan.ACTIVE_POINTER = temporary / ".active-venv"
dependency_plan.VENV_ROOT = temporary / ".venvs"
dependency_plan.resolve_plan = lambda: plan
dependency_plan._resolve_or_build_environment = lambda _uv, _plan: environment
dependency_plan._python_in = lambda _env: pathlib.Path(sys.executable)
dependency_plan._write_pointer = lambda _env: None
dependency_plan.installation_profile.selection_pending = lambda: True
sys.argv = ["dependency_plan.py", "activate", "--uv", "uv"]
raise SystemExit(dependency_plan.main())
"""
    with tempfile.TemporaryDirectory(prefix="ba-activation-stdout-") as tmp:
        result = subprocess.run(
            [sys.executable, "-c", code, str(ROOT), tmp],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout == f"{Path(tmp) / 'env'}\n"
        assert "selection-noise" not in result.stdout


def test_activation_lock_serializes_processes() -> None:
    code = """
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
temporary = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "backend"))
import dependency_plan

dependency_plan.VENV_ROOT = temporary / ".venvs"
print("ready", flush=True)
with dependency_plan.activation_lock():
    (temporary / "acquired").write_text("yes", encoding="utf-8")
"""
    with tempfile.TemporaryDirectory(prefix="ba-activation-lock-") as tmp:
        temporary = Path(tmp)
        with patch.object(
            dependency_plan,
            "VENV_ROOT",
            temporary / ".venvs",
        ):
            with dependency_plan.activation_lock():
                process = subprocess.Popen(
                    [sys.executable, "-c", code, str(ROOT), tmp],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    assert process.stdout is not None
                    assert process.stdout.readline().strip() == "ready"
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        raise AssertionError(
                            "contending activation process bypassed the lock"
                        )
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
            process.wait(timeout=5)
            if process.returncode != 0:
                assert process.stderr is not None
                raise AssertionError(process.stderr.read())
        assert (temporary / "acquired").read_text(encoding="utf-8") == "yes"


def test_activation_lock_routes_windows_to_kernel_mutex() -> None:
    calls: list[Path] = []

    @contextmanager
    def fake_mutex(path: Path):
        calls.append(path)
        yield

    with tempfile.TemporaryDirectory(prefix="ba-windows-lock-") as tmp:
        with (
            patch.object(dependency_plan, "VENV_ROOT", Path(tmp) / ".venvs"),
            patch.object(dependency_plan.os, "name", "nt"),
            patch.object(
                dependency_plan,
                "_windows_activation_mutex",
                side_effect=fake_mutex,
            ),
        ):
            with dependency_plan.activation_lock():
                pass
    assert calls == [Path(tmp) / ".dependency-plan.lock"]


def test_activation_rechecks_plan_before_pointer_swap() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-plan-recheck-") as tmp:
        backend = Path(tmp)
        venv_root = backend / ".venvs"
        pointer = backend / ".active-venv"
        candidate = venv_root / "old-plan" / "candidate"
        pointer.write_text(".venvs/existing", encoding="utf-8")
        old_plan = {"hash": "old-plan", "requirements": (), "probes": ()}
        new_plan = {"hash": "new-plan", "requirements": (), "probes": ()}
        with (
            patch.object(dependency_plan, "VENV_ROOT", venv_root),
            patch.object(dependency_plan, "ACTIVE_POINTER", pointer),
            patch.object(
                dependency_plan,
                "resolve_plan",
                side_effect=(old_plan, new_plan),
            ),
            patch.object(
                dependency_plan,
                "_resolve_or_build_environment",
                return_value=candidate,
            ),
            patch.object(
                installation_profile,
                "selection_pending",
                return_value=False,
            ),
        ):
            try:
                dependency_plan.activate("uv")
            except dependency_plan.DependencyPlanError:
                pass
            else:
                raise AssertionError("changed activation plan must fail closed")
        assert pointer.read_text(encoding="utf-8") == ".venvs/existing"


def test_environment_marker_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-plan-marker-") as tmp:
        env_dir = Path(tmp) / "env"
        python = dependency_plan._python_in(env_dir)
        python.parent.mkdir(parents=True)
        shutil.copy2(sys.executable, python)
        marker = env_dir / dependency_plan.PLAN_MARKER
        plan = {"hash": "expected", "requirements": (), "probes": ("json",)}
        invalid_values = (
            None,
            "{",
            json.dumps({"schema_version": 1, "hash": "wrong"}),
        )
        for value in invalid_values:
            marker.unlink(missing_ok=True)
            if value is not None:
                marker.write_text(value, encoding="utf-8")
            try:
                dependency_plan._assert_environment(env_dir, plan)
            except dependency_plan.DependencyPlanError:
                pass
            else:
                raise AssertionError("invalid environment marker must fail closed")


def test_relocated_environment_runs_and_repairs_missing_probe() -> None:
    uv = shutil.which("uv")
    assert uv is not None
    with tempfile.TemporaryDirectory(prefix="ba-relocatable-venv-") as tmp:
        backend = Path(tmp) / "backend"
        venv_root = backend / ".venvs"
        pointer = backend / ".active-venv"
        backend.mkdir()
        wheel = _write_probe_wheel(backend)
        (backend / "requirements.txt").write_text(
            f"{wheel.as_uri()}\n",
            encoding="utf-8",
        )
        plan = {
            "hash": "probe-plan",
            "requirements": ("requirements.txt",),
            "probes": ("runtime_probe",),
        }
        with (
            patch.object(dependency_plan, "BACKEND", backend),
            patch.object(dependency_plan, "VENV_ROOT", venv_root),
            patch.object(dependency_plan, "ACTIVE_POINTER", pointer),
            patch.object(dependency_plan, "resolve_plan", return_value=plan),
            patch.object(
                installation_profile,
                "selection_pending",
                return_value=False,
            ),
        ):
            first = dependency_plan.activate(uv)
            script = (
                first / "Scripts" / "runtime-probe.exe"
                if os.name == "nt"
                else first / "bin" / "runtime-probe"
            )
            result = subprocess.run(
                [str(script)],
                check=True,
                capture_output=True,
                text=True,
            )
            assert result.stdout.strip() == "probe-ok"

            package_dir = subprocess.run(
                [
                    str(dependency_plan._python_in(first)),
                    "-c",
                    "import pathlib,runtime_probe;"
                    "print(pathlib.Path(runtime_probe.__file__).parent)",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            shutil.rmtree(package_dir)
            try:
                dependency_plan.assert_active()
            except dependency_plan.DependencyPlanError:
                pass
            else:
                raise AssertionError("active environment missing a probe must fail")

            repaired = dependency_plan.activate(uv)
            assert repaired != first
            assert first.exists()
            dependency_plan.assert_active()


def test_target_checkout_rejects_stale_dependency_environment() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-target-dependency-") as tmp:
        backend = Path(tmp)
        env_dir = backend / ".venvs" / "candidate"
        if os.name == "nt":
            venv.EnvBuilder(with_pip=False).create(env_dir)
        else:
            python = dependency_plan._python_in(env_dir)
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable))
        (backend / ".active-venv").write_text(
            ".venvs/candidate",
            encoding="utf-8",
        )
        planner = backend / "dependency_plan.py"
        planner.write_text("raise SystemExit(1)\n", encoding="utf-8")
        try:
            dependency_plan.verified_active_env(backend)
        except dependency_plan.DependencyPlanError:
            pass
        else:
            raise AssertionError("stale target dependency plan must fail closed")
        planner.write_text("raise SystemExit(0)\n", encoding="utf-8")
        assert dependency_plan.verified_active_env(backend) == env_dir.resolve()


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
    node_source = (ROOT / "run_node.sh").read_text(encoding="utf-8")
    assert 'exec "$PY" -m uvicorn main_node:app' in node_source
    assert 'bin/uvicorn' not in node_source


def test_ui_only_rejects_team_session_creation() -> None:
    _stage_installation_profile(
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
    _stage_installation_profile(
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
    _stage_installation_profile(
        mode=installation_profile.DESKTOP_UI_ONLY,
        provider="codex",
    )
    extension_jobs.persist_running("example", "work", "job-1", phase="running")
    asyncio.run(extension_jobs.quiesce_for_ui_only())
    record = extension_jobs.read_record("example", "work", "job-1")
    assert record is not None
    assert record["status"] == "cancelled"
    assert record["error"] == "cancelled by UI-only installation mode"


def test_ui_only_cleanup_failure_is_not_ignored() -> None:
    _stage_installation_profile(
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
    test_suspended_provider_requirement_is_included()
    test_unknown_suspended_provider_requirement_fails_closed()
    test_provider_mutation_rejects_missing_runtime_before_persist()
    test_provider_plan_transition_requires_activated_candidate()
    test_provider_activation_mutations_reject_missing_runtime()
    test_rejected_provider_transitions_do_not_touch_credentials()
    test_failed_provider_persist_rolls_back_credentials()
    test_credential_transaction_requires_authoritative_snapshot()
    test_credential_write_failure_rolls_back_attempted_mutation()
    test_failed_dependency_stage_preserves_active_environment()
    test_selection_failure_restores_previous_pointer()
    test_pending_selection_does_not_write_activation_stdout()
    test_activation_stdout_is_one_path_in_isolated_process()
    test_activation_lock_serializes_processes()
    test_activation_lock_routes_windows_to_kernel_mutex()
    test_activation_rechecks_plan_before_pointer_swap()
    test_environment_marker_fails_closed()
    test_relocated_environment_runs_and_repairs_missing_probe()
    test_target_checkout_rejects_stale_dependency_environment()
    test_desktop_profile_excludes_native_dependencies()
    test_ui_only_rejects_team_session_creation()
    test_ui_only_ignores_stale_supervisor_registry()
    test_ui_only_quiesces_durable_jobs()
    test_ui_only_cleanup_failure_is_not_ignored()
    print("installation runtime policy tests passed")
