"""Coverage for desktop/node_credential_store.py — the headless keyring-backed
provider credential authority.

The module owns a per-state-root directory ``<state_root>/node-credential-authority``
holding a 32-byte seed and an encrypted ``credentials-v1.cfg`` keyring, plus
POSIX/Windows privacy assertions. File/path/seed flows run against a real
tempdir (no filesystem mocking). The encryption backend (``headless_keyring``)
is replaced with an in-memory fake so the test exercises the wrapper's privacy,
seed, and error-mapping logic directly — the keyring internals are an external
dependency, not the unit under test. The fake still writes a real keyring file
so the file-securing paths run against the real filesystem. Defensive error
paths (KeyringError, PasswordDeleteError, write stall, link race,
non-regular-after-open) inject targeted faults.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
for _p in (_HERE, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _install_fake_headless_keyring() -> None:
    """Inject an in-memory Keyring so the broken cffi/Crypto chain in the dev
    venv is never reached. Persists to ``file_path`` so file-securing logic
    still runs against a real file."""
    fake = types.ModuleType("headless_keyring")

    class _FakeKeyring:
        """File-backed in-memory keyring: items persist as JSON at file_path so
        every instance over the same authority shares state, mirroring the real
        keyring's on-disk contract that node_credential_store relies on."""

        def __init__(self, *, key: str, file_path: Path) -> None:
            self._key = key
            self._file_path = Path(file_path)
            self._items: dict[str, str] = {}
            if self._file_path.exists():
                try:
                    self._items = json.loads(self._file_path.read_text())
                except (ValueError, OSError):
                    self._items = {}

        def _persist(self) -> None:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_path.write_text(json.dumps(self._items))
            self._file_path.chmod(0o600)

        def _slot(self, service: str, account: str) -> str:
            return f"{service}\0{account}"

        def get_password(self, service: str, account: str) -> str | None:
            return self._items.get(self._slot(service, account))

        def set_password(self, service: str, account: str, value: str) -> None:
            self._items[self._slot(service, account)] = value
            self._persist()

        def delete_password(self, service: str, account: str) -> None:
            slot = self._slot(service, account)
            if slot not in self._items:
                raise PasswordDeleteError("not set")
            del self._items[slot]

    fake.Keyring = _FakeKeyring
    sys.modules["headless_keyring"] = fake


_install_fake_headless_keyring()

import node_credential_store as ncs  # noqa: E402
from node_credential_store import (  # noqa: E402
    _HeadlessProviderKeychain,
    _assert_directory,
    _assert_private_file,
    _assert_regular_file,
    _authority_dir,
    _load_or_create_seed,
    _read_seed,
    node_provider_credential_store,
)

_SEED = ncs._SEED_BYTES
_SEED_FILE = ncs._SEED_FILE
_KEYRING_FILE = ncs._KEYRING_FILE


def _seed_path(directory: Path) -> Path:
    return directory / _SEED_FILE


def _keyring_path(directory: Path) -> Path:
    return directory / _KEYRING_FILE


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.write_bytes(data)
    path.chmod(mode)


# --- privacy assertions -----------------------------------------------------


def test_assert_regular_file_accepts_regular(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    _assert_regular_file(target)  # no raise


def test_assert_regular_file_rejects_symlink(tmp_path: Path) -> None:
    dest = tmp_path / "real"
    dest.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(dest)
    with pytest.raises(PermissionError):
        _assert_regular_file(link)


def test_assert_regular_file_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        _assert_regular_file(tmp_path)


def test_assert_directory_accepts_directory(tmp_path: Path) -> None:
    target = tmp_path / "sub"
    target.mkdir()
    _assert_directory(target)  # no raise


def test_assert_directory_rejects_file(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(PermissionError):
        _assert_directory(target)


def test_assert_private_file_posix_rejects_group_bits(tmp_path: Path) -> None:
    target = tmp_path / "leaky"
    target.write_text("x")
    target.chmod(0o644)
    with pytest.raises(PermissionError):
        _assert_private_file(target)


def test_assert_private_file_posix_accepts_private(tmp_path: Path) -> None:
    target = tmp_path / "locked"
    _write_private(target, b"x")
    _assert_private_file(target)  # no raise


def test_assert_private_file_windows_accepts_private_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ncs, "windows_path_has_private_acl", lambda _p: True)
    target = tmp_path / "win"
    target.write_text("x")
    _assert_private_file(target)  # no raise


def test_assert_private_file_windows_rejects_open_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ncs, "windows_path_has_private_acl", lambda _p: False)
    target = tmp_path / "win"
    target.write_text("x")
    with pytest.raises(PermissionError):
        _assert_private_file(target)


# --- authority directory ----------------------------------------------------


def test_authority_dir_creates_and_is_idempotent(tmp_path: Path) -> None:
    first = _authority_dir(tmp_path)
    assert first.name == "node-credential-authority"
    assert first.is_dir()
    second = _authority_dir(tmp_path)  # FileExistsError branch
    assert second == first


# --- seed read / load -------------------------------------------------------


def test_read_seed_roundtrip(tmp_path: Path) -> None:
    payload = bytes(range(_SEED))
    _write_private(_seed_path(tmp_path), payload)
    assert _read_seed(_seed_path(tmp_path)) == payload


def test_read_seed_rejects_wrong_length(tmp_path: Path) -> None:
    _write_private(_seed_path(tmp_path), b"too short")
    with pytest.raises(RuntimeError):
        _read_seed(_seed_path(tmp_path))


def test_read_seed_rejects_non_regular_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_private(_seed_path(tmp_path), bytes(_SEED))
    # _assert_private_file passes first; the post-open fstat TOCTOU defence fires.
    monkeypatch.setattr(
        os, "fstat", lambda _fd: types.SimpleNamespace(st_mode=stat.S_IFDIR)
    )
    with pytest.raises(PermissionError):
        _read_seed(_seed_path(tmp_path))


def test_load_seed_creates_and_is_stable(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    seed = _load_or_create_seed(directory)
    assert len(seed) == _SEED
    assert _load_or_create_seed(directory) == seed  # existing-file branch
    assert _seed_path(directory).exists()


def test_load_seed_reads_known_existing(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    known = bytes(_SEED)
    _write_private(_seed_path(directory), known)
    assert _load_or_create_seed(directory) == known


def test_load_seed_handles_link_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _authority_dir(tmp_path)
    real_link = os.link

    def racing_link(src: str, dst: str) -> None:
        real_link(src, dst)  # another writer landed first
        raise FileExistsError(errno.EEXIST, "exists", dst)

    monkeypatch.setattr(os, "link", racing_link)
    seed = _load_or_create_seed(directory)
    assert len(seed) == _SEED
    assert _seed_path(directory).exists()


def test_load_seed_write_stall_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _authority_dir(tmp_path)
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises(OSError):
        _load_or_create_seed(directory)


# --- headless keychain ------------------------------------------------------


def _new_keychain(directory: Path) -> _HeadlessProviderKeychain:
    return _HeadlessProviderKeychain(b"0" * _SEED, _keyring_path(directory))


def test_keychain_get_missing_returns_none(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    assert _new_keychain(directory).get("svc", "acct") is None


def test_keychain_store_get_delete_roundtrip(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)
    chain.store("svc", "acct", "secret")
    assert chain.get("svc", "acct") == "secret"
    chain.delete("svc", "acct")
    assert chain.get("svc", "acct") is None


def test_keychain_validate_existing_secured_file(tmp_path: Path) -> None:
    # pre-existing keyring file exercises _validate_existing's exists branch
    directory = _authority_dir(tmp_path)
    _write_private(_keyring_path(directory), b"headless-keyring")
    chain = _new_keychain(directory)
    chain.store("svc", "acct", "v")
    assert chain.get("svc", "acct") == "v"


def test_keychain_init_secures_existing_file(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)
    chain.store("svc", "acct", "secret")  # creates + secures the keyring file
    assert _new_keychain(directory).get("svc", "acct") == "secret"  # __init__ exists branch


def test_keychain_get_keyring_error_wraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)

    def _raise(*_a: object) -> None:
        raise KeyringError("x")

    monkeypatch.setattr(chain._keyring, "get_password", _raise)
    with pytest.raises(RuntimeError):
        chain.get("svc", "acct")


def test_keychain_store_keyring_error_wraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)

    def _raise(*_a: object) -> None:
        raise KeyringError("x")

    monkeypatch.setattr(chain._keyring, "set_password", _raise)
    with pytest.raises(RuntimeError):
        chain.store("svc", "acct", "secret")


def test_keychain_delete_missing_is_swallowed(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)
    chain.store("svc", "acct", "secret")
    chain.delete("svc", "acct")
    chain.delete("svc", "acct")  # PasswordDeleteError path -> swallowed


def test_keychain_delete_keyring_error_wraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)
    chain.store("svc", "acct", "secret")

    def _raise(*_a: object) -> None:
        raise KeyringError("x")

    monkeypatch.setattr(chain._keyring, "delete_password", _raise)
    with pytest.raises(RuntimeError):
        chain.delete("svc", "acct")


def test_keychain_native_aliases_are_bound() -> None:
    assert _HeadlessProviderKeychain.native_get is _HeadlessProviderKeychain.get
    assert _HeadlessProviderKeychain.native_store is _HeadlessProviderKeychain.store
    assert _HeadlessProviderKeychain.native_delete is _HeadlessProviderKeychain.delete


def test_keychain_native_get_roundtrip(tmp_path: Path) -> None:
    directory = _authority_dir(tmp_path)
    chain = _new_keychain(directory)
    chain.native_store("svc", "acct", "v")
    assert chain.native_get("svc", "acct") == "v"
    chain.native_delete("svc", "acct")
    assert chain.native_get("svc", "acct") is None


# --- public factory ---------------------------------------------------------


def test_node_store_with_explicit_root(tmp_path: Path) -> None:
    store = node_provider_credential_store(tmp_path)
    store.store("claude", "key-1")
    assert store.read("claude") == "key-1"
    again = node_provider_credential_store(tmp_path)  # existing-keyring branch
    assert again.read("claude") == "key-1"
    store.delete("claude")


def test_node_store_defaults_to_ba_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ncs, "ba_home", lambda: tmp_path)
    store = node_provider_credential_store()
    store.store("codex", "key-2")
    assert store.read("codex") == "key-2"
