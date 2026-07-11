"""Native-transcript FTS5 index — the fast path for the raw transcript search.

Mirrors the :mod:`session_search_index` pattern (FTS5 + lean extraction +
background worker + schema-version + rebuild-into-fresh-file) but applied to the
PROVIDER-NATIVE transcript corpus (claude ``projects``/codex ``sessions``/
gemini ``tmp``/BA run-dirs) instead of BA's own ``sessions/*/events.jsonl``.

Two differences from session_search_index, both deliberate:

- **Word tokenizer (``unicode61``)**, not trigram. We match whole words
  (``\\bword\\b``); word-tokenize is correct for that and ~3-5× smaller than
  trigram (which indexes every 3-char substring).
- **Lean extraction** drops ``tool_result`` (52% of bytes, low search value) —
  same lesson session_search_index learned. Indexed: user prompts, assistant
  text, reasoning, and tool-call name+args.

Freshness (see chat thread): a file is *fresh* iff its current ``mtime`` and
``size`` match what was indexed (stat-checked, no content read). A background
daemon full-walks the roots until coverage exists, then uses steady refreshes
over indexed paths. Periodic/forced full reconciles discover new external files
and tombstone deleted ones. ``covered`` is set once a full walk has indexed
every file; while not covered (cold start), the search falls back to ``rg`` so
correctness never depends on an incomplete index.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from paths import ba_home, encode_cwd
import portable_lock
import native_internal_prompt
import run_source_index

logger = logging.getLogger(__name__)

# Injectable seams for standalone embedding (the transcript-search product
# vendors this module with its own state home and its own roots resolver).
# Backend behavior is unchanged: defaults are ba_home + the search module.
_home_resolver = ba_home
_roots_resolver_override = None


def set_home_resolver(resolver) -> None:
    """Override where the index DB / lock / worker log live (default ba_home)."""
    global _home_resolver
    _home_resolver = resolver


def set_roots_resolver(resolver) -> None:
    """Override the (native_roots, classify_root, candidate_from_match,
    is_native_transcript_path) provider — default is
    :mod:`native_session_prompt_search`. The resolver is a zero-arg callable
    returning that 4-tuple."""
    global _roots_resolver_override
    _roots_resolver_override = resolver

_SCHEMA_VERSION = 17
_FTS_COLUMNS = (
    "text", "path", "sid", "cwd", "tag", "element_kind", "tool_name",
    "ts_utc", "role", "element_id", "element_index",
    "text_sha256", "norm_text_sha256",
    "prefix_1024_sha256", "prefix_4096_sha256", "prefix_8192_sha256",
    "text_len", "norm_text_len",
)
_META_COLUMNS = _FTS_COLUMNS[1:]
_PREFIX_HASH_SIZES = (1024, 4096, 8192)
_REPEAT_MIN_COUNT = 2
_REPEAT_EXACT_MIN_NORM_CHARS = 256
_REPEAT_PREFIX_FIELDS = (
    ("prefix_8192_sha256", 8192),
    ("prefix_4096_sha256", 4096),
    ("prefix_1024_sha256", 1024),
)
_REPEAT_PREFIX_DISTINCT_TEXT_MIN_COUNT = 2
_INDEXED_KINDS = frozenset({"user_prompt", "assistant_text", "reasoning", "tool_call"})
# How many leading user prompts to scan for a BA-injection marker when
# classifying a transcript's turn_source. Codex prepends an AGENTS.md user
# message, so the marker can land at index 1+.
_INTERNAL_PROMPT_SCAN_LIMIT = 5
_POLL_INTERVAL_SECONDS = 10.0
_FRESH_WINDOW_SECONDS = 30.0  # covered + last walk within this window => trusted
_FULL_RECONCILE_INTERVAL_SECONDS = 30 * 60
_STEADY_REFRESH_FILE_BATCH = 2048
_MATCHED_SCAN_LIMIT = 20_000
_PATH_CAP = 1_000  # > this many matched files => "too broad", bail to caller
_SQLITE_BUSY_TIMEOUT_MS = 30_000
_SQL_PLAN_PROBE_LIMIT = 10_000
_QUICK_STATE_BUSY_TIMEOUT_MS = 50
_FULL_REFRESH_FILE_BATCH = 128
_FULL_SCAN_DISCOVERY_BATCH = 128
_FULL_SCAN_ENTRY_BUDGET = 4096
_CHECKPOINT_WAL_BYTES = 256 * 1024 * 1024
_WORKER_ARG = "--native-transcript-index-worker"
_WORKER_POLL_INTERVAL_SECONDS = 0.5
_WORKER_LOG_BYTES = 16 * 1024 * 1024
_MAX_FILE_TIMING_ROWS = 20
_REFRESH_REQUESTED_AT_KEY = "refresh_requested_at"
_REFRESH_HANDLED_AT_KEY = "refresh_handled_at"
_WRITER_CACHE_KIB = 200_000
_READONLY_CACHE_KIB = 8_192

_lock = threading.Lock()  # guards writer connection lifecycle + rebuild flag
_worker_started = False
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_process: subprocess.Popen | None = None
_stop = threading.Event()

# Refresh signaling: once covered, a stale query REQUESTS a refresh and waits
# for it (one delta pass) instead of dropping to rg. A DB marker wakes external
# workers; the condition variable keeps same-process tests and shutdown cheap.
_refresh_cond = threading.Condition()
_last_refresh_at = 0.0
_last_full_reconcile_at = 0.0
_refresh_requested = False
_FRESH_WAIT_TIMEOUT = 3.0  # max a query blocks for a delta refresh before rg


def _db_path() -> Path:
    return _home_resolver() / "native_transcript_index.sqlite3"


def _writer_lock_path() -> Path:
    return _db_path().with_name(_db_path().name + ".lock")


def _worker_pid_path() -> Path:
    return _db_path().with_name(_db_path().name + ".worker.pid")


def _worker_log_path() -> Path:
    return _home_resolver() / "logs" / "native-transcript-index.log"


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_worker_pid() -> int | None:
    try:
        return int(_worker_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_worker_pid(pid: int) -> None:
    path = _worker_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def _clear_worker_pid(pid: int | None = None) -> None:
    path = _worker_pid_path()
    if pid is not None and _read_worker_pid() != pid:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _append_worker_log(message: str) -> None:
    path = _worker_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _WORKER_LOG_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
        with path.open("a", encoding="utf-8") as f:
            f.write(message.rstrip() + "\n")
    except OSError:
        logger.debug("native transcript index worker log write failed", exc_info=True)


# ─── connection management ─────────────────────────────────────────────────
# One writer (guarded by _lock), one readonly connection per thread (FTS5 reads
# can't share a writer connection mid-transaction).

_writer_conn: sqlite3.Connection | None = None
_writer_conn_path: Path | None = None
_readonly_local = threading.local()


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
    _configure(conn, readonly=readonly)
    return conn


def _configure(conn: sqlite3.Connection, *, readonly: bool) -> None:
    conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cache_kib = _READONLY_CACHE_KIB if readonly else _WRITER_CACHE_KIB
    conn.execute(f"PRAGMA cache_size=-{cache_kib}")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")


def _writer_connection() -> sqlite3.Connection:
    global _writer_conn, _writer_conn_path
    path = _db_path()
    if _writer_conn is not None and _writer_conn_path == path:
        return _writer_conn
    if _writer_conn is not None:
        _writer_conn.close()
    _writer_conn = _connect(path, readonly=False)
    _writer_conn_path = path
    _ensure_schema(_writer_conn)
    return _writer_conn


def _checkpoint_if_large(conn: sqlite3.Connection) -> None:
    wal = _db_path().with_suffix(_db_path().suffix + "-wal")
    try:
        if wal.stat().st_size < _CHECKPOINT_WAL_BYTES:
            return
    except OSError:
        return
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error:
        logger.debug("native transcript WAL checkpoint failed", exc_info=True)


def _readonly_connection() -> sqlite3.Connection:
    path = _db_path()
    conn = getattr(_readonly_local, "conn", None)
    cpath = getattr(_readonly_local, "path", None)
    if conn is not None and cpath == path:
        return conn
    if conn is not None:
        conn.close()
    if not path.exists():
        # No index yet -> hand back a connection to a throwaway empty DB so the
        # caller's queries simply return no rows.
        conn = sqlite3.connect(":memory:")
        _readonly_local.conn = conn
        _readonly_local.path = None
        return conn
    conn = _connect(path, readonly=True)
    _readonly_local.conn = conn
    _readonly_local.path = path
    return conn


def _ensure_file_state_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS native_file_state (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            tag TEXT NOT NULL,
            sid TEXT,
            cwd TEXT,
            first_user_prompt_ts TEXT,
            message_count INTEGER NOT NULL,
            turn_source TEXT,
            indexed_at REAL NOT NULL
        );
        """
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    _ensure_file_state_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS native_corpus_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_full_scan_queue (
            path TEXT PRIMARY KEY,
            tag TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS native_full_scan_seen (
            path TEXT PRIMARY KEY
        );
        """
    )
    queue_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(native_full_scan_queue)")
    }
    if "processed" not in queue_columns:
        conn.execute(
            "ALTER TABLE native_full_scan_queue "
            "ADD COLUMN processed INTEGER NOT NULL DEFAULT 0"
        )
    _ensure_fts_schema(conn)


def _ensure_fts_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_element_fts'"
    ).fetchone()
    if existing:
        columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(native_element_fts)"))
        version_row = conn.execute(
            "SELECT value FROM native_corpus_state WHERE key = 'schema_version'"
        ).fetchone()
        path_index_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_element_path'"
        ).fetchone()
        meta_index_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_element_meta'"
        ).fetchone()
        text_projection_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_element_text'"
        ).fetchone()
        repeat_group_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_repeat_group'"
        ).fetchone()
        repeat_best_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_element_repeat_best'"
        ).fetchone()
        if (
            columns != _FTS_COLUMNS
            or version_row is None
            or version_row[0] != str(_SCHEMA_VERSION)
            or not path_index_exists
            or not meta_index_exists
            or not text_projection_exists
            or not repeat_group_exists
            or not repeat_best_exists
        ):
            conn.execute("DROP TABLE native_element_fts")
            conn.execute("DROP TABLE IF EXISTS native_element_path")
            conn.execute("DROP TABLE IF EXISTS native_element_meta")
            conn.execute("DROP TABLE IF EXISTS native_element_text")
            conn.execute("DROP TABLE IF EXISTS native_repeat_group")
            conn.execute("DROP TABLE IF EXISTS native_element_repeat")
            conn.execute("DROP TABLE IF EXISTS native_element_repeat_best")
            conn.execute("DROP TABLE IF EXISTS native_file_state")
            _ensure_file_state_schema(conn)
            conn.execute("DELETE FROM native_corpus_state")
            conn.execute("DELETE FROM native_full_scan_queue")
            conn.execute("DELETE FROM native_full_scan_seen")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS native_element_fts USING fts5(
            text,
            path UNINDEXED,
            sid UNINDEXED,
            cwd UNINDEXED,
            tag UNINDEXED,
            element_kind UNINDEXED,
            tool_name UNINDEXED,
            ts_utc UNINDEXED,
            role UNINDEXED,
            element_id UNINDEXED,
            element_index UNINDEXED,
            text_sha256 UNINDEXED,
            norm_text_sha256 UNINDEXED,
            prefix_1024_sha256 UNINDEXED,
            prefix_4096_sha256 UNINDEXED,
            prefix_8192_sha256 UNINDEXED,
            text_len UNINDEXED,
            norm_text_len UNINDEXED,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS native_element_path (
            rowid INTEGER PRIMARY KEY,
            path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_element_meta (
            rowid INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            sid TEXT,
            cwd TEXT,
            tag TEXT,
            element_kind TEXT,
            tool_name TEXT,
            ts_utc TEXT,
            role TEXT,
            element_id TEXT,
            element_index INTEGER,
            text_sha256 TEXT,
            norm_text_sha256 TEXT,
            prefix_1024_sha256 TEXT,
            prefix_4096_sha256 TEXT,
            prefix_8192_sha256 TEXT,
            text_len INTEGER,
            norm_text_len INTEGER
        );
        CREATE TABLE IF NOT EXISTS native_element_text (
            rowid INTEGER PRIMARY KEY,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_repeat_group (
            group_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            bucket_field TEXT NOT NULL,
            hash_key TEXT NOT NULL,
            subgroup_key TEXT NOT NULL,
            count INTEGER NOT NULL,
            representative_rowid INTEGER NOT NULL,
            common_norm_prefix_len INTEGER NOT NULL,
            first_seen_ts TEXT,
            last_seen_ts TEXT
        );
        CREATE TABLE IF NOT EXISTS native_element_repeat (
            rowid INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            raw_tail_start INTEGER NOT NULL,
            norm_tail_start INTEGER NOT NULL,
            priority_kind INTEGER NOT NULL,
            priority_prefix_len INTEGER NOT NULL,
            priority_count INTEGER NOT NULL,
            PRIMARY KEY(rowid, group_id)
        );
        CREATE TABLE IF NOT EXISTS native_element_repeat_best (
            rowid INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL,
            raw_tail_start INTEGER NOT NULL,
            norm_tail_start INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_repeat_dirty (
            bucket_field TEXT NOT NULL,
            hash_key TEXT NOT NULL,
            PRIMARY KEY(bucket_field, hash_key)
        );
        CREATE INDEX IF NOT EXISTS native_element_path_path_idx
            ON native_element_path(path);
        CREATE INDEX IF NOT EXISTS native_element_meta_path_role_ts_idx
            ON native_element_meta(path, role, ts_utc DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_kind_path_ts_idx
            ON native_element_meta(element_kind, path, ts_utc);
        CREATE INDEX IF NOT EXISTS native_element_meta_kind_ts_path_idx
            ON native_element_meta(element_kind, ts_utc, path);
        CREATE INDEX IF NOT EXISTS native_element_meta_path_ts_idx
            ON native_element_meta(path, ts_utc DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_path_rowid_idx
            ON native_element_meta(path, rowid DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_path_role_rowid_idx
            ON native_element_meta(path, role, rowid DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_cwd_role_ts_idx
            ON native_element_meta(cwd, role, ts_utc DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_cwd_ts_idx
            ON native_element_meta(cwd, ts_utc DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_cwd_role_ts_asc_idx
            ON native_element_meta(cwd, role, ts_utc ASC, rowid ASC);
        CREATE INDEX IF NOT EXISTS native_element_meta_cwd_ts_asc_idx
            ON native_element_meta(cwd, ts_utc ASC, rowid ASC);
        CREATE INDEX IF NOT EXISTS native_element_meta_sid_ts_idx
            ON native_element_meta(sid, ts_utc DESC);
        CREATE INDEX IF NOT EXISTS native_element_meta_text_hash_idx
            ON native_element_meta(text_sha256);
        CREATE INDEX IF NOT EXISTS native_element_meta_norm_hash_idx
            ON native_element_meta(norm_text_sha256, norm_text_len, rowid, ts_utc, text_len);
        CREATE INDEX IF NOT EXISTS native_element_meta_prefix_1024_idx
            ON native_element_meta(prefix_1024_sha256, norm_text_len, norm_text_sha256, rowid, ts_utc);
        CREATE INDEX IF NOT EXISTS native_element_meta_prefix_4096_idx
            ON native_element_meta(prefix_4096_sha256, norm_text_len, norm_text_sha256, rowid, ts_utc);
        CREATE INDEX IF NOT EXISTS native_element_meta_prefix_8192_idx
            ON native_element_meta(prefix_8192_sha256, norm_text_len, norm_text_sha256, rowid, ts_utc);
        CREATE INDEX IF NOT EXISTS native_repeat_group_hash_idx
            ON native_repeat_group(kind, bucket_field, hash_key);
        CREATE INDEX IF NOT EXISTS native_element_repeat_group_idx
            ON native_element_repeat(group_id);
        """
    )


def _state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM native_corpus_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _state_float(conn: sqlite3.Connection, key: str) -> float:
    value = _state_get(conn, key)
    if value is None:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _write_refresh_request_marker() -> None:
    path = _db_path()
    if not path.exists():
        return
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=100")
    try:
        _state_set(conn, _REFRESH_REQUESTED_AT_KEY, str(time.time()))
        conn.commit()
    finally:
        conn.close()


def _refresh_request_pending() -> bool:
    try:
        conn = _readonly_connection()
        return _state_float(conn, _REFRESH_REQUESTED_AT_KEY) > _state_float(
            conn, _REFRESH_HANDLED_AT_KEY
        )
    except sqlite3.Error:
        return False


def _mark_refresh_request_handled(conn: sqlite3.Connection) -> None:
    requested_at = _state_float(conn, _REFRESH_REQUESTED_AT_KEY)
    if requested_at <= _state_float(conn, _REFRESH_HANDLED_AT_KEY):
        return
    _state_set(conn, _REFRESH_HANDLED_AT_KEY, str(requested_at))


def _queue_pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM native_full_scan_queue WHERE processed = 0"
    ).fetchone()[0])


def _queue_total_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM native_full_scan_queue").fetchone()[0])


def _queue_seed(conn: sqlite3.Connection, on_disk: list[tuple[Path, str, float, int]]) -> None:
    conn.execute("DELETE FROM native_full_scan_queue")
    conn.executemany(
        "INSERT INTO native_full_scan_queue(path, tag, mtime, size, processed) VALUES (?,?,?,?,0)",
        [(str(path), tag, mt, sz) for path, tag, mt, sz in on_disk],
    )


def _queue_batch(conn: sqlite3.Connection, limit: int) -> list[tuple[Path, str, float, int]]:
    return [
        (Path(path), tag, float(mtime), int(size))
        for path, tag, mtime, size in conn.execute(
            "SELECT path, tag, mtime, size FROM native_full_scan_queue "
            "WHERE processed = 0 ORDER BY path LIMIT ?",
            (limit,),
        )
    ]


def _queue_mark_processed(conn: sqlite3.Connection, paths: list[str]) -> None:
    if not paths:
        return
    conn.executemany(
        "UPDATE native_full_scan_queue SET processed = 1 WHERE path = ?",
        [(path,) for path in paths],
    )


def _queue_clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM native_full_scan_queue")


def _queue_enqueue(conn: sqlite3.Connection, rows: list[tuple[Path, str, float, int]]) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO native_full_scan_queue"
        "(path, tag, mtime, size, processed) VALUES (?,?,?,?,0)",
        [(str(path), tag, mt, sz) for path, tag, mt, sz in rows],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO native_full_scan_seen(path) VALUES (?)",
        [(str(path),) for path, _tag, _mt, _sz in rows],
    )


def _full_scan_seen_contains(conn: sqlite3.Connection, path: Path) -> bool:
    return conn.execute(
        "SELECT 1 FROM native_full_scan_seen WHERE path = ? LIMIT 1",
        (str(path),),
    ).fetchone() is not None


def _full_scan_mark_seen(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO native_full_scan_seen(path) VALUES (?)",
        (str(path),),
    )


def _full_scan_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    raw = _state_get(conn, "full_scan_state_json")
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def _set_full_scan_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    _state_set(conn, "full_scan_state_json", json.dumps(state, separators=(",", ":")))


def _clear_full_scan_state(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM native_corpus_state WHERE key = 'full_scan_state_json'")
    conn.execute("DELETE FROM native_full_scan_seen")


def _start_full_scan(conn: sqlite3.Connection) -> dict[str, Any]:
    native_roots, _, _, _ = _roots_and_resolver()
    conn.execute("DELETE FROM native_full_scan_seen")
    _queue_clear(conn)
    state = {
        "roots": [
            {"path": str(root), "tag": tag}
            for root, tag in native_roots()
            if root.exists()
        ],
        "root_index": 0,
        "stack": [],
        "complete": False,
    }
    _set_full_scan_state(conn, state)
    return state


def _scan_state_has_duplicate_frames(state: dict[str, Any]) -> bool:
    stack = state.get("stack") if isinstance(state.get("stack"), list) else []
    seen: set[tuple[str, str]] = set()
    for item in stack:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("path") or ""),
            str(item.get("tag") or ""),
        )
        if key in seen:
            return True
        seen.add(key)
    return False


def _scan_dir_signature(path: Path) -> dict[str, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return {"dir_mtime_ns": st.st_mtime_ns, "dir_size": st.st_size}


def _scan_frame(
    path: str,
    tag: str,
    *,
    cursor: str = "",
    offset: int | None = None,
    signature: dict[str, int] | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"path": path, "tag": tag, "cursor": cursor}
    if offset is not None:
        frame["offset"] = offset
    if signature:
        frame.update(signature)
    return frame


def _scan_full_batch(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    entry_budget: int | None = None,
) -> tuple[int, bool]:
    limit = _FULL_SCAN_DISCOVERY_BATCH if limit is None else limit
    entry_budget = _FULL_SCAN_ENTRY_BUDGET if entry_budget is None else entry_budget
    state = _full_scan_state(conn) or _start_full_scan(conn)
    if _scan_state_has_duplicate_frames(state):
        state = _start_full_scan(conn)
    if state.get("complete"):
        return 0, True
    _, _, _, is_native_transcript_path = _roots_and_resolver()
    roots = state.get("roots") if isinstance(state.get("roots"), list) else []
    stack = state.get("stack") if isinstance(state.get("stack"), list) else []
    root_index = int(state.get("root_index") or 0)
    discovered: list[tuple[Path, str, float, int]] = []
    visited_entries = 0

    while len(discovered) < limit and visited_entries < entry_budget:
        if not stack:
            if root_index >= len(roots):
                state["complete"] = True
                break
            root = roots[root_index]
            root_index += 1
            stack.append({"path": root.get("path"), "tag": root.get("tag"), "cursor": ""})
            continue

        item = stack.pop()
        dir_path = Path(str(item.get("path") or ""))
        tag = str(item.get("tag") or "")
        cursor = str(item.get("cursor") or "")
        offset_raw = item.get("offset")
        has_offset = isinstance(offset_raw, int)
        offset = int(offset_raw) if has_offset else 0
        next_offset = offset
        budget_exhausted = False
        child_dirs: list[str] = []
        dir_signature = _scan_dir_signature(dir_path)
        if has_offset and dir_signature:
            if (
                item.get("dir_mtime_ns") != dir_signature.get("dir_mtime_ns")
                or item.get("dir_size") != dir_signature.get("dir_size")
            ):
                offset = 0
                next_offset = 0
        try:
            with os.scandir(dir_path) as entries:
                entry_index = 0
                for entry in entries:
                    entry_index += 1
                    if entry_index <= offset:
                        continue
                    if not has_offset and cursor and entry.name <= cursor:
                        continue
                    next_offset = entry_index
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            visited_entries += 1
                            child_dirs.append(entry.path)
                            if visited_entries >= entry_budget:
                                budget_exhausted = True
                                break
                            continue
                        path = Path(entry.path)
                        if _full_scan_seen_contains(conn, path):
                            continue
                        visited_entries += 1
                        pattern_suffix = ".pb" if tag == "windsurf" else ".jsonl"
                        if not entry.name.endswith(pattern_suffix):
                            _full_scan_mark_seen(conn, path)
                            if visited_entries >= entry_budget:
                                budget_exhausted = True
                                break
                            continue
                        if not is_native_transcript_path(path, tag):
                            _full_scan_mark_seen(conn, path)
                            if visited_entries >= entry_budget:
                                budget_exhausted = True
                                break
                            continue
                        st = path.stat()
                    except OSError:
                        if visited_entries >= entry_budget:
                            budget_exhausted = True
                            break
                        continue
                    _full_scan_mark_seen(conn, path)
                    discovered.append((path, tag, st.st_mtime, st.st_size))
                    if len(discovered) >= limit or visited_entries >= entry_budget:
                        budget_exhausted = True
                        break
        except OSError:
            continue
        if budget_exhausted:
            stack.append(_scan_frame(
                str(dir_path), tag, offset=next_offset,
                signature=dir_signature,
            ))
        for child_path in child_dirs:
            stack.append(_scan_frame(child_path, tag))

    state["root_index"] = root_index
    state["stack"] = stack
    _queue_enqueue(conn, discovered)
    _set_full_scan_state(conn, state)
    return len(discovered), bool(state.get("complete"))


def _full_scan_complete(conn: sqlite3.Connection) -> bool:
    state = _full_scan_state(conn)
    return bool(state and state.get("complete"))


def _deleted_after_full_scan_batch(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0] for row in conn.execute(
            "SELECT path FROM native_file_state "
            "WHERE path NOT IN (SELECT path FROM native_full_scan_seen) "
            "ORDER BY path LIMIT ?",
            (_FULL_REFRESH_FILE_BATCH,),
        )
    ]


# ─── roots + path resolution (reused from the search module) ───────────────
# Imported lazily so this module can be imported in tests without pulling the
# full search module's rg/subprocess machinery at import time.

def _roots_and_resolver():
    if _roots_resolver_override is not None:
        return _roots_resolver_override()
    from native_session_prompt_search import (
        _candidate_from_match,
        _classify_root,
        _is_native_transcript_path,
        _native_roots,
    )
    return _native_roots, _classify_root, _candidate_from_match, _is_native_transcript_path


def _stat_walk() -> list[tuple[Path, str, float, int]]:
    """Cheap glob+stat over every native root. No content reads (no codex
    first-line peek) so this is the freshness check, not the parse."""
    _native_roots, _classify_root, _, is_native_transcript_path = _roots_and_resolver()
    out: list[tuple[Path, str, float, int]] = []
    for root, tag in _native_roots():
        if not root.exists():
            continue
        pattern = "*.pb" if tag == "windsurf" else "*.jsonl"
        for path in root.rglob(pattern):
            if not is_native_transcript_path(path, tag):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path, tag, st.st_mtime, st.st_size))
    return out


# ─── indexing ──────────────────────────────────────────────────────────────

def _timestamp_utc(ts: str) -> str:
    value = (ts or "").strip()
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _normalize_repeated_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _raw_index_after_normalized_prefix_checked(text: str, prefix_len: int) -> tuple[int, bool]:
    normalized_len = 0
    emitted_any = False
    in_whitespace = False
    for index, char in enumerate(text):
        if char.isspace():
            if emitted_any and not in_whitespace:
                if normalized_len >= prefix_len:
                    return index, True
                normalized_len += 1
                in_whitespace = True
            continue
        emitted_any = True
        in_whitespace = False
        if normalized_len >= prefix_len:
            return index, True
        normalized_len += 1
        if normalized_len >= prefix_len:
            return index + 1, True
    return len(text), normalized_len >= prefix_len


def _raw_index_after_normalized_prefix(text: str, prefix_len: int) -> int:
    raw_index, _reached = _raw_index_after_normalized_prefix_checked(text, prefix_len)
    return raw_index


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return limit


def _shared_prefix_len(left: str, right: str, minimum: int) -> int:
    common_len = _common_prefix_len(left, right)
    if common_len > minimum and left[common_len - 1].isspace():
        common_len -= 1
    return common_len


def _text_collapse_signature(text: str) -> tuple[Any, ...]:
    normalized = _normalize_repeated_text(text)
    prefix_hashes = tuple(
        _hash_text(normalized[:size]) if normalized else ""
        for size in _PREFIX_HASH_SIZES
    )
    return (
        _hash_text(text),
        _hash_text(normalized) if normalized else "",
        *prefix_hashes,
        len(text),
        len(normalized),
    )


def _index_candidate_rows(candidate, *, source_tag: str | None = None) -> list[tuple[Any, ...]]:
    """Lean-extract one transcript to FTS rows. Drops tool_result/meta and keeps
    full indexed-element text plus structural kind/tool name for categorization."""
    tag = source_tag or candidate.format
    rows: list[tuple[Any, ...]] = []
    try:
        elements = candidate.parse_elements()
    except Exception:
        return rows
    for element_index, el in enumerate(elements):
        if el.kind not in _INDEXED_KINDS:
            continue
        text = el.text
        if not text.strip():
            continue
        rows.append((
            text, str(candidate.transcript), candidate.sid, candidate.cwd,
            tag, el.kind, el.tool_name, _timestamp_utc(el.timestamp),
            el.role, el.id, element_index, *_text_collapse_signature(text),
        ))
    return rows


def _reset_repeat_projection(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM native_element_repeat_best")
    conn.execute("DELETE FROM native_element_repeat")
    conn.execute("DELETE FROM native_repeat_group")
    conn.execute("DELETE FROM native_repeat_dirty")


def _repeat_dirty_keys_from_meta_values(
    norm_text_sha256: str,
    prefix_1024_sha256: str,
    prefix_4096_sha256: str,
    prefix_8192_sha256: str,
    norm_text_len: int,
) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if norm_text_sha256 and norm_text_len >= _REPEAT_EXACT_MIN_NORM_CHARS:
        keys.append(("norm_text_sha256", norm_text_sha256))
    for field, minimum_prefix_len in _REPEAT_PREFIX_FIELDS:
        hash_key = {
            "prefix_1024_sha256": prefix_1024_sha256,
            "prefix_4096_sha256": prefix_4096_sha256,
            "prefix_8192_sha256": prefix_8192_sha256,
        }[field]
        if hash_key and norm_text_len > minimum_prefix_len:
            keys.append((field, hash_key))
    return keys


def _mark_repeat_dirty_keys(conn: sqlite3.Connection, keys: list[tuple[str, str]]) -> None:
    if not keys:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO native_repeat_dirty(bucket_field, hash_key) VALUES (?, ?)",
        keys,
    )


def _insert_repeat_group(
    conn: sqlite3.Connection,
    *,
    kind: str,
    bucket_field: str,
    hash_key: str,
    subgroup_key: str,
    members: list[dict[str, Any]],
    common_norm_prefix_len: int,
    best_by_row: dict[int, tuple[tuple[int, int, int, int], int, int, int]],
) -> int:
    if len(members) < _REPEAT_MIN_COUNT:
        return 0
    representative = min(members, key=lambda row: int(row["rowid"]))
    timestamps = [str(row["ts_utc"] or "") for row in members if row.get("ts_utc")]
    cursor = conn.execute(
        "INSERT INTO native_repeat_group("
        "kind, bucket_field, hash_key, subgroup_key, count, representative_rowid, "
        "common_norm_prefix_len, first_seen_ts, last_seen_ts"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            kind, bucket_field, hash_key, subgroup_key, len(members),
            int(representative["rowid"]), common_norm_prefix_len,
            min(timestamps) if timestamps else "",
            max(timestamps) if timestamps else "",
        ),
    )
    group_id = int(cursor.lastrowid)
    priority_kind = 0 if kind == "exact_text" else 1
    repeat_rows = []
    for row in members:
        rowid = int(row["rowid"])
        raw_tail_start = int(row.get("raw_tail_start") or 0)
        priority = (
            priority_kind,
            -common_norm_prefix_len,
            -len(members),
            group_id,
        )
        repeat_rows.append((
            rowid, group_id, raw_tail_start, common_norm_prefix_len,
            priority_kind, common_norm_prefix_len, len(members),
        ))
        current = best_by_row.get(rowid)
        if current is None or priority < current[0]:
            best_by_row[rowid] = (priority, group_id, raw_tail_start, common_norm_prefix_len)
    conn.executemany(
        "INSERT INTO native_element_repeat("
        "rowid, group_id, raw_tail_start, norm_tail_start, "
        "priority_kind, priority_prefix_len, priority_count"
        ") VALUES (?,?,?,?,?,?,?)",
        repeat_rows,
    )
    return 1


def _repair_repeat_best(conn: sqlite3.Connection, rowids: set[int]) -> None:
    if not rowids:
        return
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS temp_repeat_repair_rowid(rowid INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp_repeat_repair_rowid")
    conn.executemany(
        "INSERT OR IGNORE INTO temp_repeat_repair_rowid(rowid) VALUES (?)",
        [(rowid,) for rowid in sorted(rowids)],
    )
    conn.execute(
        "DELETE FROM native_element_repeat_best "
        "WHERE rowid IN (SELECT rowid FROM temp_repeat_repair_rowid)"
    )
    conn.execute(
        """
        INSERT INTO native_element_repeat_best(rowid, group_id, raw_tail_start, norm_tail_start)
        SELECT rowid, group_id, raw_tail_start, norm_tail_start
        FROM (
            SELECT
                rowid,
                group_id,
                raw_tail_start,
                norm_tail_start,
                ROW_NUMBER() OVER (
                    PARTITION BY rowid
                    ORDER BY priority_kind, -priority_prefix_len, -priority_count, group_id
                ) AS rank
            FROM native_element_repeat
            WHERE rowid IN (SELECT rowid FROM temp_repeat_repair_rowid)
        )
        WHERE rank = 1
        """,
    )


def _exact_repeat_groups(
    conn: sqlite3.Connection,
) -> Iterator[tuple[str, int, list[dict[str, Any]]]]:
    current_hash = ""
    current_rows: list[dict[str, Any]] = []
    for hash_key, rowid, ts_utc, text_len, norm_text_len in conn.execute(
        """
        WITH repeated(hash_key) AS (
            SELECT norm_text_sha256
            FROM native_element_meta
            WHERE norm_text_sha256 != '' AND norm_text_len >= ?
            GROUP BY norm_text_sha256 HAVING COUNT(*) >= ?
        )
        SELECT m.norm_text_sha256, m.rowid, m.ts_utc, m.text_len, m.norm_text_len
        FROM native_element_meta m
        JOIN repeated r ON r.hash_key = m.norm_text_sha256
        WHERE m.norm_text_len >= ?
        ORDER BY m.norm_text_sha256
        """,
        (_REPEAT_EXACT_MIN_NORM_CHARS, _REPEAT_MIN_COUNT, _REPEAT_EXACT_MIN_NORM_CHARS),
    ):
        hash_key = str(hash_key)
        if current_hash and hash_key != current_hash:
            common_len = min((int(row["norm_text_len"]) for row in current_rows), default=0)
            yield current_hash, common_len, current_rows
            current_rows = []
        current_hash = hash_key
        current_rows.append({
            "rowid": int(rowid),
            "ts_utc": ts_utc or "",
            "raw_tail_start": int(text_len or 0),
            "norm_text_len": int(norm_text_len or 0),
        })
    if current_hash:
        common_len = min((int(row["norm_text_len"]) for row in current_rows), default=0)
        yield current_hash, common_len, current_rows


def _prefix_repeat_buckets(
    conn: sqlite3.Connection,
    field: str,
    minimum_prefix_len: int,
) -> Iterator[tuple[str, int, list[dict[str, Any]]]]:
    if field not in {field_name for field_name, _prefix_len in _REPEAT_PREFIX_FIELDS}:
        raise ValueError(f"unexpected repeat prefix field: {field}")
    current_hash = ""
    current_count = 0
    current_rows: list[dict[str, Any]] = []
    for hash_key, row_count, rowid, ts_utc in conn.execute(
        f"""
        WITH repeated(hash_key, row_count) AS (
            SELECT {field}, COUNT(*)
            FROM native_element_meta
            WHERE {field} != '' AND norm_text_len > ?
            GROUP BY {field}
            HAVING COUNT(*) >= ? AND COUNT(DISTINCT norm_text_sha256) >= ?
        )
        SELECT r.hash_key, r.row_count, m.rowid, m.ts_utc
        FROM repeated r
        JOIN native_element_meta m ON m.{field} = r.hash_key
        WHERE m.norm_text_len > ?
        ORDER BY r.hash_key
        """,
        (
            minimum_prefix_len,
            _REPEAT_MIN_COUNT,
            _REPEAT_PREFIX_DISTINCT_TEXT_MIN_COUNT,
            minimum_prefix_len,
        ),
    ):
        hash_key = str(hash_key)
        row_count = int(row_count)
        if current_hash and hash_key != current_hash:
            yield current_hash, current_count, current_rows
            current_rows = []
        current_hash = hash_key
        current_count = row_count
        current_rows.append({
            "rowid": int(rowid),
            "ts_utc": ts_utc or "",
            "raw_tail_start": 0,
        })
    if current_hash:
        yield current_hash, current_count, current_rows


def _repeat_bucket_members(
    conn: sqlite3.Connection,
    field: str,
    hash_key: str,
) -> tuple[str, int, list[dict[str, Any]]]:
    if field == "norm_text_sha256":
        rows = [
            {
                "rowid": int(rowid),
                "ts_utc": ts_utc or "",
                "raw_tail_start": int(text_len or 0),
                "norm_text_len": int(norm_text_len or 0),
            }
            for rowid, ts_utc, text_len, norm_text_len in conn.execute(
                "SELECT rowid, ts_utc, text_len, norm_text_len "
                "FROM native_element_meta "
                "WHERE norm_text_sha256 = ? AND norm_text_len >= ?",
                (hash_key, _REPEAT_EXACT_MIN_NORM_CHARS),
            )
        ]
        common_len = min((int(row["norm_text_len"]) for row in rows), default=0)
        return "exact_text", common_len, rows
    prefix_lengths = dict(_REPEAT_PREFIX_FIELDS)
    if field not in prefix_lengths:
        raise ValueError(f"unexpected repeat bucket field: {field}")
    minimum_prefix_len = prefix_lengths[field]
    distinct_count = conn.execute(
        f"SELECT COUNT(DISTINCT norm_text_sha256) FROM native_element_meta "
        f"WHERE {field} = ? AND norm_text_len > ?",
        (hash_key, minimum_prefix_len),
    ).fetchone()[0]
    if int(distinct_count) < _REPEAT_PREFIX_DISTINCT_TEXT_MIN_COUNT:
        return "shared_prefix", minimum_prefix_len, []
    rows = [
        {
            "rowid": int(rowid),
            "ts_utc": ts_utc or "",
            "raw_tail_start": 0,
        }
        for rowid, ts_utc in conn.execute(
            f"SELECT rowid, ts_utc FROM native_element_meta "
            f"WHERE {field} = ? AND norm_text_len > ?",
            (hash_key, minimum_prefix_len),
        )
    ]
    return "shared_prefix", minimum_prefix_len, rows


def _rebuild_repeat_bucket(conn: sqlite3.Connection, field: str, hash_key: str) -> set[int]:
    group_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT group_id FROM native_repeat_group WHERE bucket_field = ? AND hash_key = ?",
            (field, hash_key),
        )
    ]
    affected_rowids: set[int] = set()
    if group_ids:
        placeholders = ",".join("?" for _ in group_ids)
        affected_rowids.update(
            int(row[0])
            for row in conn.execute(
                f"SELECT rowid FROM native_element_repeat WHERE group_id IN ({placeholders})",
                tuple(group_ids),
            )
        )
        conn.execute(
            f"DELETE FROM native_element_repeat WHERE group_id IN ({placeholders})",
            tuple(group_ids),
        )
        conn.execute(
            f"DELETE FROM native_repeat_group WHERE group_id IN ({placeholders})",
            tuple(group_ids),
        )
    kind, common_len, members = _repeat_bucket_members(conn, field, hash_key)
    affected_rowids.update(int(row["rowid"]) for row in members)
    if len(members) >= _REPEAT_MIN_COUNT:
        _insert_repeat_group(
            conn,
            kind=kind,
            bucket_field=field,
            hash_key=hash_key,
            subgroup_key=hash_key,
            members=members,
            common_norm_prefix_len=common_len,
            best_by_row={},
        )
    return affected_rowids


def _drain_repeat_dirty_projection(conn: sqlite3.Connection) -> dict[str, Any]:
    start = time.monotonic()
    dirty = [
        (str(field), str(hash_key))
        for field, hash_key in conn.execute(
            "SELECT bucket_field, hash_key FROM native_repeat_dirty ORDER BY bucket_field, hash_key"
        )
    ]
    affected_rowids: set[int] = set()
    for field, hash_key in dirty:
        affected_rowids.update(_rebuild_repeat_bucket(conn, field, hash_key))
    _repair_repeat_best(conn, affected_rowids)
    conn.execute("DELETE FROM native_repeat_dirty")
    elapsed_s = time.monotonic() - start
    _state_set(conn, "repeat_projection_status", "ready")
    _state_set(conn, "repeat_projection_updated_at", str(time.time()))
    return {
        "duration_s": elapsed_s,
        "dirty_buckets": len(dirty),
        "rows_repaired": len(affected_rowids),
    }


def _rebuild_repeat_projection(conn: sqlite3.Connection) -> dict[str, Any]:
    start = time.monotonic()
    _reset_repeat_projection(conn)
    best_by_row: dict[int, tuple[tuple[int, int, int, int], int, int, int]] = {}
    groups_inserted = 0
    exact_groups = 0
    prefix_groups = 0

    for hash_key, common_len, members in _exact_repeat_groups(conn):
        inserted = _insert_repeat_group(
            conn,
            kind="exact_text",
            bucket_field="norm_text_sha256",
            hash_key=hash_key,
            subgroup_key=hash_key,
            members=members,
            common_norm_prefix_len=common_len,
            best_by_row=best_by_row,
        )
        groups_inserted += inserted
        exact_groups += inserted

    for field, minimum_prefix_len in _REPEAT_PREFIX_FIELDS:
        for hash_key, row_count, text_rows in _prefix_repeat_buckets(
            conn,
            field,
            minimum_prefix_len,
        ):
            inserted = _insert_repeat_group(
                conn,
                kind="shared_prefix",
                bucket_field=field,
                hash_key=hash_key,
                subgroup_key=hash_key,
                members=text_rows,
                common_norm_prefix_len=minimum_prefix_len,
                best_by_row=best_by_row,
            )
            groups_inserted += inserted
            prefix_groups += inserted

    conn.executemany(
        "INSERT INTO native_element_repeat_best("
        "rowid, group_id, raw_tail_start, norm_tail_start"
        ") VALUES (?,?,?,?)",
        [
            (rowid, group_id, raw_tail_start, norm_tail_start)
            for rowid, (_priority, group_id, raw_tail_start, norm_tail_start)
            in best_by_row.items()
        ],
    )
    elapsed_s = time.monotonic() - start
    _state_set(conn, "repeat_projection_status", "ready")
    _state_set(conn, "repeat_projection_rebuilt_at", str(time.time()))
    _state_set(conn, "repeat_projection_duration_s", f"{elapsed_s:.6f}")
    _state_set(conn, "repeat_projection_groups", str(groups_inserted))
    _state_set(conn, "repeat_projection_exact_groups", str(exact_groups))
    _state_set(conn, "repeat_projection_prefix_groups", str(prefix_groups))
    _state_set(conn, "repeat_projection_rows", str(len(best_by_row)))
    return {
        "duration_s": elapsed_s,
        "groups": groups_inserted,
        "exact_groups": exact_groups,
        "prefix_groups": prefix_groups,
        "rows": len(best_by_row),
    }


def _replace_candidate(
    conn: sqlite3.Connection,
    candidate,
    mtime: float,
    size: int,
    source_tag: str | None = None,
) -> tuple[int, dict[str, float]]:
    tag = source_tag or candidate.format
    path = str(candidate.transcript)
    delete_start = time.monotonic()
    _delete_path(conn, path, file_state=False)
    delete_s = time.monotonic() - delete_start

    parse_start = time.monotonic()
    rows = _index_candidate_rows(candidate, source_tag=tag)
    parse_s = time.monotonic() - parse_start

    insert_start = time.monotonic()
    if rows:
        path_rows = []
        meta_rows = []
        text_rows = []
        for row in rows:
            cursor = conn.execute(
                f"INSERT INTO native_element_fts({', '.join(_FTS_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _FTS_COLUMNS)})",
                row,
            )
            rowid = cursor.lastrowid
            path_rows.append((rowid, path))
            meta_rows.append((rowid, *row[1:]))
            text_rows.append((rowid, row[0]))
        conn.executemany(
            "INSERT INTO native_element_path(rowid, path) VALUES (?, ?)",
            path_rows,
        )
        conn.executemany(
            f"INSERT INTO native_element_meta(rowid, {', '.join(_META_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in range(len(_META_COLUMNS) + 1))})",
            meta_rows,
        )
        conn.executemany(
            "INSERT INTO native_element_text(rowid, text) VALUES (?, ?)",
            text_rows,
        )
        dirty_keys: list[tuple[str, str]] = []
        for meta_row in meta_rows:
            dirty_keys.extend(
                _repeat_dirty_keys_from_meta_values(
                    str(meta_row[12] or ""),
                    str(meta_row[13] or ""),
                    str(meta_row[14] or ""),
                    str(meta_row[15] or ""),
                    int(meta_row[17] or 0),
                )
            )
        _mark_repeat_dirty_keys(conn, dirty_keys)
    insert_s = time.monotonic() - insert_start

    state_start = time.monotonic()
    user_prompt_timestamps = [
        row[7] for row in rows
        if row[5] == "user_prompt" and row[7]
    ]
    first_user_prompt_ts = min(user_prompt_timestamps) if user_prompt_timestamps else None
    message_count = sum(
        1 for row in rows
        if row[5] in {"user_prompt", "assistant_text"} and row[7]
    )
    turn_source = run_source_index.classify_path(path)
    # BA internal workers (machine-completion, search, file-editor, adversarial
    # review, testape, …) often spawn providers with no durable run record, so
    # classify_path sees them as external. Their user prompts carry BA-defined
    # marker tags (`<machine-completion-prep>`, `<search-worker-provision>`,
    # `<worker-prep>`, …). Codex transcripts prepend an AGENTS.md instructions
    # user message, so the marker is not always the FIRST prompt — scan the
    # first few user prompts and override to internal (non-user) on any match.
    if turn_source == run_source_index.EXTERNAL and rows:
        user_texts = [
            r[0] for r in rows if r[5] == "user_prompt"
        ][:_INTERNAL_PROMPT_SCAN_LIMIT]
        if any(
            native_internal_prompt.is_internal_import_prompt(text)
            for text in user_texts if text
        ):
            turn_source = run_source_index.INTERNAL
    conn.execute(
        "INSERT INTO native_file_state("
        "path, mtime, size, tag, sid, cwd, first_user_prompt_ts, message_count, "
        "turn_source, indexed_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "mtime=excluded.mtime, size=excluded.size, tag=excluded.tag, "
        "sid=excluded.sid, cwd=excluded.cwd, "
        "first_user_prompt_ts=excluded.first_user_prompt_ts, "
        "message_count=excluded.message_count, "
        "turn_source=excluded.turn_source, indexed_at=excluded.indexed_at",
        (
            path,
            mtime,
            size,
            tag,
            candidate.sid,
            candidate.cwd,
            first_user_prompt_ts,
            message_count,
            turn_source,
            time.time(),
        ),
    )
    state_s = time.monotonic() - state_start
    return len(rows), {
        "delete_s": delete_s,
        "parse_s": parse_s,
        "insert_s": insert_s,
        "state_s": state_s,
    }


def _delete_path(conn: sqlite3.Connection, path: str, *, file_state: bool = True) -> None:
    rows = [
        row
        for row in conn.execute(
            "SELECT rowid, norm_text_sha256, prefix_1024_sha256, prefix_4096_sha256, "
            "prefix_8192_sha256, norm_text_len "
            "FROM native_element_meta WHERE path = ?",
            (path,),
        )
    ]
    dirty_keys: list[tuple[str, str]] = []
    for _rowid, norm_hash, prefix_1024, prefix_4096, prefix_8192, norm_len in rows:
        dirty_keys.extend(
            _repeat_dirty_keys_from_meta_values(
                str(norm_hash or ""),
                str(prefix_1024 or ""),
                str(prefix_4096 or ""),
                str(prefix_8192 or ""),
                int(norm_len or 0),
            )
        )
    _mark_repeat_dirty_keys(conn, dirty_keys)
    rowids = [row[0] for row in rows]
    if rowids:
        conn.executemany(
            "DELETE FROM native_element_repeat WHERE rowid = ?",
            [(rowid,) for rowid in rowids],
        )
        conn.executemany(
            "DELETE FROM native_element_repeat_best WHERE rowid = ?",
            [(rowid,) for rowid in rowids],
        )
        conn.executemany(
            "DELETE FROM native_element_fts WHERE rowid = ?",
            [(rowid,) for rowid in rowids],
        )
        conn.executemany(
            "DELETE FROM native_element_text WHERE rowid = ?",
            [(rowid,) for rowid in rowids],
        )
        conn.execute("DELETE FROM native_element_path WHERE path = ?", (path,))
        conn.execute("DELETE FROM native_element_meta WHERE path = ?", (path,))
    if file_state:
        conn.execute("DELETE FROM native_file_state WHERE path = ?", (path,))


def _compute_changes() -> tuple[list[tuple[Path, str, float, int]], set[str]]:
    """Return (on_disk_files, indexed_paths) so the caller can diff. on_disk is
    the cheap stat-walk; indexed_paths comes from native_file_state."""
    on_disk = _stat_walk()
    conn = _readonly_connection()
    try:
        indexed = {r[0] for r in conn.execute("SELECT path FROM native_file_state")}
    except sqlite3.OperationalError:
        indexed = set()
    return on_disk, indexed


def _indexed_file_states(conn: sqlite3.Connection) -> list[tuple[str, str, float, int]]:
    return [
        (str(path), str(tag), float(mtime), int(size))
        for path, tag, mtime, size in conn.execute(
            "SELECT path, tag, mtime, size FROM native_file_state"
        )
    ]


def _indexed_file_state_batch(
    conn: sqlite3.Connection,
    cursor: str,
    limit: int,
) -> tuple[list[tuple[str, str, float, int]], str]:
    if limit <= 0:
        return [], cursor
    rows = [
        (str(path), str(tag), float(mtime), int(size))
        for path, tag, mtime, size in conn.execute(
            "SELECT path, tag, mtime, size FROM native_file_state "
            "WHERE path > ? ORDER BY path LIMIT ?",
            (cursor, limit),
        )
    ]
    if len(rows) < limit and cursor:
        seen = {path for path, _tag, _mtime_value, _size in rows}
        rows.extend(
            (str(path), str(tag), float(mtime), int(size))
            for path, tag, mtime, size in conn.execute(
                "SELECT path, tag, mtime, size FROM native_file_state "
                "WHERE path <= ? ORDER BY path LIMIT ?",
                (cursor, limit - len(rows)),
            )
            if str(path) not in seen
        )
    if not rows:
        return [], ""
    return rows, rows[-1][0]


def _indexed_fingerprints_for_paths(
    conn: sqlite3.Connection, paths: list[str],
) -> dict[str, tuple[float, int]]:
    if not paths:
        return {}
    out: dict[str, tuple[float, int]] = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        placeholders = ",".join("?" for _ in chunk)
        out.update({
            r[0]: (r[1], r[2])
            for r in conn.execute(
                f"SELECT path, mtime, size FROM native_file_state "
                f"WHERE path IN ({placeholders})",
                chunk,
            )
        })
    return out


def _steady_known_paths(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[Path, str, float, int]], set[str], str]:
    native_roots, classify_root, _, is_native_transcript_path = _roots_and_resolver()
    roots = native_roots()
    cursor = _state_get(conn, "steady_refresh_cursor") or ""
    indexed, next_cursor = _indexed_file_state_batch(
        conn, cursor, _STEADY_REFRESH_FILE_BATCH,
    )
    on_disk: list[tuple[Path, str, float, int]] = []
    missing: set[str] = set()
    for path_str, tag, _mtime_value, _size in indexed:
        path = Path(path_str)
        source_tag = classify_root(path, roots)
        if not is_native_transcript_path(path, source_tag):
            missing.add(path_str)
            continue
        try:
            st = path.stat()
        except OSError:
            missing.add(path_str)
            continue
        on_disk.append((path, source_tag, st.st_mtime, st.st_size))
    indexed_paths = {path for path, _tag, _mtime_value, _size in indexed}
    return on_disk, indexed_paths, next_cursor


def refresh_once(*, full: bool | None = None) -> dict[str, int]:
    """One delta pass: re-index new/changed files, drop deleted ones, refresh
    the corpus watermark. Returns counts. Idempotent + safe to run anytime."""
    _, _, candidate_from_match, _ = _roots_and_resolver()
    global _last_refresh_at, _last_full_reconcile_at
    lock_path = _writer_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    if not portable_lock.try_lock_ex(handle.fileno()):
        handle.close()
        return {"walked": 0, "touched": 0, "locked": 1}
    try:
        with _lock:
            conn = _writer_connection()
            try:
                refresh_start = time.monotonic()
                phase_timings: dict[str, float] = {}
                plan_start = time.monotonic()
                covered = _state_get(conn, "covered") == "1"
                queued_full_scan = _queue_total_count(conn) > 0
                active_full_scan = _full_scan_state(conn) is not None
                do_full = (not covered or queued_full_scan or active_full_scan) if full is None else full
                if do_full:
                    _state_set(conn, "covered", "0")
                if do_full:
                    if _queue_total_count(conn) == 0 and not active_full_scan:
                        _start_full_scan(conn)
                    discovered_count, scan_complete = _scan_full_batch(conn)
                    walked_count = discovered_count
                    batch = _queue_batch(conn, _FULL_REFRESH_FILE_BATCH)
                    remaining_after_batch = max(0, _queue_pending_count(conn) - len(batch))
                    if not scan_complete:
                        remaining_after_batch = max(1, remaining_after_batch)
                    if not scan_complete or remaining_after_batch > 0:
                        deleted: list[str] = []
                    else:
                        deleted = _deleted_after_full_scan_batch(conn)
                else:
                    on_disk, indexed, steady_next_cursor = _steady_known_paths(conn)
                    walked_count = len(on_disk)
                    on_disk_paths = {str(p) for p, _tag, _mt, _sz in on_disk}
                    batch = on_disk
                    remaining_after_batch = 0
                    deleted = sorted(indexed - on_disk_paths)
                phase_timings["plan_s"] = time.monotonic() - plan_start

                fingerprint_start = time.monotonic()
                fingerprints = _indexed_fingerprints_for_paths(
                    conn, [str(path) for path, _tag, _mt, _sz in batch]
                )
                changed = [
                    (path, tag, mt, sz)
                    for path, tag, mt, sz in batch
                    if fingerprints.get(str(path)) != (mt, sz)
                ]
                phase_timings["fingerprint_s"] = time.monotonic() - fingerprint_start

                partial_start = time.monotonic()
                partial_full = False
                if do_full and not scan_complete:
                    partial_full = True
                elif do_full and remaining_after_batch > 0:
                    partial_full = True
                elif do_full and len(deleted) >= _FULL_REFRESH_FILE_BATCH:
                    partial_full = True
                phase_timings["partial_decision_s"] = time.monotonic() - partial_start

                new_or_changed = 0
                inserted_rows = 0
                parse_insert_s = 0.0
                file_timings: list[dict[str, Any]] = []
                index_start = time.monotonic()
                _state_set(conn, "schema_version", str(_SCHEMA_VERSION))
                for path, tag, mt, sz in changed:
                    if fingerprints.get(str(path)) != (mt, sz):
                        if not path.exists():
                            _delete_path(conn, str(path))
                            new_or_changed += 1
                            continue
                        per_file_start = time.monotonic()
                        candidate = candidate_from_match(path, tag)
                        rows_count, timings = _replace_candidate(
                            conn, candidate, mt, sz, source_tag=tag,
                        )
                        per_file_total_s = time.monotonic() - per_file_start
                        inserted_rows += rows_count
                        parse_insert_s += per_file_total_s
                        file_timings.append({
                            "path": str(path),
                            "tag": tag,
                            "size": sz,
                            "rows": rows_count,
                            "total_s": round(per_file_total_s, 6),
                            "delete_s": round(timings["delete_s"], 6),
                            "parse_s": round(timings["parse_s"], 6),
                            "insert_s": round(timings["insert_s"], 6),
                            "state_s": round(timings["state_s"], 6),
                        })
                        new_or_changed += 1
                phase_timings["index_s"] = time.monotonic() - index_start

                delete_start = time.monotonic()
                for path_str in deleted:
                    _delete_path(conn, path_str)
                    new_or_changed += 1
                phase_timings["delete_s"] = time.monotonic() - delete_start

                repeat_start = time.monotonic()
                repeat_stats: dict[str, Any] = {}
                projection_dirty = bool(changed or deleted)
                projection_status = _state_get(conn, "repeat_projection_status") or ""
                if partial_full:
                    if projection_dirty:
                        _reset_repeat_projection(conn)
                        _state_set(conn, "repeat_projection_status", "stale")
                elif projection_dirty and projection_status == "ready":
                    repeat_stats = _drain_repeat_dirty_projection(conn)
                elif projection_dirty or projection_status != "ready":
                    _state_set(conn, "repeat_projection_status", "building")
                    repeat_stats = _rebuild_repeat_projection(conn)
                phase_timings["repeat_projection_s"] = time.monotonic() - repeat_start

                queue_start = time.monotonic()
                if do_full:
                    _queue_mark_processed(conn, [str(path) for path, _tag, _mt, _sz in batch])
                phase_timings["queue_mark_s"] = time.monotonic() - queue_start

                state_start = time.monotonic()
                now = time.time()
                _state_set(conn, "last_walk_at", str(now))
                _mark_refresh_request_handled(conn)
                if do_full:
                    if not partial_full:
                        _queue_clear(conn)
                        _clear_full_scan_state(conn)
                        _state_set(conn, "covered", "1")
                        _state_set(conn, "last_full_reconcile_at", str(now))
                        _last_full_reconcile_at = now
                else:
                    _state_set(conn, "steady_refresh_cursor", steady_next_cursor)
                duration_s = time.monotonic() - refresh_start
                _state_set(conn, "last_refresh_duration_s", f"{duration_s:.6f}")
                _state_set(conn, "last_refresh_changed", str(len(changed)))
                _state_set(conn, "last_refresh_deleted", str(len(deleted)))
                _state_set(conn, "last_refresh_inserted_rows", str(inserted_rows))
                _state_set(conn, "last_refresh_parse_insert_s", f"{parse_insert_s:.6f}")
                _state_set(conn, "last_refresh_repeat_projection_json",
                           json.dumps(repeat_stats, separators=(",", ":")))
                _state_set(conn, "last_refresh_batch_size", str(len(batch)))
                _state_set(conn, "last_refresh_remaining", str(remaining_after_batch))
                slowest_files = sorted(
                    file_timings,
                    key=lambda row: row["total_s"],
                    reverse=True,
                )[:_MAX_FILE_TIMING_ROWS]
                _state_set(
                    conn,
                    "last_refresh_slowest_files_json",
                    json.dumps(slowest_files, separators=(",", ":")),
                )
                phase_timings["state_s"] = time.monotonic() - state_start

                commit_start = time.monotonic()
                conn.commit()
                phase_timings["commit_s"] = time.monotonic() - commit_start

                checkpoint_start = time.monotonic()
                _checkpoint_if_large(conn)
                phase_timings["checkpoint_s"] = time.monotonic() - checkpoint_start
                phase_timings["total_s"] = time.monotonic() - refresh_start
                rounded_phase_timings = {
                    key: round(value, 6)
                    for key, value in phase_timings.items()
                }
                _state_set(
                    conn,
                    "last_refresh_phase_timings_json",
                    json.dumps(rounded_phase_timings, separators=(",", ":")),
                )
                conn.commit()
                with _refresh_cond:
                    _last_refresh_at = time.time()
                    _refresh_cond.notify_all()
                if duration_s >= 1.0 or new_or_changed:
                    logger.info(
                        "native transcript index refresh full=%s partial=%s batch=%d "
                        "changed=%d deleted=%d rows=%d remaining=%d duration=%.2fs parse_insert=%.2fs",
                        do_full, partial_full, len(batch), len(changed), len(deleted),
                        inserted_rows, remaining_after_batch, duration_s, parse_insert_s,
                    )
                    logger.info(
                        "native transcript index phase timings %s",
                        json.dumps(rounded_phase_timings, separators=(",", ":")),
                    )
                    if slowest_files:
                        logger.info(
                            "native transcript index slowest files %s",
                            json.dumps(slowest_files[:5], separators=(",", ":")),
                        )
                return {
                    "walked": walked_count,
                    "touched": new_or_changed,
                    "locked": 0,
                    "full": 1 if do_full else 0,
                    "partial": 1 if partial_full else 0,
                }
            except Exception:
                conn.rollback()
                raise
    finally:
        try:
            portable_lock.unlock(handle.fileno())
        finally:
            handle.close()


# ─── public: freshness gates + search ──────────────────────────────────────

def schema_ok() -> bool:
    conn = _readonly_connection()
    try:
        v = _state_get(conn, "schema_version")
    except sqlite3.OperationalError:
        return False
    return v == str(_SCHEMA_VERSION)


def _has_incomplete_full_scan_state(conn: sqlite3.Connection) -> bool:
    state = _full_scan_state(conn)
    return bool(state and not state.get("complete"))


def is_covered() -> bool:
    """A full stat-walk has completed and every on-disk file was accounted for.
    While False (cold start), search must fall back to rg."""
    if not schema_ok():
        return False
    conn = _readonly_connection()
    try:
        if _has_incomplete_full_scan_state(conn):
            return False
        return _state_get(conn, "covered") == "1"
    except sqlite3.OperationalError:
        return False


def is_usable() -> bool:
    """covered AND the last refresh is within the freshness window. Usable =>
    FTS reflects the current corpus closely enough to answer; caller otherwise
    requests a refresh (see wait_fresh) or falls back to rg."""
    if not is_covered():
        return False
    conn = _readonly_connection()
    try:
        last_walk_at = _state_float(conn, "last_walk_at")
    except sqlite3.OperationalError:
        return False
    return last_walk_at > 0 and (time.time() - last_walk_at) <= _FRESH_WINDOW_SECONDS


def quick_state() -> dict[str, Any]:
    path = _db_path()
    if not path.exists():
        return {"schema_ok": False, "covered": False, "usable": False}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={_QUICK_STATE_BUSY_TIMEOUT_MS}")
        try:
            version_row = conn.execute(
                "SELECT value FROM native_corpus_state WHERE key = 'schema_version'"
            ).fetchone()
            covered_row = conn.execute(
                "SELECT value FROM native_corpus_state WHERE key = 'covered'"
            ).fetchone()
            last_walk_row = conn.execute(
                "SELECT value FROM native_corpus_state WHERE key = 'last_walk_at'"
            ).fetchone()
            scan_state_row = conn.execute(
                "SELECT value FROM native_corpus_state WHERE key = 'full_scan_state_json'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "schema_ok": False,
            "covered": False,
            "usable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    schema_is_ok = bool(version_row and version_row[0] == str(_SCHEMA_VERSION))
    incomplete_full_scan = False
    if scan_state_row and scan_state_row[0]:
        try:
            scan_state = json.loads(scan_state_row[0])
            incomplete_full_scan = bool(
                isinstance(scan_state, dict) and not scan_state.get("complete")
            )
        except json.JSONDecodeError:
            incomplete_full_scan = False
    covered = bool(
        schema_is_ok
        and covered_row
        and covered_row[0] == "1"
        and not incomplete_full_scan
    )
    last_walk_at = 0.0
    if covered and last_walk_row:
        try:
            last_walk_at = float(last_walk_row[0])
        except (TypeError, ValueError):
            last_walk_at = 0.0
    usable = bool(covered and last_walk_at > 0 and (time.time() - last_walk_at) <= _FRESH_WINDOW_SECONDS)
    return {"schema_ok": schema_is_ok, "covered": covered, "usable": usable}


def request_refresh() -> None:
    """Wake the worker for an immediate delta pass (vs waiting for the next poll)."""
    global _refresh_requested
    try:
        _write_refresh_request_marker()
    except sqlite3.Error:
        logger.debug("native transcript refresh request marker write failed", exc_info=True)
    with _refresh_cond:
        _refresh_requested = True
        _refresh_cond.notify()


def _full_reconcile_due() -> bool:
    global _last_full_reconcile_at
    if _last_full_reconcile_at <= 0:
        conn = _readonly_connection()
        try:
            _last_full_reconcile_at = (
                _state_float(conn, "last_full_reconcile_at")
                or _state_float(conn, "last_walk_at")
            )
        except sqlite3.OperationalError:
            _last_full_reconcile_at = 0.0
    return (time.time() - _last_full_reconcile_at) >= _FULL_RECONCILE_INTERVAL_SECONDS


def _require_off_loop(op: str) -> None:
    """Fail closed: blocking index reads must never run on an asyncio
    event-loop thread. Callers offload via an executor
    (run_requirements_query / asyncio.to_thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"native_transcript_index.{op} called on the asyncio event-loop thread; "
        "offload it via asyncio.to_thread or an executor"
    )


def wait_fresh(timeout: float = _FRESH_WAIT_TIMEOUT) -> bool:
    """Block until a refresh completes within the freshness window, or timeout.

    Used by the query path once covered: rather than fall to rg for a slightly
    stale index, wait for the one delta pass (stat-walk + parse-changed-only —
    cheap) then serve from FTS. Returns True if fresh within the timeout; the
    timeout itself is the safety when no refresh is forthcoming (worker down)."""
    _require_off_loop("wait_fresh")
    deadline = time.monotonic() + timeout
    while not is_usable():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def ensure_fresh_for_read(timeout: float = _FRESH_WAIT_TIMEOUT) -> dict[str, Any]:
    _require_off_loop("ensure_fresh_for_read")
    state = quick_state()
    if not state.get("covered"):
        return state
    ensure_started()
    if is_covered() and not is_usable():
        request_refresh()
        wait_fresh(timeout)
    return {"schema_ok": schema_ok(), "covered": is_covered(), "usable": is_usable()}


def _match_expr(tokens: list[str]) -> str:
    # OR of quoted terms preserves the token-overlap-any semantics of the
    # Python scorer; callers re-score precisely. Quoting avoids FTS5 operator
    # interpretation of the token text.
    return " OR ".join(f'"{t}"' for t in tokens)


def match_paths(tokens: list[str], allowed: set[str], *, limit: int = _PATH_CAP) -> list[tuple[str, str]] | None:
    """Fast-path file resolution: FTS returns (path, tag) for files containing
    any needle token, cwd-filtered, capped. Returns None when not usable."""
    _require_off_loop("match_paths")
    if not tokens or not is_usable():
        return None
    allowed_encoded = {encode_cwd(c) for c in allowed}
    conn = _readonly_connection()
    try:
        rows = conn.execute(
            "SELECT path, tag, cwd FROM native_element_fts WHERE native_element_fts MATCH ? "
            "LIMIT ?",
            (_match_expr(tokens), _MATCHED_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    # If the FTS scan hit the row cap, element rows were truncated — the deduped
    # path list may be SILENTLY incomplete (a path whose only matching element
    # landed past the cap is missing). Treat as "too broad / uncertain" so the
    # caller falls back to rg instead of returning a partial answer.
    if len(rows) >= _MATCHED_SCAN_LIMIT:
        return None
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, tag, cwd in rows:
        if path in seen:
            continue
        if allowed and cwd not in allowed and encode_cwd(cwd or "") not in allowed_encoded:
            continue
        seen.add(path)
        out.append((path, tag))
        if len(out) >= limit:
            return None  # too broad — signal caller to treat as not-usable
    return out


def search_rows(tokens: list[str], *, limit: int = 50) -> list[dict[str, Any]]:
    """Return raw FTS rows (text + metadata) for matched elements. The caller
    categorizes + filters by category/kind, since this module stays
    Categorizer-free to avoid a search-module cycle."""
    _require_off_loop("search_rows")
    if not tokens or not is_usable():
        return []
    conn = _readonly_connection()
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_FTS_COLUMNS)} "
            "FROM native_element_fts WHERE native_element_fts MATCH ? LIMIT ?",
            (_match_expr(tokens), _MATCHED_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(zip(_FTS_COLUMNS, row)) for row in rows[:limit]]


# ─── read-only SQL sandbox ─────────────────────────────────────────────────
# Lets a TRUSTED caller (the assistant, over the loopback) run arbitrary SELECT
# queries against the FTS corpus for full-power search: bm25 ranking, GROUP BY
# sid, NEAR/prefix, recency ordering — expressiveness a fixed tool can't offer.
#
# Security (this is arbitrary SQL, so it is fail-closed on every axis):
# - a FRESH mode=ro connection per call (never the shared readonly conn, so the
#   authorizer/timeout never leak onto match_paths/search_rows);
# - a fail-closed AUTHORIZER: only SELECT / READ / FUNCTION / RECURSIVE are
#   allowed; ATTACH (would read arbitrary files), writes, PRAGMA, and everything
#   else are DENIED — the default is DENY;
# - a wall-clock TIMEOUT via the progress handler aborts a runaway scan;
# - only a single SELECT/WITH statement is accepted.

_SQL_TIMEOUT_SECONDS = 5.0
_SQL_PROGRESS_OPS = 10_000
_SQL_SLOW_QUERY_SECONDS = 0.5
SQL_RESULT_MAX_BYTES = 16 * 1024 * 1024
_sql_activity_lock = threading.Lock()
_sql_active_queries = 0

_ALLOWED_SQL_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
})
# FTS5 internally issues `PRAGMA data_version` (a read-only change-counter query)
# while running a MATCH. Allow ONLY that pragma, and only as a query (arg2 is
# None) — never a pragma that SETS a value. Every other pragma stays denied.
_ALLOWED_READONLY_PRAGMAS = frozenset({"data_version"})

# The table + columns the assistant's SQL sees; kept here so the tool doc agrees.
SQL_TABLE = "native_element_fts"
SQL_COLUMNS = _FTS_COLUMNS
SQL_META_TABLE = "native_element_meta"
SQL_META_COLUMNS = _META_COLUMNS
SQL_ELEMENT_KINDS = tuple(sorted(_INDEXED_KINDS))

_SQL_LITERAL_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b\d+(?:\.\d+)?\b")
_SQL_ORDER_BY_RE = re.compile(r"\border\s+by\b(.*?)(?:\blimit\b|\boffset\b|\)|$)")
_SQL_TS_TOKEN_RE = re.compile(r"\bts_utc\b")
_SQL_FAST_PATH_RE = re.compile(
    r"^\s*select\s+(?P<select>[\w\s.,*]+?)\s+"
    r"from\s+native_element_fts\s+"
    r"where\s+(?P<where>.*?)\s+"
    r"order\s+by\s+(?:native_element_fts\.)?rowid\s+desc\s+"
    r"limit\s+(?P<limit>\?|\d+)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_MATCH_RECENCY_RE = re.compile(
    r"^\s*select\s+(?P<select>.+?)\s+"
    r"from\s+native_element_fts\s+"
    r"where\s+(?P<where>.*?)\s+"
    r"order\s+by\s+(?:native_element_fts\.)?ts_utc"
    r"(?:\s+(?P<direction>asc|desc))?"
    r"(?:\s*,\s*(?:native_element_fts\.)?rowid\s+asc)?"
    r"(?:\s+limit\s+(?P<limit>\?|\d+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_METADATA_COUNT_RE = re.compile(
    r"^\s*select\s+count\s*\(\s*\*\s*\)"
    r"(?:\s+as\s+(?P<alias>[a-z_][a-z0-9_]*))?\s+"
    r"from\s+native_element_fts\s+where\s+(?P<where>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_EQUAL_FILTER_RE = re.compile(
    r"^(?:native_element_fts\.)?(?P<column>path|cwd|role|element_kind)\s*=\s*(?P<value>\?|"
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_CWD_GLOB_FILTER_RE = re.compile(
    r"^(?:native_element_fts\.)?cwd\s+glob\s+(?P<value>'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_BARE_PROJECTION_RE = re.compile(
    r"^(?:native_element_fts\.)?(?P<column>rowid|[a-z_][a-z0-9_]*)"
    r"(?:\s+as\s+(?P<alias>[a-z_][a-z0-9_]*))?$",
    re.IGNORECASE,
)
_SQL_SUBSTR_PROJECTION_RE = re.compile(
    r"^substr\(\s*(?:native_element_fts\.)?text\s*,\s*(?P<start>[1-9]\d*)\s*,\s*"
    r"(?P<length>[1-9]\d*)\s*\)\s+as\s+(?P<alias>[a-z_][a-z0-9_]*)$",
    re.IGNORECASE,
)
_SQL_MATCH_FILTER_RE = re.compile(
    r"^(?:native_element_fts\.)?native_element_fts\s+match\s+(?P<value>\?|"
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_TS_BOUND_FILTER_RE = re.compile(
    r"^(?:native_element_fts\.)?ts_utc\s*(?P<operator><=|>=|<|>)\s*"
    r"(?P<value>\?|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_TEXT_LIKE_FILTER_RE = re.compile(
    r"^(?:native_element_fts\.)?text\s+like\s+(?P<value>\?|"
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")$",
    re.IGNORECASE | re.DOTALL,
)


def _sql_shape(sql: str) -> dict[str, Any]:
    normalized = " ".join(sql.lower().split())
    fingerprint_sql = _SQL_LITERAL_RE.sub("?", normalized)
    padded = f" {fingerprint_sql} "

    def has_filter(column: str) -> bool:
        pattern = (
            rf"(?<![a-z0-9_])(?:[a-z_][a-z0-9_]*\.)?{re.escape(column)}\s*"
            r"(?:=|!=|<>|<=|>=|<|>|\bin\b|\blike\b|\bbetween\b|\bis\b)"
        )
        return re.search(pattern, fingerprint_sql) is not None
    filters = [
        column for column in SQL_COLUMNS[1:]
        if has_filter(column)
    ]
    orders_by_ts_utc = any(
        _SQL_TS_TOKEN_RE.search(match.group(1)) is not None
        for match in _SQL_ORDER_BY_RE.finditer(fingerprint_sql)
    )
    return {
        "fingerprint": hashlib.sha256(fingerprint_sql.encode("utf-8")).hexdigest()[:16],
        "has_match": " match " in padded,
        "has_limit": " limit " in padded,
        "has_bm25": "bm25(" in normalized,
        "has_order_by": " order by " in padded,
        "orders_by_ts_utc": orders_by_ts_utc,
        "uses_native_file_state": " native_file_state " in padded,
        "uses_native_element_path": " native_element_path " in padded,
        "uses_native_element_meta": " native_element_meta " in padded,
        "filters": filters,
    }


def _record_sql_query(
    sql: str,
    elapsed_s: float,
    result: dict[str, Any],
    *,
    execution_route: str,
    progress_callbacks: int,
    plan_probe: dict[str, int] | None = None,
    timings: dict[str, float | int] | None = None,
    rejection: dict[str, Any] | None = None,
) -> None:
    result["elapsed_ms"] = round(elapsed_s * 1000.0, 3)
    if elapsed_s < _SQL_SLOW_QUERY_SECONDS:
        return
    logger.warning(
        "slow native transcript SQL elapsed_ms=%.1f rows=%d error=%s "
        "route=%s progress_callbacks=%d vm_steps_floor=%d plan_probe=%s timings=%s rejection=%s shape=%s",
        elapsed_s * 1000.0,
        len(result.get("rows") or []),
        bool(result.get("error")),
        execution_route,
        progress_callbacks,
        progress_callbacks * _SQL_PROGRESS_OPS,
        json.dumps(plan_probe or {}, sort_keys=True, separators=(",", ":")),
        json.dumps(timings or {}, sort_keys=True, separators=(",", ":")),
        json.dumps(rejection or {}, sort_keys=True, separators=(",", ":")),
        json.dumps(_sql_shape(sql), sort_keys=True, separators=(",", ":")),
    )


def _sql_authorizer(action: int, arg1, arg2, db_name, trigger) -> int:
    if action in _ALLOWED_SQL_ACTIONS:
        return sqlite3.SQLITE_OK
    if (
        action == sqlite3.SQLITE_PRAGMA
        and arg2 is None
        and (arg1 or "").lower() in _ALLOWED_READONLY_PRAGMAS
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@dataclass(frozen=True)
class _SqlProjection:
    sql: str
    uses_raw_text: bool = False


@dataclass(frozen=True)
class _SqlPredicate:
    sql: str
    kind: str
    param_index: int | None


@dataclass(frozen=True)
class _MatchRecencyQuery:
    projections: tuple[_SqlProjection, ...]
    predicates: tuple[_SqlPredicate, ...]
    direction: str
    limit: str | None
    metadata_index: str | None

    @property
    def uses_raw_text(self) -> bool:
        return any(item.uses_raw_text for item in self.projections)

    def render(self, *, drive: str) -> str:
        index_hint = f" INDEXED BY {self.metadata_index}" if drive == "metadata" and self.metadata_index else ""
        if drive == "metadata":
            from_sql = f"native_element_meta m{index_hint} CROSS JOIN native_element_fts e ON e.rowid = m.rowid"
        else:
            from_sql = "native_element_fts e CROSS JOIN native_element_meta m ON m.rowid = e.rowid"
        if self.uses_raw_text:
            from_sql += " CROSS JOIN native_element_text r ON r.rowid = e.rowid"
        limit_sql = f" LIMIT {self.limit}" if self.limit is not None else ""
        return (
            f"SELECT {', '.join(item.sql for item in self.projections)} FROM {from_sql} "
            f"WHERE {' AND '.join(item.sql for item in self.predicates)} "
            f"ORDER BY m.ts_utc {self.direction}, m.rowid ASC{limit_sql}"
        )


def _rewrite_selected_fts_columns(select_expr: str) -> list[str] | None:
    projections = _parse_sql_projections(select_expr)
    if projections is None or any(item.uses_raw_text for item in projections):
        return None
    return [item.sql.replace("m.", "e.", 1) for item in projections]


def _split_sql_list(value: str, delimiter: str) -> list[str] | None:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    depth = 0
    index = 0
    lowered = value.lower()
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            if depth < 0:
                return None
            index += 1
            continue
        if depth == 0 and lowered.startswith(delimiter, index):
            before = value[index - 1] if index else " "
            after_index = index + len(delimiter)
            after = value[after_index] if after_index < len(value) else " "
            if delimiter == "," or (before.isspace() and after.isspace()):
                parts.append(value[start:index].strip())
                start = after_index
                index = after_index
                continue
        index += 1
    if quote is not None or depth != 0:
        return None
    parts.append(value[start:].strip())
    return parts if all(parts) else None


def _parse_sql_projections(select_expr: str) -> tuple[_SqlProjection, ...] | None:
    parts = _split_sql_list(select_expr, ",")
    if not parts:
        return None
    projections: list[_SqlProjection] = []
    metadata_columns = {"rowid", *SQL_META_COLUMNS}
    for part in parts:
        bare_match = _SQL_BARE_PROJECTION_RE.fullmatch(part.strip())
        if bare_match:
            column = bare_match.group("column").lower()
            alias = bare_match.group("alias")
            alias_sql = f" AS {alias}" if alias else ""
            if column == "text":
                projections.append(_SqlProjection(f"e.text{alias_sql}"))
            elif column in metadata_columns:
                projections.append(_SqlProjection(f"m.{column}{alias_sql}"))
            else:
                return None
            continue
        substr_match = _SQL_SUBSTR_PROJECTION_RE.fullmatch(part.strip())
        if substr_match is None:
            return None
        start = int(substr_match.group("start"))
        length = int(substr_match.group("length"))
        if start > 1_000_000 or length > 1_000_000:
            return None
        alias = substr_match.group("alias")
        projections.append(_SqlProjection(f"substr(r.text,{start},{length}) AS {alias}", True))
    return tuple(projections)


def _split_sql_conjunctions(where: str) -> list[str] | None:
    return _split_sql_list(where, "and")


def _unwrap_sql_predicate(part: str) -> str:
    stripped = part.strip()
    if not (stripped.startswith("(") and stripped.endswith(")")):
        return stripped
    inner = stripped[1:-1].strip()
    return inner if _split_sql_list(inner, "and") == [inner] else stripped


def _rewrite_metadata_count_sql(sql: str, params: tuple = ()) -> str | None:
    match = _SQL_METADATA_COUNT_RE.fullmatch(sql)
    if match is None:
        return None
    raw_parts = _split_sql_conjunctions(match.group("where"))
    if raw_parts is None:
        return None
    predicates: list[str] = []
    placeholder_count = 0
    for raw_part in raw_parts:
        equal_filter = _SQL_EQUAL_FILTER_RE.fullmatch(_unwrap_sql_predicate(raw_part))
        if equal_filter is None:
            return None
        placeholder_count += int(equal_filter.group("value") == "?")
        predicates.append(
            f"m.{equal_filter.group('column').lower()} = {equal_filter.group('value')}"
        )
    if placeholder_count != len(params) or any(not isinstance(value, str) for value in params):
        return None
    alias = match.group("alias")
    alias_sql = f" AS {alias}" if alias else ""
    return (
        f"SELECT COUNT(*){alias_sql} FROM native_element_meta m "
        f"WHERE {' AND '.join(predicates)}"
    )


def _parse_match_recency_sql(sql: str) -> _MatchRecencyQuery | None:
    match = _SQL_MATCH_RECENCY_RE.fullmatch(sql)
    if match is None:
        return None
    projections = _parse_sql_projections(match.group("select"))
    raw_parts = _split_sql_conjunctions(match.group("where"))
    if projections is None or raw_parts is None:
        return None
    predicates: list[_SqlPredicate] = []
    seen: set[str] = set()
    ts_directions: set[str] = set()
    param_index = 0
    cwd_equality = False
    role_equality = False
    for raw_part in raw_parts:
        part = _unwrap_sql_predicate(raw_part)
        match_filter = _SQL_MATCH_FILTER_RE.fullmatch(part)
        ts_filter = _SQL_TS_BOUND_FILTER_RE.fullmatch(part)
        equal_filter = _SQL_EQUAL_FILTER_RE.fullmatch(part)
        glob_filter = _SQL_CWD_GLOB_FILTER_RE.fullmatch(part)
        if match_filter:
            if "match" in seen:
                return None
            seen.add("match")
            value = match_filter.group("value")
            current_param = param_index if value == "?" else None
            param_index += int(current_param is not None)
            predicates.append(_SqlPredicate(f"native_element_fts MATCH {value}", "match", current_param))
            continue
        if ts_filter:
            operator = ts_filter.group("operator")
            direction = operator[0]
            if direction in ts_directions:
                return None
            ts_directions.add(direction)
            value = ts_filter.group("value")
            current_param = param_index if value == "?" else None
            param_index += int(current_param is not None)
            predicates.append(_SqlPredicate(f"m.ts_utc {operator} {value}", "metadata", current_param))
            continue
        if equal_filter:
            column = equal_filter.group("column").lower()
            if column == "path":
                return None
            if column in seen:
                return None
            seen.add(column)
            value = equal_filter.group("value")
            current_param = param_index if value == "?" else None
            param_index += int(current_param is not None)
            predicates.append(_SqlPredicate(f"m.{column} = {value}", "metadata", current_param))
            cwd_equality = cwd_equality or column == "cwd"
            role_equality = role_equality or column == "role"
            continue
        if glob_filter:
            if "cwd" in seen:
                return None
            value = glob_filter.group("value")
            literal = value[1:-1].replace(value[0] * 2, value[0])
            if len(literal) < 2 or not literal.endswith("*") or any(char in literal[:-1] for char in "*?["):
                return None
            seen.add("cwd")
            predicates.append(_SqlPredicate(f"m.cwd GLOB {value}", "metadata", None))
            continue
        return None
    if "match" not in seen:
        return None
    direction = (match.group("direction") or "asc").upper()
    metadata_index = None
    if cwd_equality:
        metadata_index = (
            "native_element_meta_cwd_role_ts_asc_idx" if role_equality and direction == "ASC"
            else "native_element_meta_cwd_ts_asc_idx" if direction == "ASC"
            else "native_element_meta_cwd_role_ts_idx" if role_equality
            else "native_element_meta_cwd_ts_idx"
        )
    return _MatchRecencyQuery(
        projections=projections,
        predicates=tuple(predicates),
        direction=direction,
        limit=match.group("limit"),
        metadata_index=metadata_index,
    )


def _match_recency_rejection(sql: str) -> dict[str, Any]:
    """Return categorical optimizer diagnostics without retaining SQL or literals."""
    normalized = " ".join((sql or "").lower().split())
    structural = _SQL_LITERAL_RE.sub("?", normalized)
    statement = _SQL_MATCH_RECENCY_RE.fullmatch(sql)
    where_parts = _split_sql_conjunctions(statement.group("where")) if statement else None
    features = {
        "projection_alias": bool(re.search(r"\bselect\b.*?\bas\s+[a-z_]", structural)),
        "qualified_projection": bool(re.search(r"\bselect\b.*?native_element_fts\.", structural)),
        "secondary_order": bool(re.search(r"\border\s+by\b[^;]*,", structural)),
        "predicate_or": bool(where_parts and any(
            re.search(r"\bor\b", _SQL_LITERAL_RE.sub("?", part), re.IGNORECASE)
            for part in where_parts
        )),
        "has_offset": " offset " in f" {structural} ",
        "has_escape": bool(re.search(r"\bescape\b", structural)),
    }
    match = _SQL_MATCH_RECENCY_RE.fullmatch(sql)
    if match is None:
        reason = "statement_shape"
        stage = "statement"
    elif _parse_sql_projections(match.group("select")) is None:
        reason = "unsupported_projection"
        stage = "projection"
    elif _split_sql_conjunctions(match.group("where")) is None:
        reason = "invalid_conjunctions"
        stage = "predicate"
    else:
        reason = "unsupported_predicate"
        stage = "predicate"
    return {"stage": stage, "reason": reason, "features": features}


def _expensive_predicate_rejection(sql: str) -> dict[str, Any] | None:
    """Reject raw-text scans before opening SQLite, without retaining literals."""
    tokens = _tokenize_sql_bounded(sql)
    if tokens is None:
        stripped = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", sql, flags=re.DOTALL)
        if not (re.search(r"\blike\b", stripped, re.IGNORECASE)
                and re.search(r"\btext\b", stripped, re.IGNORECASE)):
            return None
    else:
        like_analysis = _where_like_analysis(tokens)
        if like_analysis is None:
            return None
        references_raw_text = like_analysis
    remediation = {"use_indexed_predicate": True}
    if tokens is None:
        remediation["use_match"] = True
    elif references_raw_text:
        remediation["use_match"] = True
    return {
        "error": "unsupported_expensive_predicate",
        "error_code": "unsupported_expensive_predicate",
        "remediation": remediation,
        "columns": [],
        "rows": [],
    }


def _where_like_analysis(tokens: list[tuple[str, str, int]]) -> bool | None:
    """Return whether a WHERE LIKE references raw text, or None without LIKE."""
    found_like = False
    references_raw_text = False
    clause_stops = {"group", "order", "limit", "offset", "union", "returning"}
    boundaries = {"and", "or"}
    for like_index, (_kind, value, depth) in enumerate(tokens):
        if value != "like":
            continue
        in_where = False
        for kind, prior, prior_depth in reversed(tokens[:like_index]):
            if prior_depth != depth or kind not in {"word", "identifier"}:
                continue
            if prior in clause_stops:
                break
            if prior == "where":
                in_where = True
                break
        if not in_where:
            continue
        found_like = True
        left = like_index - 1
        while left >= 0:
            _kind, token, token_depth = tokens[left]
            if token_depth < depth or (token_depth == depth and token in boundaries | {"where", ","}):
                break
            left -= 1
        right = like_index + 1
        while right < len(tokens):
            _kind, token, token_depth = tokens[right]
            if token_depth < depth or (token_depth == depth and token in boundaries | clause_stops | {","}):
                break
            right += 1
        references_raw_text = references_raw_text or _expression_references_raw_text(
            tokens[left + 1:like_index]
        ) or _expression_references_raw_text(tokens[like_index + 1:right])
    return references_raw_text if found_like else None


def _tokenize_sql_bounded(sql: str) -> list[tuple[str, str, int]] | None:
    tokens: list[tuple[str, str, int]] = []
    index = 0
    depth = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            end = sql.find("\n", index + 2)
            index = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        if char == "'":
            end = index + 1
            while end < len(sql):
                if sql[end] == "'":
                    if end + 1 < len(sql) and sql[end + 1] == "'":
                        end += 2
                        continue
                    break
                end += 1
            if end >= len(sql):
                return None
            tokens.append(("literal", "?", depth))
            index = end + 1
            continue
        if char in {'"', '`', '['}:
            close = ']' if char == '[' else char
            end = sql.find(close, index + 1)
            if end < 0:
                return None
            tokens.append(("identifier", sql[index + 1:end].lower(), depth))
            index = end + 1
            continue
        if char == '(':
            tokens.append(("lparen", char, depth))
            depth += 1
            index += 1
            continue
        if char == ')':
            if depth == 0:
                return None
            depth -= 1
            tokens.append(("rparen", char, depth))
            index += 1
            continue
        word = re.match(r"[a-z_][a-z0-9_]*", sql[index:], re.IGNORECASE)
        if word:
            value = word.group(0).lower()
            tokens.append(("word", value, depth))
            index += len(word.group(0))
            continue
        if char in "?@$:":
            param = re.match(r"(?:\?|[@$:][a-z_][a-z0-9_]*)", sql[index:], re.IGNORECASE)
            if param is None:
                return None
            tokens.append(("param", "?", depth))
            index += len(param.group(0))
            continue
        if char.isdigit():
            number = re.match(r"\d+(?:\.\d+)?", sql[index:])
            tokens.append(("literal", "?", depth))
            index += len(number.group(0)) if number else 1
            continue
        if char in ".,+-*/%<>=!|&~":
            operator = re.match(r"(?:<=|>=|<>|!=|==|\|\||<<|>>|.)", sql[index:])
            tokens.append(("operator", operator.group(0), depth))
            index += len(operator.group(0))
            continue
        return None
    return tokens if depth == 0 else None


def _expression_references_raw_text(tokens: list[tuple[str, str, int]]) -> bool:
    for index, (kind, value, _depth) in enumerate(tokens):
        if kind not in {"word", "identifier"} or value != "text":
            continue
        return True
    return False


def _top_level_where_clause(sql: str) -> str | None:
    """Return the outer WHERE body without inspecting quoted literal contents."""
    quote: str | None = None
    depth = 0
    where_end: int | None = None
    index = 0
    lowered = sql.lower()
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if quote == "]":
                if char == "]":
                    quote = None
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "[":
            quote = "]"
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0:
            word = re.match(r"[a-z_][a-z0-9_]*", lowered[index:])
            if word:
                token = word.group(0)
                if token == "where" and where_end is None:
                    where_end = index + len(token)
                elif where_end is not None and token in {"group", "order", "limit", "offset", "union"}:
                    return sql[where_end:index]
                index += len(token)
                continue
        index += 1
    return sql[where_end:] if where_end is not None else None


def _all_where_clauses(sql: str) -> list[str]:
    clauses: list[str] = []
    outer = _top_level_where_clause(sql)
    if outer is not None:
        clauses.append(outer)
    quote: str | None = None
    start: int | None = None
    depth = 0
    for index, char in enumerate(sql):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char == "'":
            quote = char
            continue
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
            continue
        if char == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                clauses.extend(_all_where_clauses(sql[start:index]))
                start = None
    return clauses


def _rewrite_match_recency_sql(sql: str) -> str | None:
    """Drive safe MATCH+metadata queries from a covering chronological index.

    The source ORDER BY guarantees only timestamp order. Rowid ASC is a stable
    refinement inside equal-timestamp groups, not an additional caller promise.
    """
    parsed = _parse_match_recency_sql(sql)
    return parsed.render(drive="metadata") if parsed else None


def _choose_match_recency_sql(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple,
) -> tuple[str, str, dict[str, int]] | None:
    """Choose the smaller bounded access path for validated MATCH recency SQL."""
    parsed = _parse_match_recency_sql(sql)
    if parsed is None:
        return None
    match_predicate = next(item for item in parsed.predicates if item.kind == "match")
    metadata_predicates = [item for item in parsed.predicates if item.kind == "metadata"]
    if match_predicate.param_index is not None and match_predicate.param_index >= len(params):
        return parsed.render(drive="metadata"), "match_metadata", {}
    if any(item.param_index is not None and item.param_index >= len(params) for item in metadata_predicates):
        return parsed.render(drive="metadata"), "match_metadata", {}
    probe_limit = _SQL_PLAN_PROBE_LIMIT + 1
    match_probe_sql = (
        "SELECT COUNT(*) FROM (SELECT 1 FROM native_element_fts "
        f"WHERE {match_predicate.sql} LIMIT {probe_limit})"
    )
    index_hint = f" INDEXED BY {parsed.metadata_index}" if parsed.metadata_index else ""
    metadata_where = (
        " WHERE " + " AND ".join(item.sql for item in metadata_predicates)
        if metadata_predicates else ""
    )
    metadata_probe_sql = (
        "SELECT COUNT(*) FROM (SELECT 1 FROM native_element_meta m "
        f"{index_hint}{metadata_where} LIMIT {probe_limit})"
    )
    match_params = (() if match_predicate.param_index is None else (params[match_predicate.param_index],))
    metadata_params = tuple(params[item.param_index] for item in metadata_predicates if item.param_index is not None)
    match_rows = int(conn.execute(match_probe_sql, match_params).fetchone()[0])
    metadata_rows = int(conn.execute(metadata_probe_sql, metadata_params).fetchone()[0])
    probe = {"match_rows": match_rows, "metadata_rows": metadata_rows}
    if match_rows > _SQL_PLAN_PROBE_LIMIT and metadata_rows > _SQL_PLAN_PROBE_LIMIT:
        return parsed.render(drive="fts"), "match_fts", probe
    if match_rows >= metadata_rows:
        return parsed.render(drive="metadata"), "match_metadata", probe
    return parsed.render(drive="fts"), "match_fts", probe


def _rewrite_fast_metadata_sql(sql: str, params: tuple = ()) -> str | None:
    metadata_count = _rewrite_metadata_count_sql(sql, params)
    if metadata_count is not None:
        return metadata_count
    match_recency = _rewrite_match_recency_sql(sql)
    if match_recency is not None:
        return match_recency
    match = _SQL_FAST_PATH_RE.match(sql)
    if not match:
        return None
    rewritten_columns = _rewrite_selected_fts_columns(match.group("select"))
    if rewritten_columns is None:
        return None

    columns: set[str] = set()
    where_parts: list[str] = []
    for raw_part in re.split(r"\s+and\s+", match.group("where"), flags=re.IGNORECASE):
        filter_match = _SQL_EQUAL_FILTER_RE.match(raw_part.strip())
        if not filter_match:
            return None
        column = filter_match.group("column").lower()
        if column in columns:
            return None
        columns.add(column)
        where_parts.append(f"m.{column} = {filter_match.group('value')}")
    if "path" not in columns:
        return None

    index_name = (
        "native_element_meta_path_role_rowid_idx"
        if "role" in columns
        else "native_element_meta_path_rowid_idx"
    )
    return (
        f"SELECT {', '.join(rewritten_columns)} "
        f"FROM native_element_meta m INDEXED BY {index_name} "
        "CROSS JOIN native_element_fts e ON e.rowid = m.rowid "
        f"WHERE {' AND '.join(where_parts)} "
        f"ORDER BY m.rowid DESC LIMIT {match.group('limit')}"
    )


def run_readonly_sql(
    sql: str,
    params: tuple = (),
    *,
    timeout_s: float = _SQL_TIMEOUT_SECONDS,
    max_result_bytes: int = SQL_RESULT_MAX_BYTES,
) -> dict[str, Any]:
    """Run one read-only SELECT against the native-transcript FTS index.

    Returns ``{columns, rows, covered, usable}`` or ``{error, ...}``.
    Hardened per the section header: authorizer denies anything but read/select,
    fresh mode=ro connection, timeout, single statement only."""
    _require_off_loop("run_readonly_sql")
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return {"error": "empty_sql", "columns": [], "rows": []}
    head = sql.lstrip("( \t\r\n").lower()
    if not (head.startswith("select") or head.startswith("with")):
        return {"error": "only a single SELECT/WITH query is allowed", "columns": [], "rows": []}
    expensive_rejection = _expensive_predicate_rejection(sql)
    if expensive_rejection is not None:
        return expensive_rejection
    query_budget = max(0.1, float(timeout_s))
    result_byte_budget = max(1, int(max_result_bytes))
    global _sql_active_queries
    with _sql_activity_lock:
        _sql_active_queries += 1
        query_concurrency = _sql_active_queries
    started = time.monotonic()
    deadline = started + query_budget
    timings: dict[str, float | int] = {}
    result: dict[str, Any] = {"columns": [], "rows": []}
    execution_route = "direct"
    plan_probe: dict[str, int] = {}
    rejection: dict[str, Any] = {}
    conn: sqlite3.Connection | None = None
    progress_callbacks = 0

    def record_reconcile_snapshot(suffix: str) -> None:
        if conn is None:
            return
        try:
            timings[f"reconcile_active_{suffix}"] = int(_full_scan_state(conn) is not None)
        except (sqlite3.Error, ValueError, TypeError):
            timings[f"reconcile_active_{suffix}"] = -1
        try:
            timings[f"wal_bytes_{suffix}"] = _db_path().with_suffix(
                _db_path().suffix + "-wal"
            ).stat().st_size
        except OSError:
            timings[f"wal_bytes_{suffix}"] = 0

    def phase(name: str, operation):
        phase_started = time.monotonic()
        try:
            return operation()
        finally:
            timings[f"{name}_ms"] = round((time.monotonic() - phase_started) * 1000.0, 3)

    def require_budget(phase_name: str) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError(f"native transcript SQL deadline exceeded during {phase_name}")

    def check_deadline() -> int:
        nonlocal progress_callbacks
        progress_callbacks += 1
        return 1 if time.monotonic() > deadline else 0

    try:
        rewritten_sql = phase("rewrite", lambda: _rewrite_fast_metadata_sql(sql, params))
        executed_sql = rewritten_sql or sql
        execution_route = (
            "metadata_count" if rewritten_sql and _SQL_METADATA_COUNT_RE.fullmatch(sql)
            else "path_metadata" if rewritten_sql
            else "direct"
        )
        require_budget("rewrite")
        path = _db_path()
        if not path.exists():
            result = {"error": "index_not_built", "columns": [], "rows": [], "covered": False, "usable": False}
            return result
        freshness_budget = min(
            _FRESH_WAIT_TIMEOUT,
            query_budget,
            max(0.0, deadline - time.monotonic()),
        )
        phase("freshness", lambda: ensure_fresh_for_read(timeout=freshness_budget))
        require_budget("freshness")
        conn = phase("open", lambda: _connect(path, readonly=True))
        require_budget("open")
        timings["query_concurrency"] = query_concurrency
        record_reconcile_snapshot("start")
        conn.set_progress_handler(check_deadline, _SQL_PROGRESS_OPS)
        conn.set_authorizer(_sql_authorizer)
        match_plan = phase("plan_probe", lambda: _choose_match_recency_sql(conn, sql, params))
        if match_plan is not None:
            executed_sql, execution_route, plan_probe = match_plan
        elif _sql_shape(sql)["has_match"] and _sql_shape(sql)["orders_by_ts_utc"]:
            rejection = _match_recency_rejection(sql)
        require_budget("plan_probe")
        cur = phase("execute", lambda: conn.execute(executed_sql, params))
        timings["cursor_execute_ms"] = timings["execute_ms"]
        columns = [d[0] for d in (cur.description or [])]
        def materialize_rows() -> tuple[list[list[Any]], int]:
            rows: list[list[Any]] = []
            result_bytes = 0
            cells = 0
            text_chunk_chars = 64 * 1024
            first_row_ms = 0.0
            fetch_ms = 0.0
            transform_ms = 0.0

            def transform_batch(raw_rows: list[tuple[Any, ...]]) -> None:
                nonlocal result_bytes, cells, transform_ms
                transform_started = time.monotonic()
                for raw_row in raw_rows:
                    row = list(raw_row)
                    for value in row:
                        require_budget("materialize")
                        if isinstance(value, str):
                            for start in range(0, len(value), text_chunk_chars):
                                result_bytes += len(value[start:start + text_chunk_chars].encode("utf-8"))
                                require_budget("materialize")
                        elif isinstance(value, (bytes, bytearray, memoryview)):
                            result_bytes += len(value)
                        elif value is not None:
                            rendered = str(value)
                            require_budget("materialize")
                            result_bytes += len(rendered.encode("utf-8"))
                        if result_bytes > result_byte_budget:
                            raise OverflowError("native transcript SQL result exceeds byte budget")
                        cells += 1
                        if cells % 256 == 0:
                            require_budget("materialize")
                    rows.append(row)
                transform_ms += (time.monotonic() - transform_started) * 1000.0

            require_budget("materialize")
            first_started = time.monotonic()
            first_row = cur.fetchone()
            first_row_ms = (time.monotonic() - first_started) * 1000.0
            if first_row is not None:
                transform_batch([first_row])
            while True:
                fetch_started = time.monotonic()
                raw_rows = cur.fetchmany(256)
                fetch_ms += (time.monotonic() - fetch_started) * 1000.0
                if not raw_rows:
                    break
                transform_batch(raw_rows)
            require_budget("materialize")
            timings["first_row_ms"] = round(first_row_ms, 3)
            timings["fetch_ms"] = round(fetch_ms, 3)
            timings["post_execute_fetch_ms"] = round(first_row_ms + fetch_ms, 3)
            timings["sqlite_work_ms"] = round(
                float(timings.get("cursor_execute_ms", 0.0)) + first_row_ms + fetch_ms,
                3,
            )
            timings["transform_ms"] = round(transform_ms, 3)
            return rows, result_bytes
        rows, result_bytes = phase("materialize", materialize_rows)
        timings["result_bytes"] = result_bytes
        result = {
            "columns": columns,
            "rows": rows,
            "covered": is_covered(),
            "usable": is_usable(),
            "timings": timings,
            "execution_route": execution_route,
        }
    except OverflowError:
        timings["result_byte_limit"] = result_byte_budget
        result = {
            "error": "result_too_large",
            "error_code": "result_too_large",
            "max_result_bytes": result_byte_budget,
            "columns": [],
            "rows": [],
        }
    except (sqlite3.Error, TimeoutError) as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "columns": [], "rows": []}
    finally:
        record_reconcile_snapshot("end")
        elapsed_s = time.monotonic() - started
        timings["total_ms"] = round(elapsed_s * 1000.0, 3)
        _record_sql_query(
            sql,
            elapsed_s,
            result,
            execution_route=execution_route,
            progress_callbacks=progress_callbacks,
            plan_probe=plan_probe,
            timings=timings,
            rejection=rejection,
        )
        if conn is not None:
            try:
                conn.set_authorizer(None)
                conn.set_progress_handler(None, 0)
            except sqlite3.Error:
                pass
            conn.close()
        with _sql_activity_lock:
            _sql_active_queries -= 1
    return result


# ─── background worker ─────────────────────────────────────────────────────

def ensure_started() -> None:
    """Start the external daemon that keeps the index covered + fresh."""
    global _worker_process, _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        _stop.clear()
        existing_pid = _read_worker_pid()
        if existing_pid and _is_process_alive(existing_pid):
            _worker_started = True
            return
        _clear_worker_pid(existing_pid)
        log_path = _worker_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), _WORKER_ARG],
                cwd=str(Path(__file__).resolve().parent),
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        _write_worker_pid(proc.pid)
        _worker_process = proc
        _worker_started = True
        logger.info("native transcript index worker process started pid=%s", proc.pid)


def _worker_main(parent_pid: int | None = None) -> None:
    # Cold start: keep doing full delta passes until covered, then poll. Each
    # refresh (refresh_once) stamps _last_refresh_at + notifies waiting queries.
    global _refresh_requested
    while not _stop.is_set():
        if parent_pid and not _is_process_alive(parent_pid):
            break
        try:
            full = None
            if is_covered() and _full_reconcile_due():
                full = True
            result = refresh_once(full=full)
            if result.get("locked"):
                _append_worker_log("native transcript index worker: writer locked")
        except Exception:
            logger.exception("native transcript index refresh failed")
            return  # avoid a hot failure loop; next ensure_started() restarts
        if result.get("partial"):
            _stop.wait(0.2)
            continue
        if is_covered():
            # Sleep for the poll interval, but wake immediately if a query
            # requested a refresh (vs waiting up to the full interval).
            deadline = time.monotonic() + _POLL_INTERVAL_SECONDS
            with _refresh_cond:
                while (
                    not _refresh_requested
                    and not _refresh_request_pending()
                    and not _stop.is_set()
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    _refresh_cond.wait(min(remaining, _WORKER_POLL_INTERVAL_SECONDS))
                _refresh_requested = False
        else:
            _stop.wait(0.2)  # throttle the initial build so we don't hog disk


def _run_worker_process() -> int:
    parent_pid = os.getppid()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pid = os.getpid()
    _write_worker_pid(pid)
    try:
        logger.info("native transcript index worker process running pid=%s parent=%s", pid, parent_pid)
        _worker_main(parent_pid=parent_pid)
        return 0
    finally:
        _clear_worker_pid(pid)
        shutdown()


def _stop_worker() -> None:
    """Signal the worker to stop, wake it from its cond wait, and join it.

    Self-contained: the ``_lock`` barrier after the join guarantees the worker
    has exited before we return — a worker mid-``refresh_once`` (holding
    ``_lock``) that the ``join(timeout)`` can't reach is caught here, so callers
    don't need their own ``_lock`` barrier before clearing ``_stop`` (which would
    otherwise resume a ghost worker that keeps polling mid-test)."""
    global _worker_started, _worker_thread, _worker_process
    _stop.set()
    with _refresh_cond:
        _refresh_cond.notify_all()
    thread = _worker_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)
        with _lock:
            # Block until any in-flight refresh_once (which holds _lock) is done;
            # on return the worker has seen _stop and exited its loop.
            pass
        if thread.is_alive():
            logger.warning("native transcript index worker did not stop within 2s")
    proc = _worker_process
    if proc is None:
        pid = _read_worker_pid()
        if pid and _is_process_alive(pid):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    else:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        _clear_worker_pid(proc.pid)
    _worker_started = False
    _worker_thread = None
    _worker_process = None


def shutdown() -> None:
    _stop_worker()
    with _lock:
        global _writer_conn
        if _writer_conn is not None:
            _writer_conn.close()
            _writer_conn = None


def _close_readonly_connection() -> None:
    """Drop the thread-local readonly conn so the next read reopens the file.

    Required whenever the DB file is (re)created: a readonly conn keeps the old
    inode and would read stale/empty data after reset."""
    conn = getattr(_readonly_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _readonly_local.conn = None
    _readonly_local.path = None


def reset_for_test() -> None:
    """Drop the persisted index + in-memory state; for isolated tests.

    Stops any running worker first so clearing ``_stop`` below can't race a
    ghost worker that would otherwise resume polling mid-test."""
    global _last_refresh_at, _last_full_reconcile_at, _refresh_requested
    _stop_worker()
    with _lock:
        global _writer_conn
        if _writer_conn is not None:
            _writer_conn.close()
            _writer_conn = None
    _stop.clear()
    with _refresh_cond:
        _last_refresh_at = 0.0
        _last_full_reconcile_at = 0.0
        _refresh_requested = False
    _close_readonly_connection()
    base = _db_path()
    for path in (base, base.with_suffix(base.suffix + "-wal"), base.with_suffix(base.suffix + "-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(_WORKER_ARG, action="store_true")
    args = parser.parse_args(argv)
    if args.native_transcript_index_worker:
        return _run_worker_process()
    return 0


__all__ = [
    "ensure_started", "is_covered", "is_usable", "match_paths", "search_rows",
    "run_readonly_sql", "SQL_TABLE", "SQL_COLUMNS", "SQL_ELEMENT_KINDS",
    "refresh_once", "request_refresh", "wait_fresh", "ensure_fresh_for_read",
    "reset_for_test", "shutdown",
]


if __name__ == "__main__":
    raise SystemExit(main())
