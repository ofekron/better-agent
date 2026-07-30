#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="better-agent-provider-sync-authority-"))
SOURCE_HOME = ROOT / "source"
TARGET_HOME = ROOT / "target"
OTHER_HOME = ROOT / "other"
os.environ["BETTER_AGENT_HOME"] = str(SOURCE_HOME)
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import config_store
import node_config_sync
import node_rpc_handlers
import provider_sync_authority

CREDENTIALS: dict[tuple[str, str], str] = {}
NOTIFICATIONS: list[str] = []


def _home() -> str:
    return os.environ["BETTER_AGENT_HOME"]


def _credential_request(action: str, provider_id: str, **kwargs) -> dict:
    key = (_home(), provider_id)
    if action == "read":
        value = CREDENTIALS.get(key, "")
        return {
            "status": "available" if value else "missing",
            **({"value": value} if value else {}),
        }
    if action == "status":
        return {"status": "available" if CREDENTIALS.get(key) else "missing"}
    if action == "store":
        CREDENTIALS[key] = kwargs["value"]
        return {"status": "available"}
    if action == "compare_set":
        current = CREDENTIALS.get(key, "")
        if current != kwargs["expected_value"]:
            return {
                "status": "available" if current else "missing",
                "applied": False,
            }
        value = kwargs["value"]
        if value:
            CREDENTIALS[key] = value
            return {"status": "available", "applied": True}
        CREDENTIALS.pop(key, None)
        return {"status": "missing", "applied": True}
    if action == "delete":
        CREDENTIALS.pop(key, None)
        return {"status": "missing"}
    raise AssertionError(f"unexpected credential action: {action}")


def _select(home: Path) -> None:
    os.environ["BETTER_AGENT_HOME"] = str(home)
    with config_store._state_cache_lock:
        config_store._state_cache = None
    with config_store._api_key_cache_lock:
        config_store._api_key_cache.clear()
    config_store._credential_status.clear()


def _provider(payload: dict, kind: str) -> dict:
    return next(record for record in payload["providers"] if record["kind"] == kind)


def _provider_by_id(payload: dict, provider_id: str) -> dict:
    return next(
        record
        for record in payload["providers"]
        if record["id"] == provider_id
    )


def _set_default(target: dict, current: dict) -> None:
    del current  # default-switch authority moved to profile activation
    import _runtime_profile_test_helpers as _rp

    _rp.activate_provider(target["id"])


def _recompute_digest(payload: dict) -> None:
    payload["provider_state_authority"]["digest"] = (
        provider_sync_authority.snapshot_digest(
            payload["default_provider_id"],
            payload["providers"],
        )
    )


class _TrackingMutationLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.export_attempted = threading.Event()
        self.export_acquired = threading.Event()

    def __enter__(self):
        exporting = threading.current_thread().name == "credential-export"
        if exporting:
            self.export_attempted.set()
        self._lock.acquire()
        if exporting:
            self.export_acquired.set()
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.release()


def _assert_conflict(payload: dict, reason: str) -> None:
    before = config_store._config_path().read_bytes()
    before_notifications = len(NOTIFICATIONS)
    try:
        config_store.import_provider_sync_state(payload)
    except config_store.ProviderStateConflict as exc:
        assert exc.reason == reason
    else:
        raise AssertionError(f"provider sync {reason} conflict was accepted")
    assert config_store._config_path().read_bytes() == before
    assert len(NOTIFICATIONS) == before_notifications


def _assert_invalid(payload: dict) -> None:
    before = config_store._config_path().read_bytes()
    before_notifications = len(NOTIFICATIONS)
    try:
        config_store.import_provider_sync_state(payload)
    except ValueError as exc:
        assert "secret-" not in str(exc)
    else:
        raise AssertionError("invalid provider sync payload was accepted")
    assert config_store._config_path().read_bytes() == before
    assert len(NOTIFICATIONS) == before_notifications


def main() -> None:
    config_store.credential_session_client.available = lambda: True
    config_store.credential_session_client.request = _credential_request
    config_store._notify_provider_config_changed = (
        lambda: NOTIFICATIONS.append(_home())
    )

    _select(SOURCE_HOME)
    initial = config_store.export_provider_sync_state()
    assert set(initial) == {
        "provider_state_authority",
        "default_provider_id",
        "providers",
    }
    assert "provider_api_keys" not in initial
    assert "api_key" not in json.dumps(initial)
    initial_authority = initial["provider_state_authority"]
    unexpected = {**copy.deepcopy(initial), "unexpected": True}
    _assert_invalid(unexpected)
    noncanonical = copy.deepcopy(initial)
    noncanonical["providers"][0]["nickname"] = " trailing "
    _recompute_digest(noncanonical)
    _assert_invalid(noncanonical)
    forged_digest = copy.deepcopy(initial)
    forged_digest["provider_state_authority"]["digest"] = "0" * 64
    _assert_invalid(forged_digest)
    duplicate = copy.deepcopy(initial)
    duplicate["providers"].append(copy.deepcopy(duplicate["providers"][0]))
    _recompute_digest(duplicate)
    _assert_invalid(duplicate)
    missing_record_authority = copy.deepcopy(initial)
    missing_record_authority["providers"][0].pop("generation")
    _recompute_digest(missing_record_authority)
    _assert_invalid(missing_record_authority)

    removable = _provider(initial, "codex")
    deleted, reason = config_store.delete_provider(
        removable["id"],
        expected_generation=removable["generation"],
        expected_revision=removable["revision"],
    )
    assert (deleted, reason) == (True, "ok")
    after_delete = config_store.export_provider_sync_state()
    assert after_delete["provider_state_authority"]["revision"] == (
        initial_authority["revision"] + 1
    )
    assert removable["id"] not in {
        record["id"] for record in after_delete["providers"]
    }

    added = config_store.add_provider({
        "name": "Codex replacement",
        "kind": "codex",
        "mode": "subscription",
    })
    before_default = config_store.export_provider_sync_state()
    assert before_default["provider_state_authority"]["revision"] == (
        after_delete["provider_state_authority"]["revision"] + 1
    )
    authority_before_policy = copy.deepcopy(
        before_default["provider_state_authority"]
    )
    config_store.set_delegate_task_policy("manual")
    assert config_store.export_provider_sync_state()["provider_state_authority"] == (
        authority_before_policy
    )
    current = _provider(before_default, "claude")
    target = _provider(before_default, "codex")
    _set_default(target, current)
    after_default = config_store.export_provider_sync_state()
    assert after_default["default_provider_id"] == added["id"]
    assert after_default["provider_state_authority"]["revision"] == (
        before_default["provider_state_authority"]["revision"] + 1
    )

    _select(TARGET_HOME)
    imported = config_store.import_provider_sync_state(before_default)
    assert imported["sync_status"] == "applied"
    assert imported["provider_state_authority"] == (
        before_default["provider_state_authority"]
    )
    before_replay = config_store._config_path().read_bytes()
    replay_notifications = len(NOTIFICATIONS)
    replayed = config_store.import_provider_sync_state(copy.deepcopy(before_default))
    assert replayed["sync_status"] == "unchanged"
    assert config_store._config_path().read_bytes() == before_replay
    assert len(NOTIFICATIONS) == replay_notifications
    projected_provider = config_store.get_provider(
        before_default["default_provider_id"]
    )
    assert projected_provider is not None
    try:
        config_store.update_provider(
            projected_provider["id"],
            {"nickname": "local divergence"},
            expected_generation=projected_provider["generation"],
            expected_revision=projected_provider["revision"],
        )
    except RuntimeError as exc:
        assert "primary-owned projection" in str(exc)
    else:
        raise AssertionError("local mutation changed a primary-owned projection")
    assert config_store._config_path().read_bytes() == before_replay
    assert len(NOTIFICATIONS) == replay_notifications

    applied_default = config_store.import_provider_sync_state(after_default)
    assert applied_default["sync_status"] == "applied"
    assert applied_default["default_provider_id"] == added["id"]
    _assert_conflict(before_default, "stale")

    divergent = copy.deepcopy(after_default)
    divergent["providers"][0]["nickname"] = "same revision, different state"
    _recompute_digest(divergent)
    _assert_conflict(divergent, "divergent")
    record_divergent = copy.deepcopy(after_default)
    record_divergent["provider_state_authority"]["revision"] += 1
    record_divergent["providers"][0]["nickname"] = "unversioned record change"
    _recompute_digest(record_divergent)
    _assert_conflict(record_divergent, "record_divergent")
    record_stale = copy.deepcopy(after_default)
    record_stale["provider_state_authority"]["revision"] += 1
    versioned_record = next(
        record
        for record in record_stale["providers"]
        if record["revision"] > 0
    )
    versioned_record["revision"] -= 1
    _recompute_digest(record_stale)
    _assert_conflict(record_stale, "record_stale")

    _select(OTHER_HOME)
    other_generation = config_store.export_provider_sync_state()
    _select(TARGET_HOME)
    _assert_conflict(other_generation, "generation")

    _select(SOURCE_HOME)
    api_provider = config_store.add_provider({
        "name": "API source",
        "kind": "claude",
        "mode": "api_key",
        "api_key": "secret-one",
    })
    source_default = config_store.get_provider(after_default["default_provider_id"])
    assert source_default is not None
    api_current = config_store.get_provider(api_provider["id"])
    assert api_current is not None
    _set_default(api_current, source_default)
    credential_free = config_store.export_provider_sync_state()
    credential_payload = config_store.export_provider_sync_state([api_provider["id"]])
    assert "secret-one" not in json.dumps(credential_free)
    assert credential_payload["provider_api_keys"] == [{
        "provider_id": api_provider["id"],
        "api_key": "secret-one",
    }]
    malformed_credential = copy.deepcopy(credential_payload)
    malformed_credential["provider_api_keys"][0]["unexpected"] = "secret-leak"
    _select(TARGET_HOME)
    _assert_invalid(malformed_credential)
    null_credentials = copy.deepcopy(credential_free)
    null_credentials["provider_api_keys"] = None
    _assert_invalid(null_credentials)

    orphan_target = ROOT / "orphan-credential-target"
    _select(orphan_target)
    CREDENTIALS[(_home(), api_provider["id"])] = "orphan-secret"
    try:
        config_store.import_provider_sync_state(credential_payload)
    except config_store.ProviderCredentialConflict:
        pass
    else:
        raise AssertionError("first adoption overwrote an orphan credential")
    assert not config_store._config_path().exists()
    assert CREDENTIALS[(_home(), api_provider["id"])] == "orphan-secret"

    fresh_target = ROOT / "credential-target"
    _select(fresh_target)
    no_key = config_store.import_provider_sync_state(credential_free)
    persisted = json.loads(config_store._config_path().read_text(encoding="utf-8"))
    persisted_api = _provider_by_id(persisted, api_provider["id"])
    source_api = _provider_by_id(credential_free, api_provider["id"])
    assert persisted_api == source_api
    assert persisted["default_provider_id"] == credential_free["default_provider_id"]
    assert no_key["default_provider_id"] != credential_free["default_provider_id"]

    before_secret_install = config_store._config_path().read_bytes()
    before_secret_notification = len(NOTIFICATIONS)
    installed = config_store.import_provider_sync_state(credential_payload)
    assert installed["sync_status"] == "credentials_applied"
    assert installed["default_provider_id"] == credential_free["default_provider_id"]
    assert CREDENTIALS[(_home(), api_provider["id"])] == "secret-one"
    assert config_store._config_path().read_bytes() == before_secret_install
    assert len(NOTIFICATIONS) == before_secret_notification

    unrelated_change = copy.deepcopy(credential_payload)
    unrelated_change["provider_state_authority"]["revision"] += 1
    unrelated_change["provider_api_keys"][0]["api_key"] = "unrelated-secret"
    unrelated_change["providers"].append(
        config_store._new_provider_record("codex")
    )
    _recompute_digest(unrelated_change)
    try:
        config_store.import_provider_sync_state(unrelated_change)
    except config_store.ProviderCredentialConflict:
        pass
    else:
        raise AssertionError(
            "unrelated aggregate change authorized a credential overwrite"
        )
    assert CREDENTIALS[(_home(), api_provider["id"])] == "secret-one"

    replay_secret = config_store.import_provider_sync_state(
        copy.deepcopy(credential_payload)
    )
    assert replay_secret["sync_status"] == "unchanged"
    assert config_store._config_path().read_bytes() == before_secret_install
    assert len(NOTIFICATIONS) == before_secret_notification

    conflicting_secret = copy.deepcopy(credential_payload)
    conflicting_secret["provider_api_keys"][0]["api_key"] = "secret-conflict"
    try:
        config_store.import_provider_sync_state(conflicting_secret)
    except config_store.ProviderCredentialConflict:
        pass
    else:
        raise AssertionError("equal-authority credential conflict was accepted")
    assert CREDENTIALS[(_home(), api_provider["id"])] == "secret-one"

    race_target = ROOT / "credential-race-target"
    _select(race_target)
    config_store.import_provider_sync_state(credential_free)
    original_authoritative_read = config_store._read_api_key_authoritative
    reads = iter(("", "raced-secret"))
    config_store._read_api_key_authoritative = lambda _provider_id: next(reads)
    before_race = config_store._config_path().read_bytes()
    try:
        try:
            config_store.import_provider_sync_state(credential_payload)
        except config_store.ProviderCredentialConflict:
            pass
        else:
            raise AssertionError("concurrent credential mutation was overwritten")
    finally:
        config_store._read_api_key_authoritative = original_authoritative_read
    assert config_store._config_path().read_bytes() == before_race

    _select(SOURCE_HOME)
    before_rotation = config_store.get_provider(api_provider["id"])
    assert before_rotation is not None
    original_mutation_lock = config_store._provider_mutation_lock
    original_save = config_store._save_state
    tracking_lock = _TrackingMutationLock()
    save_entered = threading.Event()
    release_save = threading.Event()
    rotation_errors: list[BaseException] = []
    export_result: list[dict] = []

    def fail_paused_save(*_args, **_kwargs) -> None:
        save_entered.set()
        assert release_save.wait(2)
        raise RuntimeError("forced paused rotation failure")

    config_store._provider_mutation_lock = tracking_lock
    config_store._save_state = fail_paused_save

    def rotate_and_capture() -> None:
        try:
            config_store.update_provider(
                api_provider["id"],
                {"api_key": "transient-secret"},
                expected_generation=before_rotation["generation"],
                expected_revision=before_rotation["revision"],
            )
        except BaseException as exc:
            rotation_errors.append(exc)

    rotation = threading.Thread(
        name="credential-rotation",
        target=rotate_and_capture,
    )
    exporter = threading.Thread(
        name="credential-export",
        target=lambda: export_result.append(
            config_store.export_provider_sync_state([api_provider["id"]])
        ),
    )
    try:
        rotation.start()
        assert save_entered.wait(2)
        exporter.start()
        assert tracking_lock.export_attempted.wait(2)
        assert not tracking_lock.export_acquired.is_set()
        release_save.set()
        rotation.join(2)
        exporter.join(2)
    finally:
        release_save.set()
        rotation.join(2)
        exporter.join(2)
        config_store._save_state = original_save
        config_store._provider_mutation_lock = original_mutation_lock
    assert not rotation.is_alive()
    assert not exporter.is_alive()
    assert len(rotation_errors) == 1
    assert "forced paused rotation failure" in str(rotation_errors[0])
    assert export_result[0]["provider_state_authority"] == (
        credential_payload["provider_state_authority"]
    )
    assert export_result[0]["provider_api_keys"] == [{
        "provider_id": api_provider["id"],
        "api_key": "secret-one",
    }]
    assert "transient-secret" not in json.dumps(export_result[0])

    rotated = config_store.update_provider(
        api_provider["id"],
        {"api_key": "secret-two"},
        expected_generation=before_rotation["generation"],
        expected_revision=before_rotation["revision"],
    )
    assert rotated is not None
    rotated_payload = config_store.export_provider_sync_state([api_provider["id"]])
    assert rotated_payload["provider_state_authority"]["revision"] == (
        credential_payload["provider_state_authority"]["revision"] + 1
    )

    _select(fresh_target)
    before_failed_rotation = config_store._config_path().read_bytes()
    original_save = config_store._save_state
    config_store._save_state = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("forced save failure"))
    )
    try:
        try:
            config_store.import_provider_sync_state(rotated_payload)
        except RuntimeError as exc:
            assert "forced save failure" in str(exc)
        else:
            raise AssertionError("forced provider sync save failure was ignored")
    finally:
        config_store._save_state = original_save
    assert CREDENTIALS[(_home(), api_provider["id"])] == "secret-one"
    assert config_store._config_path().read_bytes() == before_failed_rotation

    def fail_after_newer_credential(*_args, **_kwargs) -> None:
        CREDENTIALS[(_home(), api_provider["id"])] = "newer-secret"
        raise RuntimeError("forced save failure after newer credential")

    config_store._save_state = fail_after_newer_credential
    try:
        try:
            config_store.import_provider_sync_state(rotated_payload)
        except RuntimeError as exc:
            assert "rollback failed" in str(exc)
            assert "newer-secret" not in str(exc)
        else:
            raise AssertionError("rollback overwrote a newer credential")
    finally:
        config_store._save_state = original_save
    assert CREDENTIALS[(_home(), api_provider["id"])] == "newer-secret"
    assert config_store._config_path().read_bytes() == before_failed_rotation

    rotated_result = config_store.import_provider_sync_state(rotated_payload)
    assert rotated_result["sync_status"] == "applied"
    assert CREDENTIALS[(_home(), api_provider["id"])] == "secret-two"

    rpc_result = node_rpc_handlers._rpc_sync_provider_config({
        "provider_state": copy.deepcopy(rotated_payload),
    })
    assert rpc_result["sync_status"] == "unchanged"
    assert rpc_result["provider_state_authority"] == (
        rotated_payload["provider_state_authority"]
    )

    projection = node_config_sync._export_providers()
    assert "provider_api_keys" not in projection
    assert "secret-two" not in json.dumps(projection)
    assert projection["provider_state_authority"] == (
        rotated_payload["provider_state_authority"]
    )


if __name__ == "__main__":
    try:
        main()
        print("PASS provider sync authority")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
