"""Locks the extension-contributed app Settings surface.

An extension declares `entrypoints.settings_sections` and binds a setting to
one through `section`. Such a setting holds ONE app-wide value: it is rendered
in the app Settings page, excluded from the per-profile harness overlay group,
and reported as global scope. A tag rule may gate its marker sound on such a
setting through `marker.sound_setting`.

Run with:
    cd backend && .venv/bin/python scripts/test_extension_app_settings.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-extension-app-settings-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_SDK = str(Path(_BACKEND).parent / "sdk")
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)

import extension_app_settings  # noqa: E402
import extension_store  # noqa: E402
import harness_fields  # noqa: E402
import installation_profile  # noqa: E402

installation_profile.integrations_enabled = lambda: True  # type: ignore[assignment]

_EXT_ID = "test.app-settings"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _manifest(**overrides):
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": _EXT_ID,
        "name": "App settings fixture",
        "version": "1.0.0",
        "description": "fixture",
        "surfaces": [],
        "entrypoints": {
            "settings_sections": [
                {"id": "notifications", "label": "Notifications", "description": "How we reach you."},
            ],
            "settings": [
                {
                    "key": "play_sound",
                    "label": "Play a sound",
                    "type": "boolean",
                    "default": True,
                    "section": "notifications",
                },
                {"key": "api_base", "label": "API base", "type": "string"},
            ],
            "applied_config": {
                "tag_rules": [{
                    "tag": "NEEDS_USER_DECISION",
                    "marker": {
                        "color": "#ff8c00",
                        "tooltip": "Needs you",
                        "sound": True,
                        "sound_setting": "play_sound",
                    },
                }],
            },
        },
        "permissions": {},
        "marketplace": {},
    }
    manifest["entrypoints"].update(overrides)
    return manifest


def _rejects(raw, needle: str) -> bool:
    try:
        extension_store.validate_manifest(raw)
    except extension_store.ExtensionError as exc:
        if needle in str(exc):
            return True
        print(f"  wrong rejection for {needle!r}: {exc}")
        return False
    print(f"  expected rejection containing {needle!r}, got none")
    return False


def test_manifest_validation() -> None:
    validated = extension_store.validate_manifest(_manifest())
    settings = {item["key"]: item for item in validated["entrypoints"]["settings"]}
    assert settings["play_sound"].get("section") == "notifications", "section not preserved on the setting"
    assert "section" not in settings["api_base"], "unbound setting must not gain a section"
    assert validated["entrypoints"]["settings_sections"][0]["label"] == "Notifications", "section label not preserved"

    undeclared = _manifest()
    undeclared["entrypoints"]["settings"][0]["section"] = "nope"
    assert _rejects(undeclared, "settings_sections")

    secret = _manifest()
    secret["entrypoints"]["settings"][0] = {
        "key": "token", "label": "Token", "type": "secret", "section": "notifications",
    }
    assert _rejects(secret, "cannot declare a section")

    unknown_gate = _manifest()
    unknown_gate["entrypoints"]["applied_config"]["tag_rules"][0]["marker"]["sound_setting"] = "missing"
    assert _rejects(unknown_gate, "sound_setting")

    non_boolean_gate = _manifest()
    non_boolean_gate["entrypoints"]["applied_config"]["tag_rules"][0]["marker"]["sound_setting"] = "api_base"
    assert _rejects(non_boolean_gate, "sound_setting")

    print("  manifest validation ok")


def _install() -> None:
    package = Path(_TMP_HOME) / "extension-fixtures" / _EXT_ID
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    (package / "better-agent-extension.json").write_text(
        json.dumps(_manifest()), encoding="utf-8",
    )
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": _EXT_ID,
        },
    )
    extension_store.set_enabled(_EXT_ID, True)


def test_projection_and_scope() -> None:
    _install()

    sections = extension_app_settings.sections()
    section = next((s for s in sections if s["id"] == "notifications"), None)
    assert section is not None, f"notifications section missing from projection: {sections}"
    keys = [item["key"] for item in section["items"]]
    assert keys == ["play_sound"], f"only section-bound settings belong in the app section: {keys}"
    item = section["items"][0]
    assert item["value"] is True and item["extension_id"] == _EXT_ID, (
        f"projected item carries the wrong value/owner: {item}"
    )

    overlay_keys = [
        entry["name"] for entry in harness_fields._settings_group(_EXT_ID)["items"]
    ]
    assert overlay_keys == ["api_base"], (
        f"app-section settings must not be profile overlays: {overlay_keys}"
    )

    path = ["extension_instances", _EXT_ID, harness_fields.GROUP_SETTINGS, "play_sound"]
    assert harness_fields.scope_for(path) == harness_fields.SCOPE_GLOBAL, "app-section setting must be global scope"
    path = ["extension_instances", _EXT_ID, harness_fields.GROUP_SETTINGS, "api_base"]
    assert harness_fields.scope_for(path) == harness_fields.SCOPE_PROFILE, "plain setting must stay profile scope"

    extension_store.set_extension_setting(_EXT_ID, "play_sound", False)
    refreshed = next(
        s for s in extension_app_settings.sections() if s["id"] == "notifications"
    )
    assert refreshed["items"][0]["value"] is False, "written value did not reach the projection"

    print("  projection + scope ok")


def test_sdk_builders_match_core() -> None:
    from better_agent_sdk import Setting, SettingsSection

    section = SettingsSection(id="notifications", label="Notifications")
    setting = Setting(
        key="play_sound", label="Play a sound", type="boolean",
        default=True, section="notifications",
    )
    raw = _manifest()
    raw["entrypoints"]["settings_sections"] = [section.to_dict()]
    raw["entrypoints"]["settings"] = [setting.to_dict()]
    raw["entrypoints"]["applied_config"]["tag_rules"][0]["marker"]["sound_setting"] = "play_sound"
    validated = extension_store.validate_manifest(raw)
    assert validated["entrypoints"]["settings"][0].get("section") == "notifications", (
        "SDK-built setting did not survive core validation"
    )
    print("  SDK builders ok")


def main() -> int:
    tests = [
        ("manifest validation", test_manifest_validation),
        ("projection and scope", test_projection_and_scope),
        ("sdk builders match core", test_sdk_builders_match_core),
    ]
    failed = 0
    try:
        for label, runner in tests:
            try:
                runner()
            except AssertionError as exc:
                failed += 1
                print(f"{FAIL} {label}: {exc}")
            except Exception:
                failed += 1
                import traceback
                traceback.print_exc()
            else:
                print(f"{PASS} {label}")
        print(f"{len(tests) - failed}/{len(tests)} subtests passed")
        return 1 if failed else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
