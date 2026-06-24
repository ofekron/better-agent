import errno
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portable_lock  # noqa: E402


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


def test_try_lock_ex_roundtrip() -> None:
    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"bc-portable-lock-{os.getpid()}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        assert portable_lock.try_lock_ex(handle.fileno()) is True
        portable_lock.unlock(handle.fileno())
    path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_try_lock_ex_returns_false_for_posix_would_block()
    test_try_lock_ex_reraises_unexpected_posix_error()
    test_try_lock_ex_roundtrip()
    print("OK: portable_lock")
