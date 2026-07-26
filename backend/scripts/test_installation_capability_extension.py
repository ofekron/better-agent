"""The installation profile seeds setup; it never caps what can be used later.

Locks the inversion: install-time mode chooses the starting capability set, and
every capability stays reachable afterwards. Also locks the two ways an
installation used to revoke itself — a rebuilt dependency environment and a
changed provider selection — and the frozen-bundle case that has no dependency
environment to point at.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import _test_installation
import cli_paths
import config_store
import dependency_plan
import installation_capabilities
import installation_profile


@contextmanager
def _with_home():
    previous_home = os.environ.get("BETTER_AGENT_HOME")
    previous_legacy = os.environ.get("BETTER_CLAUDE_HOME")
    previous_backend = installation_profile.BACKEND_ROOT
    with tempfile.TemporaryDirectory(prefix="ba-capability-") as tmp:
        os.environ["BETTER_AGENT_HOME"] = tmp
        os.environ["BETTER_CLAUDE_HOME"] = tmp
        installation_profile.BACKEND_ROOT = Path(tmp) / "backend"
        try:
            yield Path(tmp)
        finally:
            installation_profile.BACKEND_ROOT = previous_backend
            for name, value in (
                ("BETTER_AGENT_HOME", previous_home),
                ("BETTER_CLAUDE_HOME", previous_legacy),
            ):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _restart() -> None:
    """Re-freeze the capability snapshot the way a fresh process would."""
    installation_profile.capture_active_capabilities()


def test_ui_only_install_can_enable_every_capability() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DESKTOP_UI_ONLY, provider="codex"
        )
        _restart()
        assert not installation_profile.integrations_enabled()
        assert not installation_profile.mobile_enabled()

        state = installation_profile.set_capability_enabled(
            installation_capabilities.INTEGRATIONS, True
        )
        detail = state["capabilities"][installation_capabilities.INTEGRATIONS]
        assert detail["enabled"] is True
        assert detail["restart_required"] is True, "must not claim it is already live"
        assert not installation_profile.integrations_enabled(), (
            "the running process keeps the capability set it started with"
        )

        _restart()
        assert installation_profile.integrations_enabled()
        assert installation_profile.capabilities()["capabilities"][
            installation_capabilities.INTEGRATIONS
        ]["restart_required"] is False
        installation_profile.assert_orchestration_mode_allowed("team")


def test_enabling_mobile_changes_the_dependency_plan() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DESKTOP_UI_ONLY, provider="codex"
        )
        _restart()
        before = dependency_plan.resolve_plan()["hash"]
        assert not installation_profile.capability_requested(installation_capabilities.MOBILE)

        installation_profile.set_capability_enabled(installation_capabilities.MOBILE, True)
        assert installation_profile.capability_requested(installation_capabilities.MOBILE)
        after = dependency_plan.resolve_plan()
        assert after["hash"] != before, "enabling mobile must re-plan the environment"
        assert "firebase_admin" in after["probes"]


def test_environment_rebuild_and_provider_change_never_revoke_the_install() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        _restart()
        assert installation_profile.integrations_enabled()

        backend = installation_profile.BACKEND_ROOT
        rebuilt = backend / ".venvs" / "rebuilt"
        rebuilt.mkdir(parents=True, exist_ok=True)
        (rebuilt / ".dependency-plan.json").write_text(
            json.dumps({"schema_version": 1, "hash": "rebuilt"}), encoding="utf-8"
        )
        (backend / ".active-venv").write_text(".venvs/rebuilt", encoding="utf-8")

        config = root / "config.json"
        state = json.loads(config.read_text(encoding="utf-8"))
        state["providers"].append(
            {"id": "claude-id", "kind": "claude", "suspended": False}
        )
        state["providers"][0]["suspended"] = True
        state["default_provider_id"] = "claude-id"
        config.write_text(json.dumps(state), encoding="utf-8")

        _restart()
        assert not installation_profile.selection_pending()
        assert installation_profile.integrations_enabled()
        assert installation_profile.capabilities()["status"] == "active"


def test_frozen_bundle_has_no_dependency_pointer_to_satisfy() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        backend = installation_profile.BACKEND_ROOT
        (backend / ".active-venv").unlink()
        _restart()
        assert installation_profile.capabilities()["status"] == "active", (
            "the dependency environment is a rebuildable cache, not a capability gate"
        )

        try:
            installation_profile.mark_selection_applied()
            raise AssertionError("a source checkout must still commit against a real env")
        except installation_profile.InstallationProfileError:
            pass

        sys.frozen = True  # type: ignore[attr-defined]
        try:
            installation_profile.mark_selection_applied()
            _restart()
            assert installation_profile.capabilities()["status"] == "active"
            assert installation_profile.integrations_enabled()
        finally:
            del sys.frozen  # type: ignore[attr-defined]


def test_provider_cli_stays_usable_across_an_in_place_upgrade() -> None:
    with _with_home() as root:
        profile = _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        _restart()
        launcher = Path(profile["provider_identity"]["launcher_path"])
        command = profile["provider_identity"]["command"]
        assert cli_paths.resolve_cli_binary(command) == str(launcher)

        launcher.write_bytes(b"#!/bin/sh\nexit 1\n")
        launcher.chmod(0o700)
        assert not installation_profile.executable_identity_matches(
            profile["provider_identity"]
        )
        assert cli_paths.resolve_cli_binary(command) == str(launcher), (
            "an upgraded CLI at the pinned path must stay resolvable"
        )

        import provider_setup

        installation_profile.repin_provider_executable(
            provider_setup.executable_identity(str(launcher))
        )
        assert not installation_profile.selection_pending(), (
            "re-pinning keeps the activation commit for this generation"
        )

        launcher.unlink()
        pinned, path = installation_profile.pinned_provider_executable(command)
        assert (pinned, path) == (False, None), (
            "a removed launcher must fall back to normal resolution, not report missing"
        )


def test_installer_selection_never_withdraws_configured_providers() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        config = root / "config.json"
        state = json.loads(config.read_text(encoding="utf-8"))
        state["providers"].append(
            {
                "id": "claude-id",
                "kind": "claude",
                "name": "Claude",
                "mode": "subscription",
                "suspended": False,
            }
        )
        state["default_provider_id"] = "claude-id"
        config.write_text(json.dumps(state), encoding="utf-8")

        config_store.apply_installation_profile_selection()

        saved = json.loads(config.read_text(encoding="utf-8"))
        assert saved["default_provider_id"] == "claude-id", "the user's default stands"
        assert not any(
            provider.get("suspended") for provider in saved["providers"]
        ), "setup must not suspend providers the user configured"


def test_configuring_a_provider_never_waits_on_its_runtime() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        dependency_plan.assert_state_supported(
            {"providers": [{"kind": "claude"}, {"kind": "codex"}]}
        )
        assert not hasattr(dependency_plan, "assert_state_transition_supported"), (
            "the transition block is what refused to add a provider"
        )


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
