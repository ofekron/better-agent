"""Per-project JSONL store for captured project structure updates.

Append-only entries with an unseen flag.  The MCP tool
`capture_project_update` writes here; the frontend reads unseen
counts and entries; the dedicated edit session consumes and marks
them seen.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from paths import ba_home

_lock = threading.Lock()
_counts_loaded = False
_unseen_counts: dict[str, int] = {}
_total_unseen_count = 0


def _updates_dir() -> Path:
    return ba_home() / "project_updates"


def _project_path(project_id: str) -> Path:
    d = _updates_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project_id}.jsonl"


def _read_entries_locked(project_id: str) -> list[dict]:
    path = _project_path(project_id)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _ensure_counts_locked() -> None:
    global _counts_loaded, _total_unseen_count
    if _counts_loaded:
        return
    _unseen_counts.clear()
    total = 0
    d = _updates_dir()
    if d.exists():
        for path in d.glob("*.jsonl"):
            count = 0
            for entry in _read_entries_locked(path.stem):
                if not entry.get("seen"):
                    count += 1
            if count:
                _unseen_counts[path.stem] = count
                total += count
    _total_unseen_count = total
    _counts_loaded = True


def _set_count_locked(project_id: str, count: int) -> None:
    global _total_unseen_count
    previous = _unseen_counts.get(project_id, 0)
    if count > 0:
        _unseen_counts[project_id] = count
        _total_unseen_count += count - previous
        return
    _unseen_counts.pop(project_id, None)
    _total_unseen_count -= previous


def append(project_id: str, text: str) -> dict:
    """Append a free-form project update entry. Returns the created entry."""
    entry = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seen": False,
    }
    path = _project_path(project_id)
    with _lock:
        _ensure_counts_locked()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        _set_count_locked(project_id, _unseen_counts.get(project_id, 0) + 1)
    return entry


def list_unseen(project_id: str) -> list[dict]:
    """Return all unseen entries for a project."""
    with _lock:
        return [entry for entry in _read_entries_locked(project_id) if not entry.get("seen")]


def unseen_count(project_id: str) -> int:
    with _lock:
        _ensure_counts_locked()
        return _unseen_counts.get(project_id, 0)


def total_unseen() -> int:
    """Sum of unseen counts across every project that has an update log."""
    with _lock:
        _ensure_counts_locked()
        return _total_unseen_count


def peek_total_unseen() -> int | None:
    with _lock:
        if not _counts_loaded:
            return None
        return _total_unseen_count


def mark_seen(project_id: str, entry_ids: list[str]) -> int:
    """Mark specific entries as seen. Returns count marked."""
    path = _project_path(project_id)
    if not path.exists():
        return 0
    ids_set = set(entry_ids)
    with _lock:
        _ensure_counts_locked()
        entries = _read_entries_locked(project_id)
        updated = []
        count = 0
        for entry in entries:
            if entry.get("id") in ids_set and not entry.get("seen"):
                entry["seen"] = True
                count += 1
            updated.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            for entry in updated:
                f.write(json.dumps(entry) + "\n")
        if count:
            _set_count_locked(project_id, _unseen_counts.get(project_id, 0) - count)
    return count


def list_all(project_id: str) -> list[dict]:
    """Return all entries (seen + unseen) for a project."""
    with _lock:
        return _read_entries_locked(project_id)
