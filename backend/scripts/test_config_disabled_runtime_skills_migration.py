from __future__ import annotations

import copy
import json

import _test_home

_test_home.isolate("bc-test-config-runtime-skills-")

import config_store  # noqa: E402


def test_schema_three_advances_to_current_with_runtime_skills_default() -> None:
    config_store._save_state(config_store._seed_default_state())
    current = json.loads(config_store._config_path().read_text(encoding="utf-8"))
    schema_three = copy.deepcopy(current)
    for provider in schema_three["providers"]:
        provider.pop("execution_revision")
    schema_three["schema_version"] = 3
    schema_three.pop("disabled_runtime_skills", None)
    schema_three["provider_state_authority"] = (
        config_store.provider_sync_authority.new_authority(
            schema_three["default_provider_id"],
            schema_three["providers"],
        )
    )

    migrated = config_store._migrate_versioned_state(schema_three)

    assert migrated["schema_version"] == config_store.CONFIG_SCHEMA_VERSION
    assert migrated["disabled_runtime_skills"] == []


def test_config_schema_migration_chain_has_every_edge() -> None:
    config_store._validate_config_schema_migrations()
    assert set(config_store._CONFIG_SCHEMA_MIGRATIONS) == set(
        range(
            config_store.MIN_SUPPORTED_CONFIG_SCHEMA_VERSION,
            config_store.CONFIG_SCHEMA_VERSION,
        )
    )
