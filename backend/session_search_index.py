from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from paths import ba_home

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_rebuild_lock = threading.Lock()
# Non-None only while rebuild_from_disk() is between starting its disk scan
# and its final lock-held swap. _apply_rows appends every row it writes to
# the live db here too, so the swap can replay them onto the freshly built
# db without losing writes that landed during the (unlocked) scan.
_rebuild_buffer: list[tuple[str, str | None]] | None = None
_queue: queue.Queue[tuple[str, str | None] | None] = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_writer_conn: sqlite3.Connection | None = None
_writer_conn_path: Path | None = None
_readonly_conn_local = threading.local()
_search_cache_lock = threading.Lock()
_search_cache: dict[tuple[str, int], tuple[int, float, list[tuple[str, int]]]] = {}
_search_inflight: dict[tuple[str, int], threading.Event] = {}
_SEARCH_CACHE_MAX = 128
_SEARCH_CACHE_STALE_SECONDS = 5.0
_index_generation = 0
_published_generation = 0
_published_generation_at = 0.0
_MATCHED_ROW_SCAN_LIMIT = 20_000
_REBUILD_INSERT_BATCH_SIZE = 1000
_WRITER_CACHE_KIB = 200_000
_READONLY_CACHE_KIB = 8_192
# Bumped whenever the indexed-row shape changes. A persisted index whose
# stored version differs is stale (e.g. the pre-lean-extraction multi-GB
# index that stored raw event blobs) and is rebuilt from disk on startup.
_INDEX_SCHEMA_VERSION = 2


def generation() -> int:
    with _search_cache_lock:
        return _published_generation


def has_cached_result(query: str, limit: int) -> bool:
    q = query.strip()
    if not q:
        return True
    with _search_cache_lock:
        return _cached_rows_for_limit(q, limit, time.monotonic()) is not None


def _search_cache_valid(cached_at: float, generation: int, now: float) -> bool:
    return generation == _index_generation or now - cached_at < _SEARCH_CACHE_STALE_SECONDS


def _cached_rows_for_limit(
    query: str,
    limit: int,
    now: float,
) -> list[tuple[str, int]] | None:
    best_limit = -1
    best_rows: list[tuple[str, int]] | None = None
    for (cached_query, cached_limit), (generation, cached_at, rows) in _search_cache.items():
        if cached_query != query or cached_limit < limit:
            continue
        if not _search_cache_valid(cached_at, generation, now):
            continue
        if best_rows is None or cached_limit < best_limit:
            best_limit = cached_limit
            best_rows = rows
    if best_rows is None:
        return None
    return best_rows[:limit]


def _inflight_event_for_limit(
    query: str,
    limit: int,
) -> threading.Event | None:
    best_limit: int | None = None
    best_event: threading.Event | None = None
    for (inflight_query, inflight_limit), event in _search_inflight.items():
        if inflight_query != query or inflight_limit < limit:
            continue
        if best_limit is None or inflight_limit < best_limit:
            best_limit = inflight_limit
            best_event = event
    return best_event


def _db_path() -> Path:
    return ba_home() / "session_search_index.sqlite3"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the writer connection this produces
    # (`_writer_conn`) is created by whichever thread first calls
    # `_apply_rows` (the async indexing worker thread) but may be closed by
    # whatever thread calls `rebuild_from_disk` (e.g. a startup task thread).
    # All access to it is already serialized by `_lock` in both call sites,
    # so sqlite3's own same-thread check is redundant and would otherwise
    # raise ProgrammingError on that legitimate cross-thread close.
    conn = sqlite3.connect(path, check_same_thread=False)
    _configure_connection(conn, readonly=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS session_event_fts "
        "USING fts5(session_id UNINDEXED, text, tokenize='trigram')"
    )
    return conn


def _writer_connection() -> sqlite3.Connection:
    global _writer_conn, _writer_conn_path
    path = _db_path()
    if _writer_conn is not None and _writer_conn_path == path:
        return _writer_conn
    if _writer_conn is not None:
        try:
            _writer_conn.close()
        except sqlite3.Error:
            pass
    _writer_conn = _connect()
    _writer_conn_path = path
    return _writer_conn


def _close_writer_connection_locked() -> None:
    global _writer_conn, _writer_conn_path
    if _writer_conn is None:
        return
    try:
        _writer_conn.close()
    finally:
        _writer_conn = None
        _writer_conn_path = None


def _connect_readonly() -> sqlite3.Connection | None:
    path = _db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    _configure_connection(conn, readonly=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _readonly_connection() -> sqlite3.Connection | None:
    path = _db_path()
    conn = getattr(_readonly_conn_local, "conn", None)
    conn_path = getattr(_readonly_conn_local, "path", None)
    if conn is not None and conn_path == path:
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    conn = _connect_readonly()
    _readonly_conn_local.conn = conn
    _readonly_conn_local.path = path
    return conn


def _close_readonly_connection() -> None:
    conn = getattr(_readonly_conn_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _readonly_conn_local.conn = None
    _readonly_conn_local.path = None


def _configure_connection(conn: sqlite3.Connection, *, readonly: bool) -> None:
    cache_kib = _READONLY_CACHE_KIB if readonly else _WRITER_CACHE_KIB
    conn.execute(f"PRAGMA cache_size=-{cache_kib}")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")


# Only the conversation itself is worth full-text searching. Indexing raw
# event blobs (tool inputs, tool_result file dumps, worker transcripts,
# run_state) bloated the FTS table to multi-GB and made content search take
# seconds. Extract only user/assistant text and tool names.
_INDEX_TEXT_PER_EVENT_LIMIT = 8_000


def _event_text(entry: dict[str, Any]) -> str:
    text = _searchable_event_text(entry)
    if not text:
        return ""
    if len(text) > _INDEX_TEXT_PER_EVENT_LIMIT:
        text = text[:_INDEX_TEXT_PER_EVENT_LIMIT]
    return text


def _searchable_event_text(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    # Only agent_message events carry conversation content; everything else
    # (worker_*, run_state, turn lifecycle, command_received, ...) is metadata
    # or bulk transcripts with no search value.
    if entry.get("type") != "agent_message":
        return ""
    # INVARIANT: read ONLY `data.message.content`. The entry arriving here is
    # the live journal row queued by reference (session_search_projection); on
    # the ingest path `data` is narrow-copy-on-write isolated exactly along
    # `message -> content` (see file_ref_resolver._isolate_for_rewrite), so
    # only those subtrees are owned. Reading any other (shared-by-reference)
    # subtree here would race with a post-ingest caller mutation and let the
    # index drift from the serialized row. Extend the isolator if a new field
    # must be read here.
    data = entry.get("data")
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    role = message.get("role") if isinstance(message, dict) else None
    if role not in ("user", "assistant"):
        role = data.get("type")
    if not isinstance(message, dict) or role not in ("user", "assistant"):
        return ""
    return _content_searchable_text(message.get("content"))


def _content_searchable_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text") or ""))
        elif btype == "tool_use":
            # Tool name is useful signal ("which tools did this session use");
            # the input is often a full file write / command blob — skip it.
            name = block.get("name")
            if name:
                parts.append(str(name))
        # tool_result / image / thinking → skip (bulk, low search value).
    return "\n".join(part for part in parts if part)


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


def _apply_rows_to_conn(conn: sqlite3.Connection, rows: list[tuple[str, str | None]]) -> None:
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


def _apply_rows(rows: list[tuple[str, str | None]]) -> None:
    if not rows:
        return
    global _index_generation, _published_generation, _published_generation_at
    with _lock:
        conn = _writer_connection()
        _apply_rows_to_conn(conn, rows)
        conn.commit()
        if _rebuild_buffer is not None:
            _rebuild_buffer.extend(rows)
        with _search_cache_lock:
            _index_generation += 1
            now = time.monotonic()
            if now - _published_generation_at >= _SEARCH_CACHE_STALE_SECONDS:
                _published_generation = _index_generation
                _published_generation_at = now


def _drain_pending() -> None:
    _ensure_worker()
    _queue.join()


def delete_session(session_id: str) -> None:
    _ensure_worker()
    _queue.put((session_id, None))


def search(
    query: str,
    limit: int = 50,
    *,
    max_wait_seconds: float | None = None,
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    cache_key = (q, limit)
    stale_rows: list[tuple[str, int]] | None = None
    while True:
        now = time.monotonic()
        with _search_cache_lock:
            cached = _search_cache.get(cache_key)
            if cached is not None:
                generation, cached_at, rows = cached
                if _search_cache_valid(cached_at, generation, now):
                    return [{"session_id": sid, "score": score} for sid, score in rows]
                stale_rows = rows
            reusable_rows = _cached_rows_for_limit(q, limit, now)
            if reusable_rows is not None:
                return [{"session_id": sid, "score": score} for sid, score in reusable_rows]
            event = _search_inflight.get(cache_key)
            if event is not None and max_wait_seconds is not None:
                rows = stale_rows or []
                return [{"session_id": sid, "score": score} for sid, score in rows]
            if event is None:
                event = _inflight_event_for_limit(q, limit)
                if event is not None and max_wait_seconds is not None:
                    rows = stale_rows or []
                    return [{"session_id": sid, "score": score} for sid, score in rows]
            if event is None:
                if max_wait_seconds is not None and max_wait_seconds <= 0:
                    rows = stale_rows or []
                    return [{"session_id": sid, "score": score} for sid, score in rows]
                event = threading.Event()
                _search_inflight[cache_key] = event
                if max_wait_seconds is not None:
                    threading.Thread(
                        target=_run_search_cache_fill,
                        args=(cache_key, q, limit, max_wait_seconds, event),
                        name="session-search-cache-fill",
                        daemon=True,
                    ).start()
                    if event.wait(max(0.0, max_wait_seconds)):
                        continue
                    rows = stale_rows or []
                    return [{"session_id": sid, "score": score} for sid, score in rows]
                break
        if max_wait_seconds is not None:
            if event.wait(max(0.0, max_wait_seconds)):
                continue
            rows = stale_rows or []
            return [{"session_id": sid, "score": score} for sid, score in rows]
        event.wait()
    try:
        conn = _readonly_connection()
        if conn is None:
            scores: list[tuple[str, int]] = []
        else:
            scores = _candidate_scores(conn, q, limit)
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


def _run_search_cache_fill(
    cache_key: tuple[str, int],
    query: str,
    limit: int,
    max_wait_seconds: float | None,
    event: threading.Event,
) -> None:
    try:
        conn = _readonly_connection()
        if conn is None:
            scores: list[tuple[str, int]] = []
        else:
            deadline = (
                time.monotonic() + max(0.0, max_wait_seconds)
                if max_wait_seconds is not None
                else None
            )
            scores = _candidate_scores(conn, query, limit, deadline=deadline)
            if deadline is not None and not scores and time.monotonic() >= deadline:
                return
        with _search_cache_lock:
            _search_cache[cache_key] = (_index_generation, time.monotonic(), scores)
            if len(_search_cache) > _SEARCH_CACHE_MAX:
                _search_cache.pop(next(iter(_search_cache)))
    finally:
        with _search_cache_lock:
            _search_inflight.pop(cache_key, None)
        event.set()


def has_indexed_rows() -> bool:
    conn = _readonly_connection()
    if conn is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM session_event_fts LIMIT 1",
    ).fetchone()
    return row is not None


def needs_rebuild() -> bool:
    """True when there is no usable index, or the persisted index was built
    under an older row shape and must be rebuilt lean."""
    conn = _readonly_connection()
    if conn is None:
        return True
    row = conn.execute("PRAGMA user_version").fetchone()
    stored = row[0] if row else 0
    if stored != _INDEX_SCHEMA_VERSION:
        return True
    row = conn.execute("SELECT 1 FROM session_event_fts LIMIT 1").fetchone()
    return row is None


def rebuild_from_disk() -> None:
    """Rebuild the FTS index from every session's events.jsonl on disk.

    The full-corpus disk scan + batch insert runs WITHOUT holding `_lock`,
    building into a separate temp DB file — so `_apply_rows` (the hot,
    per-event indexing path called on essentially every live session write)
    keeps applying straight through to the live db uninterrupted while the
    scan (which can take seconds to minutes) is in flight. Only the final
    swap is done under `_lock`: replay whatever `_apply_rows` wrote to the
    live db during the scan onto the temp db, then rename the temp db over
    the live one. That swap is O(rows written during the scan), not
    O(corpus size), so the hot path is blocked only briefly.
    """
    global _index_generation, _published_generation, _published_generation_at
    global _rebuild_buffer
    if not _rebuild_lock.acquire(blocking=False):
        return
    tmp_path = _tmp_rebuild_path()
    conn: sqlite3.Connection | None = None
    try:
        with _lock:
            _rebuild_buffer = []
        # Rebuild into a fresh DB file rather than DELETE+reinsert. A
        # previously bloated index (multi-GB of raw event blobs) leaves
        # FTS5 tombstoned segments and free pages behind after DELETE, so
        # queries stay slow; a fresh file is compact and fast.
        _delete_db_files(tmp_path)
        conn = _connect(tmp_path)
        import session_store
        batch: list[tuple[str, str]] = []
        for root_file in session_store._session_json_files():
            fpath = root_file.parent / root_file.stem / "events.jsonl"
            for row in _index_file_rows(root_file.stem, fpath):
                batch.append(row)
                if len(batch) >= _REBUILD_INSERT_BATCH_SIZE:
                    _insert_index_rows(conn, batch)
                    batch.clear()
        if batch:
            _insert_index_rows(conn, batch)

        with _lock:
            buffered = _rebuild_buffer or []
            _rebuild_buffer = None
            _apply_rows_to_conn(conn, buffered)
            conn.execute(f"PRAGMA user_version = {_INDEX_SCHEMA_VERSION}")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            conn = None

            _close_writer_connection_locked()
            _close_readonly_connection()
            _delete_db_files()
            _rename_db_files(tmp_path, _db_path())

            with _search_cache_lock:
                _index_generation += 1
                _published_generation = _index_generation
                _published_generation_at = time.monotonic()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _delete_db_files(tmp_path)
        with _lock:
            _rebuild_buffer = None
        _rebuild_lock.release()


def _tmp_rebuild_path() -> Path:
    base = _db_path()
    return base.with_name(base.name + ".rebuild-tmp")


def _rename_db_files(src_base: Path, dst_base: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        src = src_base.with_suffix(src_base.suffix + suffix) if suffix else src_base
        dst = dst_base.with_suffix(dst_base.suffix + suffix) if suffix else dst_base
        try:
            os.replace(src, dst)
        except FileNotFoundError:
            pass


def _insert_index_rows(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    conn.executemany(
        "INSERT INTO session_event_fts(session_id, text) VALUES (?, ?)",
        rows,
    )


def _delete_db_files(base: Path | None = None) -> None:
    if base is None:
        base = _db_path()
    for path in (
        base,
        base.with_suffix(base.suffix + "-wal"),
        base.with_suffix(base.suffix + "-shm"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("session search index: could not remove %s", path, exc_info=True)


def _candidate_scores(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    *,
    deadline: float | None = None,
) -> list[tuple[str, int]]:
    if deadline is not None:
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline else 0,
            1000,
        )
    try:
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
    except sqlite3.OperationalError as exc:
        if deadline is not None and "interrupted" in str(exc).lower():
            return []
        raise
    finally:
        if deadline is not None:
            conn.set_progress_handler(None, 0)


def _match_literal(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def _index_file(conn: sqlite3.Connection, sid: str, fpath: Path) -> None:
    batch: list[tuple[str, str]] = []
    for row in _index_file_rows(sid, fpath):
        batch.append(row)
        if len(batch) >= _REBUILD_INSERT_BATCH_SIZE:
            _insert_index_rows(conn, batch)
            batch.clear()
    if batch:
        _insert_index_rows(conn, batch)


def _index_file_rows(sid: str, fpath: Path) -> Iterator[tuple[str, str]]:
    try:
        with fpath.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _event_text(entry)
                if text:
                    yield (sid, text)
    except OSError:
        return
