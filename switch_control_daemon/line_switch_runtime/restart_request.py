from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import stat
import string
import threading
import uuid
from pathlib import Path

from .transaction import _platform_lock


_MAX_REQUEST_ID_BYTES = 100
_REQUEST_ID_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-_")
_THREAD_LOCK = threading.RLock()


def valid_restart_request_id(request_id: object) -> bool:
    return (
        isinstance(request_id, str)
        and 1 <= len(request_id) <= _MAX_REQUEST_ID_BYTES
        and all(character in _REQUEST_ID_CHARACTERS for character in request_id)
    )


def new_restart_request_id() -> str:
    return uuid.uuid4().hex


def _sync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _request_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        path_details = lock_path.lstat()
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or (details.st_dev, details.st_ino)
            != (path_details.st_dev, path_details.st_ino)
        ):
            raise OSError("restart request lock must be a regular file")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    with _THREAD_LOCK, os.fdopen(descriptor, "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        with _platform_lock(handle):
            yield


def write_restart_request(path: Path, request_id: str) -> None:
    if not valid_restart_request_id(request_id):
        raise ValueError("restart request id is invalid")
    with _request_lock(path):
        _write_restart_request_locked(path, request_id)


def _write_restart_request_locked(path: Path, request_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("restart request temporary must be a regular file")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            payload = request_id.encode("ascii")
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count == 0:
                    raise OSError("restart request write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _sync_parent(path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _claim_restart_request(path: Path) -> Path | None:
    claimed = path.with_name(f".{path.name}.consume-{uuid.uuid4().hex}.tmp")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _read_claimed_restart_request(path: Path) -> tuple[str, float]:
    path_details = path.lstat()
    if stat.S_ISLNK(path_details.st_mode):
        raise OSError("restart request must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size > _MAX_REQUEST_ID_BYTES
            or (details.st_dev, details.st_ino)
            != (path_details.st_dev, path_details.st_ino)
        ):
            raise OSError("restart request must be a small regular file")
        request_id = os.read(descriptor, _MAX_REQUEST_ID_BYTES + 1).decode("ascii")
    except (UnicodeDecodeError, OSError):
        raise OSError("restart request is invalid") from None
    finally:
        os.close(descriptor)
    if not valid_restart_request_id(request_id):
        raise OSError("restart request id is invalid")
    return request_id, details.st_mtime


def _discard_claim(path: Path) -> None:
    try:
        path.unlink()
    except IsADirectoryError:
        path.rmdir()


def consume_restart_request(
    path: Path,
    *,
    not_before: float | None = None,
) -> str | None:
    with _request_lock(path):
        claimed = _claim_restart_request(path)
        if claimed is None:
            return None
        try:
            request_id, modified_at = _read_claimed_restart_request(claimed)
        finally:
            _discard_claim(claimed)
        if not_before is not None and modified_at < not_before:
            return None
        return request_id


def remove_restart_request(path: Path, request_id: str) -> bool:
    if not valid_restart_request_id(request_id):
        raise ValueError("restart request id is invalid")
    with _request_lock(path):
        claimed = _claim_restart_request(path)
        if claimed is None:
            return False
        try:
            claimed_request_id, _modified_at = _read_claimed_restart_request(claimed)
            if claimed_request_id != request_id:
                os.replace(claimed, path)
                return False
        finally:
            try:
                _discard_claim(claimed)
            except FileNotFoundError:
                pass
        return True


def clear_restart_request(path: Path) -> None:
    with _request_lock(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--not-before", type=float)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args(argv)
    if args.clear:
        if args.not_before is not None:
            parser.error("--clear cannot be combined with --not-before")
        clear_restart_request(args.path)
        return 0
    request_id = consume_restart_request(
        args.path,
        not_before=args.not_before,
    )
    if request_id is not None:
        print(request_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
