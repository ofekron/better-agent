#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="better-agent-runtime-profiles-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
os.environ["BETTER_CLAUDE_HOME"] = str(TEST_HOME)
os.environ["BETTER_AGENT_TEST_MODE"] = "1"
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
        return {"status": "available" if CREDENTIALS.get(provider_id) else "missing"}
    if action == "delete":
        CREDENTIALS.pop(provider_id, None)
        return {"status": "missing"}
    if action == "store":
        CREDENTIALS[provider_id] = kwargs["value"]
        return {"status": "available"}
    if action == "compare_set":
        current = CREDENTIALS.get(provider_id, "")
        if current != kwargs["expected_value"]:
            return {"status": "available" if current else "missing", "applied": False}
        value = kwargs["value"]
        if value:
            CREDENTIALS[provider_id] = value
            return {"status": "available", "applied": True}
        CREDENTIALS.pop(provider_id, None)
        return {"status": "missing", "applied": True}
    raise AssertionError(f"unexpected credential action: {action}")


config_store.credential_session_client.available = lambda: True
config_store.credential_session_client.request = _credential_request


def _reset_cache() -> None:
    with config_store._state_cache_lock:
        config_store._state_cache = None


def _some_provider() -> dict:
    providers = config_store.list_provider_ui_state()["providers"]
    assert providers, "seeded state must contain a provider"
    return providers[0]


def _raises_value_error(fn) -> str:
    try:
        fn()
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected ValueError")


def test_seeded_state_has_profiles() -> None:
    ui = config_store.list_provider_ui_state()
    default_profile = config_store.get_default_runtime_profile()
    assert default_profile is not None
    assert default_profile["provider_id"] == ui["default_provider_id"]
    for provider in ui["providers"]:
        live = [
            p
            for p in config_store.list_runtime_profiles()
            if p["provider_id"] == provider["id"]
        ]
        assert len(live) == 1, "each seeded provider gets exactly one profile"


def test_add_profile_creates_canonical_record() -> None:
    provider = _some_provider()
    assert len(provider["runner_options"]) > 1, "test needs a second runner"
    runner = provider["runner_options"][-1]
    profile = config_store.add_runtime_profile(
        {"provider_id": provider["id"], "runner": runner}
    )
    assert set(profile) == config_store._RUNTIME_PROFILE_FIELDS
    assert profile["provider_id"] == provider["id"]
    assert profile["runner"] == runner
    assert profile["deleted_at"] is None
    assert profile["name"]
    assert profile["created_at"] and profile["updated_at"]
    assert isinstance(profile["default_model"], str)
    assert isinstance(profile["default_reasoning_effort"], str)
    fetched = config_store.get_runtime_profile(profile["id"])
    assert fetched == profile
    found = config_store.find_live_runtime_profile(provider["id"], runner)
    assert found == profile


def test_add_profile_rejections() -> None:
    provider = _some_provider()
    runner = provider["runner_options"][-1]
    message = _raises_value_error(
        lambda: config_store.add_runtime_profile(
            {"provider_id": provider["id"], "runner": runner}
        )
    )
    assert "already exists" in message
    message = _raises_value_error(
        lambda: config_store.add_runtime_profile(
            {"provider_id": "nope", "runner": runner}
        )
    )
    assert "unknown provider" in message
    message = _raises_value_error(
        lambda: config_store.add_runtime_profile({"provider_id": provider["id"]})
    )
    assert "runner is required" in message
    message = _raises_value_error(
        lambda: config_store.add_runtime_profile(
            {"provider_id": provider["id"], "runner": "not-a-runner"}
        )
    )
    assert "not supported" in message


def test_update_profile_patches_and_guards() -> None:
    provider = _some_provider()
    runner = provider["runner_options"][-1]
    profile = config_store.find_live_runtime_profile(provider["id"], runner)
    assert profile is not None
    updated = config_store.update_runtime_profile(
        profile["id"], {"name": "My Profile", "default_model": "some-model"}
    )
    assert updated is not None
    assert updated["name"] == "My Profile"
    assert updated["default_model"] == "some-model"
    assert updated["updated_at"] >= profile["updated_at"]
    assert updated["created_at"] == profile["created_at"]
    for field in ("provider_id", "runner", "id"):
        message = _raises_value_error(
            lambda field=field: config_store.update_runtime_profile(
                profile["id"], {field: "x"}
            )
        )
        assert "immutable" in message
    assert config_store.update_runtime_profile("missing-id", {"name": "x"}) is None
    noop = config_store.update_runtime_profile(profile["id"], {})
    assert noop is not None and noop["updated_at"] == updated["updated_at"]


def test_soft_delete_and_listing() -> None:
    provider = _some_provider()
    runner = provider["runner_options"][-1]
    profile = config_store.find_live_runtime_profile(provider["id"], runner)
    assert profile is not None
    deleted, reason = config_store.delete_runtime_profile(profile["id"])
    assert deleted and reason == "ok"
    assert config_store.find_live_runtime_profile(provider["id"], runner) is None
    live_ids = {p["id"] for p in config_store.list_runtime_profiles()}
    assert profile["id"] not in live_ids
    tombstones = [
        p
        for p in config_store.list_runtime_profiles(include_deleted=True)
        if p["id"] == profile["id"]
    ]
    assert len(tombstones) == 1
    assert tombstones[0]["deleted_at"]
    assert config_store.get_runtime_profile(profile["id"]) is not None
    assert (
        config_store.get_runtime_profile(profile["id"], include_deleted=False) is None
    )
    deleted, reason = config_store.delete_runtime_profile(profile["id"])
    assert not deleted and reason == "already_deleted"
    deleted, reason = config_store.delete_runtime_profile("missing-id")
    assert not deleted and reason == "missing"
    message = _raises_value_error(
        lambda: config_store.update_runtime_profile(profile["id"], {"name": "x"})
    )
    assert "deleted" in message


def test_resurrect_keeps_id() -> None:
    provider = _some_provider()
    runner = provider["runner_options"][-1]
    tombstone = next(
        p
        for p in config_store.list_runtime_profiles(include_deleted=True)
        if p["provider_id"] == provider["id"]
        and p["runner"] == runner
        and p["deleted_at"]
    )
    revived = config_store.add_runtime_profile(
        {
            "provider_id": provider["id"],
            "runner": runner,
            "name": "Back Again",
            "default_model": "revived-model",
        }
    )
    assert revived["id"] == tombstone["id"]
    assert revived["deleted_at"] is None
    assert revived["name"] == "Back Again"
    assert revived["default_model"] == "revived-model"
    assert revived["created_at"] == tombstone["created_at"]
    matching = [
        p
        for p in config_store.list_runtime_profiles(include_deleted=True)
        if p["provider_id"] == provider["id"] and p["runner"] == runner
    ]
    assert len(matching) == 1


def test_persistence_roundtrip() -> None:
    before = config_store.list_runtime_profiles(include_deleted=True)
    _reset_cache()
    after = config_store.list_runtime_profiles(include_deleted=True)
    assert before == after


def test_noncanonical_records_are_rejected() -> None:
    config_path = config_store._config_path()
    raw = json.loads(Path(config_path).read_text())
    assert raw.get("runtime_profiles"), "expected persisted profiles"
    idx = next(
        i
        for i, p in enumerate(raw["runtime_profiles"])
        if p["id"] != raw.get("default_runtime_profile_id") and not p["deleted_at"]
    )

    def _expect_load_failure(mutate) -> None:
        broken = copy.deepcopy(raw)
        mutate(broken)
        Path(config_path).write_text(json.dumps(broken))
        _reset_cache()
        try:
            config_store.list_runtime_profiles()
        except RuntimeError as exc:
            assert "unsupported provider config schema" in str(exc)
        else:
            raise AssertionError("expected load failure")
        Path(config_path).write_text(json.dumps(raw))
        _reset_cache()

    _expect_load_failure(
        lambda state: state["runtime_profiles"][idx].update({"extra": True})
    )
    _expect_load_failure(
        lambda state: state["runtime_profiles"][idx].pop("created_at")
    )
    _expect_load_failure(
        lambda state: state["runtime_profiles"].append(
            copy.deepcopy(state["runtime_profiles"][idx])
        )
    )
    _expect_load_failure(
        lambda state: state["runtime_profiles"][idx].update(
            {"provider_id": "ghost", "deleted_at": None}
        )
    )

    broken = copy.deepcopy(raw)
    broken["runtime_profiles"][idx].update({"provider_id": "ghost", "deleted_at": "2026-01-01T00:00:00+00:00"})
    Path(config_path).write_text(json.dumps(broken))
    _reset_cache()
    tombstones = config_store.list_runtime_profiles(include_deleted=True)
    assert any(p["provider_id"] == "ghost" for p in tombstones)
    Path(config_path).write_text(json.dumps(raw))
    _reset_cache()


def test_activation_and_default_guards() -> None:
    ui = config_store.list_provider_ui_state()
    providers = ui["providers"]
    assert providers
    provider = providers[0]
    runner = provider["runner_options"][0]
    profile = config_store.find_live_runtime_profile(provider["id"], runner)
    assert profile is not None

    activated = config_store.activate_runtime_profile(profile["id"])
    assert activated is not None and activated["id"] == profile["id"]
    assert config_store.get_default_runtime_profile_id() == profile["id"]
    default = config_store.get_default_runtime_profile()
    assert default is not None and default["id"] == profile["id"]
    assert (
        config_store.list_provider_ui_state()["default_provider_id"]
        == provider["id"]
    )

    deleted, reason = config_store.delete_runtime_profile(profile["id"])
    assert not deleted and reason == "default"

    assert config_store.activate_runtime_profile("missing-id") is None

    activated_again = config_store.activate_runtime_profile(profile["id"])
    assert activated_again is not None


def test_activation_rejects_suspended_provider() -> None:
    ui = config_store.list_provider_ui_state()
    providers = ui["providers"]
    if len(providers) < 2:
        second = config_store.add_provider(
            {"kind": "codex", "name": "Second", "mode": "subscription"}
        )
    else:
        second = providers[1]
    runner = second["runner_options"][0]
    second_profile = config_store.find_live_runtime_profile(second["id"], runner)
    if second_profile is None:
        second_profile = config_store.add_runtime_profile(
            {"provider_id": second["id"], "runner": runner}
        )
    assert config_store.set_provider_suspended(second["id"], True) is not None
    try:
        config_store.activate_runtime_profile(second_profile["id"])
    except RuntimeError as exc:
        assert "suspended" in str(exc)
    else:
        raise AssertionError("expected suspended-provider rejection")
    assert config_store.set_provider_suspended(second["id"], False) is not None


def test_suspending_default_provider_reconciles_default_profile() -> None:
    ui = config_store.list_provider_ui_state()
    default_provider_id = ui["default_provider_id"]
    default_profile_id = config_store.get_default_runtime_profile_id()
    assert default_profile_id is not None
    assert config_store.set_provider_suspended(default_provider_id, True) is not None
    new_default_profile_id = config_store.get_default_runtime_profile_id()
    assert new_default_profile_id != default_profile_id
    new_default = config_store.get_default_runtime_profile()
    new_default_provider = config_store.list_provider_ui_state()["default_provider_id"]
    if new_default is not None:
        assert new_default["provider_id"] == new_default_provider
    assert config_store.set_provider_suspended(default_provider_id, False) is not None


def test_deleting_deleted_tombstone_activation_fails() -> None:
    ui = config_store.list_provider_ui_state()
    provider = next(
        p for p in ui["providers"] if p["id"] != ui["default_provider_id"]
    )
    runner = provider["runner_options"][0]
    profile = config_store.find_live_runtime_profile(provider["id"], runner)
    if profile is None:
        profile = config_store.add_runtime_profile(
            {"provider_id": provider["id"], "runner": runner}
        )
    deleted, reason = config_store.delete_runtime_profile(profile["id"])
    assert deleted and reason == "ok"
    message = _raises_value_error(
        lambda: config_store.activate_runtime_profile(profile["id"])
    )
    assert "deleted" in message
    revived = config_store.add_runtime_profile(
        {"provider_id": provider["id"], "runner": runner}
    )
    assert revived["id"] == profile["id"]


def test_provider_deletion_cascade_and_graveyard() -> None:
    ui = config_store.list_provider_ui_state()
    victim = next(
        p for p in ui["providers"] if p["id"] != ui["default_provider_id"]
    )
    runner = victim["runner_options"][0]
    profile = config_store.find_live_runtime_profile(victim["id"], runner)
    if profile is None:
        profile = config_store.add_runtime_profile(
            {"provider_id": victim["id"], "runner": runner}
        )
    deleted, reason = config_store.delete_provider(victim["id"])
    assert deleted and reason == "ok", reason

    grave = config_store.get_deleted_provider(victim["id"])
    assert grave is not None
    assert grave["kind"] == victim["kind"] and grave["deleted_at"]
    assert set(grave) == {"id", "name", "nickname", "kind", "deleted_at"}

    tombstone = config_store.get_runtime_profile(profile["id"])
    assert tombstone is not None and tombstone["deleted_at"]
    assert config_store.find_live_runtime_profile(victim["id"], runner) is None

    live_ids = {p["id"] for p in config_store.list_provider_ui_state()["providers"]}
    assert victim["id"] not in live_ids

    recreated = config_store.add_provider(
        {"kind": victim["kind"], "name": "Recreated", "mode": "subscription"}
    )
    assert recreated["id"] != victim["id"]
    _reset_cache()
    assert config_store.get_deleted_provider(victim["id"]) is not None


def test_installation_selection_seeds_profile() -> None:
    import installation_profile

    existing_kinds = {
        p["kind"] for p in config_store.list_provider_ui_state()["providers"]
    }
    kind = next(
        k for k in ("agy", "qwen", "kimi") if k not in existing_kinds
    )
    original_load = installation_profile.load
    original_mark = installation_profile.mark_selection_applied
    installation_profile.load = lambda: {"provider": kind}
    installation_profile.mark_selection_applied = lambda: None
    try:
        config_store.apply_installation_profile_selection()
    finally:
        installation_profile.load = original_load
        installation_profile.mark_selection_applied = original_mark
    provider = next(
        p
        for p in config_store.list_provider_ui_state()["providers"]
        if p["kind"] == kind
    )
    live = [
        p
        for p in config_store.list_runtime_profiles()
        if p["provider_id"] == provider["id"]
    ]
    assert len(live) == 1, "installation selection seeds exactly one profile"
    assert live[0]["runner"] in provider["runner_options"]


def test_v2_internal_llm_assignment_migrates_to_profile_ref() -> None:
    config_path = config_store._config_path()
    raw = json.loads(Path(config_path).read_text())
    v2 = copy.deepcopy(raw)
    v2["schema_version"] = 2
    profiles_by_provider = {
        p["provider_id"]: p for p in v2.pop("runtime_profiles") if not p["deleted_at"]
    }
    v2.pop("default_runtime_profile_id")
    v2.pop("deleted_providers")
    v2.pop("disabled_runtime_skills")
    pinned_provider = None
    pinned_runner = None
    for provider in v2["providers"]:
        provider.pop("execution_revision")
        profile = profiles_by_provider.get(provider["id"])
        provider["runner"] = profile["runner"] if profile else "native"
        provider["default_model"] = profile["default_model"] if profile else ""
        provider["default_reasoning_effort"] = (
            profile["default_reasoning_effort"] if profile else ""
        )
        if pinned_provider is None:
            pinned_provider = provider["id"]
            pinned_runner = provider["runner"]
    v2["internal_llm"] = {
        "default_session": {
            "provider_id": pinned_provider,
            "runner": pinned_runner,
            "model": "pinned-model",
        }
    }
    v2["provider_state_authority"] = config_store.provider_sync_authority.new_authority(
        v2["default_provider_id"], v2["providers"]
    )
    Path(config_path).write_text(json.dumps(v2))
    _reset_cache()
    try:
        migrated = config_store.get_internal_llm_assignments()
        entry = migrated.get("default_session") or {}
        expected_profile = config_store.find_live_runtime_profile(
            pinned_provider, pinned_runner
        )
        assert expected_profile is not None
        assert entry.get("runtime_profile_id") == expected_profile["id"]
        assert entry.get("model") == "pinned-model"
        assert "provider_id" not in entry and "runner" not in entry
        resolved = config_store.resolve_internal_llm("default_session")
        assert resolved["provider_id"] == pinned_provider
        assert resolved["runner"] == pinned_runner
        assert resolved["model"] == "pinned-model"
        assert resolved["runtime_profile_id"] == expected_profile["id"]
    finally:
        Path(config_path).write_text(json.dumps(raw))
        _reset_cache()


def test_add_provider_seeds_profile() -> None:
    created = config_store.add_provider(
        {
            "kind": "openai",
            "name": "Token Router",
            "mode": "api_key",
            "base_url": "https://api.tokenrouter.com/v1",
            "default_model": "kimi-k3",
        }
    )
    live = [
        p
        for p in config_store.list_runtime_profiles()
        if p["provider_id"] == created["id"]
    ]
    assert len(live) == 1, "a user-created provider gets exactly one live profile"
    assert live[0]["runner"] == "better_agent_runner"
    assert live[0]["default_model"] == "kimi-k3", "payload default_model reaches profile"

    defaults = config_store.provider_execution_defaults(created["id"])
    assert defaults["runtime_profile_id"] == live[0]["id"]
    assert defaults["runner"] == "better_agent_runner"
    assert defaults["default_model"] == "kimi-k3"

    _reset_cache()
    persisted = [
        p
        for p in config_store.list_runtime_profiles()
        if p["provider_id"] == created["id"]
    ]
    assert len(persisted) == 1, "seeded profile survives a reload"

    # No default_model in the payload must fall back to the kind's seed, not "".
    seeded = config_store.add_provider({"kind": "claude", "mode": "subscription"})
    seeded_profile = next(
        p
        for p in config_store.list_runtime_profiles()
        if p["provider_id"] == seeded["id"]
    )
    assert seeded_profile["default_model"] == "opus", "absent key falls back to seed"
    assert seeded_profile["default_reasoning_effort"], "effort is populated"


def test_default_profile_name_includes_account_and_provider() -> None:
    # Regression: _default_runtime_profile_name previously picked
    # nickname OR name, dropping the provider/company name whenever an
    # account nickname was set. A profile name must carry both dimensions
    # so two accounts of the same provider kind stay distinguishable.
    created = config_store.add_provider(
        {
            "kind": "claude",
            "name": "Claude",
            "nickname": "Work",
            "mode": "subscription",
        }
    )
    profile = next(
        p
        for p in config_store.list_runtime_profiles()
        if p["provider_id"] == created["id"]
    )
    assert profile["name"].startswith("Claude · Work ("), profile["name"]

    no_nickname = config_store.add_provider(
        {"kind": "claude", "name": "Claude", "mode": "subscription"}
    )
    plain_profile = next(
        p
        for p in config_store.list_runtime_profiles()
        if p["provider_id"] == no_nickname["id"]
    )
    assert plain_profile["name"].startswith("Claude ("), plain_profile["name"]
    assert "·" not in plain_profile["name"]


def test_provision_catalog_seeds_profiles() -> None:
    import uuid as _uuid

    state_path = config_store._config_path()
    backup = state_path.read_bytes() if state_path.exists() else None
    state_path.unlink(missing_ok=True)
    _reset_cache()
    try:
        pid = str(_uuid.uuid4())
        result = config_store.provision_provider_catalog(
            {
                "providers": [
                    {"id": pid, "name": "C", "kind": "claude", "mode": "subscription"}
                ],
                "default_provider_id": pid,
            }
        )
        assert result, "catalog provisioning returns the provider set"
        live = [p for p in config_store.list_runtime_profiles() if p["provider_id"] == pid]
        assert len(live) == 1, "each catalog provider gets exactly one live profile"
        orphans = [
            p
            for p in config_store.list_runtime_profiles()
            if p["provider_id"] != pid
        ]
        assert not orphans, "seed-state profiles must not survive catalog replacement"
        default_profile = config_store.get_default_runtime_profile()
        assert default_profile is not None
        assert default_profile["provider_id"] == pid, "lockstep default is re-derived"
    finally:
        if backup is not None:
            state_path.write_bytes(backup)
        else:
            state_path.unlink(missing_ok=True)
        _reset_cache()


def test_sync_import_seeds_profiles() -> None:
    import uuid as _uuid

    import provider_sync_authority

    state_path = config_store._config_path()
    backup = state_path.read_bytes() if state_path.exists() else None
    try:
        pid = str(_uuid.uuid4())
        providers = [
            {
                **config_store._clean_provider_record(
                    {
                        "id": pid,
                        "name": "Synced",
                        "kind": "claude",
                        "mode": "subscription",
                    }
                ),
                **config_store._new_provider_authority(),
                "execution_revision": 0,
            }
        ]
        authority = provider_sync_authority.new_authority(pid, providers)
        config_store.apply_provider_sync_projection(
            {
                "providers": providers,
                "default_provider_id": pid,
                "provider_state_authority": authority,
            }
        )
        live = [
            p for p in config_store.list_runtime_profiles() if p["provider_id"] == pid
        ]
        assert len(live) == 1, "a synced-in provider gets exactly one live profile"
        default_profile = config_store.get_default_runtime_profile()
        assert default_profile is not None, "lockstep default must not go null"
        assert default_profile["provider_id"] == pid
    finally:
        if backup is not None:
            state_path.write_bytes(backup)
        else:
            state_path.unlink(missing_ok=True)
        _reset_cache()


def test_profile_prefill_chain() -> None:
    import user_prefs
    from provider_validation import profile_prefill_model

    provider = next(
        p
        for p in config_store.list_provider_ui_state()["providers"]
        if config_store.provider_execution_defaults(p["id"])["runtime_profile_id"]
    )
    defaults = config_store.provider_execution_defaults(provider["id"])
    profile_id = defaults["runtime_profile_id"]
    updated = config_store.update_runtime_profile(
        profile_id, {"default_model": "chain-default"}
    )
    assert updated is not None
    assert (
        profile_prefill_model(provider["id"], updated["runner"]) == "chain-default"
    ), "profile default must prefill when nothing was used yet"
    assert user_prefs.set_last_model(profile_id, "chain-last")
    assert (
        profile_prefill_model(provider["id"], updated["runner"]) == "chain-last"
    ), "last-used must win over the profile default"
    assert profile_prefill_model("ghost-provider") == ""
    assert profile_prefill_model(None) == ""


def main() -> int:
    tests = [
        test_seeded_state_has_profiles,
        test_add_profile_creates_canonical_record,
        test_add_profile_rejections,
        test_update_profile_patches_and_guards,
        test_soft_delete_and_listing,
        test_resurrect_keeps_id,
        test_persistence_roundtrip,
        test_noncanonical_records_are_rejected,
        test_activation_and_default_guards,
        test_activation_rejects_suspended_provider,
        test_suspending_default_provider_reconciles_default_profile,
        test_deleting_deleted_tombstone_activation_fails,
        test_provider_deletion_cascade_and_graveyard,
        test_add_provider_seeds_profile,
        test_default_profile_name_includes_account_and_provider,
        test_provision_catalog_seeds_profiles,
        test_sync_import_seeds_profiles,
        test_installation_selection_seeds_profile,
        test_v2_internal_llm_assignment_migrates_to_profile_ref,
        test_profile_prefill_chain,
    ]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
