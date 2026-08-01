from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from keyring.errors import KeyringError, PasswordDeleteError

from headless_keyring import Keyring
from paths import (
    ba_home,
    make_private_directory,
    make_private_file,
    windows_path_has_private_acl,
)
from provider_credentials import ProviderCredentialStore


_AUTHORITY_DIR = "node-credential-authority"
_SEED_FILE = "seed-v1"
_KEYRING_FILE = "credentials-v1.cfg"
_SEED_BYTES = 32


def _assert_regular_file(path: Path) -> None:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise PermissionError(f"{path.name} must be a regular file")


def _assert_directory(path: Path) -> None:
    observed = path.lstat()
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise PermissionError(f"{path.name} must be a directory")


def _assert_private_file(path: Path) -> None:
    _assert_regular_file(path)
    if os.name == "nt":
        if not windows_path_has_private_acl(path):
            raise PermissionError(f"{path.name} does not have a private ACL")
        return
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PermissionError(f"{path.name} permissions are not private")


def _authority_dir(state_root: Path) -> Path:
    directory = state_root / _AUTHORITY_DIR
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _assert_directory(directory)
    make_private_directory(directory)
    _assert_directory(directory)
    return directory


def _read_seed(path: Path) -> bytes:
    _assert_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise PermissionError("node credential seed must be a regular file")
        value = os.read(descriptor, _SEED_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(value) != _SEED_BYTES:
        raise RuntimeError("node credential seed is invalid")
    return value


def _load_or_create_seed(directory: Path) -> bytes:
    path = directory / _SEED_FILE
    if path.exists() or path.is_symlink():
        return _read_seed(path)
    temporary = directory / f".{_SEED_FILE}.{secrets.token_hex(8)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    seed = secrets.token_bytes(_SEED_BYTES)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(seed)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("node credential seed write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        make_private_file(temporary)
        _assert_private_file(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        # Directory fsync is POSIX-only; Windows skips it. pathlib binds Path
        # flavour to os.name, so the Windows branch cannot run on a POSIX host.
        if os.name != "nt":  # pragma: no branch
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return _read_seed(path)


class _HeadlessProviderKeychain:
    def __init__(self, seed: bytes, file_path: Path) -> None:
        self._file_path = file_path
        self._keyring = Keyring(key=seed.hex(), file_path=file_path)
        if self._file_path.exists():
            self._secure_created()

    def _validate_existing(self) -> None:
        if self._file_path.exists() or self._file_path.is_symlink():
            _assert_private_file(self._file_path)

    def _secure_created(self) -> None:
        _assert_regular_file(self._file_path)
        make_private_file(self._file_path)
        _assert_private_file(self._file_path)

    def get(self, service: str, account: str) -> str | None:
        self._validate_existing()
        try:
            return self._keyring.get_password(service, account)
        except KeyringError:
            raise RuntimeError("headless credential read failed") from None

    def store(self, service: str, account: str, value: str) -> None:
        self._validate_existing()
        try:
            self._keyring.set_password(service, account, value)
        except KeyringError:
            raise RuntimeError("headless credential write failed") from None
        self._secure_created()

    def delete(self, service: str, account: str) -> None:
        self._validate_existing()
        try:
            self._keyring.delete_password(service, account)
        except PasswordDeleteError:
            return
        except KeyringError:
            raise RuntimeError("headless credential delete failed") from None
        self._secure_created()

    native_get = get
    native_store = store
    native_delete = delete


def node_provider_credential_store(
    state_root: Path | None = None,
) -> ProviderCredentialStore:
    root = ba_home() if state_root is None else state_root
    directory = _authority_dir(root)
    seed = _load_or_create_seed(directory)
    keyring_path = directory / _KEYRING_FILE
    if keyring_path.exists() or keyring_path.is_symlink():
        _assert_private_file(keyring_path)
    return ProviderCredentialStore(
        _HeadlessProviderKeychain(seed, keyring_path),
    )
