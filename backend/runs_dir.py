"""Per-run directory + atomic JSON + pid-liveness helpers.

The "runs" directory holds per-run backend_state.json files written by
each provider's runner-supervision layer. Helpers used to live on
`provider_claude.py` (and a duplicate set on `provider_gemini.py`)
which forced lazy cross-imports and a circular dependency between
the abstract `provider` and concrete `provider_claude`.

INVARIANT: do NOT cache `runs_root()` as a module-level constant —
`ba_home()` is computed per-call so tests/scripts can flip
`BETTER_CLAUDE_HOME` after import without writing to the developer's
real `~/.better-claude/runs`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time
import heapq
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import ba_home

logger = logging.getLogger(__name__)
_RUN_STATE_LEDGER_NAME = "run_state_index.jsonl"
_RUN_STATE_LEDGER_CACHE_NAME = "run_state_index.cache.sqlite3"
_RUN_STATE_LEDGER_CACHE_VERSION = 2
_RUN_STATE_LEDGER_BACKFILL_MARKER_NAME = "run_state_index.backfilled.json"
_RUN_STATE_LEDGER_BACKFILL_VERSION = 1
_RUN_STATE_APP_INDEX_BACKFILL_MARKER_NAME = "run_state_app_index.backfilled.json"
_RUN_STATE_APP_INDEX_BACKFILL_VERSION = 3
_RECONCILED_MARKER_INDEX_NAME = "reconciled_marker_index.jsonl"
_RECONCILED_MARKER_BACKFILL_MARKER_NAME = "reconciled_marker_index.backfilled.json"
_RECONCILED_MARKER_BACKFILL_VERSION = 1
_RUN_STATE_LEDGER_SEEN: set[tuple[str, str, str]] = set()
_RUN_STATE_LEDGER_APPEND_LOCK = threading.Lock()
_RUN_STATE_LEDGER_BACKFILL_LOCK = threading.Lock()
_RUN_STATE_RECENT_SCAN_LIMIT = 256
_RUN_STATE_RECENT_INDEX_TTL_S = 1.0
_RUN_STATE_RECENT_INDEX_MAX_AGE_S = 30.0
_RUN_STATE_LOOKUP_CACHE_LOCK = threading.Lock()
_RunStateLedgerSignature = tuple[int, int, int, int, int]
_RunStateRootSignature = tuple[int, int, int, int, int]
_RUN_STATE_LEDGER_CACHE: dict[str, tuple[_RunStateLedgerSignature, dict[str, list[tuple[float, str]]]]] = {}
_RUN_STATE_RECENT_INDEX_CACHE: dict[
    str,
    tuple[
        float,
        tuple[tuple[int, int, str], ...],
        dict[str, list[Path]],
        _RunStateRootSignature,
        tuple[str, ...],
    ],
] = {}
_RUN_STATE_RECENT_INDEX_INFLIGHT: dict[str, threading.Event] = {}
_RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT: dict[str, threading.Event] = {}
_RECONCILED_MARKER_BACKFILL_LOCK = threading.Lock()
_RUN_CATALOG_LOCK = threading.RLock()


def runs_root() -> Path:
    return ba_home() / "runs"


def run_state_ledger_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RUN_STATE_LEDGER_NAME


def run_state_ledger_cache_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RUN_STATE_LEDGER_CACHE_NAME


def run_state_ledger_backfill_marker_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RUN_STATE_LEDGER_BACKFILL_MARKER_NAME


def run_state_app_index_backfill_marker_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RUN_STATE_APP_INDEX_BACKFILL_MARKER_NAME


def reconciled_marker_index_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RECONCILED_MARKER_INDEX_NAME


def reconciled_marker_index_backfill_marker_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RECONCILED_MARKER_BACKFILL_MARKER_NAME


@contextmanager
def run_catalog_lock(root: Optional[Path] = None):
    root = root or runs_root()
    lock_path = root / "run_catalog.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    from portable_lock import lock_ex, unlock
    with _RUN_CATALOG_LOCK, lock_path.open("a+b") as lock_file:
        started = time.perf_counter()
        lock_ex(lock_file.fileno())
        try:
            import perf
            perf.record(
                "run_catalog.lock_wait",
                (time.perf_counter() - started) * 1000.0,
            )
            yield
        finally:
            unlock(lock_file.fileno())


def _run_state_ledger_backfill_current(root: Path) -> bool:
    marker = run_state_ledger_backfill_marker_path(root)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("version") == _RUN_STATE_LEDGER_BACKFILL_VERSION


def _run_state_app_index_backfill_current(root: Path) -> bool:
    marker = run_state_app_index_backfill_marker_path(root)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("version") == _RUN_STATE_APP_INDEX_BACKFILL_VERSION


def _reconciled_marker_backfill_current(root: Path) -> bool:
    marker = reconciled_marker_index_backfill_marker_path(root)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("version") == _RECONCILED_MARKER_BACKFILL_VERSION


def _append_run_state_ledger(path: Path, data: dict) -> None:
    if path.name != "state.json":
        return
    session_id = data.get("session_id")
    jsonl_path = data.get("jsonl_path")
    if not session_id or not jsonl_path:
        return
    try:
        with _RUN_STATE_LEDGER_APPEND_LOCK:
            key = (str(path), str(session_id), str(jsonl_path))
            if key in _RUN_STATE_LEDGER_SEEN:
                return
            row = {
                "session_id": str(session_id),
                "jsonl_path": str(jsonl_path),
                "state_path": str(path),
                "written_at": time.time(),
            }
            app_session_id = data.get("app_session_id")
            if isinstance(app_session_id, str) and app_session_id:
                row["app_session_id"] = app_session_id
            ledger = run_state_ledger_path(path.parent.parent)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            if _run_state_ledger_has_key(ledger, key):
                _RUN_STATE_LEDGER_SEEN.add(key)
                return
            prior_signature = _run_state_ledger_signature(ledger)
            with ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
            current_signature = _run_state_ledger_signature(ledger)
            if prior_signature is not None and current_signature is not None:
                _extend_run_state_cache_after_append(
                    ledger.parent,
                    prior_signature,
                    current_signature,
                    row,
                )
            _RUN_STATE_LEDGER_SEEN.add(key)
    except Exception:
        logger.exception("runs_dir: failed to append run-state ledger")


def _backfill_run_state_app_index(root: Path) -> None:
    if _run_state_app_index_backfill_current(root):
        return
    with _RUN_STATE_LEDGER_APPEND_LOCK:
        if _run_state_app_index_backfill_current(root):
            return
        ledger = run_state_ledger_path(root)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        backfill_base = time.time()
        candidates: list[tuple[Path, dict, float]] = []
        try:
            for state_path in root.glob("*/state.json"):
                try:
                    data = json.loads(state_path.read_text(encoding="utf-8"))
                    state_mtime = state_path.stat().st_mtime
                except (OSError, json.JSONDecodeError):
                    continue
                session_id = data.get("session_id")
                jsonl_path = data.get("jsonl_path")
                app_session_id = data.get("app_session_id")
                if not (
                    isinstance(session_id, str)
                    and session_id
                    and isinstance(jsonl_path, str)
                    and jsonl_path
                    and isinstance(app_session_id, str)
                    and app_session_id
                ):
                    continue
                if not _run_state_path_string_has_ledger_shape(str(state_path), root):
                    continue
                candidates.append((state_path, data, state_mtime))
            min_mtime = min((mtime for _, _, mtime in candidates), default=0.0)
            with ledger.open("a", encoding="utf-8") as f:
                for state_path, data, state_mtime in candidates:
                    session_id = data["session_id"]
                    jsonl_path = data["jsonl_path"]
                    app_session_id = data["app_session_id"]
                    try:
                        written_at = backfill_base + (state_mtime - min_mtime)
                    except TypeError:
                        written_at = backfill_base
                    row = {
                        "session_id": session_id,
                        "jsonl_path": jsonl_path,
                        "state_path": str(state_path),
                        "app_session_id": app_session_id,
                        "written_at": written_at,
                    }
                    f.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            return
        write_json(
            run_state_app_index_backfill_marker_path(root),
            {
                "version": _RUN_STATE_APP_INDEX_BACKFILL_VERSION,
                "backfilled_at": time.time(),
            },
        )


def _run_state_path_under_root(path: Path, root_resolved: Path) -> bool:
    if path.name != "state.json":
        return False
    try:
        path.resolve().relative_to(root_resolved)
        return True
    except (OSError, ValueError):
        return False


def _run_state_path_has_ledger_shape(path: Path, root: Path) -> bool:
    if path.name != "state.json" or not path.is_absolute():
        return False
    root_absolute = root.absolute()
    return path.parent != root_absolute and path.parent.parent == root_absolute


def _run_state_path_string_has_ledger_shape(state_path: str, root: Path) -> bool:
    if os.sep != "/" or os.altsep is not None:
        return _run_state_path_has_ledger_shape(Path(state_path), root)
    root_path = str(root.absolute()).rstrip("/")
    prefix = f"{root_path}/"
    if not state_path.startswith(prefix):
        return False
    rest = state_path[len(prefix):]
    if not rest.endswith("/state.json"):
        return False
    run_name = rest[: -len("/state.json")]
    return bool(run_name) and "/" not in run_name


def _run_state_candidate_stat(path: Path, root: Path) -> os.stat_result | None:
    if not _run_state_path_has_ledger_shape(path, root):
        return None
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st


def _root_signature(root: Path) -> _RunStateRootSignature | None:
    try:
        st = root.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _pending_run_dirs_have_state(root: Path, pending_run_dirs: tuple[str, ...]) -> bool:
    for run_dir in pending_run_dirs:
        if _run_state_candidate_stat(root / run_dir / "state.json", root) is not None:
            return True
    return False


def _recent_candidates_unchanged(
    candidates: tuple[tuple[int, int, str], ...],
    root: Path,
) -> bool:
    for mtime_ns, size, state_path in candidates:
        st = _run_state_candidate_stat(Path(state_path), root)
        if st is None or st.st_mtime_ns != mtime_ns or st.st_size != size:
            return False
    return True


def _invalidate_recent_state_index(root: Path) -> None:
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        _RUN_STATE_RECENT_INDEX_CACHE.pop(str(root), None)


def _ledger_state_paths_for_sid(
    index: dict[str, list[tuple[float, str]]],
    agent_sid: str,
    root_resolved: Path,
) -> list[Path]:
    paths: list[Path] = []
    for _, state_path in index.get(agent_sid, []):
        path = Path(state_path)
        if _run_state_path_under_root(path, root_resolved):
            paths.append(path)
    return paths


def _run_state_ledger_signature(path: Path) -> _RunStateLedgerSignature | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _sqlite_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def _run_state_cache_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entries ("
        "sid TEXT NOT NULL,"
        "state_path TEXT NOT NULL,"
        "written_at REAL NOT NULL,"
        "app_session_id TEXT NOT NULL DEFAULT '',"
        "PRIMARY KEY (sid, state_path)"
        ")"
    )
    conn.execute(
        "CREATE INDEX entries_sid_written_at ON entries (sid, written_at)"
    )
    conn.execute(
        "CREATE INDEX entries_app_session_written_at ON entries (app_session_id, written_at)"
    )


def _run_state_cache_signature_current(
    conn: sqlite3.Connection,
    signature: _RunStateLedgerSignature,
) -> bool:
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return False
    expected = {
        "version": _RUN_STATE_LEDGER_CACHE_VERSION,
        "dev": signature[0],
        "ino": signature[1],
        "size": signature[2],
        "mtime_ns": signature[3],
        "ctime_ns": signature[4],
    }
    return rows == expected


def _load_run_state_cache_for_sid(
    root: Path,
    signature: _RunStateLedgerSignature,
    agent_sid: str,
    root_resolved: Path,
) -> list[Path] | None:
    try:
        with _sqlite_connect(run_state_ledger_cache_path(root)) as conn:
            if not _run_state_cache_signature_current(conn, signature):
                return None
            rows = conn.execute(
                "SELECT written_at, state_path FROM entries "
                "WHERE sid=? ORDER BY written_at",
                (agent_sid,),
            ).fetchall()
    except sqlite3.DatabaseError:
        return None
    except OSError:
        return None
    paths: list[Path] = []
    for _written_at, state_path in rows:
        if not isinstance(state_path, str):
            return None
        if not _run_state_path_string_has_ledger_shape(state_path, root):
            return None
        path = Path(state_path)
        if _run_state_path_under_root(path, root_resolved):
            paths.append(path)
    return paths


def _claim_run_state_cache_rebuild(root_key: str) -> tuple[threading.Event, bool]:
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        event = _RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.get(root_key)
        if event is not None:
            return event, False
        event = threading.Event()
        _RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT[root_key] = event
        return event, True


def _finish_run_state_cache_rebuild(root_key: str) -> None:
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        event = _RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.pop(root_key, None)
    if event is not None:
        event.set()


def _write_run_state_cache(
    root: Path,
    signature: _RunStateLedgerSignature,
    index: dict[str, list[tuple[float, str]]],
    app_session_by_state_path: dict[str, str] | None = None,
) -> None:
    cache_path = run_state_ledger_cache_path(root)
    tmp_path = cache_path.with_suffix(".sqlite3.tmp")
    try:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        with _sqlite_connect(tmp_path) as conn:
            _run_state_cache_schema(conn)
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (
                    ("version", _RUN_STATE_LEDGER_CACHE_VERSION),
                    ("dev", signature[0]),
                    ("ino", signature[1]),
                    ("size", signature[2]),
                    ("mtime_ns", signature[3]),
                    ("ctime_ns", signature[4]),
                ),
            )
            rows = [
                (
                    sid,
                    state_path,
                    written_at,
                    (app_session_by_state_path or {}).get(state_path, ""),
                )
                for sid, paths in index.items()
                for written_at, state_path in paths
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO entries (sid, state_path, written_at, app_session_id) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        logger.debug("runs_dir: failed to write run-state ledger cache", exc_info=True)


def _upsert_run_state_index_row(
    index: dict[str, list[tuple[float, str]]],
    row: dict,
) -> None:
    sid = str(row.get("session_id") or "")
    state_path = str(row.get("state_path") or "")
    if not sid or not state_path:
        return
    try:
        written_at = float(row.get("written_at"))
    except (TypeError, ValueError):
        written_at = 0.0
    values = [
        (existing_written_at, existing_state_path)
        for existing_written_at, existing_state_path in index.get(sid, [])
        if existing_state_path != state_path
    ]
    values.append((written_at, state_path))
    values.sort(key=lambda item: item[0])
    index[sid] = values


def _extend_run_state_cache_after_append(
    root: Path,
    prior_signature: _RunStateLedgerSignature,
    current_signature: _RunStateLedgerSignature,
    row: dict,
) -> None:
    state_path = str(row.get("state_path") or "")
    if not _run_state_path_string_has_ledger_shape(state_path, root):
        return
    root_key = str(root)
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        if root_key in _RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT:
            return
        cached = _RUN_STATE_LEDGER_CACHE.get(root_key)
        if cached is not None and cached[0] == prior_signature:
            index = {
                sid: list(paths)
                for sid, paths in cached[1].items()
            }
            _upsert_run_state_index_row(index, row)
            _RUN_STATE_LEDGER_CACHE[root_key] = (current_signature, index)
    try:
        with _sqlite_connect(run_state_ledger_cache_path(root)) as conn:
            if not _run_state_cache_signature_current(conn, prior_signature):
                return
            sid = str(row.get("session_id") or "")
            app_session_id = str(row.get("app_session_id") or "")
            try:
                written_at = float(row.get("written_at"))
            except (TypeError, ValueError):
                written_at = 0.0
            conn.execute(
                "INSERT OR REPLACE INTO entries (sid, state_path, written_at, app_session_id) "
                "VALUES (?, ?, ?, ?)",
                (sid, state_path, written_at, app_session_id),
            )
            conn.executemany(
                "UPDATE meta SET value=? WHERE key=?",
                (
                    (current_signature[0], "dev"),
                    (current_signature[1], "ino"),
                    (current_signature[2], "size"),
                    (current_signature[3], "mtime_ns"),
                    (current_signature[4], "ctime_ns"),
                ),
            )
            conn.commit()
    except (sqlite3.DatabaseError, OSError):
        return


def ledger_state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    ledger = run_state_ledger_path(root)
    signature = _run_state_ledger_signature(ledger)
    if signature is None:
        return []
    root_key = str(root)
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        cached = _RUN_STATE_LEDGER_CACHE.get(root_key)
        if cached is not None:
            cached_signature, index = cached
            if cached_signature == signature:
                return _ledger_state_paths_for_sid(index, agent_sid, root_resolved)
    cached_paths = _load_run_state_cache_for_sid(root, signature, agent_sid, root_resolved)
    if cached_paths is not None:
        return cached_paths
    event, owner = _claim_run_state_cache_rebuild(root_key)
    if not owner:
        event.wait()
        cached_paths = _load_run_state_cache_for_sid(root, signature, agent_sid, root_resolved)
        if cached_paths is not None:
            return cached_paths
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            cached = _RUN_STATE_LEDGER_CACHE.get(root_key)
            if cached is not None:
                cached_signature, index = cached
                if cached_signature == signature:
                    return _ledger_state_paths_for_sid(index, agent_sid, root_resolved)
    latest_by_key: dict[tuple[str, str], tuple[float, str]] = {}
    app_session_by_state_path: dict[str, str] = {}
    try:
        with ledger.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                sid = str(row.get("session_id") or "")
                state_path = row.get("state_path")
                if not sid or not state_path:
                    continue
                state_path_str = str(state_path)
                if not _run_state_path_string_has_ledger_shape(state_path_str, root):
                    continue
                try:
                    written_at = float(row.get("written_at"))
                except (TypeError, ValueError):
                    written_at = 0.0
                key = (sid, state_path_str)
                current = latest_by_key.get(key)
                if current is None or written_at >= current[0]:
                    latest_by_key[key] = (written_at, state_path_str)
                    app_session_id = row.get("app_session_id")
                    if isinstance(app_session_id, str) and app_session_id:
                        app_session_by_state_path[state_path_str] = app_session_id
    except OSError:
        if owner:
            _finish_run_state_cache_rebuild(root_key)
        return []
    index: dict[str, list[tuple[float, str]]] = {}
    for (sid, _), value in latest_by_key.items():
        index.setdefault(sid, []).append(value)
    final_signature = _run_state_ledger_signature(ledger)
    try:
        if final_signature == signature:
            with _RUN_STATE_LOOKUP_CACHE_LOCK:
                _RUN_STATE_LEDGER_CACHE[root_key] = (signature, index)
            _write_run_state_cache(root, signature, index, app_session_by_state_path)
            return _ledger_state_paths_for_sid(index, agent_sid, root_resolved)
        if owner:
            _finish_run_state_cache_rebuild(root_key)
            owner = False
        return ledger_state_files_for_sid(root, agent_sid)
    finally:
        if owner:
            _finish_run_state_cache_rebuild(root_key)


def state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    ledger_paths = ledger_state_files_for_sid(root, agent_sid)
    if ledger_paths:
        return ledger_paths
    if _run_state_ledger_backfill_current(root):
        return []
    return recent_state_files_for_sid(root, agent_sid)


def run_dirs_by_app_session(root: Path) -> dict[str, Path]:
    _backfill_run_state_app_index(root)
    ledger = run_state_ledger_path(root)
    signature = _run_state_ledger_signature(ledger)
    if signature is None:
        return {}
    try:
        root_resolved = root.resolve()
    except OSError:
        return {}
    if _load_run_state_app_index(root, signature, root_resolved) is None:
        ledger_state_files_for_sid(root, "")
    return _load_run_state_app_index(root, signature, root_resolved) or {}


def _load_run_state_app_index(
    root: Path,
    signature: _RunStateLedgerSignature,
    root_resolved: Path,
) -> dict[str, Path] | None:
    try:
        with _sqlite_connect(run_state_ledger_cache_path(root)) as conn:
            if not _run_state_cache_signature_current(conn, signature):
                return None
            rows = conn.execute(
                "SELECT app_session_id, state_path FROM entries "
                "WHERE app_session_id != '' "
                "ORDER BY app_session_id, written_at, state_path"
            ).fetchall()
    except (sqlite3.DatabaseError, OSError):
        return None
    index: dict[str, Path] = {}
    for app_session_id, state_path in rows:
        if not isinstance(app_session_id, str) or not app_session_id:
            continue
        if not isinstance(state_path, str):
            return None
        if not _run_state_path_string_has_ledger_shape(state_path, root):
            return None
        path = Path(state_path)
        if path.parent.is_symlink() or _run_state_candidate_stat(path, root) is None:
            continue
        index[app_session_id] = path.parent
    return index


def recent_state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    index = _recent_state_index_for_root(root, root_resolved, agent_sid=agent_sid)
    return _filter_recent_state_paths(index.get(agent_sid, []), root_resolved)


def recent_state_index_for_root(root: Path) -> dict[str, list[Path]]:
    try:
        root_resolved = root.resolve()
    except OSError:
        return {}
    return _recent_state_index_for_root(root, root_resolved, agent_sid=None)


def _recent_state_index_for_root(
    root: Path,
    root_resolved: Path,
    *,
    agent_sid: str | None,
) -> dict[str, list[Path]]:
    now = time.monotonic()
    root_key = str(root)
    root_signature = _root_signature(root)
    if root_signature is None:
        return {}
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        cached = _RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
        if cached is not None:
            ts, _fingerprint, index, cached_root_signature, pending_run_dirs = cached
            if now - ts < _RUN_STATE_RECENT_INDEX_TTL_S:
                return _filter_recent_state_index(index, root_resolved, agent_sid=agent_sid)
            if (
                cached_root_signature == root_signature
                and now - ts < _RUN_STATE_RECENT_INDEX_MAX_AGE_S
            ):
                reuse_index = (
                    _recent_candidates_unchanged(_fingerprint, root)
                    and not _pending_run_dirs_have_state(root, pending_run_dirs)
                )
                if reuse_index:
                    _RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
                        now,
                        _fingerprint,
                        index,
                        cached_root_signature,
                        pending_run_dirs,
                    )
                    return _filter_recent_state_index(index, root_resolved, agent_sid=agent_sid)
        event = _RUN_STATE_RECENT_INDEX_INFLIGHT.get(root_key)
        if event is None:
            event = threading.Event()
            _RUN_STATE_RECENT_INDEX_INFLIGHT[root_key] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait(1.0)
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            cached = _RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
            return (
                _filter_recent_state_index(cached[2], root_resolved, agent_sid=agent_sid)
                if cached is not None else {}
            )
    try:
        scan = _recent_state_scan(root, root_resolved)
        if scan is None:
            return {}
        candidates, pending_run_dirs = scan
        if not candidates:
            with _RUN_STATE_LOOKUP_CACHE_LOCK:
                _RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
                    now,
                    candidates,
                    {},
                    root_signature,
                    pending_run_dirs,
                )
            return {}
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            cached = _RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
            if cached is not None:
                ts, fingerprint, index, cached_root_signature, cached_pending_run_dirs = cached
                if (
                    fingerprint == candidates
                    and cached_root_signature == root_signature
                    and cached_pending_run_dirs == pending_run_dirs
                    and now - ts < _RUN_STATE_RECENT_INDEX_MAX_AGE_S
                ):
                    _RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
                        now,
                        fingerprint,
                        index,
                        cached_root_signature,
                        cached_pending_run_dirs,
                    )
                    return _filter_recent_state_index(index, root_resolved, agent_sid=agent_sid)
        index = _build_recent_state_index(candidates, root_resolved)
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            _RUN_STATE_RECENT_INDEX_CACHE[root_key] = (
                now,
                candidates,
                index,
                root_signature,
                pending_run_dirs,
            )
        return index
    finally:
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            done = _RUN_STATE_RECENT_INDEX_INFLIGHT.pop(root_key, None)
        if done is not None:
            done.set()


def _recent_state_candidates(
    root: Path,
    root_resolved: Path | None = None,
) -> tuple[tuple[int, int, str], ...]:
    scan = _recent_state_scan(root, root_resolved)
    return scan[0] if scan is not None else ()


def _recent_state_scan(
    root: Path,
    root_resolved: Path | None = None,
) -> tuple[tuple[tuple[int, int, str], ...], tuple[str, ...]] | None:
    candidates: list[tuple[int, int, str]] = []
    pending_run_dirs: list[str] = []
    if root_resolved is None:
        try:
            root_resolved = root.resolve()
        except OSError:
            return None
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                state_path = os.path.join(entry.path, "state.json")
                try:
                    st = os.stat(state_path, follow_symlinks=False)
                except OSError:
                    pending_run_dirs.append(entry.name)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    pending_run_dirs.append(entry.name)
                    continue
                candidates.append((st.st_mtime_ns, st.st_size, str(state_path)))
    except OSError:
        return None
    return tuple(heapq.nlargest(_RUN_STATE_RECENT_SCAN_LIMIT, candidates)), tuple(sorted(pending_run_dirs))


def _filter_recent_state_paths(paths: list[Path], root_resolved: Path) -> list[Path]:
    return [
        path for path in paths
        if _run_state_path_under_root(path, root_resolved)
    ]


def _filter_recent_state_index(
    index: dict[str, list[Path]],
    root_resolved: Path,
    *,
    agent_sid: str | None = None,
) -> dict[str, list[Path]]:
    if agent_sid is not None:
        safe_paths = _filter_recent_state_paths(index.get(agent_sid, []), root_resolved)
        return {agent_sid: safe_paths} if safe_paths else {}
    filtered: dict[str, list[Path]] = {}
    for sid, paths in index.items():
        safe_paths = _filter_recent_state_paths(paths, root_resolved)
        if safe_paths:
            filtered[sid] = safe_paths
    return filtered


def _build_recent_state_index(
    candidates: tuple[tuple[int, int, str], ...],
    root_resolved: Path | None = None,
) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for _, _, state_path in candidates:
        path = Path(state_path)
        if root_resolved is not None and not _run_state_path_under_root(path, root_resolved):
            continue
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        agent_sid = str(st.get("session_id") or "")
        if not agent_sid:
            continue
        index.setdefault(agent_sid, []).append(path)
    return index


def _backfill_run_state_ledger(root: Path, index: dict[str, list[Path]]) -> None:
    try:
        root_resolved = root.resolve()
    except Exception:
        return
    for paths in index.values():
        for path in paths:
            try:
                if not _run_state_path_under_root(path, root_resolved):
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                _append_run_state_ledger(path, data)


def ensure_run_state_ledger_backfilled(root: Optional[Path] = None) -> bool:
    root = root or runs_root()
    if _run_state_ledger_backfill_current(root):
        return False
    with _RUN_STATE_LEDGER_BACKFILL_LOCK:
        if _run_state_ledger_backfill_current(root):
            return False
        try:
            root_resolved = root.resolve()
            ledger = run_state_ledger_path(root)
            existing = _run_state_ledger_keys(ledger)
            rows: list[dict] = []
            now = time.time()
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    state_path = Path(entry.path) / "state.json"
                    if state_path.name != "state.json":
                        continue
                    try:
                        state_path.resolve().relative_to(root_resolved)
                        data = json.loads(state_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    session_id = data.get("session_id") if isinstance(data, dict) else None
                    jsonl_path = data.get("jsonl_path") if isinstance(data, dict) else None
                    if not session_id or not jsonl_path:
                        continue
                    key = (str(state_path), str(session_id), str(jsonl_path))
                    if key in existing:
                        continue
                    existing.add(key)
                    rows.append({
                        "session_id": str(session_id),
                        "jsonl_path": str(jsonl_path),
                        "state_path": str(state_path),
                        "written_at": now,
                    })
            ledger.parent.mkdir(parents=True, exist_ok=True)
            if rows:
                with ledger.open("a", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, separators=(",", ":")) + "\n")
                        _RUN_STATE_LEDGER_SEEN.add((
                            row["state_path"],
                            row["session_id"],
                            row["jsonl_path"],
                        ))
            write_json(
                run_state_ledger_backfill_marker_path(root),
                {
                    "version": _RUN_STATE_LEDGER_BACKFILL_VERSION,
                    "backfilled_at": now,
                    "appended": len(rows),
                },
            )
            return True
        except Exception:
            logger.exception("runs_dir: failed to backfill run-state ledger")
            return False


def append_reconciled_marker_index(
    marker_path: Path,
    provider_kind: str | None,
    ingestion_version: int,
    *,
    root: Optional[Path] = None,
) -> None:
    row = _reconciled_marker_index_row(
        marker_path,
        provider_kind,
        ingestion_version,
        root=root,
    )
    if row is None:
        return
    try:
        from reconciled_marker_index import for_path
        for_path(reconciled_marker_index_path(root or runs_root())).append(row)
    except Exception:
        logger.exception("runs_dir: failed to append reconciled-marker index")


def ensure_reconciled_marker_index_backfilled(root: Optional[Path] = None) -> bool:
    root = root or runs_root()
    if _reconciled_marker_backfill_current(root):
        return False
    with _RECONCILED_MARKER_BACKFILL_LOCK:
        if _reconciled_marker_backfill_current(root):
            return False
        try:
            index = reconciled_marker_index_path(root)
            existing = _reconciled_marker_index_keys(index)
            rows: list[dict] = []
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    marker_path = Path(entry.path) / "reconciled.marker"
                    try:
                        if marker_path.is_symlink():
                            continue
                        data = json.loads(marker_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    provider_kind = data.get("provider_kind") if isinstance(data, dict) else None
                    ingestion_version = data.get("ingestion_version") if isinstance(data, dict) else None
                    if not isinstance(provider_kind, str) or not isinstance(ingestion_version, int):
                        continue
                    row = _reconciled_marker_index_row(
                        marker_path,
                        provider_kind,
                        ingestion_version,
                        root=root,
                    )
                    if row is None:
                        continue
                    key = _reconciled_marker_index_key(row)
                    if key in existing:
                        continue
                    existing.add(key)
                    rows.append(row)
            index.parent.mkdir(parents=True, exist_ok=True)
            if rows:
                from reconciled_marker_index import for_path
                for_path(index).append_many(rows)
            write_json(
                reconciled_marker_index_backfill_marker_path(root),
                {
                    "version": _RECONCILED_MARKER_BACKFILL_VERSION,
                    "backfilled_at": time.time(),
                    "appended": len(rows),
                },
            )
            return True
        except Exception:
            logger.exception("runs_dir: failed to backfill reconciled-marker index")
            return False


def load_reconciled_marker_index(root: Optional[Path] = None) -> dict[str, dict]:
    root = root or runs_root()
    from reconciled_marker_index import for_path
    return for_path(reconciled_marker_index_path(root)).load_latest()


def reconciled_marker_index_row_matches(run_dir: Path, row: dict) -> bool:
    run_id = row.get("run_id")
    marker_path_raw = row.get("marker_path")
    if not isinstance(run_id, str) or run_id != run_dir.name:
        return False
    marker_path = run_dir / "reconciled.marker"
    if not isinstance(marker_path_raw, str) or marker_path_raw != str(marker_path):
        return False
    try:
        if run_dir.is_symlink() or marker_path.is_symlink():
            return False
        st = marker_path.lstat()
    except OSError:
        return False
    try:
        return (
            int(row.get("marker_size")) == int(st.st_size)
            and int(row.get("marker_mtime_ns")) == int(st.st_mtime_ns)
            and int(row.get("marker_inode") or 0) == int(getattr(st, "st_ino", 0) or 0)
        )
    except (TypeError, ValueError):
        return False


def _reconciled_marker_index_row(
    marker_path: Path,
    provider_kind: str | None,
    ingestion_version: int,
    *,
    root: Optional[Path] = None,
) -> dict | None:
    if marker_path.name != "reconciled.marker" or provider_kind is None:
        return None
    root = root or runs_root()
    try:
        root_resolved = root.resolve()
        marker_resolved = marker_path.resolve()
        marker_resolved.relative_to(root_resolved)
        run_dir = marker_path.parent
        run_dir.resolve().relative_to(root_resolved)
        if run_dir.parent.resolve() != root_resolved:
            return None
        if run_dir.is_symlink() or marker_path.is_symlink():
            return None
        st = marker_path.lstat()
    except (OSError, ValueError):
        return None
    return {
        "run_id": run_dir.name,
        "marker_path": str(marker_path),
        "provider_kind": str(provider_kind),
        "ingestion_version": int(ingestion_version),
        "marker_size": int(st.st_size),
        "marker_mtime_ns": int(st.st_mtime_ns),
        "marker_inode": int(getattr(st, "st_ino", 0) or 0),
        "written_at": time.time(),
    }


def _reconciled_marker_index_key(row: dict) -> tuple[str, str, int, int, int, int]:
    return (
        str(row.get("marker_path") or ""),
        str(row.get("provider_kind") or ""),
        int(row.get("ingestion_version") or 0),
        int(row.get("marker_size") or 0),
        int(row.get("marker_mtime_ns") or 0),
        int(row.get("marker_inode") or 0),
    )


def _reconciled_marker_index_keys(index: Path) -> set[tuple[str, str, int, int, int, int]]:
    from reconciled_marker_index import for_path
    return for_path(index).load_keys()


def _run_state_ledger_keys(ledger: Path) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    try:
        with ledger.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                state_path = row.get("state_path")
                session_id = row.get("session_id")
                jsonl_path = row.get("jsonl_path")
                if state_path and session_id and jsonl_path:
                    keys.add((str(state_path), str(session_id), str(jsonl_path)))
    except OSError:
        pass
    return keys


def _run_state_ledger_has_key(ledger: Path, key: tuple[str, str, str]) -> bool:
    try:
        with ledger.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                existing = (
                    str(row.get("state_path") or ""),
                    str(row.get("session_id") or ""),
                    str(row.get("jsonl_path") or ""),
                )
                if existing == key:
                    return True
    except OSError:
        return False
    return False


def iter_run_dirs(run_id_filter: Optional[set[str]] = None):
    root = runs_root()
    if not root.exists():
        return
    if run_id_filter is not None:
        for run_id in run_id_filter:
            child = root / run_id
            if child.is_dir():
                yield child
        return
    for child in root.iterdir():
        if child.is_dir():
            yield child


def prune_old_completed_runs(max_age_days: int = 7) -> int:
    root = runs_root()
    if not root.exists():
        return 0
    import perf

    cutoff = time.time() - (max_age_days * 24 * 60 * 60)
    scan_started = time.perf_counter()
    candidates: list[tuple[Path, tuple[int, int], tuple[int, int, int, int]]] = []
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                run_st = entry.stat(follow_symlinks=False)
                if not stat.S_ISDIR(run_st.st_mode):
                    continue
                complete_path = Path(entry.path) / "complete.json"
                complete_st = complete_path.stat(follow_symlinks=False)
                if not stat.S_ISREG(complete_st.st_mode) or complete_st.st_mtime >= cutoff:
                    continue
                candidates.append((
                    Path(entry.path),
                    (int(run_st.st_dev), int(run_st.st_ino)),
                    (
                        int(complete_st.st_dev),
                        int(complete_st.st_ino),
                        int(complete_st.st_mtime_ns),
                        int(complete_st.st_size),
                    ),
                ))
            except OSError:
                continue
    perf.record("startup.maintenance.prune_runs.scan", (time.perf_counter() - scan_started) * 1000.0)
    perf.record_count("startup.maintenance.prune_runs.candidates", len(candidates))

    removed = 0
    for child, expected_run, expected_complete in candidates:
        lock_started = time.perf_counter()
        with run_catalog_lock(root):
            perf.record(
                "startup.maintenance.prune_runs.candidate_lock",
                (time.perf_counter() - lock_started) * 1000.0,
            )
            try:
                run_st = child.stat(follow_symlinks=False)
                complete_st = (child / "complete.json").stat(follow_symlinks=False)
                current_run = (int(run_st.st_dev), int(run_st.st_ino))
                current_complete = (
                    int(complete_st.st_dev),
                    int(complete_st.st_ino),
                    int(complete_st.st_mtime_ns),
                    int(complete_st.st_size),
                )
                if (
                    not stat.S_ISDIR(run_st.st_mode)
                    or not stat.S_ISREG(complete_st.st_mode)
                    or current_run != expected_run
                    or current_complete != expected_complete
                    or complete_st.st_mtime >= cutoff
                ):
                    perf.record_count("startup.maintenance.prune_runs.revalidated_skip", 1)
                    continue
            except OSError:
                perf.record_count("startup.maintenance.prune_runs.revalidated_skip", 1)
                continue
            reap_started = time.perf_counter()
            if reap_run_dir(child):
                removed += 1
            perf.record(
                "startup.maintenance.prune_runs.reap",
                (time.perf_counter() - reap_started) * 1000.0,
            )
    perf.record_count("startup.maintenance.prune_runs.removed", removed)
    return removed


# In-process CLI timer tools stripped on EVERY claude spawn (replaced by
# the backend-owned scheduler). Single source of truth for both sides of
# the contract: provider_claude appends them to input.json's
# disallowed_tools; runner.py refuses to spawn if any are missing.
TIMER_TOOLS = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "ScheduleWakeup",
)

# Background execution is forbidden across ALL claude runs: the runner
# process must be able to die at turn end without orphaning or killing
# user work, so claude must never start work that outlives the turn.
# Enforced in layers (all fail-closed):
#   1. `BACKGROUND_TASKS_DISABLE_ENV=1` in the CLI env — the CLI's native
#      master switch: strips `run_in_background` from the Bash/Task tool
#      schemas, ignores a smuggled param at runtime, disables
#      timeout-auto-backgrounding, and forces subagents synchronous.
#   2. These background-interaction tools stripped via disallowed_tools
#      on every spawn (dead surface once #1 holds; kept so the model
#      never sees them).
#   3. A PreToolUse hook in runner.py denying any tool input that still
#      carries `run_in_background` / remote isolation (future-proofing
#      against CLI schema changes).
# Single source of truth for both sides of the contract: provider_claude
# appends the tools to input.json's disallowed_tools and sets the env
# vars in build_env; runner.py refuses to spawn if any tool strip is
# missing and re-asserts the env vars itself.
BACKGROUND_WORK_TOOLS = (
    "BashOutput",
    "KillShell",
    "TaskOutput",
    "TaskStop",
    "Monitor",
)
BACKGROUND_TASKS_DISABLE_ENV = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
BG_EXIT_HANDOFF_DISABLE_ENV = "CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF"
AUTO_BACKGROUND_ENV = "CLAUDE_AUTO_BACKGROUND_TASKS"


def turn_dir(run_dir: Path, turn_id: str) -> Path:
    """Per-turn artifact directory under a runner's run_dir.

    Each run serves exactly one turn; `turns/<turn_id>/{start.json,
    complete.json}` is written alongside the run-level files so
    `read_best_complete` can salvage a turn whose runner died before the
    run-level complete.json landed.
    """
    return run_dir / "turns" / turn_id


def runner_alive_path(run_dir: Path) -> Path:
    """Heartbeat sentinel file refreshed by the runner every ~5s for its
    whole lifetime, so the backend can tell a live runner from a dead
    orphan.
    """
    return run_dir / "runner_alive"


def read_best_complete(run_dir: Path) -> Optional[dict]:
    """Best available completion payload for a run, or None.

    The runner writes the per-turn ``turns/<turn_id>/complete.json``
    (with the turn's real success/error/output) BEFORE the run-level
    ``complete.json`` (runner.py:1659 then :2070). A runner that dies in
    that gap — e.g. SIGKILLed by the stuck-runner watchdog right after a
    turn succeeded — leaves a valid per-turn payload but no run-level
    file. Callers that would otherwise synthesize a "no complete.json"
    error must fall back here so the real output isn't discarded.

    Preference order:
      1. run-level ``complete.json`` (authoritative).
      2. most-recent ``turns/*/complete.json`` by mtime.
    Returns the parsed dict, or None if neither exists/parses.
    """
    run_level = run_dir / "complete.json"
    if run_level.exists():
        try:
            return json.loads(run_level.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("read_best_complete: bad run-level complete.json %s", run_dir)
    turns = run_dir / "turns"
    if not turns.is_dir():
        return None
    candidates = []
    for child in turns.iterdir():
        cj = child / "complete.json"
        try:
            candidates.append((cj.stat().st_mtime, cj))
        except OSError:
            continue
    for _, cj in sorted(candidates, reverse=True):
        try:
            return json.loads(cj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def salvage_complete_payload(run_id: str) -> Optional[dict]:
    """On-disk authority for the dead-runner synthesis path.

    `turn_manager`'s wait loop can see the runner process as dead before
    the provider's in-memory `complete` event wins the race onto the
    queue (event-loop lag, or the runner exiting in the same window it
    wrote complete.json). Rather than fabricate a failure, trust the
    complete.json the runner already wrote — it records the turn's real
    outcome. Returns {success, error, session_id, token_usage}, or None
    when no complete file exists (a genuine no-output death)."""
    data = read_best_complete(runs_root() / run_id)
    if data is None:
        return None
    return {
        "success": bool(data.get("success", False)),
        "error": data.get("error"),
        "session_id": data.get("session_id"),
        "token_usage": data.get("token_usage"),
    }


def _load_run_state_dirs_for_app_sessions(
    root: Path,
    signature: _RunStateLedgerSignature,
    root_resolved: Path,
    app_sids: frozenset[str],
) -> Optional[list[Path]]:
    """Read the run-state sqlite cache for every run dir whose
    `app_session_id` is in `app_sids`. Mirrors `_load_run_state_app_index`
    but (a) filters to the given sids and (b) keeps ALL run dirs per sid
    (a session owns one run dir per turn, not one overall). Returns None
    on a cold/stale/corrupt cache so the caller can rebuild or fall back."""
    placeholders = ",".join("?" * len(app_sids))
    try:
        with _sqlite_connect(run_state_ledger_cache_path(root)) as conn:
            if not _run_state_cache_signature_current(conn, signature):
                return None
            rows = conn.execute(
                f"SELECT state_path FROM entries "
                f"WHERE app_session_id IN ({placeholders})",
                tuple(app_sids),
            ).fetchall()
    except (sqlite3.DatabaseError, OSError):
        return None
    dirs: list[Path] = []
    seen: set[str] = set()
    for (state_path,) in rows:
        if not isinstance(state_path, str):
            return None
        if not _run_state_path_string_has_ledger_shape(state_path, root):
            return None
        path = Path(state_path)
        # Same liveness/shape guards as `_load_run_state_app_index`.
        if path.parent.is_symlink() or _run_state_candidate_stat(path, root) is None:
            continue
        if not _run_state_path_under_root(path, root_resolved):
            continue
        run_dir = path.parent
        key = str(run_dir)
        if key not in seen:
            seen.add(key)
            dirs.append(run_dir)
    return dirs


def _run_dirs_for_app_sessions_indexed(
    root: Path, app_sids: frozenset[str]
) -> Optional[list[Path]]:
    """Fast path for `delete_runs_for_sessions`: return the run dirs whose
    recorded `app_session_id` is in `app_sids`, using the run-state sqlite
    index instead of an O(N) walk of every run dir.

    Returns None when the index is cold or stale and cannot be rebuilt —
    the caller MUST then fall back to an exhaustive walk. Returns a
    (possibly empty) list of run-dir paths when the index is current.

    Mirrors `run_dirs_by_app_session`'s backfill + cache-rebuild dance so
    the `app_session_id` column is populated before we trust it; without
    the backfill, pre-existing ledger rows carry no `app_session_id` and
    the index would wrongly answer "no matches". `app_session_id` (from
    `state.json`) is the SAME key deletion attributes a run by —
    `persist_to or app_session_id` from `backend_state.json` coincides
    with it on every observed run dir — and the caller still re-verifies
    via `backend_state.json` before reaping, so an index misattribution
    can never reap a dir the exhaustive walk would not."""
    if not app_sids:
        return []
    _backfill_run_state_app_index(root)
    ledger = run_state_ledger_path(root)
    signature = _run_state_ledger_signature(ledger)
    if signature is None:
        return None
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    result = _load_run_state_dirs_for_app_sessions(
        root, signature, root_resolved, app_sids
    )
    if result is None:
        # Stale cache — rebuild it (same trigger `run_dirs_by_app_session`
        # uses), recompute the signature, then retry once.
        ledger_state_files_for_sid(root, "")
        signature = _run_state_ledger_signature(ledger)
        if signature is None:
            return None
        result = _load_run_state_dirs_for_app_sessions(
            root, signature, root_resolved, app_sids
        )
    return result


def cached_run_dirs_for_app_session(root: Path, app_session_id: str) -> list[Path]:
    if not app_session_id:
        return []
    ledger = run_state_ledger_path(root)
    signature = _run_state_ledger_signature(ledger)
    if signature is None:
        return []
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    result = _load_run_state_dirs_for_app_sessions(
        root,
        signature,
        root_resolved,
        frozenset({app_session_id}),
    )
    return result or []


def delete_runs_for_sessions(sids: set[str]) -> int:
    """Delete every run dir whose messages persist to one of `sids`.

    A run is attributed to `persist_to or app_session_id` — the SAME key
    run-recovery uses to look the session up (`run_recovery._integrate_one`
    keys `session_manager.get` on `persist_to or app_session_id`). Matching
    that exact key means we reap precisely the dirs recovery would later
    orphan-skip, and never a sibling worker run whose persist target is a
    surviving session. Returns the count removed.

    Candidate dirs come from the run-state sqlite index
    (`_run_dirs_for_app_sessions_indexed`) in O(matches) rather than an
    O(N) walk of every run dir; the index is unavailable on a cold/stale
    cache, where we fall back to the exhaustive walk. Every candidate is
    re-verified against `backend_state.json` before reaping, so the fast
    path can never reap a dir the exhaustive walk would skip.

    Called when a session tree is deleted so its detached run dirs don't
    outlive it (they'd otherwise linger until the 7-day age-prune and be
    re-scanned + skipped by run-recovery on every backend startup)."""
    if not sids:
        return 0
    root = runs_root()
    if not root.exists():
        return 0
    frozensids = frozenset(sids)
    candidates = _run_dirs_for_app_sessions_indexed(root, frozensids)
    if candidates is None:
        # Cold/stale index — exhaustive walk is the correctness backstop.
        candidates = [child for child in root.iterdir() if child.is_dir()]
    removed = 0
    for child in candidates:
        try:
            bs = json.loads((child / "backend_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        persist_sid = bs.get("persist_to") or bs.get("app_session_id")
        if persist_sid in frozensids:
            if reap_run_dir(child):
                removed += 1
    if removed:
        logger.info("delete_runs_for_sessions: removed %d run dir(s)", removed)
    return removed


def _harvest_spawn_sid(child: Path) -> None:
    """Record the run dir's provider session_id into the durable spawn
    ledger so BA-spawn provenance survives the dir's removal. Reads the sid
    from whichever run-state file carries it."""
    import spawn_ledger
    spawn_ledger.record_run_dir(child)


def reap_run_dir(child: Path) -> bool:
    """Single owner of run-dir removal: harvest the spawn sid into the
    durable ledger, THEN remove the dir. Every reap site (session-delete and
    the per-provider age-prune) routes through here so no BA-spawned sid is
    lost when its run dir is reaped. Returns True if the dir was removed."""
    _harvest_spawn_sid(child)
    try:
        shutil.rmtree(child)
        return True
    except OSError as e:
        logger.warning("reap_run_dir: failed to rm %s: %s", child, e)
        return False


def atomic_write_json(path: Path, data: dict) -> None:
    """Crash-safe JSON write for run-dir state."""
    write_json(path, data)
    if path.name == "state.json":
        _invalidate_recent_state_index(path.parent.parent)
    _append_run_state_ledger(path, data)


# A provider CLI's session jsonl is considered "freshly written" — evidence
# its writer is still alive — if touched within this window. Generous enough
# to survive a slow backend boot (the CLI writes continuously during a turn).
CLI_LIVENESS_FRESH_S = 120.0


def cli_liveness_corroborated(
    cli_pid: Optional[int],
    jsonl_path: Optional[str],
    jsonl_inode: Optional[int],
    processed_byte: Optional[int],
) -> bool:
    """True when `cli_pid` is alive AND the provider CLI's session jsonl gives
    POSITIVE evidence that this exact live process still owns the run — used at
    restart to distinguish a genuinely-still-running CLI (whose wrapper died)
    from a recycled pid.

    Corroboration = pid alive AND the recorded jsonl still exists with the
    recorded inode AND it either grew past the last ingested byte
    (`size > processed_byte`) or was written within `CLI_LIVENESS_FRESH_S`.
    A recycled pid is an unrelated process that never touches THIS session
    file, so a stale/absent jsonl fails corroboration → the run is treated as
    dead. Fail closed: any missing/unreadable signal returns False."""
    if not cli_pid or not pid_alive(int(cli_pid)):
        return False
    if not jsonl_path:
        return False
    try:
        st = Path(jsonl_path).stat()
    except OSError:
        return False
    if jsonl_inode is not None:
        try:
            if int(jsonl_inode) != st.st_ino:
                return False
        except (TypeError, ValueError):
            return False
    # Growth past the last ingested byte is positive evidence, but only when a
    # real BYTE cursor is supplied (Claude/Codex). Providers that track a line
    # cursor (Gemini) pass None and rely on mtime freshness alone.
    if processed_byte is not None:
        try:
            if st.st_size > int(processed_byte):
                return True
        except (TypeError, ValueError):
            pass
    return (time.time() - st.st_mtime) < CLI_LIVENESS_FRESH_S


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    # Delegate to the platform process-control layer. On POSIX this is the
    # original os.kill(pid, 0) probe; on Windows os.kill(pid, 0) would
    # *terminate* the process, so a Win32 handle probe is used instead.
    from proc_control import process_control

    return process_control().pid_alive(pid)
