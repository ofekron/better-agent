from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any


def fingerprint_package(spec: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    raw_root = Path(str(spec["root"])).expanduser()
    if _path_contains_symlink(raw_root, stop=raw_root.parent):
        return {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}
    root = raw_root.resolve(strict=True)
    relative_paths = set(str(item) for item in spec.get("relative_paths") or ())
    static_modules = dict(spec.get("static_modules") or {})
    files: list[Path] = []
    scanned = 0

    for module in spec.get("modules") or ():
        static_path = static_modules.get(module)
        if static_path:
            relative_paths.add(str(static_path))
            continue
        module_path = Path(*str(module).split("."))
        candidates = (module_path.with_suffix(".py"), module_path / "__init__.py")
        match = next((candidate for candidate in candidates if (root / candidate).is_file()), None)
        if match is None:
            return {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}
        relative_paths.add(match.as_posix())

    try:
        for rel_path in sorted(relative_paths):
            raw_candidate = root / rel_path
            if _path_contains_symlink(raw_candidate, stop=root):
                return {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}
            candidate = raw_candidate.resolve(strict=True)
            if not candidate.is_relative_to(root):
                return {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}
            if candidate.is_dir():
                descendants = sorted(candidate.rglob("*"))
                if any(item.is_symlink() for item in descendants):
                    return {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}
                candidates = [item for item in descendants if item.is_file()]
                scanned += len(candidates)
                files.extend(candidates)
            else:
                scanned += 1
                files.append(candidate)
    except OSError:
        return {"digest": None, "files": 0, "bytes": 0, "scan_ms": 0.0, "hash_ms": 0.0}

    scan_ms = (time.perf_counter() - started) * 1000.0
    digest = hashlib.sha256()
    bytes_read = 0
    hash_started = time.perf_counter()
    try:
        for candidate in files:
            before = candidate.stat(follow_symlinks=False)
            if candidate.is_symlink() or not candidate.is_file():
                return {"digest": None, "files": scanned, "bytes": bytes_read, "scan_ms": scan_ms, "hash_ms": 0.0}
            digest.update(candidate.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(candidate, flags)
            try:
                opened = os.fstat(fd)
                if _identity(opened) != _identity(before):
                    return {"digest": None, "files": scanned, "bytes": bytes_read, "scan_ms": scan_ms, "hash_ms": 0.0}
                while chunk := os.read(fd, 1024 * 1024):
                    bytes_read += len(chunk)
                    digest.update(chunk)
                digest.update(b"\0")
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if _identity(after) != _identity(before):
                return {"digest": None, "files": scanned, "bytes": bytes_read, "scan_ms": scan_ms, "hash_ms": 0.0}
    except OSError:
        return {"digest": None, "files": scanned, "bytes": bytes_read, "scan_ms": scan_ms, "hash_ms": 0.0}
    return {
        "digest": digest.hexdigest(),
        "files": scanned,
        "bytes": bytes_read,
        "scan_ms": scan_ms,
        "hash_ms": (time.perf_counter() - hash_started) * 1000.0,
    }


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
