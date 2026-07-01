from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple

import portable_lock
from paths import ba_home

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_PAGE_SIZE = 512


class AppendState(NamedTuple):
    fresh: bool
    line_no: int | None


class IndexStat(NamedTuple):
    mtime_ns: int
    size: int


def _db_path() -> Path:
    return ba_home() / "trace_metadata_index.sqlite3"


def _lock_path() -> Path:
    return ba_home() / "trace_metadata_index.lock"


@contextmanager
def index_lock() -> Iterator[None]:
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        portable_lock.lock_ex(handle.fileno())
        yield
    finally:
        portable_lock.unlock(handle.fileno())
        handle.close()


def prepare_append_under_lock(index_path: Path) -> AppendState:
    stat = _index_stat(index_path)
    if stat is None:
        return AppendState(False, None)
    conn = _connect()
    try:
        if not _is_fresh(conn, stat):
            return AppendState(False, None)
        line_count = _meta_int(conn, "line_count")
        if line_count is None:
            return AppendState(False, None)
        return AppendState(True, line_count + 1)
    finally:
        conn.close()


def repair_append_boundary_under_lock(index_path: Path) -> bool:
    try:
        stat = index_path.stat()
    except FileNotFoundError:
        return False
    if stat.st_size <= 0:
        return False
    with index_path.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return False
        handle.seek(0, os.SEEK_END)
        handle.write(b"\n")
    return True


def index_appended_entry_under_lock(
    entry_json: str,
    *,
    line_no: int,
    index_path: Path,
) -> None:
    stat = _index_stat(index_path)
    if stat is None:
        return
    conn = _connect()
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO trace_metadata_rows(rowid, entry_json, entry_json_folded) "
            "VALUES (?, ?, ?)",
            (line_no, entry_json, entry_json.lower()),
        )
        conn.execute(
            "INSERT INTO trace_metadata_fts(rowid, entry_json_folded) VALUES (?, ?)",
            (line_no, entry_json.lower()),
        )
        _set_meta(conn, {
            "schema_version": str(_SCHEMA_VERSION),
            "index_mtime_ns": str(stat.mtime_ns),
            "index_size": str(stat.size),
            "line_count": str(line_no),
        })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search(query: str, limit: int, index_path: Path) -> list[dict]:
    if limit <= 0:
        return []
    with index_lock():
        conn = _connect()
        try:
            _ensure_current(conn, index_path)
            return _search_current(conn, query, limit)
        finally:
            conn.close()


def rebuild_under_lock(index_path: Path) -> None:
    conn = _connect()
    try:
        _rebuild(conn, index_path)
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_current(conn: sqlite3.Connection, index_path: Path) -> None:
    stat = _index_stat(index_path)
    if stat is None:
        _create_schema(conn)
        return
    if _is_fresh(conn, stat):
        return
    _rebuild(conn, index_path)


def _is_fresh(conn: sqlite3.Connection, stat: IndexStat) -> bool:
    try:
        if not _has_schema(conn):
            return False
        if _meta_int(conn, "schema_version") != _SCHEMA_VERSION:
            return False
        return (
            _meta_int(conn, "index_mtime_ns") == stat.mtime_ns
            and _meta_int(conn, "index_size") == stat.size
            and _meta_int(conn, "line_count") is not None
        )
    except sqlite3.DatabaseError:
        return False


def _has_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trace_metadata_rows'"
    ).fetchone()
    return row is not None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    if _has_schema(conn) and _meta_int(conn, "schema_version") == _SCHEMA_VERSION:
        return
    _create_schema(conn)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS trace_metadata_fts;
        DROP TABLE IF EXISTS trace_metadata_rows;
        DROP TABLE IF EXISTS trace_metadata_meta;
        CREATE TABLE trace_metadata_rows (
            rowid INTEGER PRIMARY KEY,
            entry_json TEXT NOT NULL,
            entry_json_folded TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE trace_metadata_fts USING fts5(
            entry_json_folded,
            content='trace_metadata_rows',
            content_rowid='rowid',
            tokenize='trigram'
        );
        CREATE TABLE trace_metadata_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    _set_meta(conn, {"schema_version": str(_SCHEMA_VERSION)})
    conn.commit()


def _rebuild(conn: sqlite3.Connection, index_path: Path) -> None:
    _create_schema(conn)
    rows: list[tuple[int, str, str]] = []
    line_count = 0
    for line_no, line in _iter_index_rows(index_path):
        line_count = line_no
        stripped = line.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            continue
        rows.append((line_no, stripped, stripped.lower()))
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO trace_metadata_rows(rowid, entry_json, entry_json_folded) "
        "VALUES (?, ?, ?)",
        rows,
    )
    conn.execute("INSERT INTO trace_metadata_fts(trace_metadata_fts) VALUES('rebuild')")
    stat = _index_stat(index_path) or IndexStat(0, 0)
    _set_meta(conn, {
        "schema_version": str(_SCHEMA_VERSION),
        "index_mtime_ns": str(stat.mtime_ns),
        "index_size": str(stat.size),
        "line_count": str(line_count),
    })
    conn.commit()


def _iter_index_rows(index_path: Path) -> Iterator[tuple[int, str]]:
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                yield line_no, line.rstrip("\n").rstrip("\r")
    except OSError:
        return


def _search_current(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    folded = query.lower()
    rows = (
        _search_instr(conn, folded, limit)
        if len(folded) < 3 or "\x00" in folded
        else _search_fts(conn, folded, limit)
    )
    out: list[dict] = []
    for entry_json, entry_folded in rows:
        if entry_folded.find(folded) < 0:
            continue
        try:
            out.append(json.loads(entry_json))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def _search_instr(conn: sqlite3.Connection, folded: str, limit: int) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT entry_json, entry_json_folded FROM trace_metadata_rows "
        "WHERE instr(entry_json_folded, ?) > 0 "
        "ORDER BY rowid DESC LIMIT ?",
        (folded, limit),
    ).fetchall()


def _search_fts(conn: sqlite3.Connection, folded: str, limit: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    last_seen: int | None = None
    page_size = max(limit * 4, _PAGE_SIZE)
    while len(rows) < limit:
        where = "entry_json_folded MATCH ?"
        params: list[object] = [_match_literal(folded)]
        if last_seen is not None:
            where += " AND rowid < ?"
            params.append(last_seen)
        params.append(page_size)
        candidates = conn.execute(
            "SELECT rowid FROM trace_metadata_fts "
            f"WHERE {where} ORDER BY rowid DESC LIMIT ?",
            params,
        ).fetchall()
        if not candidates:
            break
        rowids = [int(row[0]) for row in candidates]
        last_seen = rowids[-1]
        placeholders = ",".join("?" for _ in rowids)
        fetched = conn.execute(
            "SELECT entry_json, entry_json_folded FROM trace_metadata_rows "
            f"WHERE rowid IN ({placeholders}) ORDER BY rowid DESC",
            rowids,
        ).fetchall()
        for entry_json, entry_folded in fetched:
            if entry_folded.find(folded) >= 0:
                rows.append((entry_json, entry_folded))
                if len(rows) >= limit:
                    break
    return rows


def _match_literal(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def _index_stat(index_path: Path) -> IndexStat | None:
    try:
        stat = index_path.stat()
    except OSError:
        return None
    return IndexStat(stat.st_mtime_ns, stat.st_size)


def _meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        "SELECT value FROM trace_metadata_meta WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _set_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO trace_metadata_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )
