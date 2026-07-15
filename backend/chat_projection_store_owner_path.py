from __future__ import annotations

import os
import stat
from errno import EEXIST, ENOENT
from pathlib import Path

from chat_projection_store import ChatProjectionStoreError


def validate_secure_file_stat(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ChatProjectionStoreError("insecure_store_file", "chat store must be a regular file")
    if metadata.st_uid != os.getuid():
        raise ChatProjectionStoreError("insecure_store_file", "chat store owner is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ChatProjectionStoreError("insecure_store_file", "chat store mode must be 0600")
    if metadata.st_nlink != 1:
        raise ChatProjectionStoreError("insecure_store_file", "chat store cannot be hard-linked")


def verify_anchored_file(file_fd: int, basename: str) -> None:
    expected = os.fstat(file_fd)
    visible = os.stat(basename, follow_symlinks=False)
    validate_secure_file_stat(expected)
    validate_secure_file_stat(visible)
    if (expected.st_dev, expected.st_ino) != (visible.st_dev, visible.st_ino):
        raise ChatProjectionStoreError("path_race", "chat store file changed during owner open")


def _reject_orphan_sidecars(parent_fd: int, basename: str) -> None:
    for suffix in ("-wal", "-shm"):
        try:
            os.stat(f"{basename}{suffix}", dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == ENOENT:
                continue
            raise ChatProjectionStoreError("path_open_failed", "cannot inspect store sidecar") from exc
        raise ChatProjectionStoreError(
            "orphan_sidecars", "store recovery is required before opening",
        )


def _open_directory_chain(root: Path, relative_parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for component in (*root.parts[1:], *relative_parts):
            try:
                next_fd = os.open(component, flags, dir_fd=current)
            except OSError as exc:
                if exc.errno != ENOENT:
                    raise ChatProjectionStoreError("path_escape", "directory path is not secure") from exc
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except OSError as mkdir_exc:
                    if mkdir_exc.errno != EEXIST:
                        raise ChatProjectionStoreError("path_open_failed", "cannot create store directory") from mkdir_exc
                next_fd = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def secure_open(
    root_path: Path, path: Path, *, require_sqlite_header: bool = True,
) -> tuple[Path, int, int, bool]:
    declared_root = Path(os.path.abspath(root_path.expanduser()))
    declared_candidate = path.expanduser()
    if not declared_candidate.is_absolute():
        raise ChatProjectionStoreError("invalid_path", "chat store path must be absolute")
    declared_candidate = Path(os.path.abspath(declared_candidate))
    try:
        relative = declared_candidate.relative_to(declared_root)
    except ValueError as exc:
        raise ChatProjectionStoreError("path_escape", "store path escapes Better Agent home") from exc
    root = Path(os.path.realpath(declared_root))
    candidate = root / relative
    if not relative.parts or candidate.name in ("", ".", ".."):
        raise ChatProjectionStoreError("invalid_path", "chat store file name is required")
    parent_fd = _open_directory_chain(root, relative.parts[:-1])
    main_exists = True
    try:
        os.stat(candidate.name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        if exc.errno != ENOENT:
            os.close(parent_fd)
            raise ChatProjectionStoreError("path_open_failed", "cannot inspect chat store") from exc
        main_exists = False
    if not main_exists:
        try:
            _reject_orphan_sidecars(parent_fd, candidate.name)
        except BaseException:
            os.close(parent_fd)
            raise
    created = False
    flags = os.O_RDWR | os.O_NOFOLLOW
    try:
        file_fd = os.open(candidate.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        created = True
    except OSError as exc:
        if exc.errno != EEXIST:
            os.close(parent_fd)
            raise ChatProjectionStoreError("path_open_failed", "cannot securely create chat store") from exc
        try:
            file_fd = os.open(candidate.name, flags, dir_fd=parent_fd)
        except OSError as open_exc:
            os.close(parent_fd)
            code = "path_escape" if open_exc.errno != ENOENT else "path_race"
            raise ChatProjectionStoreError(code, "cannot securely open chat store") from open_exc
    try:
        file_metadata = os.fstat(file_fd)
        validate_secure_file_stat(file_metadata)
        validate_secure_file_stat(os.stat(candidate.name, dir_fd=parent_fd, follow_symlinks=False))
        if created:
            _reject_orphan_sidecars(parent_fd, candidate.name)
        elif require_sqlite_header and (
            file_metadata.st_size < 100 or os.pread(file_fd, 16, 0) != b"SQLite format 3\x00"
        ):
            raise ChatProjectionStoreError(
                "incomplete_store", "store initialization is incomplete",
            )
    except BaseException:
        os.close(file_fd)
        os.close(parent_fd)
        raise
    return candidate, parent_fd, file_fd, created
