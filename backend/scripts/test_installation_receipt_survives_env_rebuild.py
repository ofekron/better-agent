"""A dependency-environment rebuild must not revoke a committed installation.

Requirements changes rehash the dependency plan, so boot builds a new venv and
repoints `.active-venv`. That must leave the activation receipt valid, and the
receipt re-stamp must never fall through to the installer's provider selection,
which suspends every provider the user configured after setup.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

import _test_home

_HOME = Path(_test_home.isolate(prefix="ba-install-receipt-"))

import _test_installation
import installation_profile


def _rebuild_environment(plan_hash: str) -> None:
    backend = installation_profile.BACKEND_ROOT
    environment = backend / ".venvs" / plan_hash / "build"
    environment.mkdir(parents=True, exist_ok=True)
    (environment / ".dependency-plan.json").write_text(
        json.dumps({"schema_version": 1, "hash": plan_hash}),
        encoding="utf-8",
    )
    (backend / ".active-venv").write_text(
        f".venvs/{plan_hash}/build",
        encoding="utf-8",
    )


def _configure_extra_providers() -> list[dict]:
    providers = [
        {"id": "claude-id", "kind": "claude", "suspended": False},
        {"id": "codex-id", "kind": "codex", "suspended": False},
        {"id": "agy-id", "kind": "agy", "suspended": False},
    ]
    (_HOME / "config.json").write_text(
        json.dumps({"default_provider_id": "claude-id", "providers": providers}),
        encoding="utf-8",
    )
    return providers


def test_environment_rebuild_keeps_provider_conversations_enabled() -> None:
    _test_installation.activate(_HOME)
    _rebuild_environment("rebuilt-plan-hash")
    assert not installation_profile.selection_pending()
    assert installation_profile.allows(installation_profile.PROVIDER_CONVERSATIONS)
    assert installation_profile.capabilities()["setup_required"] is False


def test_broken_environment_pointer_does_not_revoke_the_installation() -> None:
    """The pointer is validated where it is used — when an activation is
    committed and when run.sh picks the interpreter. A capability read must not
    depend on it, or a rebuilt or unreadable cache revokes a real install."""
    _test_installation.activate(_HOME)
    installation_profile.capture_active_capabilities()
    (installation_profile.BACKEND_ROOT / ".active-venv").write_text(
        "../escape",
        encoding="utf-8",
    )
    assert installation_profile.allows(installation_profile.PROVIDER_CONVERSATIONS)
    try:
        installation_profile.mark_selection_applied()
    except installation_profile.InstallationProfileError:
        pass
    else:
        raise AssertionError("committing an activation must still fail closed")


def test_legacy_receipt_is_honoured_then_restamped() -> None:
    """A receipt written in an older encoding still records the activation
    commit for this generation. It is honoured immediately — a runtime that
    never re-activates must not lose its installation — and normalised in
    place, without reapplying the installer's provider selection."""
    profile = _test_installation.activate(_HOME)
    receipt_path = _HOME / "installation-activation.json"
    committed = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_path.write_text(
        json.dumps({
            "schema_version": 1,
            "generation": committed["generation"],
            "profile_sha256": committed["profile_sha256"],
            "active_env": ".venvs/stale",
            "dependency_plan_hash": "stale",
        }),
        encoding="utf-8",
    )
    providers = _configure_extra_providers()
    assert not installation_profile.selection_pending(), (
        "an older receipt encoding still proves setup ran"
    )
    assert installation_profile.refresh_activation_receipt()
    assert not installation_profile.selection_pending()
    restamped = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert restamped == {
        "schema_version": installation_profile.RECEIPT_SCHEMA_VERSION,
        "generation": profile["generation"],
        "profile_sha256": committed["profile_sha256"],
    }
    state = json.loads((_HOME / "config.json").read_text(encoding="utf-8"))
    assert state["providers"] == providers
    assert state["default_provider_id"] == "claude-id"


def test_receipt_from_another_generation_is_not_restamped() -> None:
    _test_installation.activate(_HOME)
    receipt_path = _HOME / "installation-activation.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["generation"] = "0" * 32
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert not installation_profile.refresh_activation_receipt()
    assert installation_profile.selection_pending()


if __name__ == "__main__":
    try:
        test_environment_rebuild_keeps_provider_conversations_enabled()
        test_broken_environment_pointer_does_not_revoke_the_installation()
        test_legacy_receipt_is_honoured_then_restamped()
        test_receipt_from_another_generation_is_not_restamped()
        print("installation receipt rebuild tests passed")
    finally:
        _test_home.TestHome(str(_HOME)).release()
