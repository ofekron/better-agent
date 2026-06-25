from __future__ import annotations

import atexit
import logging
import os
import socket
import time
from pathlib import Path

from paths import ba_home
from portable_lock import try_lock_ex, unlock

_LOCK_FD: int | None = None
_LOCK_PATH: Path | None = None

# When a previous backend is being killed during a restart, uvicorn stops
# listening (freeing the TCP port) *before* the process fully exits and the
# OS releases the advisory flock on this file (the flock is dropped at
# interpreter exit via the atexit handler below). The launcher starts the new
# backend as soon as the port is free, so there is a brief window during which
# the old process is still alive and still holds the flock. Retry the
# non-blocking acquisition for a bounded period so the new backend waits for
# the old one to finish exiting instead of crashing on a spurious lock
# contention. This is well within run.sh's 60 s health-check budget.
_LOCK_ACQUIRE_RETRY_SECONDS = 15.0
_LOCK_ACQUIRE_POLL_INTERVAL = 0.25

_log = logging.getLogger("backend_instance_lock")


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
    deadline = time.monotonic() + _LOCK_ACQUIRE_RETRY_SECONDS
    warned = False
    try:
        while True:
            acquired = try_lock_ex(fd)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                holder = _read_lock_holder(path)
                detail = f" Current holder: {holder}" if holder else ""
                raise RuntimeError(
                    f"another Better Agent backend is already using "
                    f"{ba_home()}.{detail}"
                )
            if not warned:
                warned = True
                holder = _read_lock_holder(path)
                _log.warning(
                    "backend instance lock currently held%s; retrying for up "
                    "to %.0fs while the previous backend exits",
                    f" ({holder})" if holder else "",
                    _LOCK_ACQUIRE_RETRY_SECONDS,
                )
            time.sleep(min(_LOCK_ACQUIRE_POLL_INTERVAL, remaining))
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
        unlock(fd)
    finally:
        os.close(fd)


def _read_lock_holder(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


atexit.register(release_backend_instance_lock)
