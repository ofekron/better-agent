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
import json
import logging
import os
import subprocess
import sys
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from native_session_miner import _mtime
from paths import ba_home, encode_cwd
import portable_lock

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 3
_FTS_COLUMNS = (
    "text", "path", "sid", "cwd", "tag", "element_kind", "tool_name", "ts",
    "role", "element_id", "element_index",
)
_INDEX_TEXT_CAP = 8_000  # per-element text cap; tool dumps were the old bloat
_INDEXED_KINDS = frozenset({"user_prompt", "assistant_text", "reasoning", "tool_call"})
_POLL_INTERVAL_SECONDS = 2.0
_FRESH_WINDOW_SECONDS = 3.0  # covered + last walk within this window => trusted
_FULL_RECONCILE_INTERVAL_SECONDS = 30 * 60
_MATCHED_SCAN_LIMIT = 20_000
_PATH_CAP = 1_000  # > this many matched files => "too broad", bail to caller
_SQLITE_BUSY_TIMEOUT_MS = 30_000
_QUICK_STATE_BUSY_TIMEOUT_MS = 50
_FULL_REFRESH_FILE_BATCH = 128
_CHECKPOINT_WAL_BYTES = 256 * 1024 * 1024
_WORKER_ARG = "--native-transcript-index-worker"
_WORKER_POLL_INTERVAL_SECONDS = 0.5
_WORKER_LOG_BYTES = 16 * 1024 * 1024
_MAX_FILE_TIMING_ROWS = 20

_lock = threading.Lock()  # guards writer connection lifecycle + rebuild flag
_worker_started = False
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_process: subprocess.Popen | None = None
_stop = threading.Event()

# Refresh signaling: once covered, a stale query REQUESTS a refresh and waits
# for it (one delta pass) instead of dropping to rg. _last_refresh_at is the
# in-memory freshness timestamp the worker sets after each refresh.
_refresh_cond = threading.Condition()
_last_refresh_at = 0.0
_last_full_reconcile_at = 0.0
_refresh_requested = False
_FRESH_WAIT_TIMEOUT = 3.0  # max a query blocks for a delta refresh before rg


def _db_path() -> Path:
    return ba_home() / "native_transcript_index.sqlite3"


def _writer_lock_path() -> Path:
    return _db_path().with_name(_db_path().name + ".lock")


def _worker_pid_path() -> Path:
    return _db_path().with_name(_db_path().name + ".worker.pid")


def _worker_log_path() -> Path:
    return ba_home() / "logs" / "native-transcript-index.log"


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
    _configure(conn)
    return conn


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-200000")
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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS native_file_state (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            tag TEXT NOT NULL,
            sid TEXT,
            cwd TEXT,
            indexed_at REAL NOT NULL
        );
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
        if (
            columns != _FTS_COLUMNS
            or version_row is None
            or version_row[0] != str(_SCHEMA_VERSION)
            or not path_index_exists
        ):
            conn.execute("DROP TABLE native_element_fts")
            conn.execute("DROP TABLE IF EXISTS native_element_path")
            conn.execute("DELETE FROM native_file_state")
            conn.execute("DELETE FROM native_corpus_state")
            conn.execute("DELETE FROM native_full_scan_queue")
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
            ts UNINDEXED,
            role UNINDEXED,
            element_id UNINDEXED,
            element_index UNINDEXED,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS native_element_path (
            rowid INTEGER PRIMARY KEY,
            path TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS native_element_path_path_idx
            ON native_element_path(path);
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


# ─── roots + path resolution (reused from the search module) ───────────────
# Imported lazily so this module can be imported in tests without pulling the
# full search module's rg/subprocess machinery at import time.

def _roots_and_resolver():
    from native_session_prompt_search import _candidate_from_match, _classify_root, _native_roots
    return _native_roots, _classify_root, _candidate_from_match


def _stat_walk() -> list[tuple[Path, str, float, int]]:
    """Cheap glob+stat over every native root. No content reads (no codex
    first-line peek) so this is the freshness check, not the parse."""
    _native_roots, _classify_root, _ = _roots_and_resolver()
    out: list[tuple[Path, str, float, int]] = []
    for root, tag in _native_roots():
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path, tag, st.st_mtime, st.st_size))
    return out


# ─── indexing ──────────────────────────────────────────────────────────────

def _index_candidate_rows(candidate) -> list[tuple[Any, ...]]:
    """Lean-extract one transcript to FTS rows. Drops tool_result/meta and caps
    each element's text; keeps the structural kind + tool name so callers can
    categorize without re-parsing."""
    rows: list[tuple[Any, ...]] = []
    try:
        elements = candidate.parse_elements()
    except Exception:
        return rows
    for element_index, el in enumerate(elements):
        if el.kind not in _INDEXED_KINDS:
            continue
        text = el.text
        if len(text) > _INDEX_TEXT_CAP:
            text = text[:_INDEX_TEXT_CAP]
        if not text.strip():
            continue
        rows.append((
            text, str(candidate.transcript), candidate.sid, candidate.cwd,
            candidate.format, el.kind, el.tool_name, el.timestamp,
            el.role, el.id, element_index,
        ))
    return rows


def _replace_candidate(
    conn: sqlite3.Connection,
    candidate,
    mtime: float,
    size: int,
) -> tuple[int, dict[str, float]]:
    path = str(candidate.transcript)
    delete_start = time.monotonic()
    _delete_path(conn, path, file_state=False)
    delete_s = time.monotonic() - delete_start

    parse_start = time.monotonic()
    rows = _index_candidate_rows(candidate)
    parse_s = time.monotonic() - parse_start

    insert_start = time.monotonic()
    if rows:
        path_rows = []
        for row in rows:
            cursor = conn.execute(
                "INSERT INTO native_element_fts"
                "(text, path, sid, cwd, tag, element_kind, tool_name, ts, role, element_id, element_index) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            path_rows.append((cursor.lastrowid, path))
        conn.executemany(
            "INSERT INTO native_element_path(rowid, path) VALUES (?, ?)",
            path_rows,
        )
    insert_s = time.monotonic() - insert_start

    state_start = time.monotonic()
    conn.execute(
        "INSERT INTO native_file_state(path, mtime, size, tag, sid, cwd, indexed_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "mtime=excluded.mtime, size=excluded.size, tag=excluded.tag, "
        "sid=excluded.sid, cwd=excluded.cwd, indexed_at=excluded.indexed_at",
        (path, mtime, size, candidate.format, candidate.sid, candidate.cwd, time.time()),
    )
    state_s = time.monotonic() - state_start
    return len(rows), {
        "delete_s": delete_s,
        "parse_s": parse_s,
        "insert_s": insert_s,
        "state_s": state_s,
    }


def _delete_path(conn: sqlite3.Connection, path: str, *, file_state: bool = True) -> None:
    rowids = [
        row[0]
        for row in conn.execute(
            "SELECT rowid FROM native_element_path WHERE path = ?",
            (path,),
        )
    ]
    if rowids:
        conn.executemany(
            "DELETE FROM native_element_fts WHERE rowid = ?",
            [(rowid,) for rowid in rowids],
        )
        conn.execute("DELETE FROM native_element_path WHERE path = ?", (path,))
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
) -> tuple[list[tuple[Path, str, float, int]], set[str]]:
    indexed = _indexed_file_states(conn)
    on_disk: list[tuple[Path, str, float, int]] = []
    missing: set[str] = set()
    for path_str, tag, _mtime_value, _size in indexed:
        path = Path(path_str)
        try:
            st = path.stat()
        except OSError:
            missing.add(path_str)
            continue
        on_disk.append((path, tag, st.st_mtime, st.st_size))
    indexed_paths = {path for path, _tag, _mtime_value, _size in indexed}
    return on_disk, indexed_paths | missing


def refresh_once(*, full: bool | None = None) -> dict[str, int]:
    """One delta pass: re-index new/changed files, drop deleted ones, refresh
    the corpus watermark. Returns counts. Idempotent + safe to run anytime."""
    _, _, candidate_from_match = _roots_and_resolver()
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
                do_full = not covered if full is None else full
                if do_full:
                    if _queue_total_count(conn) == 0:
                        on_disk, indexed = _compute_changes()
                        _queue_seed(conn, on_disk)
                        walked_count = len(on_disk)
                        on_disk_paths = {str(p) for p, _tag, _mt, _sz in on_disk}
                    else:
                        walked_count = _queue_pending_count(conn)
                        indexed = set()
                        on_disk_paths = set()
                    batch = _queue_batch(conn, _FULL_REFRESH_FILE_BATCH)
                    remaining_after_batch = max(0, _queue_pending_count(conn) - len(batch))
                    if remaining_after_batch > 0:
                        deleted: list[str] = []
                    else:
                        if not indexed:
                            indexed = {r[0] for r in conn.execute("SELECT path FROM native_file_state")}
                        if not on_disk_paths:
                            on_disk_paths = {
                                r[0] for r in conn.execute("SELECT path FROM native_full_scan_queue")
                            }
                        deleted = sorted(indexed - on_disk_paths)[:_FULL_REFRESH_FILE_BATCH]
                else:
                    on_disk, indexed = _steady_known_paths(conn)
                    walked_count = len(on_disk)
                    on_disk_paths = {str(p) for p, _tag, _mt, _sz in on_disk}
                    batch = on_disk
                    remaining_after_batch = 0
                    deleted = sorted(indexed - on_disk_paths)
                phase_timings["plan_s"] = time.monotonic() - plan_start

                fingerprint_start = time.monotonic()
                now = time.time()
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
                if do_full and remaining_after_batch > 0:
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
                        per_file_start = time.monotonic()
                        candidate = candidate_from_match(path, tag)
                        rows_count, timings = _replace_candidate(conn, candidate, mt, sz)
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

                queue_start = time.monotonic()
                if do_full:
                    _queue_mark_processed(conn, [str(path) for path, _tag, _mt, _sz in batch])
                phase_timings["queue_mark_s"] = time.monotonic() - queue_start

                state_start = time.monotonic()
                _state_set(conn, "last_walk_at", str(now))
                if do_full:
                    if not partial_full:
                        _queue_clear(conn)
                        _state_set(conn, "covered", "1")
                        _state_set(conn, "last_full_reconcile_at", str(now))
                        _last_full_reconcile_at = now
                duration_s = time.monotonic() - refresh_start
                _state_set(conn, "last_refresh_duration_s", f"{duration_s:.6f}")
                _state_set(conn, "last_refresh_changed", str(len(changed)))
                _state_set(conn, "last_refresh_deleted", str(len(deleted)))
                _state_set(conn, "last_refresh_inserted_rows", str(inserted_rows))
                _state_set(conn, "last_refresh_parse_insert_s", f"{parse_insert_s:.6f}")
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


def is_covered() -> bool:
    """A full stat-walk has completed and every on-disk file was accounted for.
    While False (cold start), search must fall back to rg."""
    if not schema_ok():
        return False
    conn = _readonly_connection()
    try:
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
    covered = bool(schema_is_ok and covered_row and covered_row[0] == "1")
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


def wait_fresh(timeout: float = _FRESH_WAIT_TIMEOUT) -> bool:
    """Block until a refresh completes within the freshness window, or timeout.

    Used by the query path once covered: rather than fall to rg for a slightly
    stale index, wait for the one delta pass (stat-walk + parse-changed-only —
    cheap) then serve from FTS. Returns True if fresh within the timeout; the
    timeout itself is the safety when no refresh is forthcoming (worker down)."""
    deadline = time.monotonic() + timeout
    while not is_usable():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def _match_expr(tokens: list[str]) -> str:
    # OR of quoted terms preserves the token-overlap-any semantics of the
    # Python scorer; callers re-score precisely. Quoting avoids FTS5 operator
    # interpretation of the token text.
    return " OR ".join(f'"{t}"' for t in tokens)


def match_paths(tokens: list[str], allowed: set[str], *, limit: int = _PATH_CAP) -> list[tuple[str, str]] | None:
    """Fast-path file resolution: FTS returns (path, tag) for files containing
    any needle token, cwd-filtered, capped. Returns None when not usable."""
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
    if not tokens or not is_usable():
        return []
    conn = _readonly_connection()
    try:
        rows = conn.execute(
            "SELECT text, path, sid, cwd, tag, element_kind, tool_name, ts, role, element_id, element_index "
            "FROM native_element_fts WHERE native_element_fts MATCH ? LIMIT ?",
            (_match_expr(tokens), _MATCHED_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"text": t, "path": p, "sid": sid, "cwd": cwd, "tag": tag,
         "element_kind": ek, "tool_name": tn, "ts": ts,
         "role": role, "element_id": element_id, "element_index": element_index}
        for t, p, sid, cwd, tag, ek, tn, ts, role, element_id, element_index in rows[:limit]
    ]


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
# - a ROW CAP and per-cell text cap bound the payload;
# - only a single SELECT/WITH statement is accepted.

_SQL_MAX_ROWS = 200
_SQL_MAX_CELL_CHARS = 2_000
_SQL_TIMEOUT_SECONDS = 5.0
_SQL_PROGRESS_OPS = 10_000

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
SQL_ELEMENT_KINDS = tuple(sorted(_INDEXED_KINDS))


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


def _cap_cell(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _SQL_MAX_CELL_CHARS:
        return value[:_SQL_MAX_CELL_CHARS] + "…"
    return value


def run_readonly_sql(
    sql: str,
    params: tuple = (),
    *,
    row_limit: int = _SQL_MAX_ROWS,
    timeout_s: float = _SQL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one read-only SELECT against the native-transcript FTS index.

    Returns ``{columns, rows, truncated, covered, usable}`` or ``{error, ...}``.
    Hardened per the section header: authorizer denies anything but read/select,
    fresh mode=ro connection, timeout, row+cell caps, single statement only."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return {"error": "empty_sql", "columns": [], "rows": []}
    head = sql.lstrip("( \t\r\n").lower()
    if not (head.startswith("select") or head.startswith("with")):
        return {"error": "only a single SELECT/WITH query is allowed", "columns": [], "rows": []}
    path = _db_path()
    if not path.exists():
        return {"error": "index_not_built", "columns": [], "rows": [], "covered": False, "usable": False}
    row_limit = max(1, min(int(row_limit or _SQL_MAX_ROWS), _SQL_MAX_ROWS))
    conn = _connect(path, readonly=True)
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _SQL_PROGRESS_OPS)
    conn.set_authorizer(_sql_authorizer)
    try:
        cur = conn.execute(sql, params)
        columns = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchmany(row_limit + 1)
        truncated = len(fetched) > row_limit
        rows = [[_cap_cell(v) for v in row] for row in fetched[:row_limit]]
        return {
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
            "covered": is_covered(),
            "usable": is_usable(),
        }
    except sqlite3.Error as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "columns": [], "rows": []}
    finally:
        try:
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        conn.close()


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
                start_new_session=False,
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
        if is_covered():
            # Sleep for the poll interval, but wake immediately if a query
            # requested a refresh (vs waiting up to the full interval).
            deadline = time.monotonic() + _POLL_INTERVAL_SECONDS
            with _refresh_cond:
                while not _refresh_requested and not _stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    _refresh_cond.wait(remaining)
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
    "refresh_once", "request_refresh", "wait_fresh", "reset_for_test", "shutdown",
]


if __name__ == "__main__":
    raise SystemExit(main())
