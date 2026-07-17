from __future__ import annotations

import atexit
import logging
import os
import socket
import time
from pathlib import Path

from paths import ba_home
from portable_lock import try_lock_ex, unlock

# Keyed by component name ("backend", "bff", ...) so independent
# per-home singletons (the runtime and the BFF legitimately run
# concurrently) don't contend on the same lock file, while each
# component still gets its own cross-process "only one of me per home"
# guarantee instead of relying on a docstring warning.
_LOCK_FDS: dict[str, int] = {}
_LOCK_PATHS: dict[str, Path] = {}

# When a previous instance is being killed during a restart, uvicorn stops
# listening (freeing the TCP port) *before* the process fully exits and the
# OS releases the advisory flock on this file (the flock is dropped at
# interpreter exit via the atexit handler below). The launcher starts the new
# instance as soon as the port is free, so there is a brief window during
# which the old process is still alive and still holds the flock. Retry the
# non-blocking acquisition for a bounded period so the new instance waits for
# the old one to finish exiting instead of crashing on a spurious lock
# contention. This is well within run.sh's 60 s health-check budget.
_LOCK_ACQUIRE_RETRY_SECONDS = 15.0
_LOCK_ACQUIRE_POLL_INTERVAL = 0.25

_log = logging.getLogger("backend_instance_lock")


def _acquire_instance_lock(component: str) -> None:
    path = ba_home() / f"{component}.lock"
    existing_fd = _LOCK_FDS.get(component)
    if existing_fd is not None:
        if _LOCK_PATHS.get(component) == path:
            return
        raise RuntimeError(
            f"{component} instance lock already held for "
            f"{_LOCK_PATHS.get(component)}; cannot also use {path}"
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
                    f"another Better Agent {component} is already using "
                    f"{ba_home()}.{detail}"
                )
            if not warned:
                warned = True
                holder = _read_lock_holder(path)
                _log.warning(
                    "%s instance lock currently held%s; retrying for up "
                    "to %.0fs while the previous instance exits",
                    component,
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
    _LOCK_FDS[component] = fd
    _LOCK_PATHS[component] = path


def _release_instance_lock(component: str) -> None:
    fd = _LOCK_FDS.pop(component, None)
    _LOCK_PATHS.pop(component, None)
    if fd is None:
        return
    try:
        unlock(fd)
    finally:
        os.close(fd)


def acquire_backend_instance_lock() -> None:
    _acquire_instance_lock("backend")


def release_backend_instance_lock() -> None:
    _release_instance_lock("backend")


def acquire_bff_instance_lock() -> None:
    _acquire_instance_lock("bff")


def release_bff_instance_lock() -> None:
    _release_instance_lock("bff")


def _read_lock_holder(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _release_all_instance_locks() -> None:
    for component in list(_LOCK_FDS):
        _release_instance_lock(component)


atexit.register(_release_all_instance_locks)
