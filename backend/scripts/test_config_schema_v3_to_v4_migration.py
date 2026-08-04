#!/usr/bin/env python3
"""Locks the v3->v4 config_store schema edge (adds disabled_runtime_skills)
and its continuation through v5 per this repo's rule that boot-critical stores
need an explicit, version-bump-tested migration for every new field. Also locks
that the full migration chain (v1 -> current) reaches the current schema, and
that
_migrate_unversioned_provider_state's legacy path (which historically
stopped at whatever _migrate_schema_2_to_3 returned) now advances all the
way to current via _advance_to_current_schema.

Runs against an isolated state home; never touches real Better Agent data.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "backend"))

import paths  # noqa: E402

HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-config-schema-"))

import config_store  # noqa: E402


def _fresh_current_state() -> dict:
    config_store._save_state(config_store._seed_default_state())
    return json.loads(config_store._config_path().read_text(encoding="utf-8"))


def _without_execution_revision(state: dict) -> None:
    for provider in state["providers"]:
        provider.pop("execution_revision")
    state["provider_state_authority"] = (
        config_store.provider_sync_authority.new_authority(
            state["default_provider_id"],
            state["providers"],
        )
    )


def _write_and_reload(state: dict) -> dict:
    config_store._config_path().parent.mkdir(parents=True, exist_ok=True)
    config_store._config_path().write_text(json.dumps(state), encoding="utf-8")
    with config_store._state_cache_lock:
        config_store._state_cache = None
    config_store.list_providers()
    return json.loads(config_store._config_path().read_text(encoding="utf-8"))


def test_v3_state_migrates_to_v4_with_default() -> None:
    current = _fresh_current_state()
    v3 = copy.deepcopy(current)
    _without_execution_revision(v3)
    v3.pop("disabled_runtime_skills")
    v3["schema_version"] = 3
    persisted = _write_and_reload(v3)
    if persisted["schema_version"] != config_store.CONFIG_SCHEMA_VERSION:
        raise AssertionError(f"expected schema_version={config_store.CONFIG_SCHEMA_VERSION}, got {persisted['schema_version']!r}")
    if persisted["disabled_runtime_skills"] != []:
        raise AssertionError(f"expected disabled_runtime_skills=[], got {persisted['disabled_runtime_skills']!r}")


def test_v3_state_preserves_pre_existing_disabled_builtin_extensions() -> None:
    """The v3->v4 migration must be purely additive — it must not disturb
    the two fields that already existed at v3."""
    current = _fresh_current_state()
    v3 = copy.deepcopy(current)
    _without_execution_revision(v3)
    v3.pop("disabled_runtime_skills")
    v3["schema_version"] = 3
    v3["disabled_builtin_extensions"] = ["some.extension"]
    persisted = _write_and_reload(v3)
    if persisted["disabled_builtin_extensions"] != ["some.extension"]:
        raise AssertionError(
            f"v3->v4 migration disturbed disabled_builtin_extensions, got {persisted['disabled_builtin_extensions']!r}"
        )


def test_migration_chain_is_complete() -> None:
    config_store._validate_config_schema_migrations()
    assert set(config_store._CONFIG_SCHEMA_MIGRATIONS) == set(
        range(
            config_store.MIN_SUPPORTED_CONFIG_SCHEMA_VERSION,
            config_store.CONFIG_SCHEMA_VERSION,
        )
    )


def test_unversioned_legacy_state_reaches_current_not_just_v3() -> None:
    """_migrate_unversioned_provider_state used to return whatever
    _migrate_schema_2_to_3 produced directly — correct only while that was
    the last migration. Locks that it now advances all the way to current."""
    current = _fresh_current_state()
    unversioned = {
        "default_provider_id": current["default_provider_id"],
        "providers": copy.deepcopy(current["providers"]),
        "delegate_task_policy": current["delegate_task_policy"],
        "disabled_builtin_tools": current["disabled_builtin_tools"],
        "disabled_builtin_extensions": current["disabled_builtin_extensions"],
        "internal_llm": current["internal_llm"],
    }
    for provider in unversioned["providers"]:
        # Truly unversioned (pre-v1) records mint generation/revision fresh
        # during migration (_new_provider_authority()) — they must not be
        # present on the input, unlike every later schema version.
        provider.pop("generation", None)
        provider.pop("revision", None)
        provider.pop("execution_revision", None)
        provider["runner"] = config_store._clean_runner(provider["kind"], "")
        provider["default_model"] = ""
        provider["default_reasoning_effort"] = config_store._clean_default_reasoning_effort(
            provider["kind"], None
        )
    persisted = _write_and_reload(unversioned)
    if persisted["schema_version"] != config_store.CONFIG_SCHEMA_VERSION:
        raise AssertionError(
            f"unversioned legacy migration stopped at {persisted['schema_version']!r}, "
            f"expected {config_store.CONFIG_SCHEMA_VERSION}"
        )
    if persisted["disabled_runtime_skills"] != []:
        raise AssertionError(
            f"expected disabled_runtime_skills=[] after legacy migration, got {persisted['disabled_runtime_skills']!r}"
        )


if __name__ == "__main__":
    test_v3_state_migrates_to_v4_with_default()
    test_v3_state_preserves_pre_existing_disabled_builtin_extensions()
    test_migration_chain_is_complete()
    test_unversioned_legacy_state_reaches_current_not_just_v3()
    print("config-schema-v3-to-v4-migration tests passed")
