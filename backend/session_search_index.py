from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from paths import ba_home

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_rebuild_lock = threading.Lock()
_queue: queue.Queue[tuple[str, str | None] | None] = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_search_cache_lock = threading.Lock()
_search_cache: dict[tuple[str, int], tuple[int, float, list[tuple[str, int]]]] = {}
_search_inflight: dict[tuple[str, int], threading.Event] = {}
_SEARCH_CACHE_MAX = 128
_SEARCH_CACHE_STALE_SECONDS = 5.0
_index_generation = 0
_MATCHED_ROW_SCAN_LIMIT = 20_000


def _db_path() -> Path:
    return ba_home() / "session_search_index.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    _configure_connection(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS session_event_fts "
        "USING fts5(session_id UNINDEXED, text, tokenize='trigram')"
    )
    return conn


def _connect_readonly() -> sqlite3.Connection | None:
    path = _db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    _configure_connection(conn)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA cache_size=-200000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")


def _event_text(entry: dict[str, Any]) -> str:
    data = entry.get("data")
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def index_event(session_id: str, entry: dict[str, Any]) -> None:
    text = _event_text(entry)
    if not text:
        return
    _ensure_worker()
    _queue.put((session_id, text))


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(
            target=_worker_main,
            name="session-search-index",
            daemon=True,
        )
        thread.start()
        _worker_started = True


def _worker_main() -> None:
    while True:
        item = _queue.get()
        if item is None:
            _queue.task_done()
            return
        batch = [item]
        try:
            while len(batch) < 500:
                try:
                    nxt = _queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    _queue.task_done()
                    continue
                batch.append(nxt)
            _apply_rows(batch)
        except Exception:
            logger.debug("session search index async update failed", exc_info=True)
        finally:
            for _ in batch:
                _queue.task_done()


def _apply_rows(rows: list[tuple[str, str | None]]) -> None:
    if not rows:
        return
    global _index_generation
    with _lock:
        conn = _connect()
        try:
            for session_id, text in rows:
                if text is None:
                    conn.execute(
                        "DELETE FROM session_event_fts WHERE session_id = ?",
                        (session_id,),
                    )
                    continue
                conn.execute(
                    "INSERT INTO session_event_fts(session_id, text) VALUES (?, ?)",
                    (session_id, text),
                )
            conn.commit()
            with _search_cache_lock:
                _index_generation += 1
        finally:
            conn.close()


def _drain_pending() -> None:
    _ensure_worker()
    _queue.join()


def delete_session(session_id: str) -> None:
    _ensure_worker()
    _queue.put((session_id, None))


def search(query: str, limit: int = 50) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    cache_key = (q, limit)
    while True:
        now = time.monotonic()
        with _search_cache_lock:
            cached = _search_cache.get(cache_key)
            if cached is not None:
                generation, cached_at, rows = cached
                if generation == _index_generation or now - cached_at < _SEARCH_CACHE_STALE_SECONDS:
                    return [{"session_id": sid, "score": score} for sid, score in rows]
            event = _search_inflight.get(cache_key)
            if event is None:
                event = threading.Event()
                _search_inflight[cache_key] = event
                break
        event.wait()
    try:
        conn = _connect_readonly()
        if conn is None:
            scores: list[tuple[str, int]] = []
        else:
            try:
                scores = _candidate_scores(conn, q, limit)
            finally:
                conn.close()
        with _search_cache_lock:
            _search_cache[cache_key] = (_index_generation, time.monotonic(), scores)
            if len(_search_cache) > _SEARCH_CACHE_MAX:
                _search_cache.pop(next(iter(_search_cache)))
        return [{"session_id": sid, "score": score} for sid, score in scores]
    finally:
        with _search_cache_lock:
            event = _search_inflight.pop(cache_key, None)
        if event is not None:
            event.set()

def has_indexed_rows() -> bool:
    conn = _connect_readonly()
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM session_event_fts LIMIT 1",
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def rebuild_from_disk() -> None:
    global _index_generation
    if not _rebuild_lock.acquire(blocking=False):
        return
    try:
        sessions_dir = ba_home() / "sessions"
        rows: list[tuple[str, str]] = []
        if sessions_dir.is_dir():
            for fpath in sessions_dir.glob("*/events.jsonl"):
                rows.extend(_index_file_rows(fpath.parent.name, fpath))
        with _lock:
            conn = _connect()
            try:
                conn.execute("DELETE FROM session_event_fts")
                if rows:
                    conn.executemany(
                        "INSERT INTO session_event_fts(session_id, text) VALUES (?, ?)",
                        rows,
                    )
                conn.commit()
                with _search_cache_lock:
                    _index_generation += 1
            finally:
                conn.close()
    finally:
        _rebuild_lock.release()


def _candidate_scores(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[str, int]]:
    if len(query) < 3:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return conn.execute(
            "SELECT session_id, COUNT(*) AS score FROM session_event_fts "
            "WHERE lower(text) LIKE ? ESCAPE '\\' "
            "GROUP BY session_id ORDER BY score DESC LIMIT ?",
            (f"%{escaped.lower()}%", limit),
        ).fetchall()
    return conn.execute(
        "SELECT session_id, COUNT(*) AS score FROM ("
        "SELECT session_id FROM session_event_fts WHERE text MATCH ? "
        "LIMIT ?"
        ") GROUP BY session_id ORDER BY score DESC LIMIT ?",
        (_match_literal(query), _MATCHED_ROW_SCAN_LIMIT, limit),
    ).fetchall()


def _match_literal(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def _index_file(conn: sqlite3.Connection, sid: str, fpath: Path) -> None:
    rows = _index_file_rows(sid, fpath)
    if rows:
        conn.executemany(
            "INSERT INTO session_event_fts(session_id, text) VALUES (?, ?)",
            rows,
        )


def _index_file_rows(sid: str, fpath: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    try:
        with fpath.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _event_text(entry)
                if text:
                    rows.append((sid, text))
    except OSError:
        return []
    return rows
