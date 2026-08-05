from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import threading
import time
import uuid
from pathlib import Path

from paths import (
    bc_home,
    make_private_file,
    require_private_directory,
    require_private_file,
)
from portable_lock import try_lock_ex, unlock


_HANDOFF_FD_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_FD"
_HANDOFF_ID_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_ID"
_HANDOFF_TOKEN_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_TOKEN"
_HANDOFF_ENV_KEYS = (_HANDOFF_FD_ENV, _HANDOFF_ID_ENV, _HANDOFF_TOKEN_ENV)
_HELD_LEASES: dict[tuple[str, str], "PrimaryLauncherLease"] = {}
_HELD_LEASES_LOCK = threading.Lock()


class PrimaryLauncherLeaseError(RuntimeError):
    pass


class PrimaryLauncherBusyError(PrimaryLauncherLeaseError):
    pass


def _canonical_home(state_root: Path) -> tuple[Path, str]:
    root = Path(state_root).expanduser()
    if not root.is_absolute():
        raise PrimaryLauncherLeaseError("primary launcher state root must be absolute")
    try:
        require_private_directory(root)
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PrimaryLauncherLeaseError("primary launcher state root is unsafe") from exc
    return resolved, os.path.normcase(str(resolved))


def _is_reparse_point(observed: os.stat_result) -> bool:
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(observed, "st_file_attributes", 0) & attribute)


def _validate_lock_file(path: Path, fd: int) -> None:
    try:
        descriptor = os.fstat(fd)
        observed = path.lstat()
    except OSError as exc:
        raise PrimaryLauncherLeaseError("primary launcher lock identity is unavailable") from exc
    if not stat.S_ISREG(descriptor.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise PrimaryLauncherLeaseError("primary launcher lock must be a regular file")
    if descriptor.st_nlink != 1 or observed.st_nlink != 1:
        raise PrimaryLauncherLeaseError("primary launcher lock must have one link")
    if stat.S_ISLNK(observed.st_mode) or _is_reparse_point(observed):
        raise PrimaryLauncherLeaseError("primary launcher lock must not redirect")
    if not os.path.samestat(descriptor, observed):
        raise PrimaryLauncherLeaseError("primary launcher lock identity changed")
    try:
        require_private_file(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PrimaryLauncherLeaseError("primary launcher lock permissions are unsafe") from exc
    try:
        confirmed = path.lstat()
    except OSError as exc:
        raise PrimaryLauncherLeaseError("primary launcher lock identity is unavailable") from exc
    if not os.path.samestat(descriptor, confirmed) or confirmed.st_nlink != 1:
        raise PrimaryLauncherLeaseError("primary launcher lock identity changed")


def _open_lock_file(path: Path) -> int:
    flags = os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    created = False
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise PrimaryLauncherLeaseError(
                "primary launcher lock could not be opened"
            ) from exc
    except OSError as exc:
        raise PrimaryLauncherLeaseError("primary launcher lock could not be opened") from exc
    try:
        os.set_inheritable(fd, False)
        if os.name == "nt" and created:
            make_private_file(path)
        elif os.name != "nt":
            os.fchmod(fd, 0o600)
        _validate_lock_file(path, fd)
        return fd
    except Exception:
        created_identity = None
        if created:
            try:
                opened = os.fstat(fd)
                if os.path.samestat(opened, path.lstat()):
                    created_identity = opened
            except OSError:
                pass
        os.close(fd)
        if created_identity is not None:
            try:
                if os.path.samestat(created_identity, path.lstat()):
                    path.unlink()
            except OSError:
                pass
        raise


def _read_metadata(fd: int) -> dict[str, object]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64 * 1024)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrimaryLauncherLeaseError("primary launcher lease metadata is invalid") from exc
    if not isinstance(value, dict):
        raise PrimaryLauncherLeaseError("primary launcher lease metadata is invalid")
    return value


def _write_metadata(fd: int, payload: dict[str, object]) -> None:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    offset = 0
    while offset < len(raw):
        written = os.write(fd, raw[offset:])
        if written <= 0:
            raise PrimaryLauncherLeaseError("primary launcher metadata write failed")
        offset += written
    os.fsync(fd)


class PrimaryLauncherLease:
    role = "primary"
    lock_name = "primary-launcher.lock"
    handoff_env_keys = _HANDOFF_ENV_KEYS

    def __init__(
        self,
        *,
        fd: int,
        path: Path,
        canonical_home: str,
        lease_id: str,
    ) -> None:
        self._fd: int | None = fd
        self._path = path
        self._canonical_home = canonical_home
        self._lease_id = lease_id
        self._handoff_token: str | None = None

    @classmethod
    def acquire(
        cls,
        state_root: Path | None = None,
        *,
        checkout: Path | None = None,
    ) -> "PrimaryLauncherLease":
        resolved, canonical = _canonical_home(state_root or bc_home())
        path = resolved / cls.lock_name
        checkout_path = Path(checkout or Path.cwd()).resolve(strict=False)
        held_key = (cls.role, canonical)
        with _HELD_LEASES_LOCK:
            if held_key in _HELD_LEASES:
                raise PrimaryLauncherBusyError(
                    f"another {cls.role} launcher already owns {canonical}"
                )
            fd = _open_lock_file(path)
            acquired = False
            try:
                acquired = try_lock_ex(fd)
                if not acquired:
                    raise PrimaryLauncherBusyError(
                        f"another {cls.role} launcher already owns {canonical}"
                    )
                lease_id = uuid.uuid4().hex
                _write_metadata(
                    fd,
                    {
                        "checkout": os.path.normcase(str(checkout_path)),
                        "home": canonical,
                        "host": socket.gethostname(),
                        "lease_id": lease_id,
                        "pid": os.getpid(),
                        "started_at": time.time(),
                        "version": 1,
                    },
                )
                lease = cls(
                    fd=fd,
                    path=path,
                    canonical_home=canonical,
                    lease_id=lease_id,
                )
                _HELD_LEASES[held_key] = lease
                return lease
            except Exception:
                if acquired:
                    try:
                        unlock(fd)
                    except OSError:
                        pass
                os.close(fd)
                raise

    @classmethod
    def adopt(
        cls,
        fd: int,
        state_root: Path,
        *,
        lease_id: str,
        handoff_token: str,
    ) -> "PrimaryLauncherLease":
        if os.name == "nt":
            raise PrimaryLauncherLeaseError(
                "primary launcher descriptor adoption is POSIX-only"
            )
        resolved, canonical = _canonical_home(state_root)
        path = resolved / cls.lock_name
        held_key = (cls.role, canonical)
        with _HELD_LEASES_LOCK:
            if held_key in _HELD_LEASES:
                raise PrimaryLauncherBusyError(
                    f"{cls.role} launcher lease already adopted for {canonical}"
                )
            try:
                expected_offset = int(handoff_token, 16)
                if expected_offset <= 0 or os.lseek(fd, 0, os.SEEK_CUR) != expected_offset:
                    raise PrimaryLauncherLeaseError(
                        "primary launcher descriptor handoff is stale"
                    )
                os.set_inheritable(fd, False)
                _validate_lock_file(path, fd)
                if not try_lock_ex(fd):
                    raise PrimaryLauncherLeaseError(
                        "primary launcher descriptor does not own the lease"
                    )
                metadata = _read_metadata(fd)
            except Exception:
                os.close(fd)
                raise
            if metadata.get("lease_id") != lease_id or metadata.get("home") != canonical:
                os.close(fd)
                raise PrimaryLauncherLeaseError(
                    "primary launcher descriptor metadata does not match"
                )
            lease = cls(
                fd=fd,
                path=path,
                canonical_home=canonical,
                lease_id=lease_id,
            )
            _HELD_LEASES[held_key] = lease
            return lease

    @property
    def canonical_home(self) -> str:
        return self._canonical_home

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def fileno(self) -> int:
        if self._fd is None:
            raise PrimaryLauncherLeaseError("primary launcher lease is released")
        return self._fd

    def assert_owner(self, state_root: Path | None = None) -> None:
        _, canonical = _canonical_home(state_root or bc_home())
        if canonical != self._canonical_home or self._fd is None:
            raise PrimaryLauncherLeaseError("primary launcher lease does not own this home")
        with _HELD_LEASES_LOCK:
            if _HELD_LEASES.get((type(self).role, canonical)) is not self:
                raise PrimaryLauncherLeaseError("primary launcher lease is not active")
        _validate_lock_file(self._path, self._fd)

    def prepare_handoff(self) -> str:
        fd = self.fileno
        token = f"{secrets.randbits(30) + 1:x}"
        os.lseek(fd, int(token, 16), os.SEEK_SET)
        self._handoff_token = token
        return token

    def prepare_handoff_environment(self) -> dict[str, str]:
        if os.name == "nt":
            raise PrimaryLauncherLeaseError(
                "primary launcher descriptor handoff is POSIX-only"
            )
        token = self.prepare_handoff()
        os.set_inheritable(self.fileno, True)
        fd_key, id_key, token_key = type(self).handoff_env_keys
        return {fd_key: str(self.fileno), id_key: self.lease_id, token_key: token}

    @classmethod
    def adopt_from_environment(
        cls,
        state_root: Path,
    ) -> "PrimaryLauncherLease | None":
        values = [os.environ.get(key) for key in cls.handoff_env_keys]
        if not any(values):
            return None
        if not all(values):
            for key in cls.handoff_env_keys:
                os.environ.pop(key, None)
            raise PrimaryLauncherLeaseError(
                "primary launcher handoff environment is incomplete"
            )
        try:
            fd = int(values[0])
            if fd < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            for key in cls.handoff_env_keys:
                os.environ.pop(key, None)
            raise PrimaryLauncherLeaseError(
                "primary launcher handoff descriptor is invalid"
            ) from exc
        try:
            return cls.adopt(
                fd,
                state_root,
                lease_id=str(values[1]),
                handoff_token=str(values[2]),
            )
        finally:
            for key in cls.handoff_env_keys:
                os.environ.pop(key, None)

    def detach_after_transfer(self) -> None:
        if self._handoff_token is None:
            raise PrimaryLauncherLeaseError(
                "primary launcher handoff was not prepared"
            )
        fd = self._take_fd()
        os.close(fd)

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._take_fd()
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            unlock(fd)
        finally:
            os.close(fd)

    def _take_fd(self) -> int:
        if self._fd is None:
            raise PrimaryLauncherLeaseError("primary launcher lease is released")
        fd = self._fd
        self._fd = None
        held_key = (type(self).role, self._canonical_home)
        with _HELD_LEASES_LOCK:
            if _HELD_LEASES.get(held_key) is self:
                del _HELD_LEASES[held_key]
        return fd

    def __enter__(self) -> "PrimaryLauncherLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
