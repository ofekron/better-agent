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


def main() -> None:
    config_store.credential_session_client.available = lambda: True
    config_store.credential_session_client.request = _credential_request
    notifications: list[str] = []
    config_store._notify_provider_config_changed = lambda: notifications.append("changed")

    seeded = config_store.list_providers()
    persisted = json.loads(config_store._config_path().read_text(encoding="utf-8"))
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
    unsupported_states = []
    missing_generation = copy.deepcopy(canonical)
    missing_generation["providers"][0].pop("generation")
    unsupported_states.append(missing_generation)
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
