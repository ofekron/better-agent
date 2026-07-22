from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from paths import bc_home
from portable_lock import lock_ex, unlock

_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")


def _directory(namespace: str) -> Path:
    if not _SEGMENT_PATTERN.fullmatch(namespace):
        raise ValueError("invalid routine lock namespace")
    root = bc_home() / "routine-locks"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / namespace
    directory.mkdir(mode=0o700, exist_ok=True)
    for path in (root, directory):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError("routine lock path is not a directory")
    return directory


def acquire(namespace: str, task_id: str) -> int:
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("invalid routine lock identity")
    path = _directory(namespace) / f"{task_id}.lock"
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise OSError("routine lock is not a regular file")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("routine lock is not a regular file")
        lock_ex(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def release(fd: int) -> None:
    try:
        unlock(fd)
    finally:
        os.close(fd)
