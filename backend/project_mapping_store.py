"""Persistent project mappings — groups of projects across machines that are
the same logical project.

Stored at ba_home()/project_mappings.json.

Auto-matching uses three confidence levels (strongest first):
  1. git_remote — same git origin URL
  2. path       — same absolute filesystem path
  3. name       — same directory name

The user can confirm, reject, or manually reassign groups via the UI.
"""

import copy
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from json_store import read_json, write_json

from paths import ba_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Confidence levels ordered strongest → weakest.
CONFIDENCE_LEVELS = ("git_remote", "path", "name", "manual")
# For display ordering / comparison.
_CONFIDENCE_RANK = {c: i for i, c in enumerate(CONFIDENCE_LEVELS)}
_raw_cache: tuple[tuple[int, int], dict] | None = None


def _mappings_path() -> Path:
    return ba_home() / "project_mappings.json"


def _mappings_fingerprint() -> tuple[int, int]:
    try:
        st = _mappings_path().stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _read_raw() -> dict:
    global _raw_cache
    fingerprint = _mappings_fingerprint()
    cached = _raw_cache
    if cached is not None and cached[0] == fingerprint:
        return copy.deepcopy(cached[1])
    data = read_json(_mappings_path(), {"version": SCHEMA_VERSION, "groups": [], "rejected_ids": []})
    raw = data if isinstance(data, dict) else {}
    _raw_cache = (fingerprint, copy.deepcopy(raw))
    return raw


def _read_file() -> list[dict]:
    data = _read_raw()
    if isinstance(data, dict) and data.get("version") == SCHEMA_VERSION:
        groups = data.get("groups") or []
        return groups if isinstance(groups, list) else []
    return []


def _read_rejected() -> set[str]:
    data = _read_raw()
    rejected = data.get("rejected_ids") if isinstance(data, dict) else None
    return set(rejected) if isinstance(rejected, list) else set()


def _write_file(groups: list[dict], rejected_ids: Optional[set[str]] = None) -> None:
    global _raw_cache
    if rejected_ids is None:
        rejected_ids = _read_rejected()
    data = {
        "version": SCHEMA_VERSION,
        "groups": groups,
        "rejected_ids": sorted(rejected_ids),
    }
    write_json(_mappings_path(), data)
    _raw_cache = (_mappings_fingerprint(), copy.deepcopy(data))


# ---------------------------------------------------------------------------
# Auto-matching
# ---------------------------------------------------------------------------

def _strongest_confidence(existing: str, new_conf: str) -> str:
    """Return the stronger of two confidence levels."""
    return (
        existing
        if _CONFIDENCE_RANK.get(existing, 99) < _CONFIDENCE_RANK.get(new_conf, 99)
        else new_conf
    )


def auto_match(projects: list[dict]) -> list[dict]:
    """Build mapping groups from a flat project list. Returns the new groups.

    Algorithm:
      1. Group by git_remote (non-None, non-empty).
      2. Group remaining by normalized path.
      3. Group remaining by lowercase directory name.
      4. Each group with 2+ members from different nodes becomes a mapping.
    """
    # Track which projects have been assigned to a group:
    # (node_id, path) → group_id
    assigned: dict[tuple[str, str], str] = {}
    groups: dict[str, dict] = {}  # group_id → group

    def _ensure_group(group_id: str, confidence: str) -> dict:
        if group_id not in groups:
            groups[group_id] = {
                "group_id": group_id,
                "confidence": confidence,
                "members": [],
            }
        else:
            groups[group_id]["confidence"] = _strongest_confidence(
                groups[group_id]["confidence"], confidence,
            )
        return groups[group_id]

    def _member_key(p: dict) -> tuple[str, str]:
        return (p.get("node_id") or "primary", p.get("path") or "")

    # Pass 1: git_remote
    git_groups: dict[str, list[dict]] = {}
    for p in projects:
        remote = p.get("git_remote")
        if remote:
            git_groups.setdefault(remote, []).append(p)

    for remote, members in git_groups.items():
        if len(members) < 2:
            continue
        node_ids = {m.get("node_id") or "primary" for m in members}
        if len(node_ids) < 2:
            continue
        gid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"git:{remote}"))
        group = _ensure_group(gid, "git_remote")
        for m in members:
            key = _member_key(m)
            if key not in assigned:
                assigned[key] = gid
                group["members"].append({
                    "node_id": m.get("node_id") or "primary",
                    "path": m.get("path", ""),
                    "name": m.get("name", ""),
                    "git_remote": remote,
                })

    # Pass 2: path
    path_groups: dict[str, list[dict]] = {}
    for p in projects:
        if _member_key(p) in assigned:
            continue
        path_val = p.get("path", "")
        if path_val:
            path_groups.setdefault(path_val, []).append(p)

    for path_val, members in path_groups.items():
        if len(members) < 2:
            continue
        node_ids = {m.get("node_id") or "primary" for m in members}
        if len(node_ids) < 2:
            continue
        gid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"path:{path_val}"))
        group = _ensure_group(gid, "path")
        for m in members:
            key = _member_key(m)
            if key not in assigned:
                assigned[key] = gid
                group["members"].append({
                    "node_id": m.get("node_id") or "primary",
                    "path": path_val,
                    "name": m.get("name", ""),
                })

    # Pass 3: name (case-insensitive)
    name_groups: dict[str, list[dict]] = {}
    for p in projects:
        if _member_key(p) in assigned:
            continue
        name_val = (p.get("name") or "").lower()
        if name_val:
            name_groups.setdefault(name_val, []).append(p)

    for name_val, members in name_groups.items():
        if len(members) < 2:
            continue
        node_ids = {m.get("node_id") or "primary" for m in members}
        if len(node_ids) < 2:
            continue
        gid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"name:{name_val}"))
        group = _ensure_group(gid, "name")
        for m in members:
            key = _member_key(m)
            if key not in assigned:
                assigned[key] = gid
                group["members"].append({
                    "node_id": m.get("node_id") or "primary",
                    "path": m.get("path", ""),
                    "name": m.get("name", ""),
                })

    # Set auto-label from the most common name in each group
    for group in groups.values():
        names = [m.get("name", "") for m in group["members"] if m.get("name")]
        group["label"] = max(set(names), key=names.count) if names else ""

    return list(groups.values())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_mappings() -> list[dict]:
    """Return all mapping groups."""
    return _read_file()


def rebuild_and_save(projects: list[dict]) -> list[dict]:
    """Re-run auto_match against the given projects, merge with any
    manually-created groups, filter out previously-rejected groups,
    persist, and return the result."""
    existing = _read_file()
    rejected = _read_rejected()
    manual_groups = [g for g in existing if g.get("confidence") == "manual"]

    auto = auto_match(projects)
    # Filter out auto-matched groups whose id was previously rejected.
    auto = [g for g in auto if g["group_id"] not in rejected]
    all_groups = auto + manual_groups
    _write_file(all_groups, rejected)
    return all_groups


def update_group(group_id: str, *, label: Optional[str] = None,
                 members: Optional[list[dict]] = None) -> Optional[dict]:
    """Update a mapping group's label or members."""
    groups = _read_file()
    for g in groups:
        if g.get("group_id") == group_id:
            if label is not None:
                g["label"] = label
            if members is not None:
                g["members"] = members
            _write_file(groups)
            return g
    return None


def remove_group(group_id: str) -> bool:
    """Remove a group and mark it as rejected so it won't re-appear on
    the next auto-match rebuild."""
    groups = _read_file()
    rejected = _read_rejected()
    filtered = [g for g in groups if g.get("group_id") != group_id]
    if len(filtered) == len(groups):
        return False
    rejected.add(group_id)
    _write_file(filtered, rejected)
    return True
