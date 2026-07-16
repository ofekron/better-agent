from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any


_EMPTY = {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}


def fingerprint_package(spec: dict[str, Any]) -> dict[str, Any]:
    if os.name == "nt":
        return _fingerprint_windows(spec)
    return _fingerprint_posix(spec)


def fingerprint_packages(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [fingerprint_package(spec) for spec in specs]


def worker_main(connection: Any) -> None:
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            request_id, specs = request
            started = time.perf_counter()
            results = fingerprint_packages(specs)
            connection.send((request_id, results, (time.perf_counter() - started) * 1000.0))
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


def _fingerprint_posix(spec: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    root_path = Path(str(spec["root"])).expanduser()
    trusted_root = Path(str(spec.get("trusted_root") or root_path.parent)).expanduser()
    try:
        if _path_contains_symlink(root_path, stop=trusted_root.parent):
            return dict(_EMPTY)
        trusted_root_resolved = trusted_root.resolve(strict=True)
    except OSError:
        return dict(_EMPTY)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root_path, flags)
    except OSError:
        return dict(_EMPTY)
    try:
        if not root_path.resolve(strict=True).is_relative_to(trusted_root_resolved):
            return dict(_EMPTY)
        root_identity = _identity(os.fstat(root_fd))
        relative_paths = _declared_paths(spec, root_fd)
        if relative_paths is None:
            return dict(_EMPTY)
        files: set[str] = set()
        for relative_path in relative_paths:
            target_fd = _open_relative(root_fd, relative_path)
            if target_fd is None:
                return dict(_EMPTY)
            try:
                mode = os.fstat(target_fd).st_mode
                if stat.S_ISDIR(mode):
                    if not _collect_tree(target_fd, relative_path, files):
                        return dict(_EMPTY)
                elif stat.S_ISREG(mode):
                    files.add(relative_path)
                else:
                    return dict(_EMPTY)
            finally:
                os.close(target_fd)
        scan_ms = (time.perf_counter() - started) * 1000.0
        directory_identities = _directory_identities(root_fd, files)
        if directory_identities is None:
            return dict(_EMPTY)
        result = _hash_posix_files(root_fd, sorted(files), scan_ms)
        if result["digest"] is None or _identity(os.fstat(root_fd)) != root_identity:
            return dict(_EMPTY)
        if not _verify_directory_identities(root_fd, directory_identities):
            return dict(_EMPTY)
        verify_fd = os.open(root_path, flags)
        try:
            if _identity(os.fstat(verify_fd)) != root_identity:
                return dict(_EMPTY)
        finally:
            os.close(verify_fd)
        return result
    except OSError:
        return dict(_EMPTY)
    finally:
        os.close(root_fd)


def _declared_paths(spec: dict[str, Any], root_fd: int) -> set[str] | None:
    relative_paths = {_clean_relative(str(item)) for item in spec.get("relative_paths") or ()}
    if None in relative_paths:
        return None
    paths = {item for item in relative_paths if item is not None}
    static_modules = dict(spec.get("static_modules") or {})
    for module in spec.get("modules") or ():
        static_path = static_modules.get(module)
        if static_path:
            clean = _clean_relative(str(static_path))
            if clean is None:
                return None
            paths.add(clean)
            continue
        module_path = PurePosixPath(*str(module).split("."))
        candidates = (str(module_path.with_suffix(".py")), str(module_path / "__init__.py"))
        match = next((candidate for candidate in candidates if _relative_is_file(root_fd, candidate)), None)
        if match is None:
            return None
        paths.add(match)
    return paths


def _clean_relative(value: str) -> str | None:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _open_relative(root_fd: int, relative_path: str) -> int | None:
    current = os.dup(root_fd)
    try:
        parts = PurePosixPath(relative_path).parts
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except OSError:
        os.close(current)
        return None


def _relative_is_file(root_fd: int, relative_path: str) -> bool:
    fd = _open_relative(root_fd, relative_path)
    if fd is None:
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def _collect_tree(directory_fd: int, prefix: str, files: set[str]) -> bool:
    try:
        for name in sorted(os.listdir(directory_fd)):
            child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            child_path = f"{prefix}/{name}"
            if stat.S_ISLNK(child_stat.st_mode):
                return False
            if stat.S_ISREG(child_stat.st_mode):
                files.add(child_path)
                continue
            if not stat.S_ISDIR(child_stat.st_mode):
                continue
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                if not _collect_tree(child_fd, child_path, files):
                    return False
            finally:
                os.close(child_fd)
        return True
    except OSError:
        return False


def _hash_posix_files(root_fd: int, files: list[str], scan_ms: float) -> dict[str, Any]:
    digest = hashlib.sha256()
    bytes_read = 0
    hash_started = time.perf_counter()
    for relative_path in files:
        fd = _open_relative(root_fd, relative_path)
        if fd is None:
            return dict(_EMPTY)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return dict(_EMPTY)
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            while chunk := os.read(fd, 1024 * 1024):
                bytes_read += len(chunk)
                digest.update(chunk)
            digest.update(b"\0")
            if _identity(os.fstat(fd)) != _identity(before):
                return dict(_EMPTY)
        finally:
            os.close(fd)
        verify_fd = _open_relative(root_fd, relative_path)
        if verify_fd is None:
            return dict(_EMPTY)
        try:
            if _identity(os.fstat(verify_fd)) != _identity(before):
                return dict(_EMPTY)
        finally:
            os.close(verify_fd)
    return {
        "digest": digest.hexdigest(),
        "files": len(files),
        "bytes": bytes_read,
        "scan_ms": scan_ms,
        "hash_ms": (time.perf_counter() - hash_started) * 1000.0,
    }


def _directory_identities(root_fd: int, files: set[str]) -> dict[str, tuple[int, ...]] | None:
    paths: set[str] = set()
    for item in files:
        parent = PurePosixPath(item).parent
        while parent.as_posix() != ".":
            paths.add(parent.as_posix())
            parent = parent.parent
    identities: dict[str, tuple[int, ...]] = {}
    for path in paths:
        fd = _open_relative(root_fd, path)
        if fd is None:
            return None
        try:
            value = os.fstat(fd)
            if not stat.S_ISDIR(value.st_mode):
                return None
            identities[path] = _identity(value)
        finally:
            os.close(fd)
    return identities


def _verify_directory_identities(root_fd: int, expected: dict[str, tuple[int, ...]]) -> bool:
    for path, identity in expected.items():
        fd = _open_relative(root_fd, path)
        if fd is None:
            return False
        try:
            if _identity(os.fstat(fd)) != identity:
                return False
        finally:
            os.close(fd)
    return True


def _fingerprint_windows(spec: dict[str, Any]) -> dict[str, Any]:
    from extension_integrity_windows import fingerprint_package_windows

    return fingerprint_package_windows(spec)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev), int(value.st_ino), int(value.st_mode), int(value.st_size),
        int(value.st_mtime_ns), int(value.st_ctime_ns),
    )


def _path_contains_symlink(path: Path, *, stop: Path | None = None) -> bool:
    current = path
    while stop is None or current != stop:
        if current.is_symlink():
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False
