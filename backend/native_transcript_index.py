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
daemon stat-walks the roots on a short poll interval, re-indexing the delta
(new/changed files) and tombstoning deleted ones. ``covered`` is set once a full
walk has indexed every file; while not covered (cold start), the search falls
back to ``rg`` so correctness never depends on an incomplete index.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from native_session_miner import _mtime
from paths import ba_home, encode_cwd

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_INDEX_TEXT_CAP = 8_000  # per-element text cap; tool dumps were the old bloat
_INDEXED_KINDS = frozenset({"user_prompt", "assistant_text", "reasoning", "tool_call"})
_POLL_INTERVAL_SECONDS = 2.0
_FRESH_WINDOW_SECONDS = 3.0  # covered + last walk within this window => trusted
_MATCHED_SCAN_LIMIT = 20_000
_PATH_CAP = 1_000  # > this many matched files => "too broad", bail to caller

_lock = threading.Lock()  # guards writer connection lifecycle + rebuild flag
_worker_started = False
_worker_lock = threading.Lock()
_stop = threading.Event()

# Refresh signaling: once covered, a stale query REQUESTS a refresh and waits
# for it (one delta pass) instead of dropping to rg. _last_refresh_at is the
# in-memory freshness timestamp the worker sets after each refresh.
_refresh_cond = threading.Condition()
_last_refresh_at = 0.0
_refresh_requested = False
_FRESH_WAIT_TIMEOUT = 3.0  # max a query blocks for a delta refresh before rg


def _db_path() -> Path:
    return ba_home() / "native_transcript_index.sqlite3"


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
        CREATE VIRTUAL TABLE IF NOT EXISTS native_element_fts USING fts5(
            text,
            path UNINDEXED,
            sid UNINDEXED,
            cwd UNINDEXED,
            tag UNINDEXED,
            element_kind UNINDEXED,
            tool_name UNINDEXED,
            ts UNINDEXED,
            tokenize='unicode61'
        );
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
    for el in elements:
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
        ))
    return rows


def _replace_candidate(conn: sqlite3.Connection, candidate, mtime: float, size: int) -> None:
    path = str(candidate.transcript)
    conn.execute("DELETE FROM native_element_fts WHERE path = ?", (path,))
    rows = _index_candidate_rows(candidate)
    if rows:
        conn.executemany(
            "INSERT INTO native_element_fts"
            "(text, path, sid, cwd, tag, element_kind, tool_name, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    conn.execute(
        "INSERT INTO native_file_state(path, mtime, size, tag, sid, cwd, indexed_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "mtime=excluded.mtime, size=excluded.size, tag=excluded.tag, "
        "sid=excluded.sid, cwd=excluded.cwd, indexed_at=excluded.indexed_at",
        (path, mtime, size, candidate.format, candidate.sid, candidate.cwd, time.time()),
    )


def _delete_path(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM native_element_fts WHERE path = ?", (path,))
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


def refresh_once() -> dict[str, int]:
    """One delta pass: re-index new/changed files, drop deleted ones, refresh
    the corpus watermark. Returns counts. Idempotent + safe to run anytime."""
    _, _, candidate_from_match = _roots_and_resolver()
    global _last_refresh_at
    with _lock:
        conn = _writer_connection()
        try:
            on_disk, indexed = _compute_changes()
            now = time.time()
            on_disk_by_path = {str(p): (p, tag, mt, sz) for p, tag, mt, sz in on_disk}
            # Freshness fingerprint per indexed file: (mtime, size).
            fingerprints = {
                r[0]: (r[1], r[2]) for r in conn.execute(
                    "SELECT path, mtime, size FROM native_file_state"
                )
            }
            new_or_changed = 0
            for path, tag, mt, sz in on_disk:
                if fingerprints.get(str(path)) != (mt, sz):
                    candidate = candidate_from_match(path, tag)
                    _replace_candidate(conn, candidate, mt, sz)
                    new_or_changed += 1
            for path_str in indexed - set(on_disk_by_path):
                _delete_path(conn, path_str)
                new_or_changed += 1
            _state_set(conn, "last_walk_at", str(now))
            _state_set(conn, "covered", "1")
            _state_set(conn, "schema_version", str(_SCHEMA_VERSION))
            conn.commit()
            with _refresh_cond:
                _last_refresh_at = time.time()
                _refresh_cond.notify_all()
            return {"walked": len(on_disk), "touched": new_or_changed}
        except Exception:
            conn.rollback()
            raise


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
    if not is_covered() or _last_refresh_at <= 0:
        return False
    return (time.time() - _last_refresh_at) <= _FRESH_WINDOW_SECONDS


def request_refresh() -> None:
    """Wake the worker for an immediate delta pass (vs waiting for the next poll)."""
    global _refresh_requested
    with _refresh_cond:
        _refresh_requested = True
        _refresh_cond.notify()


def wait_fresh(timeout: float = _FRESH_WAIT_TIMEOUT) -> bool:
    """Block until a refresh completes within the freshness window, or timeout.

    Used by the query path once covered: rather than fall to rg for a slightly
    stale index, wait for the one delta pass (stat-walk + parse-changed-only —
    cheap) then serve from FTS. Returns True if fresh within the timeout; the
    timeout itself is the safety when no refresh is forthcoming (worker down)."""
    deadline = time.monotonic() + timeout
    with _refresh_cond:
        while (time.time() - _last_refresh_at) > _FRESH_WINDOW_SECONDS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _refresh_cond.wait(remaining)
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
            "SELECT text, path, sid, cwd, tag, element_kind, tool_name, ts "
            "FROM native_element_fts WHERE native_element_fts MATCH ? LIMIT ?",
            (_match_expr(tokens), _MATCHED_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"text": t, "path": p, "sid": sid, "cwd": cwd, "tag": tag,
         "element_kind": ek, "tool_name": tn, "ts": ts}
        for t, p, sid, cwd, tag, ek, tn, ts in rows[:limit]
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
SQL_COLUMNS = ("text", "path", "sid", "cwd", "tag", "element_kind", "tool_name", "ts")
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
    """Start the background daemon that keeps the index covered + fresh."""
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker_main, name="native-transcript-index", daemon=True)
        thread.start()
        _worker_started = True


def _worker_main() -> None:
    # Cold start: keep doing full delta passes until covered, then poll. Each
    # refresh (refresh_once) stamps _last_refresh_at + notifies waiting queries.
    global _refresh_requested
    while not _stop.is_set():
        try:
            refresh_once()
        except Exception:
            logger.debug("native transcript index refresh failed", exc_info=True)
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


def shutdown() -> None:
    _stop.set()
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
    """Drop the persisted index + in-memory state; for isolated tests."""
    global _worker_started, _last_refresh_at, _refresh_requested
    with _lock:
        global _writer_conn
        if _writer_conn is not None:
            _writer_conn.close()
            _writer_conn = None
        _writer_started = False
    _stop.clear()
    with _refresh_cond:
        _last_refresh_at = 0.0
        _refresh_requested = False
    _close_readonly_connection()
    base = _db_path()
    for path in (base, base.with_suffix(base.suffix + "-wal"), base.with_suffix(base.suffix + "-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "ensure_started", "is_covered", "is_usable", "match_paths", "search_rows",
    "run_readonly_sql", "SQL_TABLE", "SQL_COLUMNS", "SQL_ELEMENT_KINDS",
    "refresh_once", "request_refresh", "wait_fresh", "reset_for_test", "shutdown",
]
