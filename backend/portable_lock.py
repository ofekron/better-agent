"""Cross-platform advisory file locking.

The codebase was written for macOS/Linux and uses ``fcntl.flock`` to
serialize concurrent writers to the same on-disk JSON file (e.g. two
browser tabs approving the same delegation). ``fcntl`` doesn't exist on
Windows, so this module provides ``lock_ex`` / ``unlock`` that map to
``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows.

Semantics preserved: ``lock_ex`` blocks until an exclusive lock is held;
``try_lock_ex`` returns whether a non-blocking exclusive lock was acquired;
``unlock`` releases it. All take a raw OS file descriptor.

The Windows (``msvcrt``) implementation is factored into module-level
helpers that take the ``msvcrt`` module as an argument. ``msvcrt`` exists
only on Windows, so passing it in (rather than importing it at module load)
keeps the module importable on POSIX while letting tests on POSIX exercise
the Windows locking logic by injecting a fake ``msvcrt`` — the real
cross-platform bug surface (cursor positioning and contention handling)
is then covered on every CI runner, not just Windows.
"""

import os
import errno

_LOCK_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def _is_lock_contention(exc: OSError) -> bool:
    return exc.errno in _LOCK_CONTENTION_ERRNOS


def _msvcrt_lock_ex(msvcrt_module, fd: int) -> None:
    # msvcrt locks a byte range at the current file position. Lock a
    # single byte from offset 0 so the lock region is deterministic
    # regardless of where the buffered writer left the cursor.
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt_module.locking(fd, msvcrt_module.LK_LOCK, 1)


def _msvcrt_try_lock_ex(msvcrt_module, fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt_module.locking(fd, msvcrt_module.LK_NBLCK, 1)
    except OSError as exc:
        if _is_lock_contention(exc):
            return False
        raise
    return True


def _msvcrt_unlock(msvcrt_module, fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt_module.locking(fd, msvcrt_module.LK_UNLCK, 1)


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

except ImportError:  # Windows  pragma: no cover
    import msvcrt  # pragma: no cover

    def lock_ex(fd: int) -> None:  # pragma: no cover
        _msvcrt_lock_ex(msvcrt, fd)

    def try_lock_ex(fd: int) -> bool:  # pragma: no cover
        return _msvcrt_try_lock_ex(msvcrt, fd)

    def unlock(fd: int) -> None:  # pragma: no cover
        _msvcrt_unlock(msvcrt, fd)
