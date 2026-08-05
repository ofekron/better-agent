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

from paths import bc_home, require_private_directory, require_private_file
from portable_lock import try_lock_ex, unlock


_LOCK_NAME = "primary-launcher.lock"
_HANDOFF_FD_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_FD"
_HANDOFF_ID_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_ID"
_HANDOFF_TOKEN_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_HANDOFF_TOKEN"
_HANDOFF_ENV_KEYS = (_HANDOFF_FD_ENV, _HANDOFF_ID_ENV, _HANDOFF_TOKEN_ENV)
_HELD_LEASES: dict[str, "PrimaryLauncherLease"] = {}
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
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PrimaryLauncherLeaseError("primary launcher lock could not be opened") from exc
    try:
        os.set_inheritable(fd, False)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        _validate_lock_file(path, fd)
        return fd
    except Exception:
        os.close(fd)
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
        path = resolved / _LOCK_NAME
        checkout_path = Path(checkout or Path.cwd()).resolve(strict=False)
        with _HELD_LEASES_LOCK:
            if canonical in _HELD_LEASES:
                raise PrimaryLauncherBusyError(
                    f"another primary launcher already owns {canonical}"
                )
            fd = _open_lock_file(path)
            acquired = False
            try:
                acquired = try_lock_ex(fd)
                if not acquired:
                    raise PrimaryLauncherBusyError(
                        f"another primary launcher already owns {canonical}"
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
                _HELD_LEASES[canonical] = lease
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
        path = resolved / _LOCK_NAME
        with _HELD_LEASES_LOCK:
            if canonical in _HELD_LEASES:
                raise PrimaryLauncherBusyError(
                    f"primary launcher lease already adopted for {canonical}"
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
            _HELD_LEASES[canonical] = lease
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
            if _HELD_LEASES.get(canonical) is not self:
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
        return {
            _HANDOFF_FD_ENV: str(self.fileno),
            _HANDOFF_ID_ENV: self.lease_id,
            _HANDOFF_TOKEN_ENV: token,
        }

    @classmethod
    def adopt_from_environment(
        cls,
        state_root: Path,
    ) -> "PrimaryLauncherLease | None":
        values = [os.environ.get(key) for key in _HANDOFF_ENV_KEYS]
        if not any(values):
            return None
        if not all(values):
            for key in _HANDOFF_ENV_KEYS:
                os.environ.pop(key, None)
            raise PrimaryLauncherLeaseError(
                "primary launcher handoff environment is incomplete"
            )
        try:
            fd = int(values[0])
            if fd < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            for key in _HANDOFF_ENV_KEYS:
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
            for key in _HANDOFF_ENV_KEYS:
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
        with _HELD_LEASES_LOCK:
            if _HELD_LEASES.get(self._canonical_home) is self:
                del _HELD_LEASES[self._canonical_home]
        return fd

    def __enter__(self) -> "PrimaryLauncherLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
