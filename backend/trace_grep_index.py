from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import portable_lock
from paths import ba_home

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_FIELD_PROMPT = "input_prompt"
_FIELD_OUTPUT = "raw_output"
_REBUILD_COND = threading.Condition()
_REBUILDING = False


def _db_path() -> Path:
    return ba_home() / "trace_grep_index.sqlite3"


def _lock_path() -> Path:
    return ba_home() / "trace_grep_index.lock"


@contextmanager
def _index_lock() -> Iterator[None]:
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        portable_lock.lock_ex(handle.fileno())
        yield
    finally:
        portable_lock.unlock(handle.fileno())
        handle.close()


def index_trace(trace: dict, trace_path: Path) -> None:
    stat = trace_path.stat()
    with _index_lock():
        conn = _connect_writer()
        try:
            _ensure_schema(conn)
            _replace_trace(conn, trace, trace_path, stat)
            conn.commit()
        finally:
            conn.close()


def search(
    pattern: str,
    *,
    traces_dir: Path,
    field: str = "all",
    session_id: str | None = None,
    step_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    fields = _fields_for_filter(field)
    if fields is None:
        return []
    if not traces_dir.exists():
        return []
    _wait_for_same_process_rebuild()
    with _index_lock():
        conn = _connect_writer()
        try:
            if _needs_rebuild(conn, traces_dir):
                _run_singleflight_rebuild(conn, traces_dir)
            return _search_locked(
                conn,
                pattern,
                fields=fields,
                session_id=session_id,
                step_type=step_type,
                limit=limit,
            )
        except sqlite3.DatabaseError:
            logger.debug("trace grep index search failed; rebuilding", exc_info=True)
            conn.close()
            conn = _connect_writer()
            _run_singleflight_rebuild(conn, traces_dir, force=True)
            return _search_locked(
                conn,
                pattern,
                fields=fields,
                session_id=session_id,
                step_type=step_type,
                limit=limit,
            )
        finally:
            conn.close()


def rebuild_from_disk(traces_dir: Path) -> None:
    with _index_lock():
        conn = _connect_writer()
        try:
            _run_singleflight_rebuild(conn, traces_dir, force=True)
        finally:
            conn.close()


def _connect_writer() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row and row[0] == _SCHEMA_VERSION and _has_schema(conn):
        return
    _create_schema(conn)


def _has_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trace_grep_rows'"
    ).fetchone()
    return row is not None


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trace_grep_rows_ai;
        DROP TRIGGER IF EXISTS trace_grep_rows_ad;
        DROP TRIGGER IF EXISTS trace_grep_rows_au;
        DROP TABLE IF EXISTS trace_grep_fts;
        DROP TABLE IF EXISTS trace_grep_rows;
        DROP TABLE IF EXISTS trace_grep_files;

        CREATE TABLE trace_grep_rows (
            rowid INTEGER PRIMARY KEY,
            trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            timestamp TEXT,
            user_prompt_preview TEXT,
            step_index INTEGER NOT NULL,
            step_type TEXT,
            thread_name TEXT,
            matched_field TEXT NOT NULL,
            field_order INTEGER NOT NULL,
            session_dir_name TEXT NOT NULL,
            trace_filename TEXT NOT NULL,
            search_text TEXT NOT NULL,
            search_text_folded TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            UNIQUE(trace_id, step_index, matched_field)
        );
        CREATE VIRTUAL TABLE trace_grep_fts USING fts5(
            search_text_folded,
            content='trace_grep_rows',
            content_rowid='rowid',
            tokenize='trigram'
        );
        CREATE TABLE trace_grep_files (
            trace_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            trace_path TEXT UNIQUE NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            indexed_at REAL NOT NULL
        );
        CREATE TRIGGER trace_grep_rows_ai AFTER INSERT ON trace_grep_rows BEGIN
            INSERT INTO trace_grep_fts(rowid, search_text_folded)
            VALUES (new.rowid, new.search_text_folded);
        END;
        CREATE TRIGGER trace_grep_rows_ad AFTER DELETE ON trace_grep_rows BEGIN
            INSERT INTO trace_grep_fts(trace_grep_fts, rowid, search_text_folded)
            VALUES('delete', old.rowid, old.search_text_folded);
        END;
        CREATE TRIGGER trace_grep_rows_au AFTER UPDATE ON trace_grep_rows BEGIN
            INSERT INTO trace_grep_fts(trace_grep_fts, rowid, search_text_folded)
            VALUES('delete', old.rowid, old.search_text_folded);
            INSERT INTO trace_grep_fts(rowid, search_text_folded)
            VALUES (new.rowid, new.search_text_folded);
        END;
        """
    )
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


def _replace_trace(
    conn: sqlite3.Connection,
    trace: dict,
    trace_path: Path,
    stat: os.stat_result,
) -> None:
    trace_id = str(trace.get("trace_id") or "")
    session_id = str(trace.get("session_id") or "")
    if not trace_id or not session_id:
        return
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM trace_grep_rows WHERE trace_id = ?", (trace_id,))
    conn.execute("DELETE FROM trace_grep_files WHERE trace_id = ?", (trace_id,))
    rows = list(_projection_rows(trace, trace_path, stat))
    conn.executemany(
        "INSERT INTO trace_grep_rows("
        "trace_id, session_id, timestamp, user_prompt_preview, step_index, step_type, "
        "thread_name, matched_field, field_order, session_dir_name, trace_filename, "
        "search_text, search_text_folded, source_size, source_mtime_ns"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT INTO trace_grep_files(trace_id, session_id, trace_path, mtime_ns, size, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (trace_id, session_id, str(trace_path), stat.st_mtime_ns, stat.st_size, time.time()),
    )


def _projection_rows(trace: dict, trace_path: Path, stat: os.stat_result) -> Iterator[tuple]:
    trace_id = str(trace.get("trace_id") or "")
    session_id = str(trace.get("session_id") or "")
    timestamp = trace.get("timestamp")
    user_prompt_preview = str(trace.get("user_prompt") or "")[:100]
    session_dir_name = trace_path.parent.name
    trace_filename = trace_path.name
    for index, step in enumerate(trace.get("steps", []) or []):
        if not isinstance(step, dict):
            continue
        for matched_field, field_order in ((_FIELD_PROMPT, 0), (_FIELD_OUTPUT, 1)):
            text = step.get(matched_field)
            if not isinstance(text, str) or text == "":
                continue
            yield (
                trace_id,
                session_id,
                timestamp,
                user_prompt_preview,
                index,
                step.get("step_type"),
                step.get("thread_name"),
                matched_field,
                field_order,
                session_dir_name,
                trace_filename,
                text,
                text.lower(),
                stat.st_size,
                stat.st_mtime_ns,
            )


def _needs_rebuild(conn: sqlite3.Connection, traces_dir: Path) -> bool:
    row = conn.execute("PRAGMA user_version").fetchone()
    if not row or row[0] != _SCHEMA_VERSION or not _has_schema(conn):
        return True
    manifest = {
        path: (mtime_ns, size)
        for path, mtime_ns, size in conn.execute(
            "SELECT trace_path, mtime_ns, size FROM trace_grep_files"
        ).fetchall()
    }
    files = {
        str(path): (stat.st_mtime_ns, stat.st_size)
        for path, stat in _trace_files_with_stats(traces_dir)
    }
    return manifest != files


def _trace_files_with_stats(traces_dir: Path) -> Iterator[tuple[Path, os.stat_result]]:
    if not traces_dir.exists():
        return
    for session_dir in sorted(traces_dir.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        for trace_path in sorted(session_dir.glob("*.json"), reverse=True):
            try:
                yield trace_path, trace_path.stat()
            except OSError:
                continue


def _run_singleflight_rebuild(
    conn: sqlite3.Connection,
    traces_dir: Path,
    *,
    force: bool = False,
) -> None:
    global _REBUILDING
    with _REBUILD_COND:
        while _REBUILDING:
            _REBUILD_COND.wait()
        if not force and not _needs_rebuild(conn, traces_dir):
            return
        _REBUILDING = True
    try:
        if not force and not _needs_rebuild(conn, traces_dir):
            return
        _rebuild_locked(conn, traces_dir)
    finally:
        with _REBUILD_COND:
            _REBUILDING = False
            _REBUILD_COND.notify_all()


def _wait_for_same_process_rebuild() -> None:
    with _REBUILD_COND:
        while _REBUILDING:
            _REBUILD_COND.wait()


def _rebuild_locked(conn: sqlite3.Connection, traces_dir: Path) -> None:
    _create_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    for trace_path, stat in _trace_files_with_stats(traces_dir):
        trace = _load_trace_file(trace_path)
        if trace is None:
            continue
        _replace_trace_rows_without_begin(conn, trace, trace_path, stat)
    conn.commit()


def _replace_trace_rows_without_begin(
    conn: sqlite3.Connection,
    trace: dict,
    trace_path: Path,
    stat: os.stat_result,
) -> None:
    trace_id = str(trace.get("trace_id") or "")
    session_id = str(trace.get("session_id") or "")
    if not trace_id or not session_id:
        return
    conn.execute("DELETE FROM trace_grep_rows WHERE trace_id = ?", (trace_id,))
    conn.execute("DELETE FROM trace_grep_files WHERE trace_id = ?", (trace_id,))
    rows = list(_projection_rows(trace, trace_path, stat))
    conn.executemany(
        "INSERT INTO trace_grep_rows("
        "trace_id, session_id, timestamp, user_prompt_preview, step_index, step_type, "
        "thread_name, matched_field, field_order, session_dir_name, trace_filename, "
        "search_text, search_text_folded, source_size, source_mtime_ns"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT INTO trace_grep_files(trace_id, session_id, trace_path, mtime_ns, size, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (trace_id, session_id, str(trace_path), stat.st_mtime_ns, stat.st_size, time.time()),
    )


def _load_trace_file(trace_path: Path) -> dict | None:
    try:
        return json.loads(trace_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _fields_for_filter(field: str) -> tuple[str, ...] | None:
    if field == "prompts":
        return (_FIELD_PROMPT,)
    if field == "outputs":
        return (_FIELD_OUTPUT,)
    if field == "all":
        return (_FIELD_PROMPT, _FIELD_OUTPUT)
    return None


def _search_locked(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    fields: tuple[str, ...],
    session_id: str | None,
    step_type: str | None,
    limit: int,
) -> list[dict]:
    folded = pattern.lower()
    params: list[object] = []
    where: list[str] = []
    from_sql = "trace_grep_rows r"
    if len(folded) >= 3 and "\x00" not in folded:
        from_sql = "trace_grep_fts f JOIN trace_grep_rows r ON r.rowid = f.rowid"
        where.append("f.search_text_folded MATCH ?")
        params.append(_match_literal(folded))
    else:
        where.append("instr(r.search_text_folded, ?) > 0")
        params.append(folded)
    where.append(f"r.matched_field IN ({','.join('?' for _ in fields)})")
    params.extend(fields)
    if session_id:
        where.append("r.session_id = ?")
        params.append(session_id)
    if step_type:
        where.append("r.step_type = ?")
        params.append(step_type)
    params.append(limit)
    rows = conn.execute(
        "SELECT r.trace_id, r.session_id, r.timestamp, r.user_prompt_preview, "
        "r.step_index, r.step_type, r.thread_name, r.matched_field, r.search_text, "
        "r.search_text_folded "
        f"FROM {from_sql} WHERE {' AND '.join(where)} "
        "ORDER BY r.session_dir_name DESC, r.trace_filename DESC, "
        "r.step_index ASC, r.field_order ASC LIMIT ?",
        params,
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        pos = row[9].find(folded)
        if pos < 0:
            continue
        start = max(0, pos - 50)
        end = min(len(row[8]), pos + len(pattern) + 50)
        out.append({
            "trace_id": row[0],
            "session_id": row[1],
            "timestamp": row[2],
            "user_prompt_preview": row[3],
            "step_index": row[4],
            "step_type": row[5],
            "thread_name": row[6],
            "matched_field": row[7],
            "match_context": row[8][start:end],
        })
    return out


def _match_literal(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'
