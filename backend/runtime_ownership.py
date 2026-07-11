from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO

from paths import ba_home


_LOCK_FILE_NAME = "session-root.writer.lock"
_DEFAULT_BLOCK_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.05
_LOCK_HANDLE: IO[str] | None = None
_LOCK_PATH: Path | None = None
_LOCK_MUTEX = threading.Lock()
_CURRENT_PROCESS_WRITER = False


class RuntimeOwnershipError(RuntimeError):
    pass


def runtime_dir() -> Path:
    return ba_home() / "runtime"


def ensure_runtime_dir() -> Path:
    path = runtime_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def writer_lock_path() -> Path:
    return runtime_dir() / _LOCK_FILE_NAME


def _try_lock_file(handle: IO[str]) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _lock_file(handle: IO[str], *, blocking: bool, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        if _try_lock_file(handle):
            return True
        if not blocking or time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_RETRY_SECONDS)


def _unlock_file(handle: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_lock_owner(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _runtime_lock_mismatch_message(held_path: Path, current_path: Path) -> str:
    return (
        "runtime writer lock was acquired for a different state root: "
        f"held={held_path} current={current_path}"
    )


def acquire_runtime_writer_lock(
    *,
    blocking: bool = False,
    timeout_seconds: float = _DEFAULT_BLOCK_TIMEOUT_SECONDS,
) -> bool:
    global _LOCK_HANDLE, _LOCK_PATH
    with _LOCK_MUTEX:
        ensure_runtime_dir()
        current_path = writer_lock_path()
        if _LOCK_HANDLE is not None:
            if _LOCK_PATH == current_path:
                return True
            raise RuntimeOwnershipError(
                _runtime_lock_mismatch_message(_LOCK_PATH or current_path, current_path)
            )
        handle = current_path.open("a+", encoding="utf-8")
        if not _lock_file(handle, blocking=blocking, timeout_seconds=timeout_seconds):
            owner = _read_lock_owner(current_path)
            handle.close()
            if blocking:
                detail = f" pid={owner}" if owner else ""
                raise RuntimeOwnershipError(
                    f"runtime writer lock is held by another process at {current_path}{detail}"
                )
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        _LOCK_HANDLE = handle
        _LOCK_PATH = current_path
        return True


def register_current_process_writer() -> None:
    global _CURRENT_PROCESS_WRITER
    _CURRENT_PROCESS_WRITER = True
    ensure_current_process_writer_lock()


def ensure_current_process_writer_lock() -> None:
    if not _CURRENT_PROCESS_WRITER:
        raise RuntimeOwnershipError("current process is not the runtime writer")
    with _LOCK_MUTEX:
        current_path = writer_lock_path()
        if _LOCK_HANDLE is not None and _LOCK_PATH == current_path:
            return
    release_runtime_writer_lock()
    acquire_runtime_writer_lock(blocking=True)


def unregister_current_process_writer() -> None:
    global _CURRENT_PROCESS_WRITER
    _CURRENT_PROCESS_WRITER = False
    release_runtime_writer_lock()


def release_runtime_writer_lock() -> None:
    global _LOCK_HANDLE, _LOCK_PATH
    with _LOCK_MUTEX:
        handle = _LOCK_HANDLE
        _LOCK_HANDLE = None
        _LOCK_PATH = None
        if handle is None:
            return
        try:
            _unlock_file(handle)
        finally:
            handle.close()


def holds_runtime_writer_lock() -> bool:
    with _LOCK_MUTEX:
        if _LOCK_HANDLE is None:
            return False
        current_path = writer_lock_path()
        if _LOCK_PATH != current_path:
            raise RuntimeOwnershipError(
                _runtime_lock_mismatch_message(_LOCK_PATH or current_path, current_path)
            )
        return True


def assert_runtime_writer() -> None:
    if _CURRENT_PROCESS_WRITER:
        ensure_current_process_writer_lock()
        return
    if holds_runtime_writer_lock():
        return
    raise RuntimeOwnershipError(
        "session-root writes require the better-agent-runtime writer lock"
    )


class runtime_writer:
    def __init__(
        self,
        *,
        blocking: bool = False,
        timeout_seconds: float = _DEFAULT_BLOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._blocking = blocking
        self._timeout_seconds = timeout_seconds
        self.acquired = False
        self._already_held = False

    def __enter__(self) -> "runtime_writer":
        self._already_held = holds_runtime_writer_lock()
        self.acquired = acquire_runtime_writer_lock(
            blocking=self._blocking,
            timeout_seconds=self._timeout_seconds,
        )
        if not self.acquired:
            raise RuntimeOwnershipError(
                f"runtime writer lock is held by another process at {writer_lock_path()}"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._already_held:
            release_runtime_writer_lock()
