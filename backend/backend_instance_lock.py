from __future__ import annotations

import atexit
import ctypes
import json
import logging
import os
import re
import socket
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from paths import bc_home, make_private_file, require_private_directory, require_private_file
from portable_lock import try_lock_ex, unlock
from primary_launcher_lease import PrimaryLauncherLease

_LOCK_TRANSFER_ENV = "BETTER_AGENT_BACKEND_INSTANCE_LOCK"
_ACK_TRANSFER_ENV = "BETTER_AGENT_BACKEND_INSTANCE_ACK"
_RESERVATION_ID_ENV = "BETTER_AGENT_BACKEND_RESERVATION_ID"
_LAUNCHER_LEASE_ID_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_LEASE_ID"
_TRANSFER_ENV_KEYS = (
    _LOCK_TRANSFER_ENV,
    _ACK_TRANSFER_ENV,
    _RESERVATION_ID_ENV,
    _LAUNCHER_LEASE_ID_ENV,
)
_HEX_32_RE = re.compile(r"[0-9a-f]{32}")
_LOCK_FD: int | None = None
_LOCK_PATH: Path | None = None
_SPAWN_INHERITANCE_LOCK = threading.Lock()
_LOCK_ACQUIRE_RETRY_SECONDS = 15.0
_LOCK_ACQUIRE_POLL_INTERVAL = 0.25
_log = logging.getLogger("backend_instance_lock")


class BackendInstanceReservationError(RuntimeError):
    pass


class BackendInstanceBusyError(BackendInstanceReservationError):
    pass


def backend_instance_transfer_env_keys() -> tuple[str, ...]:
    return _TRANSFER_ENV_KEYS


def _canonical_home(root: Path | None = None) -> tuple[Path, str]:
    candidate = Path(root or bc_home()).expanduser()
    if not candidate.is_absolute():
        raise BackendInstanceReservationError("backend state home must be absolute")
    try:
        require_private_directory(candidate)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BackendInstanceReservationError("backend state home is unsafe") from exc
    return resolved, os.path.normcase(str(resolved))


def _lock_identity(fd: int) -> str:
    observed = os.fstat(fd)
    return f"{observed.st_dev:x}:{observed.st_ino:x}"


def _validate_lock_file(path: Path, fd: int) -> None:
    try:
        opened = os.fstat(fd)
        observed = path.lstat()
    except OSError as exc:
        raise BackendInstanceReservationError("backend lock identity is unavailable") from exc
    reparse = bool(
        getattr(observed, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or opened.st_nlink != 1
        or observed.st_nlink != 1
        or stat.S_ISLNK(observed.st_mode)
        or reparse
        or not os.path.samestat(opened, observed)
    ):
        raise BackendInstanceReservationError("backend lock identity is unsafe")
    try:
        require_private_file(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BackendInstanceReservationError("backend lock permissions are unsafe") from exc
    confirmed = path.lstat()
    if confirmed.st_nlink != 1 or not os.path.samestat(opened, confirmed):
        raise BackendInstanceReservationError("backend lock identity changed")


def _create_private_lock_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    make_private_file(path)


def _open_windows_exclusive(path: Path) -> int:
    import msvcrt

    _create_private_lock_file(path)
    require_private_file(path)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    raw_handle = create_file(
        str(path),
        0x80000000 | 0x40000000,
        0,
        None,
        3,
        0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if raw_handle == invalid:
        error = ctypes.get_last_error()
        if error in {5, 32, 33}:
            raise BackendInstanceBusyError(
                f"another Better Agent backend is already using {path.parent}"
            )
        raise BackendInstanceReservationError(
            f"backend lock could not be opened (Windows error {error})"
        )
    try:
        fd = msvcrt.open_osfhandle(int(raw_handle), os.O_RDWR)
    except Exception:
        kernel32.CloseHandle(raw_handle)
        raise
    os.set_handle_inheritable(msvcrt.get_osfhandle(fd), False)
    return fd


def _open_posix_exclusive(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.set_inheritable(fd, False)
        os.fchmod(fd, 0o600)
        _validate_lock_file(path, fd)
        if not try_lock_ex(fd):
            raise BackendInstanceBusyError(
                f"another Better Agent backend is already using {path.parent}"
            )
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_exclusive(path: Path) -> int:
    fd = _open_windows_exclusive(path) if os.name == "nt" else _open_posix_exclusive(path)
    try:
        _validate_lock_file(path, fd)
        return fd
    except Exception:
        if os.name != "nt":
            try:
                unlock(fd)
            except OSError:
                pass
        os.close(fd)
        raise


def _read_metadata(fd: int) -> dict[str, object]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        value = json.loads(os.read(fd, 64 * 1024).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackendInstanceReservationError("backend reservation metadata is invalid") from exc
    if not isinstance(value, dict):
        raise BackendInstanceReservationError("backend reservation metadata is invalid")
    return value


def _write_metadata(fd: int, payload: dict[str, object]) -> None:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    offset = 0
    while offset < len(raw):
        written = os.write(fd, raw[offset:])
        if written <= 0:
            raise BackendInstanceReservationError("backend reservation metadata write failed")
        offset += written
    os.fsync(fd)


def _transfer_value(fd: int) -> int:
    if os.name != "nt":
        return fd
    import msvcrt
    return int(msvcrt.get_osfhandle(fd))


def _set_transfer_inheritable(fd: int, inheritable: bool) -> None:
    if os.name == "nt":
        os.set_handle_inheritable(_transfer_value(fd), inheritable)
    else:
        os.set_inheritable(fd, inheritable)


def _duplicate_windows_handle(fd: int) -> int:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.DuplicateHandle.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    )
    kernel32.DuplicateHandle.restype = ctypes.c_int
    current = kernel32.GetCurrentProcess()
    duplicate = ctypes.c_void_p()
    if not kernel32.DuplicateHandle(
        current,
        ctypes.c_void_p(msvcrt.get_osfhandle(fd)),
        current,
        ctypes.byref(duplicate),
        0,
        False,
        0x00000002,
    ):
        raise BackendInstanceReservationError(
            f"backend transfer handle duplication failed (Windows error {ctypes.get_last_error()})"
        )
    return int(duplicate.value)


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def _merge_spawn_kwargs(
    existing: dict[str, object],
    transfer_values: tuple[int, ...],
) -> dict[str, object]:
    merged = dict(existing)
    if os.name != "nt":
        inherited = tuple(int(value) for value in merged.pop("pass_fds", ()))
        merged["pass_fds"] = tuple(dict.fromkeys((*inherited, *transfer_values)))
        return merged
    if "pass_fds" in merged:
        raise BackendInstanceReservationError("pass_fds is not valid on Windows")
    startupinfo = merged.get("startupinfo") or subprocess.STARTUPINFO()
    attributes = dict(getattr(startupinfo, "lpAttributeList", None) or {})
    handles = list(attributes.get("handle_list", ()))
    handles.extend(transfer_values)
    attributes["handle_list"] = list(dict.fromkeys(handles))
    startupinfo.lpAttributeList = attributes
    merged["startupinfo"] = startupinfo
    merged["close_fds"] = True
    return merged


class PrimaryBackendInstanceReservation:
    def __init__(
        self,
        *,
        fd: int,
        path: Path,
        canonical_home: str,
        lease_id: str,
        reservation_id: str,
        ack_read_fd: int,
        ack_write_fd: int,
    ) -> None:
        self._fd: int | None = fd
        self._path = path
        self._canonical_home = canonical_home
        self._lease_id = lease_id
        self._reservation_id = reservation_id
        self._ack_read_fd: int | None = ack_read_fd
        self._ack_write_fd: int | None = ack_write_fd
        self._acknowledged = False
        self._windows_transfer_handles: tuple[int, int] | None = None

    @property
    def canonical_home(self) -> str:
        return self._canonical_home

    @property
    def reservation_id(self) -> str:
        return self._reservation_id

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def lock_identity(self) -> str:
        if self._fd is None:
            raise BackendInstanceReservationError("backend reservation is released")
        return _lock_identity(self._fd)

    def assert_owner(self, launcher_lease: PrimaryLauncherLease) -> None:
        if type(launcher_lease) is not PrimaryLauncherLease:
            raise BackendInstanceReservationError(
                "backend reservation requires a concrete launcher lease"
            )
        launcher_lease.assert_owner(Path(self._canonical_home))
        if (
            self._fd is None
            or launcher_lease.lease_id != self._lease_id
            or launcher_lease.canonical_home != self._canonical_home
        ):
            raise BackendInstanceReservationError(
                "backend reservation does not belong to launcher lease"
            )
        _validate_lock_file(self._path, self._fd)

    def child_env(self) -> dict[str, str]:
        if self._fd is None or self._ack_write_fd is None:
            raise BackendInstanceReservationError("backend reservation is not transferable")
        if os.name == "nt":
            if self._windows_transfer_handles is None:
                lock_handle = _duplicate_windows_handle(self._fd)
                try:
                    ack_handle = _duplicate_windows_handle(self._ack_write_fd)
                except Exception:
                    _close_windows_handle(lock_handle)
                    raise
                self._windows_transfer_handles = (lock_handle, ack_handle)
            lock_value, ack_value = self._windows_transfer_handles
        else:
            lock_value, ack_value = self._fd, self._ack_write_fd
        return {
            _LOCK_TRANSFER_ENV: str(lock_value),
            _ACK_TRANSFER_ENV: str(ack_value),
            _RESERVATION_ID_ENV: self._reservation_id,
            _LAUNCHER_LEASE_ID_ENV: self._lease_id,
        }

    @contextmanager
    def child_spawn_kwargs(
        self,
        existing: dict[str, object] | None = None,
    ) -> Iterator[dict[str, object]]:
        if self._fd is None or self._ack_write_fd is None:
            raise BackendInstanceReservationError("backend reservation is not transferable")
        completed = False
        with _SPAWN_INHERITANCE_LOCK:
            if os.name != "nt":
                yield _merge_spawn_kwargs(
                    existing or {}, (self._fd, self._ack_write_fd)
                )
                completed = True
            else:
                self.child_env()
                handles = self._windows_transfer_handles
                if handles is None:
                    raise BackendInstanceReservationError(
                        "backend transfer handles are unavailable"
                    )
                try:
                    for handle in handles:
                        os.set_handle_inheritable(handle, True)
                    yield _merge_spawn_kwargs(existing or {}, handles)
                    completed = True
                finally:
                    for handle in handles:
                        try:
                            os.set_handle_inheritable(handle, False)
                        except OSError:
                            pass
                        try:
                            _close_windows_handle(handle)
                        except OSError:
                            pass
                    self._windows_transfer_handles = None
        if completed:
            os.close(self._ack_write_fd)
            self._ack_write_fd = None

    def await_adoption(
        self,
        process: subprocess.Popen[object],
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        if self._ack_write_fd is not None:
            raise BackendInstanceReservationError("backend child was not spawned")
        if self._ack_read_fd is None:
            raise BackendInstanceReservationError("backend admission acknowledgement is unavailable")
        result: list[bytes | BaseException] = []

        def read_ack() -> None:
            try:
                result.append(os.read(self._ack_read_fd, 128))
            except BaseException as exc:
                result.append(exc)

        reader = threading.Thread(target=read_ack, name="backend-admission-ack")
        reader.start()
        reader.join(timeout_seconds)
        if reader.is_alive():
            process.kill()
            process.wait(timeout=5)
            os.close(self._ack_read_fd)
            self._ack_read_fd = None
            reader.join(timeout=5)
            raise BackendInstanceReservationError(
                "backend child did not acknowledge admission"
            )
        os.close(self._ack_read_fd)
        self._ack_read_fd = None
        expected = f"{self._reservation_id}\n".encode()
        if result != [expected]:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise BackendInstanceReservationError(
                "backend child admission acknowledgement is invalid"
            )
        self._acknowledged = True

    def detach_after_transfer(self) -> None:
        if not self._acknowledged:
            raise BackendInstanceReservationError(
                "backend admission must be acknowledged before transfer"
            )
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def release(self) -> None:
        if self._windows_transfer_handles is not None:
            for handle in self._windows_transfer_handles:
                try:
                    _close_windows_handle(handle)
                except OSError:
                    pass
            self._windows_transfer_handles = None
        for name in ("_ack_read_fd", "_ack_write_fd"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if os.name != "nt":
                unlock(fd)
        finally:
            os.close(fd)


def reserve_primary_backend_instance_lock(
    launcher_lease: PrimaryLauncherLease,
) -> PrimaryBackendInstanceReservation:
    if type(launcher_lease) is not PrimaryLauncherLease:
        raise BackendInstanceReservationError(
            "primary backend reservation requires a concrete launcher lease"
        )
    root, canonical = _canonical_home(Path(launcher_lease.canonical_home))
    launcher_lease.assert_owner(root)
    path = root / "backend.lock"
    fd = _open_exclusive(path)
    ack_read_fd, ack_write_fd = os.pipe()
    for pipe_fd in (ack_read_fd, ack_write_fd):
        _set_transfer_inheritable(pipe_fd, False)
    reservation_id = uuid.uuid4().hex
    try:
        _write_metadata(
            fd,
            {
                "backend_lock_identity": _lock_identity(fd),
                "home": canonical,
                "host": socket.gethostname(),
                "launcher_lease_id": launcher_lease.lease_id,
                "pid": os.getpid(),
                "reservation_id": reservation_id,
                "reserved_at": time.time(),
                "version": 2,
            },
        )
        return PrimaryBackendInstanceReservation(
            fd=fd,
            path=path,
            canonical_home=canonical,
            lease_id=launcher_lease.lease_id,
            reservation_id=reservation_id,
            ack_read_fd=ack_read_fd,
            ack_write_fd=ack_write_fd,
        )
    except Exception:
        os.close(ack_read_fd)
        os.close(ack_write_fd)
        if os.name != "nt":
            unlock(fd)
        os.close(fd)
        raise


def _adopted_fd(raw_value: int) -> int:
    if os.name != "nt":
        os.set_inheritable(raw_value, False)
        return raw_value
    import msvcrt
    os.set_handle_inheritable(raw_value, False)
    return msvcrt.open_osfhandle(raw_value, os.O_RDWR)


def adopt_primary_backend_instance_lock() -> None:
    global _LOCK_FD, _LOCK_PATH
    if _LOCK_FD is not None:
        primary_path = _canonical_home()[0] / "backend.lock"
        if _LOCK_PATH == primary_path:
            return
        raise BackendInstanceReservationError(
            f"backend instance lock already held for {_LOCK_PATH}; cannot also use {primary_path}"
        )
    values = {key: os.environ.get(key, "") for key in _TRANSFER_ENV_KEYS}
    if any(not value for value in values.values()):
        raise BackendInstanceReservationError(
            "backend reservation environment is missing"
        )
    try:
        raw_lock = int(values[_LOCK_TRANSFER_ENV])
        raw_ack = int(values[_ACK_TRANSFER_ENV])
    except ValueError as exc:
        raise BackendInstanceReservationError(
            "backend reservation descriptors are invalid"
        ) from exc
    reservation_id = values[_RESERVATION_ID_ENV]
    lease_id = values[_LAUNCHER_LEASE_ID_ENV]
    if _HEX_32_RE.fullmatch(reservation_id) is None or _HEX_32_RE.fullmatch(lease_id) is None:
        raise BackendInstanceReservationError("backend reservation identity is invalid")
    fd = _adopted_fd(raw_lock)
    ack_fd = _adopted_fd(raw_ack)
    for key in _TRANSFER_ENV_KEYS:
        os.environ.pop(key, None)
    path = _canonical_home()[0] / "backend.lock"
    try:
        _validate_lock_file(path, fd)
        metadata = _read_metadata(fd)
        identity = _lock_identity(fd)
        if (
            metadata.get("version") != 2
            or metadata.get("home") != os.path.normcase(str(path.parent))
            or metadata.get("launcher_lease_id") != lease_id
            or metadata.get("reservation_id") != reservation_id
            or metadata.get("backend_lock_identity") != identity
        ):
            raise BackendInstanceReservationError(
                "backend reservation identity does not match"
            )
        from backend_launch_authority import consume_primary_backend_launch_authority

        consume_primary_backend_launch_authority(
            launcher_lease_id=lease_id,
            backend_reservation_id=reservation_id,
            backend_lock_identity=identity,
        )
        _write_metadata(
            fd,
            {
                **metadata,
                "admitted_at": time.time(),
                "pid": os.getpid(),
            },
        )
        os.write(ack_fd, f"{reservation_id}\n".encode())
        os.close(ack_fd)
        _LOCK_FD = fd
        _LOCK_PATH = path
    except Exception:
        os.close(ack_fd)
        os.close(fd)
        raise


def _acquire_node_backend_instance_lock() -> None:
    global _LOCK_FD, _LOCK_PATH
    path = bc_home() / "node-backend.lock"
    if _LOCK_FD is not None:
        if _LOCK_PATH == path:
            return
        raise RuntimeError(
            f"backend instance lock already held for {_LOCK_PATH}; cannot also use {path}"
        )
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + _LOCK_ACQUIRE_RETRY_SECONDS
    try:
        while not try_lock_ex(fd):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"another Better Agent backend is already using {bc_home()}")
            time.sleep(min(_LOCK_ACQUIRE_POLL_INTERVAL, remaining))
        _write_metadata(fd, {"home": str(bc_home()), "host": socket.gethostname(), "pid": os.getpid(), "role": "node"})
        _LOCK_FD = fd
        _LOCK_PATH = path
    except Exception:
        os.close(fd)
        raise


def acquire_backend_instance_lock(*, role: str = "primary") -> None:
    if role == "primary":
        adopt_primary_backend_instance_lock()
        return
    if role != "node":
        raise ValueError(f"unsupported backend role: {role}")
    _acquire_node_backend_instance_lock()


def release_backend_instance_lock() -> None:
    global _LOCK_FD, _LOCK_PATH
    if _LOCK_FD is None:
        return
    fd = _LOCK_FD
    _LOCK_FD = None
    _LOCK_PATH = None
    try:
        if os.name != "nt":
            unlock(fd)
    finally:
        os.close(fd)


def _read_lock_holder(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


atexit.register(release_backend_instance_lock)
