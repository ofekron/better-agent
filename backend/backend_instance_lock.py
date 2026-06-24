from __future__ import annotations

import atexit
import fcntl
import os
import socket
from pathlib import Path

from paths import ba_home

_LOCK_FD: int | None = None
_LOCK_PATH: Path | None = None


def acquire_backend_instance_lock() -> None:
    global _LOCK_FD, _LOCK_PATH

    path = ba_home() / "backend.lock"
    if _LOCK_FD is not None:
        if _LOCK_PATH == path:
            return
        raise RuntimeError(
            f"backend instance lock already held for {_LOCK_PATH}; "
            f"cannot also use {path}"
        )

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        holder = _read_lock_holder(path)
        os.close(fd)
        detail = f" Current holder: {holder}" if holder else ""
        raise RuntimeError(
            f"another Better Agent backend is already using {ba_home()}.{detail}"
        ) from exc
    except Exception:
        os.close(fd)
        raise

    os.ftruncate(fd, 0)
    os.write(
        fd,
        (
            f"pid={os.getpid()}\n"
            f"host={socket.gethostname()}\n"
            f"ba_home={ba_home()}\n"
        ).encode("utf-8"),
    )
    os.fsync(fd)
    _LOCK_FD = fd
    _LOCK_PATH = path


def release_backend_instance_lock() -> None:
    global _LOCK_FD, _LOCK_PATH

    if _LOCK_FD is None:
        return
    fd = _LOCK_FD
    _LOCK_FD = None
    _LOCK_PATH = None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_lock_holder(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


atexit.register(release_backend_instance_lock)
