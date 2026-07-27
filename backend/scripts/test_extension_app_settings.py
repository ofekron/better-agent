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


def test_manifest_validation() -> bool:
    validated = extension_store.validate_manifest(_manifest())
    settings = {item["key"]: item for item in validated["entrypoints"]["settings"]}
    if settings["play_sound"].get("section") != "notifications":
        print("  section not preserved on the setting")
        return False
    if "section" in settings["api_base"]:
        print("  unbound setting must not gain a section")
        return False
    if validated["entrypoints"]["settings_sections"][0]["label"] != "Notifications":
        print("  section label not preserved")
        return False

    undeclared = _manifest()
    undeclared["entrypoints"]["settings"][0]["section"] = "nope"
    if not _rejects(undeclared, "settings_sections"):
        return False

    secret = _manifest()
    secret["entrypoints"]["settings"][0] = {
        "key": "token", "label": "Token", "type": "secret", "section": "notifications",
    }
    if not _rejects(secret, "cannot declare a section"):
        return False

    unknown_gate = _manifest()
    unknown_gate["entrypoints"]["applied_config"]["tag_rules"][0]["marker"]["sound_setting"] = "missing"
    if not _rejects(unknown_gate, "sound_setting"):
        return False

    non_boolean_gate = _manifest()
    non_boolean_gate["entrypoints"]["applied_config"]["tag_rules"][0]["marker"]["sound_setting"] = "api_base"
    if not _rejects(non_boolean_gate, "sound_setting"):
        return False

    print("  manifest validation ok")
    return True


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


def test_projection_and_scope() -> bool:
    _install()

    sections = extension_app_settings.sections()
    section = next((s for s in sections if s["id"] == "notifications"), None)
    if section is None:
        print(f"  notifications section missing from projection: {sections}")
        return False
    keys = [item["key"] for item in section["items"]]
    if keys != ["play_sound"]:
        print(f"  only section-bound settings belong in the app section: {keys}")
        return False
    item = section["items"][0]
    if item["value"] is not True or item["extension_id"] != _EXT_ID:
        print(f"  projected item carries the wrong value/owner: {item}")
        return False

    overlay_keys = [
        entry["name"] for entry in harness_fields._settings_group(_EXT_ID)["items"]
    ]
    if overlay_keys != ["api_base"]:
        print(f"  app-section settings must not be profile overlays: {overlay_keys}")
        return False

    path = ["extension_instances", _EXT_ID, harness_fields.GROUP_SETTINGS, "play_sound"]
    if harness_fields.scope_for(path) != harness_fields.SCOPE_GLOBAL:
        print("  app-section setting must be global scope")
        return False
    path = ["extension_instances", _EXT_ID, harness_fields.GROUP_SETTINGS, "api_base"]
    if harness_fields.scope_for(path) != harness_fields.SCOPE_PROFILE:
        print("  plain setting must stay profile scope")
        return False

    extension_store.set_extension_setting(_EXT_ID, "play_sound", False)
    refreshed = next(
        s for s in extension_app_settings.sections() if s["id"] == "notifications"
    )
    if refreshed["items"][0]["value"] is not False:
        print("  written value did not reach the projection")
        return False

    print("  projection + scope ok")
    return True


def test_sdk_builders_match_core() -> bool:
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
    if validated["entrypoints"]["settings"][0].get("section") != "notifications":
        print("  SDK-built setting did not survive core validation")
        return False
    print("  SDK builders ok")
    return True


def main() -> int:
    try:
        results = [
            test_manifest_validation(),
            test_projection_and_scope(),
            test_sdk_builders_match_core(),
        ]
        if not all(results):
            print("FAIL test_extension_app_settings")
            return 1
        print("PASS test_extension_app_settings")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
