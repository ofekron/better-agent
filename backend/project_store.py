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
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from json_store import read_json, write_json

import git_repo_info
from paths import ba_home
from session_manager import manager as session_manager

SCHEMA_VERSION = 2

class ProjectStoreError(RuntimeError):
    """Raised when projects.json is on a schema we cannot read."""


def _projects_path() -> Path:
    return ba_home() / "projects.json"


def _now() -> str:
    return datetime.now().isoformat()


def _backup_path() -> Path:
    return _projects_path().with_name("projects.v1.bak.json")


def _discover_git_remote(path: str) -> Optional[str]:
    """Shell out to git to discover the origin remote URL."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url or None
    except Exception:
        pass
    return None


def ensure_git_remote(project_path: str) -> Optional[str]:
    """Return the git origin remote URL for a local project."""
    return _discover_git_remote(project_path)


def _string_or_now(value: object) -> str:
    return value if isinstance(value, str) and value else _now()


def _record_from_v1(raw: object) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    norm = _normalize(raw.get("path", ""))
    if not norm:
        return None
    name = raw.get("name")
    return {
        "path": norm,
        "node_id": "primary",
        "name": name if isinstance(name, str) and name else Path(norm).name or norm,
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
        norm = _normalize(r.get("path", ""))
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


def _normalize(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        return str(Path(path).expanduser().resolve())
    except Exception:
        return None


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
        cwd = _normalize(s.get("cwd", ""))
        if not cwd:
            continue
        node_id = s.get("node_id") or "primary"
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
            "name": Path(cwd).name or cwd,
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


def _is_gitdir(path: str) -> bool:
    """True when `path` is a git internal directory (a bare gitdir or a
    submodule's `.git/modules/<name>`), not a real working checkout. Git
    reports such paths as the "main worktree" for submodules, so callers
    must skip them when looking for a real checkout."""
    if not path:
        return False
    if path.endswith("/.git") or path.endswith("/.git/"):
        return True
    return "/.git/" in path


def _resolve_canonical(member_paths: list[str]) -> str:
    """Pick the stable canonical checkout for a repo group.

    Prefer git's main worktree, but only when it is a real working
    directory — submodules report their `.git/modules` gitdir as the
    "main worktree", which is not a checkout. Fall back to the first
    member path that is a real, non-gitdir directory."""
    for path in member_paths:
        mw = git_repo_info.main_worktree(path)
        if mw and not _is_gitdir(mw) and Path(mw).is_dir():
            return mw
    for path in member_paths:
        if path and not _is_gitdir(path) and Path(path).is_dir():
            return path
    return member_paths[0] if member_paths else ""


def _existing_worktrees(canonical: str) -> list[dict]:
    """Worktree entries for the repo at `canonical`, dropping paths that
    no longer exist on disk (stale temp worktrees left behind by aborted
    rebases/cherry-picks) and git-internal gitdirs (submodules list their
    `.git/modules` dir as the main worktree). The canonical checkout is
    always present in the result. Returns a single-entry list if
    `canonical` is not actually a git repo."""
    entries = git_repo_info.worktree_entries(canonical)
    out: list[dict] = []
    if entries:
        for entry in entries:
            wt_path = entry.get("path") or ""
            if not wt_path or _is_gitdir(wt_path):
                continue
            # Existence checked on the local filesystem only; remote-node
            # projects are not grouped here.
            if not Path(wt_path).is_dir():
                continue
            out.append({
                "path": wt_path,
                "branch": entry.get("branch"),
                "name": git_repo_info.worktree_name(wt_path),
                "is_main": bool(entry.get("is_main")),
            })
    # Ensure the canonical checkout itself is represented (for submodules
    # git omits the main checkout and lists only the gitdir + siblings).
    # The canonical is always the main worktree.
    if canonical and not _is_gitdir(canonical) and Path(canonical).is_dir():
        canonical_entry = next((w for w in out if w["path"] == canonical), None)
        if canonical_entry is None:
            out.insert(0, {
                "path": canonical,
                "branch": None,
                "name": git_repo_info.worktree_name(canonical),
                "is_main": True,
            })
        for w in out:
            w["is_main"] = w["path"] == canonical
    if not out:
        out = [{
            "path": canonical,
            "branch": None,
            "name": Path(canonical).name or canonical,
            "is_main": True,
        }]
    return out


def list_projects_grouped() -> list[dict]:
    """Return projects grouped by git repo, sorted by `last_used` desc.

    Every checked-out worktree of one repo collapses into a single
    project record. The record's `path` is the repo's main worktree (the
    stable canonical identity also used for session matching); `worktrees`
    lists every existing worktree so the UI can offer a per-worktree
    selector. Non-git projects form a group of one. Grouping is COMPUTED
    over the flat `projects.json` list (never stored), so a fresh
    `git worktree add` shows up automatically once the git TTL cache
    expires."""
    flat = list_projects()
    # Group key: (node_id, common_dir_or_path). Same common dir ⇒ same repo.
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in flat:
        node_id = p.get("node_id") or "primary"
        path = p.get("path") or ""
        common = git_repo_info.repo_common_dir(path) if path else None
        gkey = (node_id, common or path)
        groups.setdefault(gkey, []).append(p)

    out: list[dict] = []
    for (node_id, _gkey), members in groups.items():
        canonical = _resolve_canonical([m.get("path") or "" for m in members])
        worktrees = _existing_worktrees(canonical)
        # Prefer the canonical member's registered name; fall back to the
        # most-recently-used member, then the canonical dir name.
        name = next(
            (m.get("name") for m in members if m.get("path") == canonical
             and m.get("name")),
            None,
        )
        if not name:
            members_sorted = sorted(
                members, key=lambda m: m.get("last_used", ""), reverse=True,
            )
            fallback_name = (
                members_sorted[0].get("name")
                if members_sorted and members_sorted[0].get("name")
                else None
            )
            name = fallback_name or Path(canonical).name or canonical
        git_remote = next(
            (m.get("git_remote") for m in members if m.get("git_remote")), None,
        )
        created_at = min(
            (m.get("created_at") or _now() for m in members), default=_now(),
        )
        last_used = max(
            (m.get("last_used") or _now() for m in members), default=_now(),
        )
        out.append({
            "path": canonical,
            "node_id": node_id,
            "name": name,
            "git_remote": git_remote,
            "created_at": created_at,
            "last_used": last_used,
            "worktrees": worktrees,
        })
    out.sort(key=lambda p: p.get("last_used", ""), reverse=True)
    return out


def add_project(
    path: str,
    name: Optional[str] = None,
    *,
    node_id: str = "primary",
) -> Optional[dict]:
    """Upsert a project by (node_id, absolute path). Returns the
    resolved record. `node_id` defaults to the local sentinel so
    single-machine deploys keep their flat namespace."""
    norm = _normalize(path)
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
        "name": name or Path(norm).name or norm,
        "git_remote": git_remote,
        "created_at": now,
        "last_used": now,
    }
    projects.append(record)
    _write_file(projects)
    return record


def touch_project(path: str, *, node_id: str = "primary") -> None:
    """Update last_used for an existing project. No-op if not present."""
    norm = _normalize(path)
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
    norm = _normalize(path)
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
    """Ensure git_remote is present in the BC-home project registry."""
    projects = _read_file() if _projects_path().exists() else _seed_from_sessions_if_empty()
    changed = 0
    for p in projects:
        if (p.get("node_id") or "primary") == "primary":
            remote = ensure_git_remote(p.get("path", ""))
            if remote and p.get("git_remote") != remote:
                p["git_remote"] = remote
                changed += 1
    if changed:
        _write_file(projects)
    return changed
