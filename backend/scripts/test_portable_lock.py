import errno
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portable_lock  # noqa: E402


class _FakeMsvcrt:
    """Records ``locking`` calls; optionally raises a stashed OSError."""

    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.error: OSError | None = None

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes))
        if self.error is not None:
            raise self.error


def _temp_fd() -> tuple[Path, int]:
    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"bc-portable-lock-{os.getpid()}.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    return path, fd


def test_is_lock_contention_classifies_errnos() -> None:
    for code in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
        assert portable_lock._is_lock_contention(OSError(code, "x")) is True
    assert portable_lock._is_lock_contention(OSError(errno.EBADF, "x")) is False


def test_msvcrt_lock_ex_seeks_to_start_then_locks_one_byte() -> None:
    path, fd = _temp_fd()
    fake = _FakeMsvcrt()
    try:
        assert portable_lock._msvcrt_lock_ex(fake, fd) is None
        # cursor reset to offset 0 before locking
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert fake.calls == [(fd, _FakeMsvcrt.LK_LOCK, 1)]
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def test_msvcrt_unlock_seeks_to_start_then_unlocks_one_byte() -> None:
    path, fd = _temp_fd()
    fake = _FakeMsvcrt()
    try:
        portable_lock._msvcrt_unlock(fake, fd)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert fake.calls == [(fd, _FakeMsvcrt.LK_UNLCK, 1)]
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def test_msvcrt_try_lock_ex_success_returns_true() -> None:
    path, fd = _temp_fd()
    fake = _FakeMsvcrt()
    try:
        assert portable_lock._msvcrt_try_lock_ex(fake, fd) is True
        assert fake.calls == [(fd, _FakeMsvcrt.LK_NBLCK, 1)]
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def test_msvcrt_try_lock_ex_contention_returns_false() -> None:
    path, fd = _temp_fd()
    fake = _FakeMsvcrt()
    fake.error = OSError(errno.EAGAIN, "resource busy")
    try:
        assert portable_lock._msvcrt_try_lock_ex(fake, fd) is False
        assert fake.calls == [(fd, _FakeMsvcrt.LK_NBLCK, 1)]
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def test_msvcrt_try_lock_ex_reraises_unexpected_error() -> None:
    path, fd = _temp_fd()
    fake = _FakeMsvcrt()
    fake.error = OSError(errno.EBADF, "bad fd")
    try:
        try:
            portable_lock._msvcrt_try_lock_ex(fake, fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError("unexpected lock error was swallowed")
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def test_try_lock_ex_returns_false_for_posix_would_block() -> None:
    fcntl_module = getattr(portable_lock, "fcntl", None)
    if fcntl_module is None:
        return
    original_flock = fcntl_module.flock

    def busy_flock(fd: int, operation: int) -> None:
        raise OSError(errno.EAGAIN, "resource temporarily unavailable")

    fcntl_module.flock = busy_flock
    try:
        assert portable_lock.try_lock_ex(0) is False
    finally:
        fcntl_module.flock = original_flock


def test_try_lock_ex_reraises_unexpected_posix_error() -> None:
    fcntl_module = getattr(portable_lock, "fcntl", None)
    if fcntl_module is None:
        return
    original_flock = fcntl_module.flock

    def broken_flock(fd: int, operation: int) -> None:
        raise OSError(errno.EBADF, "bad file descriptor")

    fcntl_module.flock = broken_flock
    try:
        try:
            portable_lock.try_lock_ex(0)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError("unexpected lock error was swallowed")
    finally:
        fcntl_module.flock = original_flock


def test_posix_lock_ex_then_unlock_roundtrip() -> None:
    if getattr(portable_lock, "fcntl", None) is None:
        return
    path, fd = _temp_fd()
    try:
        portable_lock.lock_ex(fd)
        # a second non-blocking acquire on the same fd in the same process
        # is a no-op upgrade for flock, so this proves the call succeeded
        # without raising and that unlock releases cleanly.
        assert portable_lock.try_lock_ex(fd) is True
        portable_lock.unlock(fd)
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_is_lock_contention_classifies_errnos()
    test_msvcrt_lock_ex_seeks_to_start_then_locks_one_byte()
    test_msvcrt_unlock_seeks_to_start_then_unlocks_one_byte()
    test_msvcrt_try_lock_ex_success_returns_true()
    test_msvcrt_try_lock_ex_contention_returns_false()
    test_msvcrt_try_lock_ex_reraises_unexpected_error()
    test_try_lock_ex_returns_false_for_posix_would_block()
    test_try_lock_ex_reraises_unexpected_posix_error()
    test_posix_lock_ex_then_unlock_roundtrip()
    print("OK: portable_lock")
