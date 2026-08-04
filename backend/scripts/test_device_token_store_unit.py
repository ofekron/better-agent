"""Unit coverage for device_token_store.py.

Rebuildable registration table for push-device tokens + per-device
notification preferences, persisted to push/device_tokens.json under the state
home. Atomic JSON writes under a module RLock; no pydantic, no async, no event
bus. The store is intentionally self-healing: a stale schema or a non-dict
devices map resets to empty rather than 500ing callers (it is a disposable
projection of registration, not a durable source of truth).

Covers every branch of every function directly, including the schema-reset and
non-dict defensive paths that the script-style test_push_notifications.py happy
paths never reach.

Home is engaged to an isolated tempdir at import time. An autouse fixture
resets the module's path-cache globals and clears the store file before each
test: the path cache is keyed on ba_home(), and pytest may collect
test_push_notifications.py (which imports this module) in the same process, so
without the reset a stale cache could pin writes to another file's home.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402

_test_home.isolate("device-token-store-test-")

import device_token_store as store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    """Start each test cold: path cache re-resolved to THIS home, store file
    absent. Guards against cross-file path-cache pollution."""
    store._PATH_CACHE_HOME = None
    store._PATH_CACHE_VALUE = None
    path = store._path()  # cache miss -> repopulates against our home
    if path.exists():
        path.unlink()
    yield


def _write_raw(payload: dict) -> Path:
    """Write an arbitrary JSON payload straight to the store file."""
    path = store._path()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── _path ─────────────────────────────────────────────────────────────────── #

def test_path_caches_value_after_first_resolution():
    store._PATH_CACHE_HOME = None
    store._PATH_CACHE_VALUE = None
    first = store._path()  # cache miss -> mkdir + build + populate cache
    second = store._path()  # cache hit -> returns cached value
    assert first is second
    assert store._PATH_CACHE_VALUE is second
    assert store._PATH_CACHE_HOME is not None


# ── _read_locked ──────────────────────────────────────────────────────────── #

def test_read_locked_absent_file_returns_defaults():
    assert store._read_locked() == {
        "schema_version": 1,
        "devices": {},
        "notification_preferences": {},
    }


def test_read_locked_happy_returns_stored_payload():
    payload = {
        "schema_version": 1,
        "devices": {"d": {"device_id": "d"}},
        "notification_preferences": {"d": {}},
    }
    _write_raw(payload)
    data = store._read_locked()
    assert "d" in data["devices"]
    assert data["notification_preferences"] == {"d": {}}


def test_read_locked_stale_schema_resets_to_empty():
    _write_raw({"schema_version": 99, "devices": {}, "notification_preferences": {}})
    data = store._read_locked()
    assert data == {
        "schema_version": 1,
        "devices": {},
        "notification_preferences": {},
    }


def test_read_locked_devices_not_dict_resets_to_empty():
    _write_raw({"schema_version": 1, "devices": [], "notification_preferences": {}})
    data = store._read_locked()
    assert data["devices"] == {}


def test_read_locked_notification_preferences_not_dict_coerced():
    _write_raw({"schema_version": 1, "devices": {}, "notification_preferences": "nope"})
    data = store._read_locked()
    assert data["notification_preferences"] == {}


# ── _preferences ──────────────────────────────────────────────────────────── #

def test_preferences_non_dict_returns_defaults_copy():
    expected = dict(store.DEFAULT_NOTIFICATION_PREFERENCES)
    assert store._preferences("nope") == expected
    assert store._preferences(None) == expected
    # returned dict is a fresh copy, not the module default identity
    assert store._preferences(None) is not store.DEFAULT_NOTIFICATION_PREFERENCES


def test_preferences_uses_bools_defaults_missing_and_coerces_non_bool():
    stored = {"pending_approvals": False, "completed_turns": "x", "extra": True}
    result = store._preferences(stored)
    assert result["pending_approvals"] is False        # bool -> used
    assert result["pending_questions"] is True          # missing -> default
    assert result["completed_turns"] is True            # non-bool -> default
    assert "extra" not in result                        # only known categories


# ── _validate_preferences ─────────────────────────────────────────────────── #

def test_validate_preferences_not_dict_raises():
    with pytest.raises(store.NotificationPreferencesError, match="must be an object"):
        store._validate_preferences("nope", partial=True)


def test_validate_preferences_unexpected_key_raises():
    with pytest.raises(store.NotificationPreferencesError, match="unsupported"):
        store._validate_preferences({"unknown": True}, partial=True)


def test_validate_preferences_full_requires_all_categories():
    with pytest.raises(store.NotificationPreferencesError, match="all notification"):
        store._validate_preferences({"pending_approvals": True}, partial=False)


def test_validate_preferences_non_bool_value_raises():
    with pytest.raises(store.NotificationPreferencesError, match="boolean"):
        store._validate_preferences({"pending_approvals": "yes"}, partial=True)


def test_validate_preferences_partial_happy():
    assert store._validate_preferences({"pending_approvals": False}, partial=True) == {
        "pending_approvals": False
    }


def test_validate_preferences_full_happy():
    full = {c: True for c in store.NOTIFICATION_CATEGORIES}
    assert store._validate_preferences(full, partial=False) == full


# ── register_token ────────────────────────────────────────────────────────── #

def test_register_new_device_returns_public_shape():
    rec = store.register_token("d1", "tok1", "android", "s1")
    assert rec["device_id"] == "d1"
    assert rec["platform"] == "android"
    assert rec["session_ids"] == ["s1"]
    assert rec["notification_preferences"] == dict(store.DEFAULT_NOTIFICATION_PREFERENCES)
    assert "token" not in rec  # public shape omits the secret token
    assert rec["created_at"] == rec["updated_at"]


def test_register_with_partial_preferences_merges_over_defaults():
    rec = store.register_token("d2", "tok2", "ios", "s2", {"pending_approvals": False})
    assert rec["notification_preferences"]["pending_approvals"] is False
    assert rec["notification_preferences"]["pending_questions"] is True


def test_register_same_device_accumulates_sessions_and_preserves_created_at():
    r1 = store.register_token("d3", "tok3", "ios", "s1")
    created = r1["created_at"]
    store.register_token("d3", "tok3", "ios", "s1")  # same session -> no-op append
    r3 = store.register_token("d3", "tok3", "ios", "s2")  # new session -> append
    assert r3["session_ids"] == ["s1", "s2"]
    assert r3["created_at"] == created  # original creation timestamp preserved


def test_register_token_reassigns_duplicate_token_to_new_device():
    store.register_token("d-old", "shared", "ios", "s-shared")
    store.register_token("d-new", "shared", "ios", "s-shared")
    tokens = store.get_tokens_for_session("s-shared")
    assert [t["device_id"] for t in tokens] == ["d-new"]


def test_register_handles_non_dict_records_without_crashing():
    # Corrupt store exercising every defensive guard in register_token's
    # duplicate scan + existing-record lookup:
    #   - "d-junk": non-dict  -> isinstance(dict) False (skipped in scan)
    #   - "d-other": dict, different token -> token== False (not a duplicate)
    #   - "d4": non-dict AND is the target id -> new-record path; the scan also
    #     hits the != device_id False branch on itself.
    _write_raw({
        "schema_version": 1,
        "devices": {
            "d-junk": "not-a-dict",
            "d-other": {"device_id": "d-other", "token": "different"},
            "d4": 123,
        },
        "notification_preferences": {},
    })
    rec = store.register_token("d4", "tok4", "ios", "s9")
    assert rec["session_ids"] == ["s9"]  # existing was non-dict -> new record


# ── unregister_token ──────────────────────────────────────────────────────── #

def test_unregister_token_absent_returns_false():
    assert store.unregister_token("nope") is False


def test_unregister_token_present_returns_true_then_false():
    store.register_token("d5", "tok5", "ios", "s5")
    assert store.unregister_token("d5") is True
    assert store.unregister_token("d5") is False  # already gone


# ── unregister_token_for_value ────────────────────────────────────────────── #

def test_unregister_token_for_value_no_match_returns_false():
    assert store.unregister_token_for_value("absent") is False


def test_unregister_token_for_value_skips_non_dict_and_removes_match():
    store.register_token("d6", "tok6", "ios", "s6")
    data = store._read_locked()
    data["devices"]["d-junk"] = "not-a-dict"  # non-dict must be skipped, not crash
    store._write_locked(data)
    assert store.unregister_token_for_value("tok6") is True
    assert store.get_tokens_for_session("s6") == []
    # non-dict entry left untouched (only the matching record was removed)
    assert store._read_locked()["devices"]["d-junk"] == "not-a-dict"


# ── get_tokens_for_session(_category) ─────────────────────────────────────── #

def test_get_tokens_for_session_delegates_to_category():
    store.register_token("d10", "tok10", "ios", "s10")
    assert store.get_tokens_for_session("s10") == store.get_tokens_for_session_category("s10")


def test_get_tokens_for_session_category_invalid_category_raises():
    with pytest.raises(store.NotificationPreferencesError, match="unsupported"):
        store.get_tokens_for_session_category("s", "bogus")


def test_get_tokens_for_session_category_none_returns_all_session_devices():
    store.register_token("d7", "tok7", "ios", "s7")
    store.register_token("d8", "tok8", "android", "s7")
    result = store.get_tokens_for_session_category("s7", None)
    assert sorted(t["device_id"] for t in result) == ["d7", "d8"]
    assert set(result[0].keys()) == {"device_id", "token", "platform"}


def test_get_tokens_for_session_category_filters_by_disabled_preference():
    store.register_token("d7", "tok7", "ios", "s7", {"completed_turns": False})
    store.register_token("d8", "tok8", "android", "s7")
    excluded = store.get_tokens_for_session_category("s7", "completed_turns")
    assert [t["device_id"] for t in excluded] == ["d8"]
    included = store.get_tokens_for_session_category("s7", "pending_questions")
    assert sorted(t["device_id"] for t in included) == ["d7", "d8"]


def test_get_tokens_for_session_category_skips_non_dict_records():
    store.register_token("d9", "tok9", "ios", "s9")
    data = store._read_locked()
    data["devices"]["d-junk"] = "not-a-dict"
    store._write_locked(data)
    result = store.get_tokens_for_session_category("s9", None)
    assert [t["device_id"] for t in result] == ["d9"]


# ── get_notification_preferences ──────────────────────────────────────────── #

def test_get_notification_preferences_unknown_device_returns_defaults():
    assert store.get_notification_preferences("unknown") == dict(
        store.DEFAULT_NOTIFICATION_PREFERENCES
    )


# ── update_notification_preferences ───────────────────────────────────────── #

def test_update_notification_preferences_empty_patch_raises():
    with pytest.raises(store.NotificationPreferencesError, match="at least one"):
        store.update_notification_preferences("d", {})


def test_update_notification_preferences_for_unregistered_device_skips_updated_at():
    # No prior device record -> data["devices"].get returns None -> updated_at
    # skip branch; preferences still persisted.
    result = store.update_notification_preferences("d11", {"pending_approvals": False})
    assert result["pending_approvals"] is False
    assert result["pending_questions"] is True
    assert store.get_notification_preferences("d11") == result


def test_update_notification_preferences_for_registered_device_touches_updated_at():
    store.register_token("d12", "tok12", "ios", "s12")
    before = store._read_locked()["devices"]["d12"]["updated_at"]
    store.update_notification_preferences("d12", {"completed_turns": False})
    after = store._read_locked()["devices"]["d12"]["updated_at"]
    assert after >= before
    assert store.get_notification_preferences("d12")["completed_turns"] is False
