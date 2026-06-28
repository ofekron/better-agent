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
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
