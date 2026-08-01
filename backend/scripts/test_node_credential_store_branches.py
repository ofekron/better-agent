from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "desktop"))

import pytest  # noqa: E402

# desktop/ is excluded from the backend-only test image; skip cleanly there.
pytest.importorskip("node_credential_store")

from keyring.errors import KeyringError  # noqa: E402

import node_credential_store  # noqa: E402
from node_credential_store import (  # noqa: E402
    _HeadlessProviderKeychain,
    _assert_private_file,
    _read_seed,
    node_provider_credential_store,
)

_AUTHORITY = "node-credential-authority"
_SEED = "seed-v1"
_SEED_BYTES = 32


def _write_seed(authority: Path, data: bytes, mode: int = 0o600) -> Path:
    authority.mkdir(mode=0o700, exist_ok=True)
    seed = authority / _SEED
    seed.write_bytes(data)
    seed.chmod(mode)
    return seed


def test_assert_private_file_rejects_group_or_world_readable_unix(
    tmp_path,
) -> None:
    target = tmp_path / "loose"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o644)

    with pytest.raises(PermissionError, match="permissions are not private"):
        _assert_private_file(target)


def test_assert_private_file_enforces_windows_acl(tmp_path, monkeypatch) -> None:
    target = tmp_path / "guarded"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(node_credential_store.os, "name", "nt")

    # Missing private ACL -> rejected (the Windows privacy gate).
    monkeypatch.setattr(
        node_credential_store, "windows_path_has_private_acl", lambda _p: False
    )
    with pytest.raises(PermissionError, match="does not have a private ACL"):
        _assert_private_file(target)

    # Private ACL present -> accepted without consulting the POSIX mode bits.
    target.chmod(0o644)
    monkeypatch.setattr(
        node_credential_store, "windows_path_has_private_acl", lambda _p: True
    )
    _assert_private_file(target)


def test_read_seed_rejects_invalid_length(tmp_path) -> None:
    authority = tmp_path / _AUTHORITY
    _write_seed(authority, b"x" * (_SEED_BYTES // 2))

    with pytest.raises(RuntimeError, match="seed is invalid"):
        node_provider_credential_store(tmp_path)


def test_read_seed_rejects_non_regular_descriptor(tmp_path, monkeypatch) -> None:
    authority = tmp_path / _AUTHORITY
    seed = _write_seed(authority, b"x" * _SEED_BYTES)

    # The descriptor is opened from a path that passed _assert_private_file,
    # yet fstat reports a non-regular file (TOCTOU defense-in-depth).
    directory_stat = os.stat(tmp_path)
    monkeypatch.setattr(node_credential_store.os, "fstat", lambda _fd: directory_stat)

    with pytest.raises(PermissionError, match="seed must be a regular file"):
        _read_seed(seed)


def test_load_or_create_seed_raises_when_write_stalls(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(node_credential_store.os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="seed write made no progress"):
        node_provider_credential_store(tmp_path)

    # No finalized seed survives a stalled write.
    assert not (tmp_path / _AUTHORITY / _SEED).exists()


class _NoFileKeyring:
    """Stand-in for headless_keyring.Keyring that creates no file on disk."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _RaisingKeyring:
    def get_password(self, _service: str, _account: str) -> str | None:
        raise KeyringError("read boom")

    def set_password(self, _service: str, _account: str, _value: str) -> None:
        raise KeyringError("write boom")

    def delete_password(self, _service: str, _account: str) -> None:
        raise KeyringError("delete boom")


def test_headless_keychain_wraps_keyring_errors_and_allows_missing_file(
    tmp_path, monkeypatch
) -> None:
    # Constructed against a not-yet-existing keyring file: no _secure_created,
    # and every accessor skips _assert_private_file while the file stays absent.
    monkeypatch.setattr(node_credential_store, "Keyring", _NoFileKeyring)
    keychain = _HeadlessProviderKeychain(
        b"\x00" * _SEED_BYTES, tmp_path / "credentials-v1.cfg"
    )
    assert not keychain._file_path.exists()
    monkeypatch.setattr(keychain, "_keyring", _RaisingKeyring())

    with pytest.raises(RuntimeError, match="headless credential read failed"):
        keychain.get("provider-a", "account-a")
    with pytest.raises(RuntimeError, match="headless credential write failed"):
        keychain.store("provider-a", "account-a", "secret")
    with pytest.raises(RuntimeError, match="headless credential delete failed"):
        keychain.delete("provider-a", "account-a")


def test_delete_existing_credential_resecures_keyring(tmp_path) -> None:
    store = node_provider_credential_store(tmp_path)
    store.store("provider-a", "secret")

    # A successful delete reaches _secure_created (re-secures the keyring file).
    store.delete("provider-a")

    assert store.read("provider-a") is None
    keyring = tmp_path / _AUTHORITY / "credentials-v1.cfg"
    assert stat.S_IMODE(keyring.stat().st_mode) == 0o600
