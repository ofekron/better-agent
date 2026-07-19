from __future__ import annotations

import os
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time

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
        config_store._api_key_read_locks.clear()
    with config_store._state_cache_lock:
        config_store._state_cache = None
    config_store._keyring_blocked_entries.clear()
    config_store._credential_status.clear()


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


def test_failed_stable_read_is_suppressed_until_credential_mutation() -> None:
    real_get = config_store.oskeychain.get
    real_store = config_store.oskeychain.store
    real_api = config_store._use_stable_macos_keychain
    reads = 0

    def denied_get(service: str, username: str, *, timeout: float) -> str | None:
        nonlocal reads
        reads += 1
        raise RuntimeError("denied")

    config_store.oskeychain.get = denied_get
    config_store.oskeychain.store = lambda service, username, password: None
    config_store._use_stable_macos_keychain = lambda: True
    try:
        _reset_cache()
        barrier = threading.Barrier(8)
        results: list[str] = []

        def read() -> None:
            barrier.wait()
            results.append(config_store._read_api_key("provider-blocked"))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert results == [""] * 8
        assert reads == 1

        config_store._write_api_key("provider-blocked", "replacement")
        with config_store._api_key_cache_lock:
            config_store._api_key_cache.pop("provider-blocked", None)
        assert config_store._read_api_key("provider-blocked") == ""
        assert reads == 2
    finally:
        config_store.oskeychain.get = real_get
        config_store.oskeychain.store = real_store
        config_store._use_stable_macos_keychain = real_api


def test_native_session_provider_kind_never_reads_keychain() -> None:
    import native_session_miner

    real_load = config_store._load_state
    real_read = config_store._read_api_key
    config_store._load_state = lambda: {
        "providers": [{"id": "provider-1", "kind": "codex", "mode": "api_key"}],
    }
    config_store._read_api_key = lambda provider_id: (_ for _ in ()).throw(
        AssertionError("metadata classification must not read credentials")
    )
    try:
        assert native_session_miner._provider_kind({"provider_id": "provider-1"}) == "codex"
    finally:
        config_store._load_state = real_load
        config_store._read_api_key = real_read


def test_provider_config_reads_are_pure_and_ui_status_is_explicit() -> None:
    real_load = config_store._load_state
    real_read = config_store._read_api_key
    real_status = config_store.provider_credential_status
    provider = {
        "id": "provider-pure",
        "name": "Pure provider",
        "kind": "codex",
        "mode": "api_key",
        "capabilities": {"supports_fork": False},
    }
    reads: list[str] = []
    status_reads: list[str] = []
    config_store._load_state = lambda: {
        "default_provider_id": provider["id"],
        "providers": [provider],
    }

    def read(provider_id: str) -> str:
        reads.append(provider_id)
        return "secret"

    config_store._read_api_key = read
    config_store.provider_credential_status = lambda provider_id: (
        status_reads.append(provider_id) or "available"
    )
    try:
        listed = config_store.list_providers()
        fetched = config_store.get_provider(provider["id"])
        resolved = config_store.resolve_provider_ref("Pure provider")
        exported = config_store.export_provider_sync_state()

        assert reads == []
        assert status_reads == []
        for record in (listed["providers"][0], fetched, resolved, exported["providers"][0]):
            assert record is not None
            assert record["supports_fork"] is False
            assert "has_api_key" not in record

        ui_state = config_store.list_provider_ui_state()
        assert reads == []
        assert status_reads == [provider["id"]]
        assert ui_state["providers"][0]["credential_status"] == "available"
        assert ui_state["providers"][0]["has_api_key"] is True
        assert ui_state["providers"][0]["supports_fork"] is False
    finally:
        config_store._load_state = real_load
        config_store._read_api_key = real_read
        config_store.provider_credential_status = real_status


def test_distinct_provider_reads_do_not_block_each_other() -> None:
    real_get = config_store.oskeychain.get
    real_api = config_store._use_stable_macos_keychain
    overlap = threading.Barrier(2)

    def denied_get(service: str, username: str, *, timeout: float) -> str | None:
        overlap.wait(timeout=1)
        raise RuntimeError("denied")

    config_store.oskeychain.get = denied_get
    config_store._use_stable_macos_keychain = lambda: True
    try:
        _reset_cache()
        threads = [
            threading.Thread(target=config_store._read_api_key, args=(provider_id,))
            for provider_id in ("provider-a", "provider-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        config_store.oskeychain.get = real_get
        config_store._use_stable_macos_keychain = real_api


def test_failed_stable_delete_keeps_cached_provider_key() -> None:
    real_delete = config_store.oskeychain.delete
    real_api = config_store._use_stable_macos_keychain

    def denied_delete(service: str, username: str) -> None:
        raise RuntimeError("denied")

    config_store.oskeychain.delete = denied_delete
    config_store._use_stable_macos_keychain = lambda: True
    try:
        _reset_cache()
        with config_store._api_key_cache_lock:
            config_store._api_key_cache["provider-1"] = "cached-key"
        try:
            config_store._delete_api_key("provider-1")
        except RuntimeError as exc:
            assert str(exc) == "stable macOS keychain delete failed"
        else:
            raise AssertionError("failed stable delete must propagate")
        with config_store._api_key_cache_lock:
            assert config_store._api_key_cache["provider-1"] == "cached-key"
    finally:
        config_store.oskeychain.delete = real_delete
        config_store._use_stable_macos_keychain = real_api


def test_macos_read_uses_stable_security_identity_and_exact_account() -> None:
    calls = []
    real_api = config_store._use_stable_macos_keychain
    real_get = config_store.oskeychain.get
    config_store._use_stable_macos_keychain = lambda: True
    config_store.oskeychain.get = lambda service, account, **kwargs: calls.append(
        (service, account, kwargs["timeout"])
    ) or "  key  \n"
    try:
        value = config_store._get_password_with_reason(
            config_store.KEYRING_SERVICE,
            config_store._keyring_username("provider-1"),
            "unused by stable security reader",
        )
    finally:
        config_store._use_stable_macos_keychain = real_api
        config_store.oskeychain.get = real_get
    assert value == "  key  "
    assert calls == [(
        config_store.KEYRING_SERVICE,
        "provider:provider-1",
        config_store._KEYRING_TIMEOUT,
    )]


def test_macos_write_uses_stable_security_identity_without_secret_argv() -> None:
    calls = []
    real_api = config_store._use_stable_macos_keychain
    real_store = config_store.oskeychain.store
    config_store._use_stable_macos_keychain = lambda: True
    config_store.oskeychain.store = lambda service, account, value: calls.append(
        (service, account, value)
    )
    try:
        config_store._set_password_with_reason(
            config_store.KEYRING_SERVICE,
            config_store._keyring_username("provider-1"),
            "secret-value",
            "unused by stable security writer",
        )
    finally:
        config_store._use_stable_macos_keychain = real_api
        config_store.oskeychain.store = real_store
    assert calls == [(
        config_store.KEYRING_SERVICE,
        "provider:provider-1",
        "secret-value",
    )]


def test_darwin_stable_write_does_not_depend_on_keyring_backend() -> None:
    stable_calls = []
    fallback_calls = []
    real_system = platform.system
    real_get_keyring = config_store.keyring.get_keyring
    real_set_password = config_store.keyring.set_password
    real_original_set = config_store._ORIGINAL_KEYRING_SET_PASSWORD
    real_store = config_store.oskeychain.store

    def fallback_set(service: str, account: str, value: str) -> None:
        fallback_calls.append((service, account, value))

    platform.system = lambda: "Darwin"
    config_store.keyring.get_keyring = lambda: object()
    config_store.keyring.set_password = fallback_set
    config_store._ORIGINAL_KEYRING_SET_PASSWORD = fallback_set
    config_store.oskeychain.store = lambda service, account, value: stable_calls.append(
        (service, account, value)
    )
    try:
        config_store._set_password_with_reason(
            config_store.KEYRING_SERVICE,
            config_store._keyring_username("provider-isolated"),
            "secret-value",
            "unused by stable security writer",
        )
    finally:
        platform.system = real_system
        config_store.keyring.get_keyring = real_get_keyring
        config_store.keyring.set_password = real_set_password
        config_store._ORIGINAL_KEYRING_SET_PASSWORD = real_original_set
        config_store.oskeychain.store = real_store

    assert stable_calls == [(
        config_store.KEYRING_SERVICE,
        "provider:provider-isolated",
        "secret-value",
    )]
    assert fallback_calls == []


def test_macos_store_input_keeps_secret_out_of_argv_and_quotes_identifiers() -> None:
    payload = config_store.oskeychain._store_input(
        "service '; $()",
        'account "quoted" ; $(x) \\ path',
        "secret-value",
    )
    assert b"secret-value" not in payload
    assert b"7365637265742d76616c7565" in payload
    assert b'\\"quoted\\"' in payload
    assert b'$(x)' in payload
    try:
        config_store.oskeychain._store_input("bad\nservice", "account", "value")
    except ValueError:
        pass
    else:
        raise AssertionError("control characters must be rejected")


def test_macos_write_failure_propagates_without_cache_update() -> None:
    real_api = config_store._use_stable_macos_keychain
    real_store = config_store.oskeychain.store
    config_store._use_stable_macos_keychain = lambda: True
    config_store.oskeychain.store = lambda *args: (_ for _ in ()).throw(
        RuntimeError("denied")
    )
    _reset_cache()
    try:
        try:
            config_store._write_api_key("provider-failed", "secret-value")
        except RuntimeError as exc:
            assert "secret-value" not in str(exc)
        else:
            raise AssertionError("failed writes must propagate")
        with config_store._api_key_cache_lock:
            assert "provider-failed" not in config_store._api_key_cache
    finally:
        config_store._use_stable_macos_keychain = real_api
        config_store.oskeychain.store = real_store


def test_macos_security_stdin_real_keychain_lifecycle() -> None:
    if sys.platform != "darwin":
        return
    with tempfile.TemporaryDirectory() as directory:
        keychain = Path(directory) / "probe.keychain-db"
        subprocess.run(
            ["/usr/bin/security", "create-keychain", "-p", "probe-pass", str(keychain)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(
                ["/usr/bin/security", "unlock-keychain", "-p", "probe-pass", str(keychain)],
                check=True,
            )
            service = "probe service '; $()"
            account = 'account "quoted" ; $(x) \\ path'
            for value in ("first-value", "second-value"):
                payload = config_store.oskeychain._store_input(service, account, value)
                command = payload.decode("utf-8").rstrip("\n")
                proc = subprocess.run(
                    ["/usr/bin/security", "-q", "-i"],
                    input=(command + f' "{keychain}"\n').encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                assert proc.returncode == 0
            stored = subprocess.run(
                [
                    "/usr/bin/security", "find-generic-password",
                    "-s", service, "-a", account, "-w", str(keychain),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.rstrip("\n")
            assert stored == "second-value"

            python_binary = subprocess.run(
                [sys.executable, "-c", "import sys; print(sys.executable)"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            legacy_account = "legacy-python-owner"
            subprocess.run(
                [
                    "/usr/bin/security", "add-generic-password",
                    "-s", service, "-a", legacy_account,
                    "-w", "legacy-value", "-T", python_binary, "-T", "", str(keychain),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            legacy_payload = config_store.oskeychain._store_input(
                service, legacy_account, "migrated-value"
            )
            legacy_command = legacy_payload.decode("utf-8").rstrip("\n")
            legacy_update = subprocess.run(
                ["/usr/bin/security", "-q", "-i"],
                input=(legacy_command + f' "{keychain}"\n').encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            assert legacy_update.returncode == 0
        finally:
            subprocess.run(
                ["/usr/bin/security", "delete-keychain", str(keychain)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


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
    test_failed_stable_read_is_suppressed_until_credential_mutation()
    test_native_session_provider_kind_never_reads_keychain()
    test_provider_config_reads_are_pure_and_ui_status_is_explicit()
    test_distinct_provider_reads_do_not_block_each_other()
    test_macos_read_uses_stable_security_identity_and_exact_account()
    test_macos_write_uses_stable_security_identity_without_secret_argv()
    test_darwin_stable_write_does_not_depend_on_keyring_backend()
    test_macos_store_input_keeps_secret_out_of_argv_and_quotes_identifiers()
    test_macos_write_failure_propagates_without_cache_update()
    test_macos_security_stdin_real_keychain_lifecycle()
    test_load_state_uses_fingerprint_cache_and_external_invalidates()
    print("OK: config_store keyring compatibility")
