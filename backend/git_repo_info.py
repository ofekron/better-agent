"""Git repo / worktree resolution for project grouping.

A Better Agent "project" is a directory, but a single git repo can be
checked out into multiple *worktrees* (e.g. a `dev` checkout and a
`main` checkout side by side). The project UI groups every worktree of
one repo under a single project tab, with a worktree selector below it.

Two facts tie a directory to its repo, both obtained by shelling out to
git (the frontend cannot run git, so all resolution lives here and is
projected through `/api/projects`):

  • `repo_common_dir(path)` — the repo's shared common dir. Every
    worktree of the same repo resolves to the SAME common dir, so it is
    the stable identity used to group projects and to decide which
    project a session belongs to. Nested repos (a separate repo cloned
    inside a worktree) resolve to a DIFFERENT common dir, so they are
    never wrongly folded into the parent.

  • `worktree_entries(path)` — every checked-out worktree of the repo
    (`git worktree list --porcelain`), each with its branch. New
    worktrees appear automatically without re-registering a project.

Both are cached with a short TTL: worktrees change rarely, but when they
do (a fresh `git worktree add`) the cache expires within the TTL so the
UI self-heals. Negative results (non-git directories) are cached too so
a path under `/tmp` does not shell out on every session-list match.
"""

from __future__ import annotations

import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Optional

# Cache TTL in seconds. Short enough that a new worktree shows up within
# a minute, long enough that a session-list scan over hundreds of rows
# does not re-shell-out per row.
_TTL_SECONDS = 60.0

_common_dir_cache: dict[str, tuple[float, Optional[str]]] = {}
_worktrees_cache: dict[str, tuple[float, Optional[list[dict]]]] = {}
_cache_lock = threading.Lock()
_cache_generation = 0


def _now() -> float:
    import time
    return time.monotonic()


def _run_git(args: list[str], cwd: str, timeout: float = 5.0) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _normalize(path: str) -> str:
    """Absolute, resolved, NFC-normalized path. Used as the cache key so
    `foo/` and `/private/var/foo` (macOS firmlinks) collapse together."""
    try:
        resolved = str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return path
    return unicodedata.normalize("NFC", resolved)


def repo_common_dir_with_expiry(
    path: str,
) -> tuple[Optional[str], float, int]:
    norm = _normalize(path)
    while True:
        now = _now()
        with _cache_lock:
            generation = _cache_generation
            cached = _common_dir_cache.get(norm)
            if cached and now - cached[0] < _TTL_SECONDS:
                return cached[1], cached[0] + _TTL_SECONDS, generation
        raw = _run_git(["rev-parse", "--git-common-dir"], norm)
        common: Optional[str] = None
        if raw is not None:
            candidate = raw.strip()
            if candidate:
                p = Path(candidate)
                if not p.is_absolute():
                    p = Path(norm) / p
                try:
                    common = unicodedata.normalize("NFC", str(p.resolve()))
                except (OSError, RuntimeError):
                    common = unicodedata.normalize("NFC", str(p))
        published_at = _now()
        with _cache_lock:
            if generation != _cache_generation:
                continue
            _common_dir_cache[norm] = (published_at, common)
            return common, published_at + _TTL_SECONDS, generation


def repo_common_dir(path: str) -> Optional[str]:
    """Return the resolved absolute git common dir for the repo
    containing `path`, or None if `path` is not inside a git repo.

    Every worktree of the same repo returns the same value, so this is
    the canonical repo identity for grouping and session matching."""
    return repo_common_dir_with_expiry(path)[0]


def cache_generation_snapshot() -> int:
    with _cache_lock:
        return _cache_generation


def worktree_entries(path: str) -> Optional[list[dict]]:
    """Return every checked-out worktree of the repo at `path`:

        [{"path": <abs>, "branch": <name|None>, "is_main": <bool>}]

    The first entry is the main worktree (the one holding the `.git`
    dir). Returns None if `path` is not inside a git repo."""
    norm = _normalize(path)
    while True:
        now = _now()
        with _cache_lock:
            generation = _cache_generation
            cached = _worktrees_cache.get(norm)
            if cached and now - cached[0] < _TTL_SECONDS:
                return cached[1]
        raw = _run_git(["worktree", "list", "--porcelain"], norm)
        entries: Optional[list[dict]] = None
        if raw is not None:
            entries = []
            current: Optional[dict] = None
            for line in raw.splitlines():
                if not line:
                    if current is not None:
                        entries.append(current)
                        current = None
                    continue
                if line.startswith("worktree "):
                    wt = unicodedata.normalize("NFC", line[len("worktree "):])
                    current = {"path": wt, "branch": None, "is_main": False}
                elif current is not None and line.startswith("branch "):
                    ref = line[len("branch "):]
                    current["branch"] = ref.rsplit("/", 1)[-1] if ref else None
                elif current is not None and line.startswith("detached"):
                    current["branch"] = None
            if current is not None:
                entries.append(current)
            for i, entry in enumerate(entries):
                entry["is_main"] = i == 0
        with _cache_lock:
            if generation != _cache_generation:
                continue
            _worktrees_cache[norm] = (_now(), entries)
            return entries


def worktree_roots(path: str) -> Optional[list[str]]:
    """Convenience: just the worktree root paths (no metadata)."""
    entries = worktree_entries(path)
    if entries is None:
        return None
    return [e["path"] for e in entries]


def main_worktree(path: str) -> Optional[str]:
    """The main worktree path (holding `.git`) for the repo at `path`, or
    None if not a git repo. Used as the stable canonical project path."""
    entries = worktree_entries(path)
    if not entries:
        return None
    return entries[0]["path"]


def worktree_name(path: str) -> str:
    """Display name for a worktree root path — its final path segment."""
    return Path(path).name or path


def clear_caches() -> None:
    """Drop all cached git lookups. Tests call this between cases so an
    isolation tempdir's stale negative cache does not leak."""
    global _cache_generation
    with _cache_lock:
        _common_dir_cache.clear()
        _worktrees_cache.clear()
        _cache_generation += 1
