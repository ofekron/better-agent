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

from paths import ba_home, encode_cwd

_lock = threading.Lock()


def _updates_dir() -> Path:
    return ba_home() / "project_updates"


def _project_path(project_id: str) -> Path:
    d = _updates_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project_id}.jsonl"


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
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def list_unseen(project_id: str) -> list[dict]:
    """Return all unseen entries for a project."""
    path = _project_path(project_id)
    if not path.exists():
        return []
    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not entry.get("seen"):
            entries.append(entry)
    return entries


def unseen_count(project_id: str) -> int:
    return len(list_unseen(project_id))


def total_unseen() -> int:
    """Sum of unseen counts across every project that has an update log."""
    d = _updates_dir()
    if not d.exists():
        return 0
    total = 0
    for path in d.glob("*.jsonl"):
        total += unseen_count(path.stem)
    return total


def mark_seen(project_id: str, entry_ids: list[str]) -> int:
    """Mark specific entries as seen. Returns count marked."""
    path = _project_path(project_id)
    if not path.exists():
        return 0
    ids_set = set(entry_ids)
    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines()
        updated = []
        count = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("id") in ids_set and not entry.get("seen"):
                entry["seen"] = True
                count += 1
            updated.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            for entry in updated:
                f.write(json.dumps(entry) + "\n")
    return count


def list_all(project_id: str) -> list[dict]:
    """Return all entries (seen + unseen) for a project."""
    path = _project_path(project_id)
    if not path.exists():
        return []
    with _lock:
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
