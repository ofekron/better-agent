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
from unittest.mock import patch
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
    installation_capabilities.forget_active()
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


def test_a_receipt_from_an_older_encoding_still_proves_setup_ran() -> None:
    """Most runtimes never call `dependency_plan.activate` — a frozen bundle, a
    container, a line attaching to an existing home. A change in how receipts
    are written must not take their installation away."""
    with _with_home() as root:
        profile = _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        receipt_path = root / "installation-activation.json"
        committed = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt_path.write_text(
            json.dumps({
                "schema_version": 2,
                "generation": profile["generation"],
                "profile_sha256": committed["profile_sha256"],
                "provider_selection_sha256": "0" * 64,
            }),
            encoding="utf-8",
        )
        _restart()
        assert installation_profile.capabilities()["setup_required"] is False
        assert installation_profile.allows(installation_profile.PROVIDER_CONVERSATIONS)
        assert installation_profile.integrations_enabled()


def test_a_status_refresh_cannot_move_the_pin_to_another_path() -> None:
    """A status refresh is background work with no user intent behind it. If it
    re-pinned whatever PATH resolves to, any executable answering `--version`
    from an earlier PATH entry would inherit the pin."""
    with _with_home() as root:
        import provider_setup

        legit = root / "legit-bin" / "codex"
        legit.parent.mkdir(parents=True, exist_ok=True)
        legit.write_bytes(b"#!/bin/sh\nexit 0\n")
        legit.chmod(0o700)
        _test_installation.activate(
            root,
            mode=installation_profile.DEFAULT,
            provider="codex",
            launcher_path=str(legit),
        )
        _restart()

        other = root / "other-bin" / "codex"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"#!/bin/sh\nexit 0\n")
        other.chmod(0o700)
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{other.parent}{os.pathsep}{previous_path}"
        try:
            provider_setup._repin_verified_executable(
                provider_setup.installer_for("codex")
            )
            recorded = installation_profile.load()["provider_identity"]
            assert recorded["launcher_path"] == str(legit), recorded["launcher_path"]
            assert cli_paths.resolve_cli_binary("codex") == str(legit)
        finally:
            os.environ["PATH"] = previous_path

        # The same refresh still records new bytes at the pinned path.
        legit.write_bytes(b"#!/bin/sh\nexit 2\n")
        legit.chmod(0o700)
        provider_setup._repin_verified_executable(
            provider_setup.installer_for("codex")
        )
        updated = installation_profile.load()["provider_identity"]
        assert updated["launcher_path"] == str(legit)
        assert installation_profile.executable_identity_matches(updated)
        assert not installation_profile.selection_pending()


def test_a_provider_without_its_runtime_fails_where_the_kind_is_known() -> None:
    """Configuring a provider never waits on its runtime, so selecting one
    before the activation that installs it is legitimate — and must say so
    instead of dying on an import inside a detached runner."""
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        import provider

        with patch.object(dependency_plan, "_module_available", return_value=False):
            try:
                provider._resolve_class("claude")
            except dependency_plan.DependencyPlanError as exc:
                assert "restart" in str(exc), str(exc)
            else:
                raise AssertionError("a missing provider runtime must be reported")


def test_setup_makes_the_provider_the_user_chose_the_default() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        config = root / "config.json"
        state = json.loads(config.read_text(encoding="utf-8"))
        state["providers"].append(
            {"id": "claude-id", "kind": "claude", "name": "Claude", "suspended": False}
        )
        state["default_provider_id"] = "claude-id"
        config.write_text(json.dumps(state), encoding="utf-8")

        config_store.apply_installation_profile_selection(make_default=True)
        chosen = json.loads(config.read_text(encoding="utf-8"))
        assert chosen["default_provider_id"] == "codex-id", (
            "answering the installer's provider question must move the default"
        )
        assert not any(p.get("suspended") for p in chosen["providers"])


def test_a_state_home_with_no_profile_adopts_an_installed_cli() -> None:
    """A fresh state home has no profile, so it can serve nothing. Adoption
    answers the one question setup would have asked from what is already on the
    machine, preferring what this home is already configured for."""
    with _with_home() as root:
        import installation_bootstrap
        import provider_setup

        assert installation_profile.load()["status"] == "setup_required"
        (root / "config.json").write_text(
            json.dumps({
                "default_provider_id": "codex-id",
                "providers": [{"id": "codex-id", "kind": "codex", "suspended": False}],
            }),
            encoding="utf-8",
        )
        assert installation_bootstrap.configured_provider_kind() == "codex"

        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        stub = bin_dir / provider_setup.installer_for("codex").command
        stub.write_bytes(b"#!/bin/sh\nexit 0\n")
        stub.chmod(0o700)
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{previous_path}"
        try:
            assert installation_bootstrap.adoptable_provider_kind() == "codex", (
                "adoption must prefer the kind this home is configured for"
            )
        finally:
            os.environ["PATH"] = previous_path


def test_readiness_rejects_a_receipt_that_is_not_this_commit() -> None:
    """The tolerant compare must stay narrow: it forgives the encoding, never
    a receipt for a different installation or a tampered one."""
    with _with_home() as root:
        profile = _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        receipt_path = root / "installation-activation.json"
        committed = json.loads(receipt_path.read_text(encoding="utf-8"))
        for broken in (
            {},
            {"generation": "0" * 32, "profile_sha256": committed["profile_sha256"]},
            {"generation": profile["generation"], "profile_sha256": "0" * 64},
            {"generation": profile["generation"]},
        ):
            receipt_path.write_text(json.dumps(broken), encoding="utf-8")
            _restart()
            assert installation_profile.capabilities()["setup_required"] is True, broken
            assert not installation_profile.allows(
                installation_profile.PROVIDER_CONVERSATIONS
            ), broken


def _apply_selection_argv(make_default: bool) -> list[str]:
    """The argv `_commit_activation` hands the apply-selection subprocess."""
    seen: list[list[str]] = []

    class _Completed:
        returncode = 0

    def _fake_run(argv, **_kwargs):
        seen.append(list(argv))
        return _Completed()

    with (
        patch.object(installation_profile, "selection_pending", lambda: True),
        patch.object(installation_profile, "refresh_activation_receipt", lambda: False),
        patch.object(dependency_plan.subprocess, "run", _fake_run),
    ):
        dependency_plan._commit_activation(Path("/usr/bin/python3"), make_default=make_default)
    return seen[0] if seen else []


def test_adoption_does_not_seize_a_default_the_user_already_had() -> None:
    """Setup asks the user which provider they want, so their answer moves the
    default. Adoption only infers one from whichever CLI is on the machine, so
    it must not. Asserted at the seam the flag actually crosses."""
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        assert "--make-default" in _apply_selection_argv(True)
        assert "--make-default" not in _apply_selection_argv(False)

        # The seam adoption crosses: setup activates with the user's answer,
        # adoption activates without one. Nothing here may touch this
        # checkout's dependency environment, so the activation's own side
        # effects are stubbed — `_write_pointer` writes to backend/.active-venv.
        forwarded: list[bool] = []
        with (
            patch.object(dependency_plan, "_assert_environment"),
            patch.object(dependency_plan, "_write_pointer") as pointer,
            patch.object(dependency_plan, "resolve_plan", return_value={"hash": "x"}),
            patch.object(installation_profile, "stage_activation"),
            patch.object(installation_profile, "selection_pending", lambda: False),
            patch.object(
                dependency_plan,
                "_commit_activation",
                lambda _python, make_default=False: forwarded.append(make_default),
            ),
        ):
            for chosen in (True, False):
                dependency_plan.activate_prepared_installation(
                    Path("/tmp/env"),
                    {"mode": "default"},
                    "uv",
                    make_default=chosen,
                )
            assert pointer.called
        assert forwarded == [True, False], forwarded

        config = root / "config.json"
        state = json.loads(config.read_text(encoding="utf-8"))
        state["providers"].append(
            {"id": "agy-id", "kind": "agy", "name": "AGY", "suspended": False}
        )
        state["default_provider_id"] = "agy-id"
        config.write_text(json.dumps(state), encoding="utf-8")
        config_store._state_cache = None

        config_store.apply_installation_profile_selection(make_default=False)
        adopted = json.loads(config.read_text(encoding="utf-8"))
        assert adopted["default_provider_id"] == "agy-id", (
            "adoption must leave the configured default alone"
        )
        assert not any(p.get("suspended") for p in adopted["providers"])
        config_store._state_cache = None


def test_a_pending_runtime_is_reported_rather_than_assumed_capable() -> None:
    with _with_home() as root:
        _test_installation.activate(
            root, mode=installation_profile.DEFAULT, provider="codex"
        )
        config_store._state_cache = None
        with patch.object(dependency_plan, "_module_available", return_value=False):
            added = config_store.add_provider({
                "name": "Claude",
                "kind": "claude",
                "mode": "subscription",
            })
            assert added["runtime_pending"] is True, (
                "a provider whose runtime is not installed must not read as resolved"
            )
        config_store._state_cache = None


def test_a_probe_that_cannot_be_inspected_reads_as_unavailable() -> None:
    assert dependency_plan._module_available("no.such.parent.module") is False
    assert dependency_plan._module_available("json") is True


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
