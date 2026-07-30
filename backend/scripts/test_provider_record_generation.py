#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="better-agent-provider-authority-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import config_store

CREDENTIALS: dict[str, str] = {}


def _credential_request(action: str, provider_id: str, **kwargs) -> dict:
    if action == "read":
        value = CREDENTIALS.get(provider_id, "")
        return {
            "status": "available" if value else "missing",
            **({"value": value} if value else {}),
        }
    if action == "status":
        return {
            "status": "available" if CREDENTIALS.get(provider_id) else "missing"
        }
    if action == "delete":
        CREDENTIALS.pop(provider_id, None)
        return {"status": "missing"}
    if action == "store":
        CREDENTIALS[provider_id] = kwargs["value"]
        return {"status": "available"}
    if action == "compare_set":
        current = CREDENTIALS.get(provider_id, "")
        if current != kwargs["expected_value"]:
            return {
                "status": "available" if current else "missing",
                "applied": False,
            }
        value = kwargs["value"]
        if value:
            CREDENTIALS[provider_id] = value
            return {"status": "available", "applied": True}
        CREDENTIALS.pop(provider_id, None)
        return {"status": "missing", "applied": True}
    raise AssertionError(f"unexpected credential action: {action}")


def _reset_cache() -> None:
    with config_store._state_cache_lock:
        config_store._state_cache = None


def _authority(record: dict) -> tuple[str, int]:
    return record["generation"], record["revision"]


def _provider(provider_id: str) -> dict:
    record = config_store.get_provider(provider_id)
    assert record is not None
    return record


def _set_default(target: dict, current: dict) -> dict:
    state = config_store.set_default_provider(
        target["id"],
        expected_generation=target["generation"],
        expected_revision=target["revision"],
        expected_default_provider_id=current["id"],
        expected_default_generation=current["generation"],
        expected_default_revision=current["revision"],
    )
    assert state is not None
    return state


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for nested in value.values()
            for key in _all_keys(nested)
        }
    if isinstance(value, list):
        return {key for nested in value for key in _all_keys(nested)}
    return set()


def _unversioned_provider_state(canonical: dict) -> dict:
    state = _schema_v2_state(canonical)
    state.pop("schema_version")
    state.pop("provider_state_authority")
    state.pop("provider_state_projected")
    state.pop("runtime_profiles")
    state.pop("default_runtime_profile_id")
    state.pop("deleted_providers")
    for provider in state["providers"]:
        provider.pop("generation")
        provider.pop("revision")
    return state


def _schema_v1_state(canonical: dict) -> dict:
    state = _schema_v2_state(canonical)
    state["schema_version"] = 1
    state.pop("provider_state_authority")
    state.pop("provider_state_projected")
    state.pop("runtime_profiles")
    state.pop("default_runtime_profile_id")
    state.pop("deleted_providers")
    return state


def _schema_v2_state(canonical: dict) -> dict:
    state = copy.deepcopy(canonical)
    state["schema_version"] = 2
    profiles_by_provider = {
        profile["provider_id"]: profile
        for profile in state["runtime_profiles"]
        if not profile["deleted_at"]
    }
    for provider in state["providers"]:
        profile = profiles_by_provider.get(provider["id"])
        kind = provider["kind"]
        provider["runner"] = (
            profile["runner"] if profile else config_store._clean_runner(kind, "")
        )
        provider["default_model"] = profile["default_model"] if profile else ""
        provider["default_reasoning_effort"] = (
            profile["default_reasoning_effort"]
            if profile
            else config_store._clean_default_reasoning_effort(kind, None)
        )
    state["provider_state_authority"] = (
        config_store.provider_sync_authority.new_authority(
            state["default_provider_id"],
            state["providers"],
        )
    )
    return state


def main() -> None:
    config_store.credential_session_client.available = lambda: True
    config_store.credential_session_client.request = _credential_request
    notifications: list[str] = []
    config_store._notify_provider_config_changed = lambda: notifications.append("changed")

    seeded = config_store.list_providers()
    persisted = json.loads(config_store._config_path().read_text(encoding="utf-8"))
    config_store._validate_config_schema_migrations()
    assert set(config_store._CONFIG_SCHEMA_MIGRATIONS) == set(
        range(
            config_store.MIN_SUPPORTED_CONFIG_SCHEMA_VERSION,
            config_store.CONFIG_SCHEMA_VERSION,
        )
    )
    assert persisted["schema_version"] == config_store.CONFIG_SCHEMA_VERSION
    assert all(record["revision"] == 0 for record in seeded["providers"])
    assert len({record["generation"] for record in seeded["providers"]}) == len(
        seeded["providers"]
    )

    created = config_store.add_provider(
        {"name": "Authority proof", "kind": "claude", "mode": "subscription"}
    )
    provider_id = created["id"]
    created_authority = _authority(created)
    assert created_authority[1] == 0
    uuid.UUID(created_authority[0])

    credential_provider = config_store.add_provider(
        {
            "name": "Credential authority proof",
            "kind": "claude",
            "mode": "api_key",
            "api_key": "secret-one",
        }
    )
    credential_bytes = config_store._config_path().read_bytes()
    credential_notifications = len(notifications)
    credential_noop = config_store.update_provider(
        credential_provider["id"],
        {"api_key": "secret-one"},
        expected_generation=credential_provider["generation"],
        expected_revision=credential_provider["revision"],
    )
    assert credential_noop is not None
    assert _authority(credential_noop) == _authority(credential_provider)
    assert config_store._config_path().read_bytes() == credential_bytes
    assert len(notifications) == credential_notifications
    credential_changed = config_store.update_provider(
        credential_provider["id"],
        {"api_key": "secret-two"},
        expected_generation=credential_provider["generation"],
        expected_revision=credential_provider["revision"],
    )
    assert credential_changed is not None
    assert credential_changed["revision"] == credential_provider["revision"] + 1

    clone_source = config_store.add_provider({
        "name": "Clone source",
        "kind": "codex",
        "mode": "api_key",
        "api_key": "clone-source-secret",
    })
    clone_target = config_store.add_provider({
        "name": "Clone target",
        "kind": "codex",
        "mode": "api_key",
        "api_key": "clone-target-secret",
    })
    before_clone = config_store.export_provider_sync_state()
    before_clone_authority = before_clone["provider_state_authority"]
    clone_notifications = len(notifications)
    assert config_store.clone_provider_credential(
        clone_source["id"],
        clone_target["id"],
    ) == "available"
    cloned_target = _provider(clone_target["id"])
    cloned_state = config_store.export_provider_sync_state()
    assert CREDENTIALS[clone_target["id"]] == "clone-source-secret"
    assert cloned_target["revision"] == clone_target["revision"] + 1
    assert (
        cloned_state["provider_state_authority"]["revision"]
        == before_clone_authority["revision"] + 1
    )
    assert len(notifications) == clone_notifications + 1

    no_op_bytes = config_store._config_path().read_bytes()
    no_op_notifications = len(notifications)
    assert config_store.clone_provider_credential(
        clone_source["id"],
        clone_target["id"],
    ) == "available"
    assert _authority(_provider(clone_target["id"])) == _authority(cloned_target)
    assert config_store._config_path().read_bytes() == no_op_bytes
    assert len(notifications) == no_op_notifications

    original_save_state = config_store._save_state
    config_store._save_state = lambda _state: (_ for _ in ()).throw(
        RuntimeError("injected save failure")
    )
    CREDENTIALS[clone_source["id"]] = "rollback-source-secret"
    try:
        try:
            config_store.clone_provider_credential(
                clone_source["id"],
                clone_target["id"],
            )
        except RuntimeError as exc:
            assert str(exc) == "injected save failure"
        else:
            raise AssertionError("credential clone ignored config save failure")
    finally:
        config_store._save_state = original_save_state
    assert CREDENTIALS[clone_target["id"]] == "clone-source-secret"
    assert config_store._config_path().read_bytes() == no_op_bytes

    original_request = config_store.credential_session_client.request
    compare_attempted = False

    def conflicting_request(action: str, provider_id: str, **kwargs) -> dict:
        nonlocal compare_attempted
        if action == "compare_set" and provider_id == clone_target["id"]:
            compare_attempted = True
            CREDENTIALS[provider_id] = "concurrent-newer-secret"
            return {"status": "available", "applied": False}
        return _credential_request(action, provider_id, **kwargs)

    CREDENTIALS[clone_source["id"]] = "concurrent-source-secret"
    config_store.credential_session_client.request = conflicting_request
    try:
        try:
            config_store.clone_provider_credential(
                clone_source["id"],
                clone_target["id"],
            )
        except config_store.ProviderCredentialConflict:
            pass
        else:
            raise AssertionError("concurrent credential mutation was overwritten")
    finally:
        config_store.credential_session_client.request = original_request
    assert compare_attempted
    assert CREDENTIALS[clone_target["id"]] == "concurrent-newer-secret"
    assert config_store._config_path().read_bytes() == no_op_bytes

    isolated_target_id = "isolated-clone-target"
    isolated_bytes = config_store._config_path().read_bytes()
    isolated_notifications = len(notifications)
    assert config_store.clone_provider_credential(
        clone_source["id"],
        isolated_target_id,
    ) == "available"
    assert CREDENTIALS[isolated_target_id] == "concurrent-source-secret"
    assert config_store._config_path().read_bytes() == isolated_bytes
    assert len(notifications) == isolated_notifications

    updated = config_store.update_provider(
        provider_id,
        {"nickname": "primary"},
        expected_generation=created_authority[0],
        expected_revision=created_authority[1],
    )
    assert updated is not None
    assert updated["generation"] == created_authority[0]
    assert updated["revision"] == 1

    _reset_cache()
    reloaded = _provider(provider_id)
    assert _authority(reloaded) == _authority(updated)

    before_conflict = config_store._config_path().read_bytes()
    before_conflict_notifications = len(notifications)
    try:
        config_store.update_provider(
            provider_id,
            {"nickname": "stale writer"},
            expected_generation=created_authority[0],
            expected_revision=0,
        )
    except config_store.ProviderConfigConflict as exc:
        assert (exc.generation, exc.revision) == _authority(updated)
    else:
        raise AssertionError("stale provider update was accepted")
    assert config_store._config_path().read_bytes() == before_conflict
    assert len(notifications) == before_conflict_notifications

    try:
        config_store.update_provider(
            provider_id,
            {"nickname": "partial CAS"},
            expected_generation=updated["generation"],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("partial provider authority was accepted")

    unchanged = config_store.update_provider(
        provider_id,
        {"nickname": "primary"},
        expected_generation=updated["generation"],
        expected_revision=updated["revision"],
    )
    assert unchanged is not None
    assert _authority(unchanged) == _authority(updated)
    assert config_store._config_path().read_bytes() == before_conflict
    assert len(notifications) == before_conflict_notifications

    child_code = """
import config_store, sys
try:
    config_store.update_provider(
        sys.argv[1],
        {"nickname": sys.argv[4]},
        expected_generation=sys.argv[2],
        expected_revision=int(sys.argv[3]),
    )
except config_store.ProviderConfigConflict:
    print("conflict")
else:
    print("committed")
"""
    child_env = {
        **os.environ,
        "BETTER_AGENT_HOME": str(TEST_HOME),
        "PYTHONPATH": str(BACKEND),
    }
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                provider_id,
                updated["generation"],
                str(updated["revision"]),
                nickname,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )
        for nickname in ("winner-a", "winner-b")
    ]
    outcomes = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        outcomes.append(stdout.strip())
    assert sorted(outcomes) == ["committed", "conflict"]
    _reset_cache()
    after_race = _provider(provider_id)
    assert after_race["revision"] == updated["revision"] + 1

    previous_default_id = seeded["default_provider_id"]
    previous_default_before = _provider(previous_default_id)
    alternate_id = next(
        record["id"]
        for record in seeded["providers"]
        if record["id"] != previous_default_id
    )
    alternate_before = _provider(alternate_id)
    _set_default(alternate_before, previous_default_before)
    stale_default_bytes = config_store._config_path().read_bytes()
    stale_default_notifications = len(notifications)
    try:
        config_store.set_default_provider(
            provider_id,
            expected_generation=after_race["generation"],
            expected_revision=after_race["revision"],
            expected_default_provider_id=previous_default_before["id"],
            expected_default_generation=previous_default_before["generation"],
            expected_default_revision=previous_default_before["revision"],
        )
    except config_store.ProviderConfigConflict:
        pass
    else:
        raise AssertionError("stale current-default authority was accepted")
    assert config_store._config_path().read_bytes() == stale_default_bytes
    assert len(notifications) == stale_default_notifications
    alternate_after = _provider(alternate_id)
    previous_default_after_alternate = _provider(previous_default_id)
    _set_default(previous_default_after_alternate, alternate_after)
    previous_default_before = _provider(previous_default_id)

    changed_default = _set_default(after_race, previous_default_before)
    selected = _provider(provider_id)
    previous_default_after = _provider(previous_default_id)
    assert selected["revision"] == after_race["revision"] + 1
    assert previous_default_after["revision"] == previous_default_before["revision"] + 1

    default_bytes = config_store._config_path().read_bytes()
    default_notifications = len(notifications)
    repeated_default = _set_default(selected, selected)
    assert config_store._config_path().read_bytes() == default_bytes
    assert len(notifications) == default_notifications

    _set_default(previous_default_after, selected)
    active_before_suspend = _provider(previous_default_id)
    suspension_replacement_before = _provider(alternate_id)
    suspended_state = config_store.set_provider_suspended(
        previous_default_id,
        True,
        expected_generation=active_before_suspend["generation"],
        expected_revision=active_before_suspend["revision"],
    )
    assert suspended_state is not None
    active_suspended = _provider(previous_default_id)
    suspension_replacement_after = _provider(alternate_id)
    assert active_suspended["revision"] == active_before_suspend["revision"] + 1
    assert (
        suspension_replacement_after["revision"]
        == suspension_replacement_before["revision"] + 1
    )
    assert suspended_state["default_provider_id"] == alternate_id
    suspend_bytes = config_store._config_path().read_bytes()
    suspend_notifications = len(notifications)
    config_store.set_provider_suspended(
        previous_default_id,
        True,
        expected_generation=active_suspended["generation"],
        expected_revision=active_suspended["revision"],
    )
    assert config_store._config_path().read_bytes() == suspend_bytes
    assert len(notifications) == suspend_notifications

    config_store.set_provider_suspended(
        previous_default_id,
        False,
        expected_generation=active_suspended["generation"],
        expected_revision=active_suspended["revision"],
    )
    active_unsuspended = _provider(previous_default_id)
    current_default = _provider(alternate_id)
    _set_default(active_unsuspended, current_default)

    deletable = _provider(provider_id)
    deleted, reason = config_store.delete_provider(
        provider_id,
        expected_generation=deletable["generation"],
        expected_revision=deletable["revision"],
    )
    assert (deleted, reason) == (True, "ok")
    assert config_store.get_provider(provider_id) is None

    original_uuid4 = config_store.uuid.uuid4
    replacement_generation = str(uuid.uuid4())
    replacement_values = iter((uuid.UUID(provider_id), uuid.UUID(replacement_generation)))
    config_store.uuid.uuid4 = lambda: next(replacement_values)
    try:
        replacement = config_store.add_provider(
            {"name": "Replacement", "kind": "claude", "mode": "subscription"}
        )
    finally:
        config_store.uuid.uuid4 = original_uuid4
    assert replacement["id"] == provider_id
    assert replacement["generation"] == replacement_generation
    assert replacement["generation"] != deletable["generation"]
    assert replacement["revision"] == 0
    try:
        config_store.update_provider(
            provider_id,
            {"nickname": "ABA stale writer"},
            expected_generation=deletable["generation"],
            expected_revision=deletable["revision"],
        )
    except config_store.ProviderConfigConflict:
        pass
    else:
        raise AssertionError("ABA replacement accepted stale authority")

    projection = config_store.export_provider_sync_state()
    projected = next(
        record for record in projection["providers"] if record["id"] == provider_id
    )
    assert _authority(projected) == _authority(replacement)
    projection_keys = _all_keys(projection)
    assert {
        "api_key",
        "provider_api_keys",
        "_credential_authoritative",
        "record_version",
    }.isdisjoint(projection_keys)
    assert "secret-one" not in json.dumps(projection)
    assert "secret-two" not in json.dumps(projection)

    canonical = json.loads(config_store._config_path().read_text(encoding="utf-8"))
    schema_v2 = _schema_v2_state(canonical)
    config_store._config_path().write_text(
        json.dumps(schema_v2),
        encoding="utf-8",
    )
    _reset_cache()
    migrated_v2 = config_store.list_providers()
    persisted_v2 = json.loads(
        config_store._config_path().read_text(encoding="utf-8")
    )
    assert persisted_v2["schema_version"] == config_store.CONFIG_SCHEMA_VERSION
    assert persisted_v2["provider_state_authority"]["generation"] == (
        schema_v2["provider_state_authority"]["generation"]
    )
    assert persisted_v2["provider_state_authority"]["revision"] == (
        schema_v2["provider_state_authority"]["revision"] + 1
    )
    assert persisted_v2["provider_state_authority"]["digest"] == (
        config_store.provider_sync_authority.snapshot_digest(
            persisted_v2["default_provider_id"],
            persisted_v2["providers"],
        )
    )
    canonical = json.loads(config_store._config_path().read_text(encoding="utf-8"))
    unsupported_states = []
    missing_generation = copy.deepcopy(canonical)
    missing_generation["providers"][0].pop("generation")
    unsupported_states.append(missing_generation)
    missing_provider_state_authority = copy.deepcopy(canonical)
    missing_provider_state_authority.pop("provider_state_authority")
    unsupported_states.append(missing_provider_state_authority)
    missing_projection_marker = copy.deepcopy(canonical)
    missing_projection_marker.pop("provider_state_projected")
    unsupported_states.append(missing_projection_marker)
    below_minimum = copy.deepcopy(canonical)
    below_minimum["schema_version"] = (
        config_store.MIN_SUPPORTED_CONFIG_SCHEMA_VERSION - 1
    )
    unsupported_states.append(below_minimum)
    wrong_version = copy.deepcopy(canonical)
    wrong_version["schema_version"] += 1
    unsupported_states.append(wrong_version)
    unsupported_states.append({"schema_version": config_store.CONFIG_SCHEMA_VERSION + 1})
    unsupported_states.append({"unexpected": "shape"})
    duplicate_id = copy.deepcopy(canonical)
    duplicate_id["providers"][1]["id"] = duplicate_id["providers"][0]["id"]
    unsupported_states.append(duplicate_id)
    noncanonical_default = copy.deepcopy(canonical)
    default_id = noncanonical_default["default_provider_id"]
    next(
        record
        for record in noncanonical_default["providers"]
        if record["id"] == default_id
    )["suspended"] = True
    unsupported_states.append(noncanonical_default)
    for unsupported in unsupported_states:
        config_store._config_path().write_text(
            json.dumps(unsupported),
            encoding="utf-8",
        )
        _reset_cache()
        unsupported_bytes = config_store._config_path().read_bytes()
        try:
            config_store.list_providers()
        except RuntimeError as exc:
            assert "unsupported provider config schema" in str(exc)
        else:
            raise AssertionError("unsupported provider schema was accepted")
        assert config_store._config_path().read_bytes() == unsupported_bytes

    schema_v1 = _schema_v1_state(canonical)
    unsupported_v1_states = []
    unexpected_v1 = copy.deepcopy(schema_v1)
    unexpected_v1["unexpected"] = True
    unsupported_v1_states.append(unexpected_v1)
    malformed_v1 = copy.deepcopy(schema_v1)
    malformed_v1["providers"][0]["mode"] = "invalid"
    unsupported_v1_states.append(malformed_v1)
    for unsupported in unsupported_v1_states:
        config_store._config_path().write_text(
            json.dumps(unsupported),
            encoding="utf-8",
        )
        _reset_cache()
        unsupported_bytes = config_store._config_path().read_bytes()
        try:
            config_store.list_providers()
        except RuntimeError as exc:
            assert "unsupported provider config schema" in str(exc)
        else:
            raise AssertionError("unsupported schema v1 provider state was accepted")
        assert config_store._config_path().read_bytes() == unsupported_bytes

    config_store._config_path().write_text(
        json.dumps(schema_v1),
        encoding="utf-8",
    )
    _reset_cache()
    migrated_v1 = config_store.list_providers()
    persisted_v1_migration = json.loads(
        config_store._config_path().read_text(encoding="utf-8")
    )
    assert persisted_v1_migration["schema_version"] == config_store.CONFIG_SCHEMA_VERSION
    assert persisted_v1_migration["default_provider_id"] == schema_v1["default_provider_id"]
    _EXECUTION_FIELDS = ("runner", "default_model", "default_reasoning_effort")
    assert persisted_v1_migration["providers"] == [
        {k: v for k, v in provider.items() if k not in _EXECUTION_FIELDS}
        for provider in schema_v1["providers"]
    ]
    migrated_profiles = {
        profile["provider_id"]: profile
        for profile in persisted_v1_migration["runtime_profiles"]
        if not profile["deleted_at"]
    }
    for provider in schema_v1["providers"]:
        profile = migrated_profiles[provider["id"]]
        for field in _EXECUTION_FIELDS:
            assert profile[field] == provider[field]
    assert persisted_v1_migration["default_runtime_profile_id"] == (
        migrated_profiles[schema_v1["default_provider_id"]]["id"]
    )
    assert persisted_v1_migration["provider_state_projected"] is False
    # 1→2 mints the authority (revision 0), 2→3 advances it for the field move.
    assert persisted_v1_migration["provider_state_authority"]["revision"] == 1
    uuid.UUID(persisted_v1_migration["provider_state_authority"]["generation"])
    assert persisted_v1_migration["provider_state_authority"]["digest"] == (
        config_store.provider_sync_authority.snapshot_digest(
            persisted_v1_migration["default_provider_id"],
            persisted_v1_migration["providers"],
        )
    )
    assert migrated_v1["provider_state_authority"] == (
        persisted_v1_migration["provider_state_authority"]
    )
    migrated_v1_bytes = config_store._config_path().read_bytes()
    _reset_cache()
    assert config_store.list_providers() == migrated_v1
    assert config_store._config_path().read_bytes() == migrated_v1_bytes

    legacy_provider_state = _unversioned_provider_state(canonical)
    legacy_without_nickname = copy.deepcopy(legacy_provider_state)
    legacy_without_nickname["providers"][0].pop("nickname")
    config_store._config_path().write_text(
        json.dumps(legacy_without_nickname),
        encoding="utf-8",
    )
    _reset_cache()
    migrated_without_nickname = config_store.list_providers()
    assert migrated_without_nickname["providers"][0]["nickname"] == ""

    unsupported_legacy_states = []
    unexpected_top_level = copy.deepcopy(legacy_provider_state)
    unexpected_top_level["unexpected"] = "shape"
    unsupported_legacy_states.append(unexpected_top_level)
    missing_top_level = copy.deepcopy(legacy_provider_state)
    missing_top_level.pop("internal_llm")
    unsupported_legacy_states.append(missing_top_level)
    mixed_authority = copy.deepcopy(legacy_provider_state)
    mixed_authority["providers"][0]["generation"] = str(uuid.uuid4())
    unsupported_legacy_states.append(mixed_authority)
    partial_authority = copy.deepcopy(legacy_provider_state)
    partial_authority["providers"][0]["revision"] = 0
    unsupported_legacy_states.append(partial_authority)
    unexpected_provider_field = copy.deepcopy(legacy_provider_state)
    unexpected_provider_field["providers"][0]["unexpected"] = True
    unsupported_legacy_states.append(unexpected_provider_field)
    partial_provider = copy.deepcopy(legacy_provider_state)
    partial_provider["providers"][0].pop("runner")
    unsupported_legacy_states.append(partial_provider)
    noncanonical_provider = copy.deepcopy(legacy_provider_state)
    noncanonical_provider["providers"][0]["mode"] = "invalid"
    unsupported_legacy_states.append(noncanonical_provider)
    noncanonical_top_level = copy.deepcopy(legacy_provider_state)
    noncanonical_top_level["delegate_task_policy"] = "invalid"
    unsupported_legacy_states.append(noncanonical_top_level)
    for unsupported in unsupported_legacy_states:
        config_store._config_path().write_text(
            json.dumps(unsupported),
            encoding="utf-8",
        )
        _reset_cache()
        unsupported_bytes = config_store._config_path().read_bytes()
        try:
            config_store.list_providers()
        except RuntimeError as exc:
            assert "unsupported provider config schema" in str(exc)
        else:
            raise AssertionError("unsupported unversioned provider schema was accepted")
        assert config_store._config_path().read_bytes() == unsupported_bytes

    config_store._config_path().write_text(
        json.dumps(legacy_provider_state),
        encoding="utf-8",
    )
    _reset_cache()
    migrated_provider_state = config_store.list_providers()
    persisted_migration = json.loads(
        config_store._config_path().read_text(encoding="utf-8")
    )
    assert persisted_migration["schema_version"] == config_store.CONFIG_SCHEMA_VERSION
    assert persisted_migration["provider_state_projected"] is False
    assert persisted_migration["provider_state_authority"] == (
        migrated_provider_state["provider_state_authority"]
    )
    uuid.UUID(persisted_migration["provider_state_authority"]["generation"])
    assert persisted_migration["provider_state_authority"]["revision"] == 0
    assert [provider["id"] for provider in migrated_provider_state["providers"]] == [
        provider["id"] for provider in legacy_provider_state["providers"]
    ]
    for provider in migrated_provider_state["providers"]:
        assert provider["revision"] == 0
        uuid.UUID(provider["generation"])
    migrated_bytes = config_store._config_path().read_bytes()
    migrated_authority = {
        provider["id"]: _authority(provider)
        for provider in migrated_provider_state["providers"]
    }
    _reset_cache()
    reloaded_provider_state = config_store.list_providers()
    assert config_store._config_path().read_bytes() == migrated_bytes
    assert {
        provider["id"]: _authority(provider)
        for provider in reloaded_provider_state["providers"]
    } == migrated_authority

    config_store._config_path().write_text(
        json.dumps({"mode": "subscription", "base_url": ""}),
        encoding="utf-8",
    )
    _reset_cache()
    migrated_legacy = config_store.list_providers()
    assert migrated_legacy["providers"][0]["revision"] == 0
    uuid.UUID(migrated_legacy["providers"][0]["generation"])

    print("PASS provider record generation")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
