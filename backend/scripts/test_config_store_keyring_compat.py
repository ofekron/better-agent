from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-config-keyring-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402
import keyring  # noqa: E402
import oskeychain  # noqa: E402


def _reset_cache() -> None:
    with config_store._api_key_cache_lock:
        config_store._api_key_cache.clear()
        config_store._api_key_read_locks.clear()
    with config_store._state_cache_lock:
        config_store._state_cache = None
    config_store._credential_status.clear()


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


def test_macos_store_input_keeps_secret_out_of_argv_and_quotes_identifiers() -> None:
    payload = oskeychain._store_input(
        "service '; $()",
        'account "quoted" ; $(x) \\ path',
        "secret-value",
    )
    assert b"secret-value" not in payload
    assert b"7365637265742d76616c7565" in payload
    assert b'\\"quoted\\"' in payload
    assert b'$(x)' in payload
    try:
        oskeychain._store_input("bad\nservice", "account", "value")
    except ValueError:
        pass
    else:
        raise AssertionError("control characters must be rejected")


def test_unavailable_credential_authority_does_not_cache_write() -> None:
    _reset_cache()
    try:
        config_store._write_api_key("provider-failed", "secret-value")
    except RuntimeError as exc:
        assert str(exc) == "provider credential authority is unavailable"
        assert "secret-value" not in str(exc)
    else:
        raise AssertionError("missing credential authority must fail")
    with config_store._api_key_cache_lock:
        assert "provider-failed" not in config_store._api_key_cache


def test_flat_migration_uses_deterministic_broker_owned_credential() -> None:
    real_available = config_store.credential_session_client.available
    real_request = config_store.credential_session_client.request
    calls: list[tuple[str, str]] = []
    config_store.credential_session_client.available = lambda: True
    config_store.credential_session_client.request = (
        lambda op, provider_id, **_kwargs: calls.append((op, provider_id))
        or {"status": "available", "value": "legacy-secret"}
    )
    flat = {
        "mode": "api_key",
        "base_url": "https://api.z.ai/api/anthropic",
        "config_dir": "~/.claude-zai",
    }
    try:
        first = config_store._migrate_flat_to_providers(flat)
        second = config_store._migrate_flat_to_providers(flat)
    finally:
        config_store.credential_session_client.available = real_available
        config_store.credential_session_client.request = real_request
    first_id = first["providers"][0]["id"]
    assert second["providers"][0]["id"] == first_id
    assert calls == [("migrate_flat", first_id), ("migrate_flat", first_id)]
    with config_store._api_key_cache_lock:
        assert config_store._api_key_cache[first_id] == "legacy-secret"


def test_non_darwin_keyring_failures_normalize_to_runtime_error() -> None:
    real_platform = oskeychain.sys.platform
    real_get = keyring.get_password
    real_store = keyring.set_password
    real_delete = keyring.delete_password

    def denied(*args, **kwargs):
        raise keyring.errors.KeyringError("denied")

    oskeychain.sys.platform = "win32"
    keyring.get_password = denied
    keyring.set_password = denied
    keyring.delete_password = denied
    try:
        operations = (
            lambda: oskeychain.get("service", "account"),
            lambda: oskeychain.store("service", "account", "secret"),
            lambda: oskeychain.delete("service", "account"),
        )
        for operation in operations:
            try:
                operation()
            except RuntimeError:
                continue
            raise AssertionError("keyring failure did not normalize to RuntimeError")
    finally:
        oskeychain.sys.platform = real_platform
        keyring.get_password = real_get
        keyring.set_password = real_store
        keyring.delete_password = real_delete


def test_macos_operations_use_stable_security_binary() -> None:
    calls = []
    real_platform = oskeychain.sys.platform
    real_run = oskeychain.subprocess.run

    class Result:
        returncode = 0
        stdout = "stored-value\n"

    oskeychain.sys.platform = "darwin"
    oskeychain.subprocess.run = lambda command, **kwargs: calls.append(
        (command, kwargs)
    ) or Result()
    try:
        oskeychain.store("service", "account", "secret-value")
        assert oskeychain.get("service", "account") == "stored-value\n"
        oskeychain.delete("service", "account")
    finally:
        oskeychain.sys.platform = real_platform
        oskeychain.subprocess.run = real_run

    assert [command[:2] for command, _ in calls] == [
        ["/usr/bin/security", "-q"],
        ["/usr/bin/security", "find-generic-password"],
        ["/usr/bin/security", "delete-generic-password"],
    ]
    assert all("secret-value" not in command for command, _ in calls)


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
                payload = oskeychain._store_input(service, account, value)
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
            legacy_payload = oskeychain._store_input(
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
    test_native_session_provider_kind_never_reads_keychain()
    test_provider_config_reads_are_pure_and_ui_status_is_explicit()
    test_macos_store_input_keeps_secret_out_of_argv_and_quotes_identifiers()
    test_unavailable_credential_authority_does_not_cache_write()
    test_flat_migration_uses_deterministic_broker_owned_credential()
    test_non_darwin_keyring_failures_normalize_to_runtime_error()
    test_macos_operations_use_stable_security_binary()
    test_macos_security_stdin_real_keychain_lifecycle()
    test_load_state_uses_fingerprint_cache_and_external_invalidates()
    print("OK: config_store keyring compatibility")
