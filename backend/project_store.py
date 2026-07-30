"""Persistent list of project directories the user has opened, per node.

Stored at ba_home()/projects.json.

Schema v2 (current) — `{version: 2, projects: [{path, node_id, name,
git_remote, created_at, last_used}]}`. Multi-machine: each project lives
under exactly one node (the machine where its files reside). On first
read without a file present, seeds the list from any existing session
cwds + their session.node_id so users don't lose context after upgrade.

Schema v1 (legacy) — a bare list of `{path, name, created_at,
last_used}` (no node_id). Legacy rows are migrated to v2 with
`node_id="primary"`, while a copy is retained at `projects.v1.bak.json`.
"""

import json
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from json_store import read_json, write_json

from node_path_authority import NodePathError, canonical_node_path, node_path_name
from paths import ba_home
from session_manager import manager as session_manager

SCHEMA_VERSION = 3
_migration_lock = threading.Lock()

class ProjectStoreError(RuntimeError):
    """Raised when projects.json is on a schema we cannot read."""


def _projects_path() -> Path:
    return ba_home() / "projects.json"


def _now() -> str:
    return datetime.now().isoformat()


def _backup_path() -> Path:
    return _projects_path().with_name("projects.v1.bak.json")


def _v2_backup_path() -> Path:
    return _projects_path().with_name("projects.v2.bak.json")


def _v2_migration_path() -> Path:
    return _projects_path().with_name("projects.v2.migration.json")


def _v2_quarantine_path() -> Path:
    return _projects_path().with_name("projects.v2.quarantine.json")


def _git(path: str, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None


def probe_git_remote(path: str) -> tuple[bool, Optional[str]]:
    """``(resolved, url)`` for a checkout's remote.

    ``resolved`` is False when the checkout could not be inspected at all
    (path missing, unreachable volume, git unavailable) — callers must keep
    whatever they already stored rather than treat that as "no remote".
    ``resolved`` is True with ``None`` when the directory exists but has no
    remote, so a stored value can be cleared.

    Prefers ``origin``, else the first configured remote: a repo whose remote
    is named something else (e.g. ``github``) previously resolved to nothing
    and kept a stale URL forever, which mis-groups projects across machines.
    """
    if not path or not Path(path).is_dir():
        return (False, None)
    listed = _git(path, "remote")
    if listed is None:
        return (False, None)
    if listed.returncode != 0:
        # The directory exists but git won't treat it as a repository.
        return (True, None)
    names = [name.strip() for name in listed.stdout.splitlines() if name.strip()]
    if not names:
        return (True, None)
    chosen = "origin" if "origin" in names else names[0]
    resolved = _git(path, "remote", "get-url", chosen)
    if resolved is None:
        return (False, None)
    if resolved.returncode != 0:
        return (True, None)
    return (True, resolved.stdout.strip() or None)


def ensure_git_remote(project_path: str) -> Optional[str]:
    """Return the git remote URL for a local project, or None."""
    return probe_git_remote(project_path)[1]


def _string_or_now(value: object) -> str:
    return value if isinstance(value, str) and value else _now()


def _record_from_v1(raw: object) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    norm = _normalize(raw.get("path", ""), "primary")
    if not norm:
        return None
    name = raw.get("name")
    return {
        "path": norm,
        "node_id": "primary",
        "name": name if isinstance(name, str) and name else node_path_name(norm, "primary") or norm,
        "git_remote": ensure_git_remote(norm),
        "created_at": _string_or_now(raw.get("created_at")),
        "last_used": _string_or_now(raw.get("last_used")),
    }


def _v1_records(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        record = _record_from_v1(item)
        if not record:
            continue
        key = (record["node_id"], record["path"])
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def _merge_missing_projects(projects: list[dict], candidates: list[dict]) -> bool:
    seen = {
        (p.get("node_id") or "primary", p.get("path") or "")
        for p in projects
    }
    changed = False
    for candidate in candidates:
        key = (candidate.get("node_id") or "primary", candidate.get("path") or "")
        if key in seen:
            continue
        projects.append(candidate)
        seen.add(key)
        changed = True
    return changed


def _read_json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _migrate_legacy_or_raise() -> None:
    """Migrate an on-disk v1 project list. Idempotent on absent/v2."""
    with _migration_lock:
        _migrate_legacy_or_raise_locked()


def _migrate_legacy_or_raise_locked() -> None:
    _resume_pending_v2_migration()
    path = _projects_path()
    if not path.exists():
        return
    try:
        raw = _read_json_file(path)
    except json.JSONDecodeError:
        # Empty or corrupt — let read_json's default kick in later.
        return
    if isinstance(raw, dict) and raw.get("version") == SCHEMA_VERSION:
        return
    if isinstance(raw, dict) and raw.get("version") == 2:
        projects = raw.get("projects")
        if not isinstance(projects, list):
            raise ProjectStoreError("project_store: v2 projects must be a list")
        repaired, rewrites, quarantined = _repair_v2_projects(projects)
        try:
            shutil.copyfile(path, _v2_backup_path())
            if quarantined:
                write_json(_v2_quarantine_path(), {"records": quarantined})
            write_json(
                _v2_migration_path(),
                {
                    "rewrites": [
                        {"node_id": node_id, "from": old_path, "to": new_path}
                        for (node_id, old_path), new_path in rewrites.items()
                    ],
                    "quarantined": quarantined,
                },
            )
            _write_file(repaired)
            _rewrite_project_references(rewrites, repaired)
            _v2_migration_path().unlink(missing_ok=True)
        except OSError as e:
            raise RuntimeError(f"project_store: v2 migration failed: {e}") from e
        return
    if isinstance(raw, list):
        bak = _backup_path()
        try:
            shutil.copyfile(path, bak)
            _write_file(_v1_records(raw))
        except OSError as e:
            raise RuntimeError(
                f"project_store: legacy v1 projects.json detected but migration "
                f"failed: {e}. Manually back up {path} to {bak}."
            ) from e
        return
    raise ProjectStoreError(
        f"project_store: unsupported shape {type(raw).__name__!r} in "
        f"{path}. Move it aside and restart."
    )


_v1_backup_records_cache: list[dict] | None = None
_deleted_legacy_keys_cache: tuple[tuple[int, int], set[tuple[str, str]]] | None = None
_list_projects_cache: tuple[tuple[int, int], tuple[int, int], list[dict]] | None = None


def _v1_backup_records() -> list[dict]:
    """Parse the v1 backup once per process — each row shells out to git
    to discover its remote. The backup is write-once (created during the
    v1→v2 migration), so caching it for the process lifetime is safe."""
    global _v1_backup_records_cache
    if _v1_backup_records_cache is None:
        bak = _backup_path()
        try:
            raw = _read_json_file(bak) if bak.exists() else []
        except (json.JSONDecodeError, OSError):
            raw = []
        _v1_backup_records_cache = _v1_records(raw)
    return _v1_backup_records_cache


def _legacy_deletions_path() -> Path:
    return _projects_path().with_name("projects.deleted.json")


def _read_legacy_deletions() -> list[dict]:
    try:
        raw = _read_json_file(_legacy_deletions_path())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _write_legacy_deletions(rows: list[dict]) -> None:
    global _deleted_legacy_keys_cache
    write_json(_legacy_deletions_path(), rows)
    _deleted_legacy_keys_cache = None


def _legacy_deletions_fingerprint() -> tuple[int, int]:
    try:
        st = _legacy_deletions_path().stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _projects_fingerprint() -> tuple[int, int]:
    try:
        st = _projects_path().stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _deleted_legacy_keys() -> set[tuple[str, str]]:
    """Paths the user removed — repair must not resurrect them."""
    global _deleted_legacy_keys_cache
    fingerprint = _legacy_deletions_fingerprint()
    cached = _deleted_legacy_keys_cache
    if cached is not None and cached[0] == fingerprint:
        return set(cached[1])
    out: set[tuple[str, str]] = set()
    for r in _read_legacy_deletions():
        if not isinstance(r, dict):
            continue
        node_id = r.get("node_id") or "primary"
        norm = _normalize(r.get("path", ""), node_id)
        if norm:
            out.add((r.get("node_id") or "primary", norm))
    _deleted_legacy_keys_cache = (fingerprint, set(out))
    return out


def _set_legacy_deletion(node_id: str, path: str) -> None:
    rows = _read_legacy_deletions()
    existing = {
        (r.get("node_id") or "primary", r.get("path"))
        for r in rows if isinstance(r, dict)
    }
    if (node_id, path) in existing:
        return
    rows.append({"node_id": node_id, "path": path})
    _write_legacy_deletions(rows)


def _clear_legacy_deletion(node_id: str, path: str) -> None:
    rows = _read_legacy_deletions()
    filtered = [
        r for r in rows
        if not (
            isinstance(r, dict)
            and (r.get("node_id") or "primary") == node_id
            and r.get("path") == path
        )
    ]
    if len(filtered) != len(rows):
        _write_legacy_deletions(filtered)


def _repair_from_legacy_backup(projects: list[dict]) -> list[dict]:
    """Idempotently restore any v1-backup project missing from the
    current list, except ones the user explicitly deleted.

    Runs on every read rather than under a one-shot marker: that marker
    was written before earlier migration losses had healed, which left
    real projects (e.g. testape) permanently unrecoverable. The
    git-touching backup parse is cached, so the per-read cost is just a
    tombstone-file read plus a set-membership check over cached rows."""
    deleted = _deleted_legacy_keys()
    candidates = [
        c for c in _v1_backup_records()
        if (c.get("node_id") or "primary", c.get("path") or "") not in deleted
    ]
    if candidates and _merge_missing_projects(projects, candidates):
        _write_file(projects)
    return projects


def _read_file() -> list[dict]:
    """Return the projects list (always v2 in-memory shape)."""
    _migrate_legacy_or_raise()
    data = read_json(_projects_path(), {"version": SCHEMA_VERSION, "projects": []})
    if isinstance(data, dict) and data.get("version") == SCHEMA_VERSION:
        projects = data.get("projects") or []
        return _repair_from_legacy_backup(projects) if isinstance(projects, list) else []
    return []


def _write_file(projects: list[dict]) -> None:
    global _list_projects_cache
    write_json(_projects_path(), {"version": SCHEMA_VERSION, "projects": projects})
    _list_projects_cache = None


def _normalize(path: str, node_id: str = "primary") -> Optional[str]:
    if not path:
        return None
    try:
        return canonical_node_path(path, node_id)
    except (NodePathError, OSError, ValueError):
        return None


_EMBEDDED_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")


def _repair_remote_path(path: str, node_id: str) -> Optional[str]:
    if node_id != "primary" and isinstance(path, str):
        match = _EMBEDDED_WINDOWS_ABSOLUTE.search(path)
        if match and match.start() > 0:
            return _normalize(path[match.start():], node_id)
    return _normalize(path, node_id)


def _repair_v2_projects(
    projects: list[dict],
) -> tuple[list[dict], dict[tuple[str, str], str], list[dict]]:
    merged: dict[tuple[str, str], dict] = {}
    rewrites: dict[tuple[str, str], str] = {}
    quarantined: list[dict] = []
    for raw in projects:
        if not isinstance(raw, dict):
            quarantined.append({"reason": "record_not_object", "record": raw})
            continue
        raw_node_id = raw.get("node_id") or "primary"
        if not isinstance(raw_node_id, str):
            quarantined.append({"reason": "node_id_not_string", "record": raw})
            continue
        node_id = raw_node_id
        old_path = raw.get("path")
        if not isinstance(old_path, str):
            quarantined.append({"reason": "path_not_string", "record": raw})
            continue
        path = _repair_remote_path(old_path, node_id)
        if not path:
            quarantined.append({"reason": "path_not_absolute", "record": raw})
            continue
        rewrites[(node_id, old_path)] = path
        key = (node_id, path)
        current = merged.get(key)
        candidate = {
            **raw,
            "path": path,
            "node_id": node_id,
            "name": raw.get("name") or node_path_name(path, node_id) or path,
        }
        if candidate["name"] in {old_path, path}:
            candidate["name"] = node_path_name(path, node_id) or path
        if current is None:
            merged[key] = candidate
            continue
        current["created_at"] = min(
            _string_or_now(current.get("created_at")),
            _string_or_now(candidate.get("created_at")),
        )
        current["last_used"] = max(
            _string_or_now(current.get("last_used")),
            _string_or_now(candidate.get("last_used")),
        )
        if not current.get("git_remote") and candidate.get("git_remote"):
            current["git_remote"] = candidate["git_remote"]
    return list(merged.values()), rewrites, quarantined


def _rewrite_project_references(
    rewrites: dict[tuple[str, str], str],
    projects: list[dict],
) -> None:
    import project_mapping_store
    import native_mcp_grants
    import ui_selection

    ui_selection.rewrite_project_paths(rewrites)
    native_mcp_grants.rewrite_project_paths(rewrites)
    project_mapping_store.rewrite_project_paths(rewrites)
    project_mapping_store.rebuild_and_save(projects)


def _resume_pending_v2_migration() -> None:
    pending = read_json(_v2_migration_path(), {})
    rows = pending.get("rewrites") if isinstance(pending, dict) else None
    if not isinstance(rows, list):
        return
    rewrites: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        node_id = row.get("node_id")
        old_path = row.get("from")
        new_path = row.get("to")
        if all(isinstance(value, str) and value for value in (node_id, old_path, new_path)):
            rewrites[(node_id, old_path)] = new_path
    data = read_json(_projects_path(), {})
    projects = data.get("projects") if isinstance(data, dict) else None
    if isinstance(projects, list):
        _rewrite_project_references(rewrites, projects)
        _v2_migration_path().unlink(missing_ok=True)


def _seed_from_sessions_if_empty() -> list[dict]:
    """One-time bootstrap: pull cwds from existing sessions on first
    read. Carries each session's `node_id` onto its project record so
    multi-machine deploys land the projects under the right machine."""
    existing = _read_file()
    if existing or _projects_path().exists():
        return existing

    import session_store

    seen: dict[tuple[str, str], dict] = {}
    for s in session_manager.list():
        if not session_store.should_auto_register_project(s):
            continue
        node_id = s.get("node_id") or "primary"
        cwd = _normalize(s.get("cwd", ""), node_id)
        if not cwd:
            continue
        key = (node_id, cwd)
        if key in seen:
            continue
        # is_dir check on local filesystem only — remote-node projects
        # come from sessions whose cwd we can't validate without an
        # RPC roundtrip. Skip the check for non-primary nodes: trust
        # the session record (the cwd was validated at session-create
        # time via cwd_roots).
        if node_id == "primary" and not Path(cwd).is_dir():
            continue
        ts = s.get("updated_at") or _now()
        git_remote = ensure_git_remote(cwd) if node_id == "primary" else None
        seen[key] = {
            "path": cwd,
            "node_id": node_id,
            "name": node_path_name(cwd, node_id) or cwd,
            "git_remote": git_remote,
            "created_at": ts,
            "last_used": ts,
        }
    seeded = sorted(seen.values(), key=lambda p: p.get("last_used", ""), reverse=True)
    _write_file(seeded)
    return seeded


def list_projects() -> list[dict]:
    """Return projects sorted by `last_used` descending. Each row
    carries `node_id` so the frontend can group/filter by machine."""
    global _list_projects_cache
    projects_fp = _projects_fingerprint()
    deletions_fp = _legacy_deletions_fingerprint()
    cached = _list_projects_cache
    if cached is not None and cached[0] == projects_fp and cached[1] == deletions_fp:
        return [dict(project) for project in cached[2]]
    projects = (
        _seed_from_sessions_if_empty() if not _projects_path().exists()
        else _read_file()
    )
    projects.sort(key=lambda p: p.get("last_used", ""), reverse=True)
    _list_projects_cache = (
        _projects_fingerprint(),
        _legacy_deletions_fingerprint(),
        [dict(project) for project in projects],
    )
    return [dict(project) for project in projects]


def add_project(
    path: str,
    name: Optional[str] = None,
    *,
    node_id: str = "primary",
) -> Optional[dict]:
    """Upsert a project by (node_id, absolute path). Returns the
    resolved record. `node_id` defaults to the local sentinel so
    single-machine deploys keep their flat namespace."""
    norm = _normalize(path, node_id)
    if not norm:
        return None
    # Local node: create the directory if it doesn't exist yet. Remote
    # nodes can't be mkdir'd from here, so trust the caller's path.
    if node_id == "primary":
        try:
            Path(norm).mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    _clear_legacy_deletion(node_id, norm)
    projects = _read_file()
    now = _now()

    for p in projects:
        if p.get("path") == norm and (p.get("node_id") or "primary") == node_id:
            p["last_used"] = now
            if name:
                p["name"] = name
            p.setdefault("node_id", node_id)
            # Backfill git_remote for existing primary projects
            if "git_remote" not in p and node_id == "primary":
                p["git_remote"] = ensure_git_remote(norm)
            _write_file(projects)
            return p

    git_remote = ensure_git_remote(norm) if node_id == "primary" else None
    record = {
        "path": norm,
        "node_id": node_id,
        "name": name or node_path_name(norm, node_id) or norm,
        "git_remote": git_remote,
        "created_at": now,
        "last_used": now,
    }
    projects.append(record)
    _write_file(projects)
    return record


def touch_project(path: str, *, node_id: str = "primary") -> None:
    """Update last_used for an existing project. No-op if not present."""
    norm = _normalize(path, node_id)
    if not norm:
        return
    projects = _read_file()
    changed = False
    for p in projects:
        if p.get("path") == norm and (p.get("node_id") or "primary") == node_id:
            p["last_used"] = _now()
            changed = True
            break
    if changed:
        _write_file(projects)


def remove_project(path: str, *, node_id: str = "primary") -> bool:
    norm = _normalize(path, node_id)
    if not norm:
        return False
    projects = _read_file()
    filtered = [
        p for p in projects
        if not (
            p.get("path") == norm
            and (p.get("node_id") or "primary") == node_id
        )
    ]
    if len(filtered) == len(projects):
        return False
    _write_file(filtered)
    _set_legacy_deletion(node_id, norm)
    return True


def backfill_git_remotes() -> int:
    """Re-derive git_remote for primary projects from their checkouts.

    The stored value is a snapshot that goes stale when a repo renames or
    replaces its remote, so a differing result overwrites it — including
    clearing it when the checkout no longer has a remote. Checkouts that
    could not be inspected are left untouched, so an offline volume never
    erases a known remote."""
    projects = _read_file() if _projects_path().exists() else _seed_from_sessions_if_empty()
    changed = 0
    for p in projects:
        if (p.get("node_id") or "primary") == "primary":
            resolved, remote = probe_git_remote(p.get("path", ""))
            if resolved and p.get("git_remote") != remote:
                p["git_remote"] = remote
                changed += 1
    if changed:
        _write_file(projects)
    return changed
