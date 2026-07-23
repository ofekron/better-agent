"""Tiny shared helpers for the JSON-file stores (project, worker, config).

Each store was reinventing the same three-step pattern:
  1. mkdir(mode=0o700, parents=True, exist_ok=True)
  2. json.loads(path.read_text()) with a warning + default on parse failure
  3. atomic write: tmp sibling + os.replace (crash-safe)

Centralized here so adding a new store (settings, templates, …) is two
lines at the call site. `write_json` is the single canonical atomic
JSON writer for this backend; `runs_dir.atomic_write_json` delegates
to it.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
_WINDOWS_REPLACE_RETRY_DELAYS_S = (0.01, 0.025, 0.05, 0.1, 0.2)


def _is_windows() -> bool:
    return os.name == "nt"


def read_json(path: Path, default: T) -> T:
    """Parse `path` as JSON. Returns `default` if the file is missing or
    malformed. If the parsed value's type differs from the default's type,
    the default is returned instead (guards against a hand-edited file
    silently flipping a list into a dict)."""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("failed to parse %s: %s", path, e)
        return default
    if type(data) is not type(default):  # noqa: E721
        logger.warning(
            "%s has wrong type (%s, expected %s) — ignoring",
            path, type(data).__name__, type(default).__name__,
        )
        return default
    return data


def _replace_atomic(src: Path, dst: Path) -> None:
    """Replace ``dst`` with bounded retries for transient Windows locks."""
    for delay in _WINDOWS_REPLACE_RETRY_DELAYS_S:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _is_windows():
                raise
            time.sleep(delay)
    os.replace(src, dst)


def _fsync_parent_directory(path: Path) -> None:
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        if not _is_windows():
            raise


def write_json(path: Path, data, mode: int = 0o700) -> None:
    """Write `data` as pretty-printed JSON, atomically. Creates parent
    dirs with the given mode (restricted by default since these stores
    hold per-user config and secrets-adjacent metadata).

    Atomicity: write to a `.tmp` sibling, then `os.replace` onto the
    target. A crash mid-write leaves the prior file intact instead of a
    truncated half-write — these files back durable per-user state, so a
    partial write must never become the canonical copy. The tmp is
    cleaned up on failure so a crash does not leave debris behind."""
    path.parent.mkdir(mode=mode, parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        _replace_atomic(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_json_durable(path: Path, data, mode: int = 0o700) -> None:
    """Atomically replace JSON and durably commit the file and parent entry."""
    path.parent.mkdir(mode=mode, parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_atomic(tmp, path)
        _fsync_parent_directory(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
