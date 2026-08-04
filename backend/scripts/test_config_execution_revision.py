from __future__ import annotations

import copy
import json

import pytest

import _test_home

_test_home.isolate("bc-test-config-execution-revision-")

import config_store  # noqa: E402


_CREDENTIALS: dict[str, str] = {}


def _credential_request(action: str, provider_id: str, **kwargs) -> dict:
    current = _CREDENTIALS.get(provider_id, "")
    if action == "read":
        return {
            "status": "available" if current else "missing",
            **({"value": current} if current else {}),
        }
    if action == "status":
        return {"status": "available" if current else "missing"}
    if action == "compare_set":
        if current != kwargs["expected_value"]:
            return {
                "status": "available" if current else "missing",
                "applied": False,
            }
        value = kwargs["value"]
        if value:
            _CREDENTIALS[provider_id] = value
            return {"status": "available", "applied": True}
        _CREDENTIALS.pop(provider_id, None)
        return {"status": "missing", "applied": True}
    raise AssertionError(f"unexpected credential action: {action}")


def _reset_config() -> None:
    config_store._config_path().unlink(missing_ok=True)
    with config_store._state_cache_lock:
        config_store._state_cache = None
    with config_store._api_key_cache_lock:
        config_store._api_key_cache.clear()
        config_store._api_key_read_locks.clear()
    config_store._credential_status.clear()


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _CREDENTIALS.clear()
    _reset_config()
    monkeypatch.setattr(
        config_store.credential_session_client,
        "available",
        lambda: True,
    )
    monkeypatch.setattr(
        config_store.credential_session_client,
        "request",
        _credential_request,
    )
    config_store._save_state(config_store._seed_default_state())


def _add_provider(**overrides) -> dict:
    return config_store.add_provider({
        "name": "Execution revision proof",
        "kind": "claude",
        "mode": "subscription",
        **overrides,
    })


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "codex"},
        {"mode": "api_key"},
        {"base_url": "https://api.example.test/v1"},
        {"config_dir": str(config_store.ba_home() / "alternate-claude")},
        {"default_permission": {"mode": "plan"}},
        {"suspended": True},
        {"allowed_sinks": ["api.example.test"]},
        {"capabilities": {"supports_fork": False}},
    ],
)
def test_execution_field_changes_advance_only_current_incarnation(
    payload: dict,
) -> None:
    created = _add_provider()

    updated = config_store.update_provider(created["id"], payload)

    assert updated is not None
    assert updated["generation"] == created["generation"]
    assert updated["revision"] == created["revision"] + 1
    assert updated["execution_revision"] == created["execution_revision"] + 1


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Renamed provider"},
        {"nickname": "Personal"},
        {"custom_models": ["custom-model"]},
    ],
)
def test_provider_metadata_changes_leave_execution_revision_stable(
    payload: dict,
) -> None:
    created = _add_provider()

    updated = config_store.update_provider(created["id"], payload)

    assert updated is not None
    assert updated["generation"] == created["generation"]
    assert updated["revision"] == created["revision"] + 1
    assert updated["execution_revision"] == created["execution_revision"]


def test_default_and_profile_metadata_leave_execution_revisions_stable() -> None:
    before = config_store.list_providers()
    previous = next(
        provider
        for provider in before["providers"]
        if provider["id"] == before["default_provider_id"]
    )
    created = _add_provider()
    profile = next(
        profile
        for profile in config_store.list_runtime_profiles()
        if profile["provider_id"] == created["id"]
    )

    config_store.activate_runtime_profile(profile["id"])

    activated_previous = config_store.get_provider(previous["id"])
    activated_created = config_store.get_provider(created["id"])
    assert activated_previous is not None
    assert activated_created is not None
    assert activated_previous["revision"] == previous["revision"] + 1
    assert activated_previous["execution_revision"] == previous["execution_revision"]
    assert activated_created["revision"] == created["revision"] + 1
    assert activated_created["execution_revision"] == created["execution_revision"]

    config_store.update_runtime_profile(profile["id"], {"name": "Renamed profile"})

    after_profile_edit = config_store.get_provider(created["id"])
    assert after_profile_edit is not None
    assert after_profile_edit["revision"] == activated_created["revision"]
    assert (
        after_profile_edit["execution_revision"]
        == activated_created["execution_revision"]
    )


def test_credential_replace_delete_and_noop_use_execution_revision() -> None:
    created = _add_provider(mode="api_key", api_key="secret-one")

    replaced = config_store.update_provider(
        created["id"],
        {"api_key": "secret-two"},
    )
    assert replaced is not None
    assert replaced["revision"] == created["revision"] + 1
    assert replaced["execution_revision"] == created["execution_revision"] + 1

    unchanged = config_store.update_provider(
        created["id"],
        {"api_key": "secret-two"},
    )
    assert unchanged is not None
    assert unchanged["revision"] == replaced["revision"]
    assert unchanged["execution_revision"] == replaced["execution_revision"]

    deleted = config_store.update_provider(created["id"], {"api_key": ""})
    assert deleted is not None
    assert deleted["revision"] == replaced["revision"] + 1
    assert deleted["execution_revision"] == replaced["execution_revision"] + 1


def test_credential_clone_advances_only_target_execution_revision() -> None:
    source = _add_provider(
        name="Clone source",
        mode="api_key",
        api_key="source-secret",
    )
    target = _add_provider(
        name="Clone target",
        mode="api_key",
        api_key="target-secret",
    )

    assert (
        config_store.clone_provider_credential(source["id"], target["id"])
        == "available"
    )

    after_source = config_store.get_provider(source["id"])
    after_target = config_store.get_provider(target["id"])
    assert after_source is not None
    assert after_target is not None
    assert after_source["revision"] == source["revision"]
    assert after_source["execution_revision"] == source["execution_revision"]
    assert after_target["revision"] == target["revision"] + 1
    assert after_target["execution_revision"] == target["execution_revision"] + 1


def test_credential_import_preserves_authoritative_execution_revision() -> None:
    created = _add_provider(mode="api_key", api_key="secret-one")
    rotated = config_store.update_provider(created["id"], {"api_key": "secret-two"})
    assert rotated is not None
    payload = config_store.export_provider_sync_state([created["id"]])
    _CREDENTIALS.clear()
    _reset_config()

    result = config_store.import_provider_sync_state(payload)

    imported = config_store.get_provider(created["id"])
    assert imported is not None
    assert result["provider_api_key_count"] == 1
    assert imported["generation"] == rotated["generation"]
    assert imported["revision"] == rotated["revision"]
    assert imported["execution_revision"] == rotated["execution_revision"]


def test_schema_v4_migration_adds_execution_revision_without_reincarnation() -> None:
    current = json.loads(config_store._config_path().read_text(encoding="utf-8"))
    schema_v4 = copy.deepcopy(current)
    schema_v4["schema_version"] = 4
    expected_authorities = {}
    for provider in schema_v4["providers"]:
        expected_authorities[provider["id"]] = (
            provider["generation"],
            provider["revision"],
        )
        provider.pop("execution_revision")
    schema_v4["provider_state_authority"] = (
        config_store.provider_sync_authority.new_authority(
            schema_v4["default_provider_id"],
            schema_v4["providers"],
        )
    )

    migrated = config_store._migrate_versioned_state(schema_v4)

    assert migrated["schema_version"] == 5
    assert {
        provider["id"]: (provider["generation"], provider["revision"])
        for provider in migrated["providers"]
    } == expected_authorities
    assert all(
        provider["execution_revision"] == 0
        for provider in migrated["providers"]
    )
    config_store._validate_config_schema_migrations()
