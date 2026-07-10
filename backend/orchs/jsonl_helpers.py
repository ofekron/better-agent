"""Provider-aware claude jsonl path helpers.

Strategies need to locate claude session jsonl files on disk by `sid`
and count lines. The path resolution honors the `CLAUDE_CONFIG_DIR`
env var so users with `~/.claude-zai` (or similar) get the right
files. Lives here (under `orchs/`) instead of `orchestrator.py` so
strategy modules don't import a private symbol via a circular-dodge.
"""

from __future__ import annotations

import os
import time
import json
import logging
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_JSONL_PATH_CACHE: dict[tuple[str, str, str], tuple[float, Optional[Path]]] = {}
_JSONL_PATH_NEGATIVE_TTL_S = 5.0
_JSONL_INDEX_TTL_S = 5.0
_CLAUDE_PATH_INDEX: tuple[str, float, dict[str, Path]] | None = None
_CLAUDE_PATH_INDEX_LOCK = threading.Lock()
_RUN_STATE_PATH_CACHE: dict[tuple[str, str], tuple[float, Optional[Path]]] = {}
_JSONL_LINE_COUNT_LOCK = threading.Lock()
_JSONL_LINE_COUNT_CACHE: dict[str, tuple[tuple[int, int, int], int]] = {}
_JSONL_LINE_COUNT_INFLIGHT: dict[str, threading.Lock] = {}
_JSONL_PATH_REVISIONS: dict[str, int] = {}
_JSONL_PATH_LINE_COUNTS: dict[str, int] = {}
_MISSING_JSONL_WARNING_TTL_S = 60.0
_MISSING_JSONL_WARNED_AT: dict[tuple[str, str, str], float] = {}


def note_jsonl_append(path: Path, line_count: int) -> None:
    key = str(path)
    count = int(line_count)
    with _JSONL_LINE_COUNT_LOCK:
        if _JSONL_PATH_LINE_COUNTS.get(key) == count:
            return
        _JSONL_PATH_LINE_COUNTS[key] = count
        _JSONL_PATH_REVISIONS[key] = _JSONL_PATH_REVISIONS.get(key, 0) + 1


def notify_jsonl_appended(path: Path) -> None:
    key = str(path)
    with _JSONL_LINE_COUNT_LOCK:
        _JSONL_PATH_REVISIONS[key] = _JSONL_PATH_REVISIONS.get(key, 0) + 1


def path_revision(path: Path) -> int:
    with _JSONL_LINE_COUNT_LOCK:
        return _JSONL_PATH_REVISIONS.get(str(path), 0)


def path_revision_token(paths: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    with _JSONL_LINE_COUNT_LOCK:
        return tuple((path, _JSONL_PATH_REVISIONS.get(path, 0)) for path in paths)


def _claude_projects_dir() -> Path:
    """Resolve <CLAUDE_CONFIG_DIR>/projects, defaulting to ~/.claude.

    Computed per-call rather than at import time so test rigs can flip
    the env mid-process.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    base = Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / ".claude"
    return base / "projects"


def _cache_key(agent_sid: str) -> tuple[str, str, str]:
    try:
        from runs_dir import runs_root

        runs = str(runs_root())
    except Exception:
        runs = ""
    return (str(_claude_projects_dir()), runs, agent_sid)


def _cached_path(agent_sid: str) -> tuple[bool, Optional[Path]]:
    key = _cache_key(agent_sid)
    cached = _JSONL_PATH_CACHE.get(key)
    if cached is None:
        return False, None
    ts, path = cached
    if path is None:
        if time.monotonic() - ts < _JSONL_PATH_NEGATIVE_TTL_S:
            return True, None
        _JSONL_PATH_CACHE.pop(key, None)
        return False, None
    if path.exists():
        return True, path
    _JSONL_PATH_CACHE.pop(key, None)
    return False, None


def _cache_existing_path(agent_sid: str, path: Path) -> Path:
    _JSONL_PATH_CACHE[_cache_key(agent_sid)] = (time.monotonic(), path)
    return path


def _cache_missing_path(agent_sid: str) -> None:
    _JSONL_PATH_CACHE[_cache_key(agent_sid)] = (time.monotonic(), None)


def _encoded_cwd_path(cwd: str, agent_sid: str) -> Optional[Path]:
    if not cwd:
        return None
    try:
        from paths import encode_cwd
    except Exception:
        return None
    return _claude_projects_dir() / encode_cwd(cwd) / f"{agent_sid}.jsonl"


def _session_encoded_cwd_path(session: dict, cwd: str, agent_sid: str) -> Optional[Path]:
    if not cwd:
        return None
    try:
        from paths import claude_projects_root_for_session, encode_cwd
    except Exception:
        return None
    return claude_projects_root_for_session(session) / encode_cwd(cwd) / f"{agent_sid}.jsonl"


def _claude_path_index() -> dict[str, Path]:
    global _CLAUDE_PATH_INDEX
    projects = _claude_projects_dir()
    key = str(projects)
    now = time.monotonic()
    if _CLAUDE_PATH_INDEX is not None:
        cached_key, ts, cached = _CLAUDE_PATH_INDEX
        if cached_key == key and now - ts < _JSONL_INDEX_TTL_S:
            return cached
    if not _CLAUDE_PATH_INDEX_LOCK.acquire(blocking=False):
        if _CLAUDE_PATH_INDEX is not None:
            cached_key, _ts, cached = _CLAUDE_PATH_INDEX
            if cached_key == key:
                return cached
        with _CLAUDE_PATH_INDEX_LOCK:
            if _CLAUDE_PATH_INDEX is None:
                return {}
            cached_key, _ts, cached = _CLAUDE_PATH_INDEX
            return cached if cached_key == key else {}
    indexed: dict[str, Path] = {}
    try:
        try:
            for path in projects.glob("*/*.jsonl"):
                indexed.setdefault(path.stem, path)
        except OSError:
            indexed = {}
        _CLAUDE_PATH_INDEX = (key, now, indexed)
        return indexed
    finally:
        _CLAUDE_PATH_INDEX_LOCK.release()


def _run_state_cache_get(root_key: str, agent_sid: str) -> tuple[bool, Optional[Path]]:
    now = time.monotonic()
    cached = _RUN_STATE_PATH_CACHE.get((root_key, agent_sid))
    if cached is None:
        return False, None
    ts, path = cached
    if path is None:
        if now - ts < _JSONL_PATH_NEGATIVE_TTL_S:
            return True, None
        _RUN_STATE_PATH_CACHE.pop((root_key, agent_sid), None)
        return False, None
    if path.exists():
        return True, path
    _RUN_STATE_PATH_CACHE.pop((root_key, agent_sid), None)
    return False, None


def _run_state_cache_put(root_key: str, agent_sid: str, path: Optional[Path]) -> Optional[Path]:
    _RUN_STATE_PATH_CACHE[(root_key, agent_sid)] = (time.monotonic(), path)
    return path


def _state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    from runs_dir import state_files_for_sid
    return state_files_for_sid(root, agent_sid)


def _run_state_path_for_sid(agent_sid: str) -> Optional[Path]:
    try:
        from runs_dir import runs_root
    except Exception:
        return None
    root = runs_root()
    key = str(root)
    cached_hit, cached = _run_state_cache_get(key, agent_sid)
    if cached_hit:
        return cached
    if not root.is_dir():
        return _run_state_cache_put(key, agent_sid, None)
    newest: tuple[float, Path] | None = None
    for state_path in _state_files_for_sid(root, agent_sid):
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(st.get("session_id") or "") != agent_sid:
            continue
        jp_str = st.get("jsonl_path")
        candidate = Path(jp_str) if jp_str else state_path.parent / "session_events.jsonl"
        if not candidate.exists():
            continue
        try:
            mt = state_path.stat().st_mtime
        except OSError:
            mt = 0.0
        if newest is None or (mt, str(candidate)) > (newest[0], str(newest[1])):
            newest = (mt, candidate)
    return _run_state_cache_put(key, agent_sid, newest[1] if newest is not None else None)


def compute_jsonl_path(cwd: str, agent_sid: str) -> Optional[Path]:
    """Locate the session jsonl for ANY provider on this process's
    local disk by agent_sid.

    Pure local-disk resolution. INVARIANT: this helper has ONE semantic
    (local file path). Callers that need to read a remote worker's
    jsonl from primary MUST go through `compute_jsonl_read_path`
    (which routes via the session's `node_id`).

    Resolution order:
      1. Claude's projects dir: `<claude config>/projects/*/<sid>.jsonl`
         — claude CLI's encoded-cwd dirname rules don't always match
         worker_store.encode_cwd, and the sid is unique, so the glob
         is the source of truth.
      2. Gemini run-dir scan: `<ba_home>/runs/*/state.json` carrying
         that sid → that run's `session_events.jsonl`. Per-run files,
         so we have to scan; cheap because there are few in-flight
         runs and `state.json` is small.

    Returns the FIRST hit found across providers. Cwd arg is
    informational only.
    """
    _ = cwd
    cached_hit, cached = _cached_path(agent_sid)
    if cached_hit:
        return cached
    encoded_path = _encoded_cwd_path(cwd, agent_sid)
    if encoded_path is not None and encoded_path.exists():
        return _cache_existing_path(agent_sid, encoded_path)
    claude_index = _claude_path_index()
    claude_path = claude_index.get(agent_sid)
    if claude_path is not None and claude_path.exists():
        return _cache_existing_path(agent_sid, claude_path)
    # Gemini path — scan run dirs for one whose state.json discovered
    # this agent_sid; the runner writes the discovered sid into
    # state.json at init time. Resumed turns reuse the same sid across
    # multiple run dirs, so we collect ALL matches and return the
    # newest (by state.json mtime) — that's the most-recent turn whose
    # events.jsonl the supervisor / replay actually wants.
    run_path = _run_state_path_for_sid(agent_sid)
    if run_path is not None and run_path.exists():
        return _cache_existing_path(agent_sid, run_path)
    _warn_missing_jsonl(cwd, agent_sid, encoded_path, claude_index)
    _cache_missing_path(agent_sid)
    return None


def _warn_missing_jsonl(
    cwd: str,
    agent_sid: str,
    encoded_path: Optional[Path],
    claude_index: dict[str, Path],
) -> None:
    key = _cache_key(agent_sid)
    now = time.monotonic()
    last = _MISSING_JSONL_WARNED_AT.get(key)
    if last is not None and now - last < _MISSING_JSONL_WARNING_TTL_S:
        return
    _MISSING_JSONL_WARNED_AT[key] = now
    log.warning(
        "ingestion: no jsonl located for agent_sid=%s cwd=%r — tried "
        "encoded_cwd=%s (exists=%s); claude index=%d entries, run-state path cache=%d "
        "entries under projects=%s. Events for this sid will NOT ingest.",
        agent_sid,
        cwd,
        encoded_path,
        bool(encoded_path is not None and encoded_path.exists()),
        len(claude_index),
        len(_RUN_STATE_PATH_CACHE),
        _claude_projects_dir(),
    )


def compute_jsonl_read_path(
    cwd: str,
    agent_sid: str,
    session: Optional[dict] = None,
) -> Optional[Path]:
    """Resolve the jsonl path a CALLER ON PRIMARY should `Read()`.

    For local-pinned sessions: behaves exactly like `compute_jsonl_path`
    (real claude jsonl on primary's disk).

    For remote-pinned sessions: returns the shadow-jsonl path that
    `shadow_jsonl.append()` writes to as the node streams raw lines
    over WS. Manager's standard `Read` tool sees a regular local file.

    `session` is the Better Agent session record carrying the `node_id` field.
    If omitted, falls back to local resolution — keeps single-machine
    callers unchanged.
    """
    if session is None:
        return compute_jsonl_path(cwd, agent_sid)
    node_id = session.get("node_id") or "primary"
    try:
        from topology import local_node_id
        here = local_node_id()
    except Exception:
        here = "primary"
    if node_id == here:
        encoded_path = _session_encoded_cwd_path(session, cwd, agent_sid)
        if encoded_path is not None and encoded_path.exists():
            return _cache_existing_path(agent_sid, encoded_path)
        return compute_jsonl_path(cwd, agent_sid)
    # Remote: shadow path. Lazy import — shadow_jsonl pulls in
    # asyncio etc. that the headless helpers shouldn't need.
    import shadow_jsonl
    from session_manager import manager as _sm
    root_id = _sm._root_id_for(session["id"]) or session["id"]
    path = shadow_jsonl.shadow_path(root_id, agent_sid)
    return path if path.exists() else None


def count_jsonl_lines(path: Path) -> int:
    try:
        stat = path.stat()
    except OSError:
        return 0
    fingerprint = (int(stat.st_mtime_ns), int(stat.st_size), int(getattr(stat, "st_ino", 0)))
    key = str(path)
    with _JSONL_LINE_COUNT_LOCK:
        cached = _JSONL_LINE_COUNT_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        path_lock = _JSONL_LINE_COUNT_INFLIGHT.get(key)
        if path_lock is None:
            path_lock = threading.Lock()
            _JSONL_LINE_COUNT_INFLIGHT[key] = path_lock
    with path_lock:
        with _JSONL_LINE_COUNT_LOCK:
            cached = _JSONL_LINE_COUNT_CACHE.get(key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
        try:
            with path.open("rb") as f:
                count = sum(1 for _ in f)
        except OSError:
            return 0
        with _JSONL_LINE_COUNT_LOCK:
            _JSONL_LINE_COUNT_CACHE[key] = (fingerprint, count)
            if len(_JSONL_LINE_COUNT_INFLIGHT) > 512:
                active = {
                    cache_key
                    for cache_key in _JSONL_LINE_COUNT_CACHE
                }
                for lock_key in list(_JSONL_LINE_COUNT_INFLIGHT):
                    if lock_key not in active:
                        _JSONL_LINE_COUNT_INFLIGHT.pop(lock_key, None)
        note_jsonl_append(path, count)
        return count


def jsonl_byte_size(path: Optional[Path]) -> int:
    """Byte length of a jsonl file (0 if absent). Used for byte-offset deltas:
    the caller samples the fork's new output via `tail -c +<size+1>`, which
    seeks in O(1) instead of Read's line-count scan."""
    if path is None or not path.exists():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0
