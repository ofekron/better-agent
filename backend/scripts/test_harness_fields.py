from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-fields-")

import config_store
import extension_store
import harness_field_writer
import harness_fields
import harness_profile_resolver
import harness_profile_store
import installation_profile

installation_profile.integrations_enabled = lambda: True

FIXTURE_ID = "fixture.consolidated"


def _install_fixture_extension() -> None:
    """A runtime-ready extension declaring one MCP server, one skill, one
    plain setting and one secret setting — enough surface to exercise every
    scope branch in the registry."""
    package = Path(_TMP_HOME) / "consolidated-extension"
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": FIXTURE_ID,
        "name": "Consolidated Fixture",
        "version": "1.0.0",
        "description": "Consolidated Fixture",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [{"name": "fixture_server", "label": "Fixture Server", "command": "true"}],
            "skills": [
                {"name": "fixture-skill", "path": "skills/fixture-skill", "description": "A fixture skill"}
            ],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [
                {"key": "endpoint", "label": "Endpoint", "type": "string", "default": "https://a.test"},
                {"key": "token", "label": "Token", "type": "secret"},
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
    data["extensions"][FIXTURE_ID] = {
        "manifest": validated,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/consolidated",
            "ref": "",
            "commit_sha": "consolidated-fixture",
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


def _group(extension_id: str, group_id: str) -> dict:
    descriptor = harness_fields.descriptor()
    entry = next(item for item in descriptor["extensions"] if item["id"] == extension_id)
    return next(group for group in entry["groups"] if group["id"] == group_id)


def test_descriptor_exposes_every_configurable_group() -> None:
    descriptor = harness_fields.descriptor()
    entry = next(item for item in descriptor["extensions"] if item["id"] == FIXTURE_ID)
    group_ids = {group["id"] for group in entry["groups"]}
    assert harness_fields.GROUP_MCP in group_ids
    assert harness_fields.GROUP_SKILLS in group_ids
    assert harness_fields.GROUP_SETTINGS in group_ids
    assert harness_fields.GROUP_USER_INSTRUCTIONS in group_ids
    # Builtin tool candidates come from the one canonical disableable set.
    assert {item["name"] for item in descriptor["builtin_tools"]["items"]} == set(
        config_store.DISABLEABLE_BUILTIN_TOOLS
    )


def test_secret_setting_is_global_scope_and_never_exposes_a_value() -> None:
    settings = _group(FIXTURE_ID, harness_fields.GROUP_SETTINGS)
    token = next(item for item in settings["items"] if item["name"] == "token")
    endpoint = next(item for item in settings["items"] if item["name"] == "endpoint")
    assert token["scope"] == harness_fields.SCOPE_GLOBAL
    assert token["default_value"] is None
    assert endpoint["scope"] == harness_fields.SCOPE_PROFILE
    assert harness_fields.scope_for(
        ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SETTINGS, "token"]
    ) == harness_fields.SCOPE_GLOBAL
    assert harness_fields.scope_for(
        ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SETTINGS, "endpoint"]
    ) == harness_fields.SCOPE_PROFILE


def test_reading_a_secret_typed_setting_does_not_raise() -> None:
    """Regression: the keychain account was joined with "/", which
    password_manager rejects, so reading any extension declaring a
    secret-typed setting raised instead of reporting presence."""
    settings = extension_store.get_extension_settings(FIXTURE_ID)
    assert settings["secret_present"]["token"] is False
    assert settings["values"]["token"] is None


def test_global_scoped_groups_are_not_profile_overridable() -> None:
    for group in (
        harness_fields.GROUP_PERMISSIONS,
        harness_fields.GROUP_NATIVE_EXPOSURE,
        harness_fields.GROUP_UI_SURFACES,
        harness_fields.GROUP_FRONTEND_MODULES,
    ):
        assert harness_fields.scope_for(
            ["extension_instances", FIXTURE_ID, group, "anything"]
        ) == harness_fields.SCOPE_GLOBAL


def test_delta_is_minimal_relative_to_default() -> None:
    assert harness_fields.delta_for(["a", "b"], ["b", "c"]) == {"add": ["c"], "remove": ["a"]}
    assert harness_fields.delta_for(["a"], ["a"]) == {"add": [], "remove": []}


def test_default_write_passes_through_to_owning_store() -> None:
    harness_field_writer.apply_field_writes(
        "default",
        None,
        [{"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SETTINGS, "endpoint"],
          "value": "https://written.test"}],
    )
    assert extension_store.get_extension_settings(FIXTURE_ID)["values"]["endpoint"] == "https://written.test"
    # Default is a projection, so the change is visible without any stored profile.
    default = harness_profile_resolver.compute_default_profile()
    overlays = default["extension_instances"][FIXTURE_ID]["setting_overlays"]
    assert overlays["endpoint"]["value"] == "https://written.test"

    harness_field_writer.apply_field_writes(
        "default",
        None,
        [{"path": [harness_fields.GROUP_DISABLED_BUILTIN_TOOLS, "ask"], "value": False}],
    )
    assert "ask" in config_store.get_disabled_builtin_tools()


def test_named_profile_item_toggle_stores_a_delta_not_a_store_write() -> None:
    harness_profile_store.create_profile({"id": "toggle.profile", "name": "Toggle"})
    before = extension_store.extension_runtime_skills(FIXTURE_ID)
    assert before[0]["enabled"] is True

    harness_field_writer.apply_field_writes(
        "toggle.profile",
        None,
        [{"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SKILLS, "fixture-skill"],
          "value": False}],
    )

    # The global store is untouched — only the profile narrowed.
    assert extension_store.extension_runtime_skills(FIXTURE_ID)[0]["enabled"] is True
    resolved = harness_profile_resolver.resolve_profile("toggle.profile")
    skills = resolved["extension_instances"][FIXTURE_ID][harness_fields.GROUP_SKILLS]
    assert skills["resolved"] == []
    assert skills["override"] == {"add": [], "remove": ["fixture-skill"]}


def test_named_profile_global_field_write_still_writes_through() -> None:
    harness_profile_store.create_profile({"id": "global.write", "name": "Global Write"})
    harness_field_writer.apply_field_writes(
        "global.write",
        None,
        [{"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_UI_SURFACES, "page"],
          "value": False}],
    )
    # A global field has one value regardless of the profile it was edited from.
    assert extension_store.get_ui_settings(FIXTURE_ID)["page_enabled"] is False


def test_user_instructions_override_lands_on_the_default_source_key() -> None:
    harness_profile_store.create_profile({"id": "instr.profile", "name": "Instructions"})
    harness_field_writer.apply_field_writes(
        "instr.profile",
        None,
        [{"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_USER_INSTRUCTIONS],
          "value": "profile-specific guidance"}],
    )
    name = harness_fields.user_instruction_source_name(FIXTURE_ID)
    resolved = harness_profile_resolver.resolve_profile("instr.profile")
    entry = resolved["instruction_sources"][name]
    assert entry["resolved"] == {"kind": "inline", "content": "profile-specific guidance"}
    assert entry["override"] is not None

    # Clearing reverts to inheriting Default's (absent) user instructions.
    harness_field_writer.apply_field_writes(
        "instr.profile",
        None,
        [{"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_USER_INSTRUCTIONS],
          "value": ""}],
    )
    reverted = harness_profile_resolver.resolve_profile("instr.profile")
    assert name not in reverted["instruction_sources"]


def test_two_toggles_on_one_leaf_compose_in_a_single_request() -> None:
    harness_profile_store.create_profile({"id": "compose.profile", "name": "Compose"})
    harness_field_writer.apply_field_writes(
        "compose.profile",
        None,
        [
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SKILLS, "fixture-skill"],
             "value": False},
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SKILLS, "fixture-skill"],
             "value": True},
        ],
    )
    resolved = harness_profile_resolver.resolve_profile("compose.profile")
    skills = resolved["extension_instances"][FIXTURE_ID][harness_fields.GROUP_SKILLS]
    # Net effect is a no-op, so the stored delta must be empty rather than
    # carrying a stale remove from the first toggle.
    assert skills["override"] == {"add": [], "remove": []}
    assert skills["resolved"] == ["fixture-skill"]


def main() -> int:
    _install_fixture_extension()
    assert extension_store.is_extension_runtime_ready(FIXTURE_ID)
    test_descriptor_exposes_every_configurable_group()
    test_secret_setting_is_global_scope_and_never_exposes_a_value()
    test_reading_a_secret_typed_setting_does_not_raise()
    test_global_scoped_groups_are_not_profile_overridable()
    test_delta_is_minimal_relative_to_default()
    test_default_write_passes_through_to_owning_store()
    test_named_profile_item_toggle_stores_a_delta_not_a_store_write()
    test_named_profile_global_field_write_still_writes_through()
    test_user_instructions_override_lands_on_the_default_source_key()
    test_two_toggles_on_one_leaf_compose_in_a_single_request()
    print("PASS harness field registry + write routing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
