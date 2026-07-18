from __future__ import annotations

from contextlib import contextmanager
import os
import threading

from .paths import pointer_path

_thread_lock = threading.RLock()
_local = threading.local()


@contextmanager
def _platform_lock(handle, platform: str | None = None):
    platform = platform or os.name
    if platform == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def mutation_lock():
    with _thread_lock:
        depth = int(getattr(_local, "depth", 0))
        if depth:
            _local.depth = depth + 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        path = pointer_path().with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            with _platform_lock(handle):
                _local.depth = 1
                try:
                    yield
                finally:
                    _local.depth = 0
