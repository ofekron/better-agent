from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-resolver-default-")

import config_store
import extension_store
import harness_profile_resolver
import harness_profile_store
import installation_profile

installation_profile.integrations_enabled = lambda: True

_FIXTURE_BROWSER_HARNESS_EXTENSION_ID = "fixture.browser-harness"


def _install_browser_harness_extension_with_headless_setting() -> None:
    """Installs a minimal runtime-ready browserHarness-role extension whose
    manifest declares the "headless" boolean setting, matching the shape the
    real browser-harness extension's settings schema uses. Written directly
    via the store internals (same pattern as other fixture installs in this
    test suite), so PATCH /api/extensions/{id}/settings and
    compute_default_profile() exercise the real settings read/write path."""
    package = Path(_TMP_HOME) / "browser-harness-extension"
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": _FIXTURE_BROWSER_HARNESS_EXTENSION_ID,
        "core_roles": ["browser-harness"],
        "name": "Browser Harness",
        "version": "1.0.0",
        "description": "Browser Harness",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [
                {"key": "headless", "label": "Headless", "type": "boolean", "default": False},
            ],
        },
        "permissions": {},
        "marketplace": {},
    }
    manifest.setdefault("protocol", {
        "version": 1,
        "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
    })
    validated = extension_store.validate_manifest(manifest)
    (package / "better-agent-extension.json").write_text(json.dumps(validated), encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID] = {
        "manifest": validated,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/browser-harness",
            "ref": "",
            "commit_sha": "browser-harness-fixture",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def test_zero_override_profile_resolves_identically_to_default() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    config_store.set_disabled_builtin_extensions([])
    harness_profile_store.create_profile({"id": "zero.override", "name": "Zero Override"})
    default_resolved = harness_profile_resolver.resolve_profile("default")
    named_resolved = harness_profile_resolver.resolve_profile("zero.override")
    assert named_resolved["extension_instances"] == default_resolved["extension_instances"]
    assert named_resolved["disabled_builtin_tools"]["resolved"] == default_resolved["disabled_builtin_tools"]["resolved"]
    assert named_resolved["disabled_builtin_tools"]["override"] is None
    assert named_resolved["disabled_builtin_extensions"]["resolved"] == default_resolved["disabled_builtin_extensions"]["resolved"]
    assert named_resolved["disabled_builtin_extensions"]["override"] is None


def test_overridden_field_stays_pinned_across_default_change() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    harness_profile_store.create_profile({"id": "pinned.tools", "name": "Pinned Tools"})
    harness_profile_store.apply_override_patch(
        "pinned.tools",
        [{
            "path": ["disabled_builtin_tools"],
            "op": "set",
            "value": {"add": ["mssg"], "remove": []},
        }],
    )
    before = harness_profile_resolver.resolve_profile("pinned.tools")
    assert set(before["disabled_builtin_tools"]["resolved"]) == {"ask", "mssg"}
    assert before["disabled_builtin_tools"]["override"] == {"add": ["mssg"], "remove": []}

    # Mutate live Default's disabled_builtin_tools out from under the profile.
    config_store.set_disabled_builtin_tools(["create_session", "delegate_task"])

    after = harness_profile_resolver.resolve_profile("pinned.tools")
    # The override recomputes as a delta over the NEW Default base (the
    # override's "add" is still honored; it's not a frozen snapshot of the
    # merged list) but the override delta itself (what the user actually
    # set) must stay pinned, unaffected by the Default mutation.
    assert after["disabled_builtin_tools"]["override"] == {"add": ["mssg"], "remove": []}
    assert "mssg" in after["disabled_builtin_tools"]["resolved"]


def test_unoverridden_field_tracks_default_live() -> None:
    config_store.set_disabled_builtin_extensions([])
    harness_profile_store.create_profile({"id": "tracks.default", "name": "Tracks Default"})
    before = harness_profile_resolver.resolve_profile("tracks.default")
    assert before["disabled_builtin_extensions"]["resolved"] == []
    assert before["disabled_builtin_extensions"]["override"] is None

    config_store.set_disabled_builtin_extensions(["ofek-dev.todos"])

    after = harness_profile_resolver.resolve_profile("tracks.default")
    assert after["disabled_builtin_extensions"]["resolved"] == ["ofek-dev.todos"]
    assert after["disabled_builtin_extensions"]["override"] is None


def test_clearing_override_reverts_to_tracking_default() -> None:
    config_store.set_disabled_builtin_tools(["ask"])
    harness_profile_store.create_profile({"id": "clear.me", "name": "Clear Me"})
    harness_profile_store.apply_override_patch(
        "clear.me",
        [{
            "path": ["disabled_builtin_tools"],
            "op": "set",
            "value": {"add": ["mssg"], "remove": []},
        }],
    )
    overridden = harness_profile_resolver.resolve_profile("clear.me")
    assert overridden["disabled_builtin_tools"]["override"] is not None

    harness_profile_store.apply_override_patch(
        "clear.me",
        [{"path": ["disabled_builtin_tools"], "op": "clear"}],
    )
    cleared = harness_profile_resolver.resolve_profile("clear.me")
    assert cleared["disabled_builtin_tools"]["override"] is None
    assert cleared["disabled_builtin_tools"]["resolved"] == ["ask"]

    config_store.set_disabled_builtin_tools(["ask", "create_session"])
    tracked = harness_profile_resolver.resolve_profile("clear.me")
    assert tracked["disabled_builtin_tools"]["resolved"] == ["ask", "create_session"]


def test_default_headless_reflects_extension_setting_write() -> None:
    _install_browser_harness_extension_with_headless_setting()
    assert extension_store.is_extension_runtime_ready(_FIXTURE_BROWSER_HARNESS_EXTENSION_ID)

    before = harness_profile_resolver.compute_default_profile()
    assert before["extension_instances"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID]["headless"] is False

    # Same write path PATCH /api/extensions/{id}/settings uses.
    extension_store.set_extension_setting(_FIXTURE_BROWSER_HARNESS_EXTENSION_ID, "headless", True)

    after = harness_profile_resolver.compute_default_profile()
    assert after["extension_instances"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID]["headless"] is True


def main() -> int:
    test_zero_override_profile_resolves_identically_to_default()
    test_overridden_field_stays_pinned_across_default_change()
    test_unoverridden_field_tracks_default_live()
    test_clearing_override_reverts_to_tracking_default()
    test_default_headless_reflects_extension_setting_write()
    print("PASS harness profile resolver default synthesis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
