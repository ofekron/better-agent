from __future__ import annotations

import os
import json
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-config-keyring-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402


def _reset_cache() -> None:
    with config_store._api_key_cache_lock:
        config_store._api_key_cache.clear()
    with config_store._state_cache_lock:
        config_store._state_cache = None
    config_store._keyring_blocked = False


def test_provider_api_key_uses_agent_service_and_legacy_fallback() -> None:
    values: dict[tuple[str, str], str] = {}

    def get_password(service: str, username: str) -> str | None:
        return values.get((service, username))

    def set_password(service: str, username: str, password: str) -> None:
        values[(service, username)] = password

    def delete_password(service: str, username: str) -> None:
        values.pop((service, username), None)

    real_get = config_store.keyring.get_password
    real_set = config_store.keyring.set_password
    real_delete = config_store.keyring.delete_password
    config_store.keyring.get_password = get_password
    config_store.keyring.set_password = set_password
    config_store.keyring.delete_password = delete_password
    try:
        _reset_cache()
        username = config_store._keyring_username("provider-1")
        values[(config_store.LEGACY_KEYRING_SERVICE, username)] = "legacy-key"
        assert config_store._read_api_key("provider-1") == "legacy-key"

        _reset_cache()
        config_store._write_api_key("provider-1", "agent-key")
        assert values[(config_store.KEYRING_SERVICE, username)] == "agent-key"
        assert values[(config_store.LEGACY_KEYRING_SERVICE, username)] == "agent-key"
        assert config_store._read_api_key("provider-1") == "agent-key"

        config_store._delete_api_key("provider-1")
        assert (config_store.KEYRING_SERVICE, username) not in values
        assert (config_store.LEGACY_KEYRING_SERVICE, username) not in values

        _reset_cache()
        values[(config_store.LEGACY_KEYRING_SERVICE, config_store.LEGACY_KEYRING_USERNAME)] = "old-slot"
        assert config_store._read_legacy_api_key() == "old-slot"
        config_store._delete_legacy_api_key()
        assert (config_store.LEGACY_KEYRING_SERVICE, config_store.LEGACY_KEYRING_USERNAME) not in values
    finally:
        config_store.keyring.get_password = real_get
        config_store.keyring.set_password = real_set
        config_store.keyring.delete_password = real_delete


def test_denied_keyring_read_is_not_cached_and_a_later_read_recovers() -> None:
    """A single denied/failed Keychain read (e.g. the user clicking "Deny"
    on the macOS access prompt) must not be cached as a permanent "no key"
    result — the next read, once the key is actually reachable, must
    return the real value instead of being poisoned forever."""
    values: dict[tuple[str, str], str] = {}
    fail_next = {"count": 0}

    def get_password(service: str, username: str) -> str | None:
        if fail_next["count"] > 0:
            fail_next["count"] -= 1
            raise config_store.keyring.errors.KeyringError("Keychain Access Denied (-128)")
        return values.get((service, username))

    real_get = config_store.keyring.get_password
    config_store.keyring.get_password = get_password
    try:
        _reset_cache()
        username = config_store._keyring_username("provider-denied")
        values[(config_store.KEYRING_SERVICE, username)] = "real-key"

        # First read: the first service lookup is denied. `_read_api_key_uncached`
        # short-circuits on the first failure, so a single queued failure is
        # enough to exercise the denied path. Must resolve to empty without
        # caching the denial.
        fail_next["count"] = 1
        assert config_store._read_api_key("provider-denied") == ""
        with config_store._api_key_cache_lock:
            assert "provider-denied" not in config_store._api_key_cache

        # Second read: keychain is reachable again. Must return the real
        # key, not a cached empty string from the earlier denial.
        assert config_store._read_api_key("provider-denied") == "real-key"
        with config_store._api_key_cache_lock:
            assert config_store._api_key_cache["provider-denied"] == "real-key"
    finally:
        config_store.keyring.get_password = real_get


def test_load_state_uses_fingerprint_cache_and_external_invalidates() -> None:
    _reset_cache()
    first = config_store._load_state()
    original_read_json = config_store.read_json
    calls = 0

    def counted_read_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_json(*args, **kwargs)

    config_store.read_json = counted_read_json
    try:
        second = config_store._load_state()
        assert second == first
        assert calls == 0

        path = config_store._config_path()
        raw = original_read_json(path, {})
        raw["delegate_task_policy"] = "manual"
        path.write_text(json.dumps(raw), encoding="utf-8")
        third = config_store._load_state()
        assert calls == 1
        assert third["delegate_task_policy"] == "manual"
    finally:
        config_store.read_json = original_read_json


if __name__ == "__main__":
    test_provider_api_key_uses_agent_service_and_legacy_fallback()
    test_denied_keyring_read_is_not_cached_and_a_later_read_recovers()
    test_load_state_uses_fingerprint_cache_and_external_invalidates()
    print("OK: config_store keyring compatibility")
