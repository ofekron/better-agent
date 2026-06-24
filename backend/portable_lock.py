"""Cross-platform advisory file locking.

The codebase was written for macOS/Linux and uses ``fcntl.flock`` to
serialize concurrent writers to the same on-disk JSON file (e.g. two
browser tabs approving the same delegation). ``fcntl`` doesn't exist on
Windows, so this module provides ``lock_ex`` / ``unlock`` that map to
``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows.

Semantics preserved: ``lock_ex`` blocks until an exclusive lock is held;
``try_lock_ex`` returns whether a non-blocking exclusive lock was acquired;
``unlock`` releases it. All take a raw OS file descriptor.
"""

import os
import errno

_LOCK_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def _is_lock_contention(exc: OSError) -> bool:
    return exc.errno in _LOCK_CONTENTION_ERRNOS


try:
    import fcntl

    def lock_ex(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def try_lock_ex(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if _is_lock_contention(exc):
                return False
            raise
        return True

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def lock_ex(fd: int) -> None:
        # msvcrt locks a byte range at the current file position. Lock a
        # single byte from offset 0 so the lock region is deterministic
        # regardless of where the buffered writer left the cursor.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def try_lock_ex(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_lock_contention(exc):
                return False
            raise
        return True

    def unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
