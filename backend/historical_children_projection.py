from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import Future, InvalidStateError
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keyed_lane_executor import KeyedLaneExecutor
from paths import ba_home
import perf
import portable_lock


SCHEMA = 5
MAX_LIMIT = 100
MAX_BYTES = 2 * 1024 * 1024
ALL_NODES_PARENT = "__all_nodes__"
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_rebuilding: set[str] = set()
_rebuild_dirty: set[str] = set()
_rebuild_pending: dict[str, tuple[dict[str, Any] | None, bool]] = {}
_current_waiters: dict[str, set[Future]] = {}
_rebuild_local = threading.local()
_query_observer = None
_change_observer = None
# One dedicated thread per (root_id, lane): "startup" for background sweep
# rebuilds, "ondemand" for priority rebuilds. Each root's rebuild work is
# fully isolated from every other root's -- no root can queue behind an
# unrelated root's rebuild in either lane.
_executor_pool = KeyedLaneExecutor(
    lanes=("startup", "ondemand"),
    thread_name_prefix="historical",
)
_shutdown = False
logger = logging.getLogger(__name__)


def reopen() -> None:
    global _shutdown, _executor_pool
    with _locks_guard:
        if not _shutdown:
            return
        _executor_pool = KeyedLaneExecutor(
            lanes=("startup", "ondemand"),
            thread_name_prefix="historical",
        )
        _rebuilding.clear()
        _rebuild_dirty.clear()
        _rebuild_pending.clear()
        _current_waiters.clear()
        _shutdown = False


class ProjectionUnavailable(RuntimeError):
    pass


class ProjectionBusy(ProjectionUnavailable):
    pass


class _ProjectionInvalid(ProjectionUnavailable):
    pass


class ProjectionConflict(ValueError):
    pass


class _RebuildPromoted(RuntimeError):
    pass


def _lock(root_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(root_id, threading.RLock())


@contextmanager
def _timed_lock(root_id: str, phase: str):
    lock = _lock(root_id)
    started = time.perf_counter()
    lock.acquire()
    acquired = time.perf_counter()
    perf.record(f"historical.lock.{phase}.wait", (acquired - started) * 1000)
    try:
        yield
    finally:
        perf.record(f"historical.lock.{phase}.held", (time.perf_counter() - acquired) * 1000)
        lock.release()


def set_change_observer(observer) -> None:
    global _change_observer
    _change_observer = observer


def _notify_changed(root_id: str, manifest: dict[str, Any] | None) -> None:
    if manifest is not None and _change_observer is not None and root_id not in _rebuilding:
        _change_observer({**manifest, "root_id": root_id})


def _settle_current_waiters(root_id: str, error: BaseException | None = None) -> None:
    with _locks_guard:
        waiters = _current_waiters.pop(root_id, set())
    for waiter in waiters:
        if waiter.cancelled() or waiter.done():
            continue
        try:
            if error is None:
                waiter.set_result(None)
            else:
                waiter.set_exception(error)
        except InvalidStateError:
            continue


def ensure_current(
    root_id: str,
    root_snapshot: dict[str, Any] | None,
    *,
    priority: bool = True,
) -> Future:
    waiter: Future = Future()
    if _is_current(root_id):
        waiter.set_result(None)
        return waiter
    with _locks_guard:
        _current_waiters.setdefault(root_id, set()).add(waiter)

    def discard(done: Future) -> None:
        if not done.cancelled():
            return
        with _locks_guard:
            root_waiters = _current_waiters.get(root_id)
            if root_waiters is None:
                return
            root_waiters.discard(done)
            if not root_waiters:
                _current_waiters.pop(root_id, None)

    waiter.add_done_callback(discard)
    if _is_current(root_id):
        _settle_current_waiters(root_id)
        return waiter
    scheduled = schedule_rebuild(root_id, root_snapshot, priority=priority)
    if scheduled is None and _shutdown:
        _settle_current_waiters(
            root_id,
            ProjectionUnavailable("historical projection is shutting down"),
        )
    return waiter


def _path(root_id: str) -> Path:
    digest = hashlib.sha256(root_id.encode()).hexdigest()
    return ba_home() / "cache" / "historical-children" / f"{digest}.sqlite3"


def _journal(root_id: str) -> Path:
    if not isinstance(root_id, str) or not root_id or len(root_id) > 128:
        raise ProjectionConflict("invalid historical root")
    if root_id in (".", "..") or "/" in root_id or "\\" in root_id or Path(root_id).is_absolute():
        raise ProjectionConflict("invalid historical root")
    sessions = (ba_home() / "sessions").resolve()
    candidate = sessions / root_id / "events.jsonl"
    resolved_parent = candidate.parent.resolve(strict=False)
    if resolved_parent != sessions / root_id or sessions not in resolved_parent.parents:
        raise ProjectionConflict("invalid historical root path")
    return candidate


def _lock_path(root_id: str) -> Path:
    return Path(str(_path(root_id)) + ".lock")


@contextmanager
def _sidecar_lock(root_id: str):
    path = _lock_path(root_id)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ProjectionUnavailable("historical projection serializer is unavailable") from exc
    acquired = False
    try:
        try:
            portable_lock.lock_ex(fd)
            acquired = True
        except OSError as exc:
            raise ProjectionUnavailable("historical projection serializer is unavailable") from exc
        yield
    finally:
        try:
            if acquired:
                portable_lock.unlock(fd)
        finally:
            os.close(fd)


_SCHEMA_SQL = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE messages(sid TEXT NOT NULL,msg_id TEXT NOT NULL,root_node TEXT NOT NULL,revision TEXT NOT NULL,generation INTEGER NOT NULL,direct_child_count INTEGER NOT NULL,PRIMARY KEY(sid,msg_id))",
    "CREATE TABLE orphans(sid TEXT NOT NULL,seq INTEGER NOT NULL,payload_start INTEGER NOT NULL,payload_end INTEGER NOT NULL,PRIMARY KEY(sid,seq))",
    "CREATE TABLE nodes(sid TEXT NOT NULL,msg_id TEXT NOT NULL,node_id TEXT NOT NULL,parent_id TEXT NOT NULL,parent_key TEXT,ordinal INTEGER NOT NULL,type TEXT NOT NULL,revision TEXT NOT NULL,summary TEXT NOT NULL,payload_start INTEGER,payload_end INTEGER,worker_json TEXT,renderable INTEGER NOT NULL,PRIMARY KEY(sid,msg_id,node_id))",
    "CREATE TABLE parent_aggregates(sid TEXT NOT NULL,msg_id TEXT NOT NULL,parent_id TEXT NOT NULL,child_count INTEGER NOT NULL,xor_hash TEXT NOT NULL,PRIMARY KEY(sid,msg_id,parent_id))",
    "CREATE INDEX nodes_parent ON nodes(sid,msg_id,parent_id,ordinal)",
)


def _validate_schema(conn: sqlite3.Connection) -> None:
    try:
        schema = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if schema is None or int(schema[0]) != SCHEMA:
            raise _ProjectionInvalid("historical projection schema mismatch")
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise ProjectionBusy("historical projection is busy") from exc
        if "no such table" in message or "malformed" in message:
            raise _ProjectionInvalid("historical projection schema is corrupt") from exc
        raise ProjectionUnavailable("historical projection is unavailable") from exc
    except (sqlite3.DatabaseError, ValueError) as exc:
        if isinstance(exc, _ProjectionInvalid):
            raise
        raise _ProjectionInvalid("historical projection schema is corrupt") from exc


def _connect(root_id: str, *, create: bool) -> sqlite3.Connection:
    path = _path(root_id)
    if not create and not path.is_file():
        raise ProjectionUnavailable("historical projection is rebuilding")
    conn = None
    try:
        if not create:
            if Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists():
                raise ProjectionUnavailable("historical projection WAL is unavailable")
            uri = f"file:{path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0)
            conn.row_factory = sqlite3.Row
            if _query_observer is not None:
                conn.set_trace_callback(_query_observer)
            _validate_schema(conn)
            return conn
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existed = path.exists()
        conn = sqlite3.connect(path, timeout=0)
        if _query_observer is not None:
            conn.set_trace_callback(_query_observer)
        conn.row_factory = sqlite3.Row
        if not existed:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_SQL:
                conn.execute(statement)
            conn.execute("INSERT INTO meta VALUES('schema',?)", (str(SCHEMA),))
            conn.execute("INSERT INTO meta VALUES('ready','0')")
            conn.execute("INSERT INTO meta VALUES('projection_revision','0')")
            conn.execute("INSERT INTO meta VALUES('cursor_secret',?)", (os.urandom(32).hex(),))
            conn.commit()
        _validate_schema(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.OperationalError as exc:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise ProjectionBusy("historical projection is busy") from exc
        raise ProjectionUnavailable("historical projection is unavailable") from exc
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            conn.close()
        raise _ProjectionInvalid("historical projection is corrupt") from exc
    except (OSError, ValueError) as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, _ProjectionInvalid):
            raise
        raise ProjectionUnavailable("historical projection is unavailable") from exc


@contextmanager
def _connection(root_id: str, *, create: bool):
    if not create:
        conn = _connect(root_id, create=False)
        try:
            yield conn
        finally:
            conn.close()
        return
    with _sidecar_lock(root_id):
        conn = _connect(root_id, create=True)
        try:
            try:
                with conn:
                    yield conn
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" in message or "busy" in message:
                    raise ProjectionBusy("historical projection is busy") from exc
                raise ProjectionUnavailable("historical projection is unavailable") from exc
            except sqlite3.DatabaseError as exc:
                raise _ProjectionInvalid("historical projection is corrupt") from exc
        finally:
            conn.close()


def _clear_projection(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','0')")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM parent_aggregates")
    conn.execute("DELETE FROM orphans")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('indexed_end','0')")


def _replace_invalid_projection(root_id: str) -> None:
    with _sidecar_lock(root_id):
        try:
            conn = _connect(root_id, create=True)
        except _ProjectionInvalid:
            conn = None
        if conn is not None:
            try:
                with conn:
                    _clear_projection(conn)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return
            finally:
                conn.close()
        path = _path(root_id)
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            candidate.unlink(missing_ok=True)
        conn = _connect(root_id, create=True)
        try:
            with conn:
                _clear_projection(conn)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class _JournalSnapshot:
    exists: bool
    size: int
    identity: str


def _journal_snapshot(root_id: str) -> _JournalSnapshot:
    try:
        stat = _journal(root_id).stat()
    except FileNotFoundError:
        return _JournalSnapshot(False, 0, "absent")
    return _JournalSnapshot(
        True,
        int(stat.st_size),
        f"present:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}",
    )


def _event_uuid(event: dict[str, Any]) -> str | None:
    for owner in (event, event.get("data")):
        if not isinstance(owner, dict):
            continue
        for key in ("uuid", "id", "event_id"):
            value = owner.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _parent_uuid(event: dict[str, Any]) -> str | None:
    for owner in (event, event.get("data")):
        if not isinstance(owner, dict):
            continue
        for key in ("parentUuid", "parent_uuid", "parent_id"):
            value = owner.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _node_id(msg_id: str, event: dict[str, Any], seq: int) -> str:
    source = _event_uuid(event) or f"{msg_id}:{seq}"
    return "event-" + hashlib.sha256(source.encode()).hexdigest()[:24]


def _root_node(msg_id: str) -> str:
    return f"message-{msg_id}"


def _summary(event: dict[str, Any]) -> str:
    data = event.get("data")
    if event.get("type") == "agent_message" and isinstance(data, dict):
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, list):
            text = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
            if text:
                return text[:160]
    return str(event.get("type") or "event")[:160]


_HIDDEN_EVENT_TYPES = {
    "session_discovered", "turn_start", "turn_complete", "turn_started",
    "turn_stopped", "turn_detached", "run_state", "messages_delta",
    "command_received", "user_message_queued", "user_message_sent",
    "user_message_received", "user_message_done", "user_message_failed",
}


def is_renderable_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_type in _HIDDEN_EVENT_TYPES:
        return False
    if event_type == "agent_message":
        message = data.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if isinstance(content, str):
            return bool(content.strip())
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"tool_use", "tool_result"}:
                return True
            if block_type in {"text", "thinking", "output_text"} and str(
                block.get("text") or block.get("thinking") or ""
            ).strip():
                return True
        return False
    if event_type in {"thinking", "output", "tool_result"}:
        value = data.get("thought") if event_type == "thinking" else data.get("output")
        cleaned = re.sub(r"[\u200B-\u200D\u2060\uFEFF\u00AD]", "", str(value or ""))
        cleaned = re.sub(r"^\s*💬\s*", "", cleaned).strip()
        return bool(cleaned) and not cleaned.startswith("📋 Session started:")
    if event_type == "complete":
        if not data.get("success"):
            return True
        usage = data.get("token_usage") if isinstance(data.get("token_usage"), dict) else {}
        return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0) > 0
    if event_type == "todos_snapshot":
        return bool(data.get("todos"))
    if event_type == "model_switched":
        return any(data.get(key) for key in ("model", "provider_id", "reasoning_effort"))
    if event_type == "model_fallback":
        return bool(data.get("from_model") or data.get("to_model"))
    if event_type == "pr_link":
        return bool(data.get("prUrl") or data.get("pr_url"))
    if event_type == "worker_event":
        inner = data.get("event")
        return isinstance(inner, dict) and is_renderable_event(inner)
    if event_type == "lifecycle_notice":
        return bool(str(data.get("message") or ""))
    return True


def _node_token(row: dict[str, Any] | sqlite3.Row) -> str:
    return _digest({key: row[key] for key in ("node_id", "parent_id", "ordinal", "type", "revision")})


def _xor_hash(left: str, right: str) -> str:
    return f"{int(left, 16) ^ int(right, 16):064x}"


def _apply_aggregate(conn: sqlite3.Connection, sid: str, msg_id: str, parent_id: str, token: str, delta: int) -> None:
    row = conn.execute(
        "SELECT child_count,xor_hash FROM parent_aggregates WHERE sid=? AND msg_id=? AND parent_id=?",
        (sid, msg_id, parent_id),
    ).fetchone()
    count = (int(row["child_count"]) if row else 0) + delta
    xor_hash = _xor_hash(str(row["xor_hash"]) if row else "0" * 64, token)
    if count < 0:
        raise ProjectionUnavailable("historical aggregate underflow")
    if count == 0:
        conn.execute("DELETE FROM parent_aggregates WHERE sid=? AND msg_id=? AND parent_id=?", (sid, msg_id, parent_id))
        return
    conn.execute(
        "INSERT INTO parent_aggregates VALUES(?,?,?,?,?) ON CONFLICT(sid,msg_id,parent_id) DO UPDATE SET child_count=excluded.child_count,xor_hash=excluded.xor_hash",
        (sid, msg_id, parent_id, count, xor_hash),
    )


def _apply_node_aggregate(conn: sqlite3.Connection, sid: str, msg_id: str, parent_id: str, token: str, delta: int) -> None:
    _apply_aggregate(conn, sid, msg_id, parent_id, token, delta)
    _apply_aggregate(conn, sid, msg_id, ALL_NODES_PARENT, token, delta)


def _aggregate(conn: sqlite3.Connection, sid: str, msg_id: str, parent_id: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT child_count,xor_hash FROM parent_aggregates WHERE sid=? AND msg_id=? AND parent_id=?",
        (sid, msg_id, parent_id),
    ).fetchone()
    return (int(row["child_count"]), str(row["xor_hash"])) if row else (0, "0" * 64)


def _derived_revision(conn: sqlite3.Connection, sid: str, msg_id: str, parent_id: str, base_revision: str = "") -> str:
    count, xor_hash = _aggregate(conn, sid, msg_id, parent_id)
    global_count, global_xor = _aggregate(conn, sid, msg_id, ALL_NODES_PARENT)
    return _digest({"parent": parent_id, "base": base_revision, "count": count, "xor": xor_hash, "global_count": global_count, "global_xor": global_xor})


def _refresh_message(conn: sqlite3.Connection, sid: str, msg_id: str) -> dict[str, Any]:
    root = _root_node(msg_id)
    count, _ = _aggregate(conn, sid, msg_id, root)
    revision = _derived_revision(conn, sid, msg_id, root)
    prior = conn.execute("SELECT revision,generation,direct_child_count FROM messages WHERE sid=? AND msg_id=?", (sid, msg_id)).fetchone()
    changed = prior is None or str(prior[0]) != revision or int(prior[2]) != count
    generation = (int(prior[1]) + 1) if prior is not None and changed else (int(prior[1]) if prior else 1)
    conn.execute(
        "INSERT INTO messages VALUES(?,?,?,?,?,?) ON CONFLICT(sid,msg_id) DO UPDATE SET root_node=excluded.root_node,revision=excluded.revision,generation=excluded.generation,direct_child_count=excluded.direct_child_count",
        (sid, msg_id, root, revision, generation, count),
    )
    if changed:
        conn.execute(
            "UPDATE meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='projection_revision'"
        )
    return {"root_id": root, "sid": sid, "msg_id": msg_id, "revision": revision, "direct_child_count": count, "generation": generation}


def note_event(root_id: str, entry: dict[str, Any], start: int, end: int) -> None:
    if getattr(_rebuild_local, "root_id", None) != root_id:
        with _locks_guard:
            if root_id in _rebuilding:
                _rebuild_dirty.add(root_id)
                return
    sid, msg_id = entry.get("sid"), entry.get("msg_id")
    resolved: tuple[dict[str, Any], int, int] | None = None
    manifest = None
    with _lock(root_id), _connection(root_id, create=True) as conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','0')")
        indexed = conn.execute("SELECT value FROM meta WHERE key='indexed_end'").fetchone()
        high_water = max(int(indexed[0]) if indexed is not None else 0, end)
        conn.execute("INSERT OR REPLACE INTO meta VALUES('indexed_end',?)", (str(high_water),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('journal_identity',?)", (_journal_snapshot(root_id).identity,))
        if entry.get("type") == "event_ownership_resolved" and isinstance(sid, str):
            data = entry.get("data") or {}
            event_seq, target = data.get("event_seq"), data.get("message_id") or msg_id
            if isinstance(event_seq, int) and isinstance(target, str) and target:
                orphan = conn.execute("SELECT payload_start,payload_end FROM orphans WHERE sid=? AND seq=?", (sid, event_seq)).fetchone()
                if orphan is not None:
                    journal = _journal(root_id)
                    with journal.open("rb") as source:
                        source.seek(int(orphan[0]))
                        raw = source.read(int(orphan[1]) - int(orphan[0]))
                    original = json.loads(raw)
                    original["msg_id"] = target
                    resolved = (original, int(orphan[0]), int(orphan[1]))
                    conn.execute("DELETE FROM orphans WHERE sid=? AND seq=?", (sid, event_seq))
            if resolved is None:
                if root_id not in _rebuilding:
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','1')")
                return
        if not isinstance(sid, str) or not sid or not isinstance(msg_id, str) or not msg_id:
            seq = entry.get("seq")
            if isinstance(sid, str) and sid and isinstance(seq, int) and end > start:
                conn.execute("INSERT OR REPLACE INTO orphans VALUES(?,?,?,?)", (sid, seq, start, end))
            if root_id not in _rebuilding:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','1')")
            return
    if resolved is not None:
        note_event(root_id, resolved[0], resolved[1], resolved[2])
        return
    event = {"type": entry.get("type"), "data": entry.get("data")}
    seq = entry.get("seq")
    if not isinstance(seq, int) or end <= start:
        return
    node_id = _node_id(msg_id, event, seq)
    parent_uuid = _parent_uuid(event)
    parent_id = _root_node(msg_id)
    with _lock(root_id), _connection(root_id, create=True) as conn:
        if parent_uuid:
            parent = conn.execute(
                "SELECT node_id,parent_id,renderable FROM nodes WHERE sid=? AND msg_id=? AND node_id=?",
                (sid, msg_id, "event-" + hashlib.sha256(parent_uuid.encode()).hexdigest()[:24]),
            ).fetchone()
            if parent:
                parent_id = str(parent["node_id"] if parent["renderable"] else parent["parent_id"])
        renderable = is_renderable_event(event)
        existing = conn.execute(
            "SELECT node_id,parent_id,ordinal,type,revision,renderable FROM nodes WHERE sid=? AND msg_id=? AND node_id=?",
            (sid, msg_id, node_id),
        ).fetchone()
        if existing is not None and existing["renderable"]:
            _apply_node_aggregate(conn, sid, msg_id, str(existing["parent_id"]), _node_token(existing), -1)
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(sid,msg_id,node_id) DO UPDATE SET parent_id=excluded.parent_id,parent_key=excluded.parent_key,ordinal=excluded.ordinal,type=excluded.type,revision=excluded.revision,summary=excluded.summary,payload_start=excluded.payload_start,payload_end=excluded.payload_end,worker_json=NULL,renderable=excluded.renderable",
            (sid, msg_id, node_id, parent_id, parent_uuid, seq, str(entry.get("type") or "event"), _digest(event), _summary(event), start, end, int(renderable)),
        )
        inserted = conn.execute(
            "SELECT node_id,parent_id,ordinal,type,revision,renderable FROM nodes WHERE sid=? AND msg_id=? AND node_id=?",
            (sid, msg_id, node_id),
        ).fetchone()
        if renderable:
            _apply_node_aggregate(conn, sid, msg_id, parent_id, _node_token(inserted), 1)
        canonical_uuid = _event_uuid(event)
        if canonical_uuid:
            waiting = conn.execute(
                "SELECT node_id FROM nodes WHERE sid=? AND msg_id=? AND parent_key=? AND node_id<>?",
                (sid, msg_id, canonical_uuid, node_id),
            ).fetchall()
            for candidate in waiting:
                cursor, seen = node_id, {str(candidate[0])}
                cyclic = False
                while cursor != _root_node(msg_id):
                    if cursor in seen:
                        cyclic = True
                        break
                    seen.add(cursor)
                    owner = conn.execute(
                        "SELECT parent_id FROM nodes WHERE sid=? AND msg_id=? AND node_id=?",
                        (sid, msg_id, cursor),
                    ).fetchone()
                    if owner is None:
                        break
                    cursor = str(owner[0])
                if not cyclic:
                    child = conn.execute(
                        "SELECT node_id,parent_id,ordinal,type,revision,renderable FROM nodes WHERE sid=? AND msg_id=? AND node_id=?",
                        (sid, msg_id, candidate[0]),
                    ).fetchone()
                    if child is None:
                        continue
                    if child["renderable"]:
                        _apply_node_aggregate(conn, sid, msg_id, str(child["parent_id"]), _node_token(child), -1)
                    target_parent = node_id if renderable else parent_id
                    conn.execute(
                        "UPDATE nodes SET parent_id=? WHERE sid=? AND msg_id=? AND node_id=?",
                        (target_parent, sid, msg_id, candidate[0]),
                    )
                    moved = dict(child)
                    moved["parent_id"] = target_parent
                    if child["renderable"]:
                        _apply_node_aggregate(conn, sid, msg_id, target_parent, _node_token(moved), 1)
        manifest = _refresh_message(conn, sid, msg_id)
        if root_id not in _rebuilding:
            conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','1')")
    _notify_changed(root_id, manifest)


def note_workers(root_id: str, sid: str, msg_id: str, workers: list[dict[str, Any]]) -> None:
    manifest = None
    with _timed_lock(root_id, "worker_write"), _connection(root_id, create=True) as conn:
        prior_workers = conn.execute(
            "SELECT node_id,parent_id,ordinal,type,revision,renderable FROM nodes WHERE sid=? AND msg_id=? AND type='worker'",
            (sid, msg_id),
        ).fetchall()
        for prior in prior_workers:
            _apply_node_aggregate(conn, sid, msg_id, str(prior["parent_id"]), _node_token(prior), -1)
        conn.execute("DELETE FROM nodes WHERE sid=? AND msg_id=? AND type='worker'", (sid, msg_id))
        base = 1 << 60
        for index, worker in enumerate(workers):
            delegation = str(worker.get("delegation_id") or worker.get("id") or f"{msg_id}:{index}")
            node_id = "worker-" + hashlib.sha256(delegation.encode()).hexdigest()[:24]
            shell = {key: value for key, value in worker.items() if key not in ("events", "_uid_idx")}
            shell["events"] = []
            conn.execute(
                "INSERT INTO nodes VALUES(?,?,?,?,NULL,?,?,?,?,NULL,NULL,?,1) ON CONFLICT(sid,msg_id,node_id) DO UPDATE SET parent_id=excluded.parent_id,parent_key=NULL,ordinal=excluded.ordinal,revision=excluded.revision,summary=excluded.summary,worker_json=excluded.worker_json,renderable=1",
                (sid, msg_id, node_id, _root_node(msg_id), base + index, "worker", _digest(shell), str(worker.get("worker_description") or "worker")[:160], json.dumps(shell, separators=(",", ":"))),
            )
            inserted = conn.execute(
                "SELECT node_id,parent_id,ordinal,type,revision,renderable FROM nodes WHERE sid=? AND msg_id=? AND node_id=?",
                (sid, msg_id, node_id),
            ).fetchone()
            _apply_node_aggregate(conn, sid, msg_id, _root_node(msg_id), _node_token(inserted), 1)
        manifest = _refresh_message(conn, sid, msg_id)
        if root_id not in _rebuilding:
            conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','1')")
    _notify_changed(root_id, manifest)


@contextmanager
def locked_root_manifest(
    root_id: str, sid: str, msg_id: str, summary: str = "",
):
    """Yield the current root manifest while holding its projection read lock."""
    with _lock(root_id), _connection(root_id, create=False) as conn:
        _validate_current_locked(root_id, conn)
        row = conn.execute("SELECT root_node,revision,direct_child_count FROM messages WHERE sid=? AND msg_id=?", (sid, msg_id)).fetchone()
        if row is None:
            raise ProjectionUnavailable("historical projection is rebuilding")
        yield {"id": row["root_node"], "type": "turn_root", "revision": row["revision"], "direct_child_count": int(row["direct_child_count"]), "display_summary": summary[:160]}


def _validate_current_locked(root_id: str, conn: sqlite3.Connection) -> _JournalSnapshot:
    ready = conn.execute("SELECT value FROM meta WHERE key='ready'").fetchone()
    indexed = conn.execute("SELECT value FROM meta WHERE key='indexed_end'").fetchone()
    identity = conn.execute("SELECT value FROM meta WHERE key='journal_identity'").fetchone()
    journal = _journal_snapshot(root_id)
    if (
        ready is None or ready[0] != "1" or indexed is None
        or int(indexed[0]) != journal.size or identity is None
        or identity[0] != journal.identity
    ):
        raise ProjectionUnavailable("historical projection is not current")
    return journal


def root_manifest(root_id: str, sid: str, msg_id: str, summary: str = "") -> dict[str, Any]:
    with locked_root_manifest(root_id, sid, msg_id, summary) as manifest:
        return manifest


def _cursor_encode(secret: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(bytes.fromhex(secret), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _cursor_decode(secret: str, token: str) -> dict[str, Any]:
    try:
        packed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if base64.urlsafe_b64encode(packed).decode().rstrip("=") != token:
            raise ValueError
        raw, signature = packed[:-32], packed[-32:]
        if len(signature) != 32 or not hmac.compare_digest(signature, hmac.new(bytes.fromhex(secret), raw, hashlib.sha256).digest()):
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectionConflict("invalid historical cursor") from exc


def children(root_id: str, sid: str, msg_id: str, parent_id: str, revision: str, *, limit: int, cursor: str | None = None) -> dict[str, Any]:
    if limit < 1 or limit > MAX_LIMIT:
        raise ProjectionConflict("invalid historical children limit")
    journal = _journal(root_id)
    with _timed_lock(root_id, "children_read"), _connection(root_id, create=False) as conn:
        journal_snapshot = _validate_current_locked(root_id, conn)
        journal_size = journal_snapshot.size
        message = conn.execute("SELECT root_node,revision,generation,direct_child_count FROM messages WHERE sid=? AND msg_id=?", (sid, msg_id)).fetchone()
        if message is None:
            raise ProjectionUnavailable("historical projection is rebuilding")
        if parent_id == message["root_node"]:
            parent_revision, parent_type, parent_summary = message["revision"], "turn_root", ""
        else:
            parent = conn.execute("SELECT revision,type,summary FROM nodes WHERE sid=? AND msg_id=? AND node_id=?", (sid, msg_id, parent_id)).fetchone()
            if parent is None:
                raise ProjectionConflict("unknown historical parent")
            parent_revision = _derived_revision(conn, sid, msg_id, parent_id, str(parent["revision"]))
            parent_type, parent_summary = parent["type"], parent["summary"]
        if revision != parent_revision:
            raise ProjectionConflict("historical revision mismatch")
        after_ordinal = -1
        secret = conn.execute("SELECT value FROM meta WHERE key='cursor_secret'").fetchone()[0]
        if cursor:
            decoded = _cursor_decode(secret, cursor)
            expected = {"root": root_id, "sid": sid, "msg": msg_id, "parent": parent_id, "revision": revision, "generation": int(message["generation"])}
            if any(decoded.get(key) != value for key, value in expected.items()) or not isinstance(decoded.get("after"), int):
                raise ProjectionConflict("historical cursor scope mismatch")
            after_ordinal = decoded["after"]
        total_children, _ = _aggregate(conn, sid, msg_id, parent_id)
        rows = conn.execute("SELECT * FROM nodes WHERE sid=? AND msg_id=? AND parent_id=? AND renderable=1 AND ordinal>? ORDER BY ordinal LIMIT ?", (sid, msg_id, parent_id, after_ordinal, limit + 1)).fetchall()
        output, total = [], 0
        source = journal.open("rb") if journal_snapshot.exists else None
        try:
            for row in rows[:limit]:
                if row["worker_json"] is not None:
                    payload = json.loads(row["worker_json"])
                else:
                    if source is None:
                        raise ProjectionUnavailable("historical event payload journal is unavailable")
                    start, end = int(row["payload_start"]), int(row["payload_end"])
                    if start < 0 or end <= start or end > journal_size:
                        raise ProjectionUnavailable("historical projection journal fence mismatch")
                    source.seek(start)
                    raw = source.read(end - start)
                    if len(raw) != end - start or not raw.endswith(b"\n"):
                        raise ProjectionUnavailable("historical projection payload is unavailable")
                    entry = json.loads(raw)
                    payload = {"type": entry.get("type"), "data": entry.get("data")}
                encoded = json.dumps(payload, separators=(",", ":")).encode()
                total += len(encoded)
                if total > MAX_BYTES:
                    raise ProjectionConflict("historical children payload limit exceeded")
                count, _ = _aggregate(conn, sid, msg_id, row["node_id"])
                child_revision = _derived_revision(conn, sid, msg_id, row["node_id"], str(row["revision"]))
                output.append({"id": row["node_id"], "type": row["type"], "revision": child_revision, "direct_child_count": count, "display_summary": row["summary"], "render_payload": payload})
        finally:
            if source is not None:
                source.close()
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and output:
            next_cursor = _cursor_encode(secret, {"root": root_id, "sid": sid, "msg": msg_id, "parent": parent_id, "revision": revision, "generation": int(message["generation"]), "after": int(rows[limit - 1]["ordinal"])})
        parent = {"id": parent_id, "type": parent_type, "revision": parent_revision, "direct_child_count": total_children, "display_summary": parent_summary}
        return {"parent": parent, "children": output, "has_more": has_more, "next_cursor": next_cursor}


def _is_current(root_id: str) -> bool:
    try:
        journal = _journal_snapshot(root_id)
        with _lock(root_id), _connection(root_id, create=False) as conn:
            ready = conn.execute("SELECT value FROM meta WHERE key='ready'").fetchone()
            indexed = conn.execute("SELECT value FROM meta WHERE key='indexed_end'").fetchone()
            identity = conn.execute("SELECT value FROM meta WHERE key='journal_identity'").fetchone()
            return ready is not None and ready[0] == "1" and indexed is not None and int(indexed[0]) == journal.size and identity is not None and identity[0] == journal.identity
    except (ProjectionUnavailable, ProjectionConflict, OSError, ValueError, sqlite3.DatabaseError):
        return False


def _resume_at_eof(root_id: str, root_snapshot: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    journal = _journal_snapshot(root_id)
    with _lock(root_id), _connection(root_id, create=False) as conn:
        ready = conn.execute("SELECT value FROM meta WHERE key='ready'").fetchone()
        indexed = conn.execute("SELECT value FROM meta WHERE key='indexed_end'").fetchone()
        identity = conn.execute("SELECT value FROM meta WHERE key='journal_identity'").fetchone()
        if (
            ready is None or ready[0] != "0" or indexed is None
            or int(indexed[0]) != journal.size or identity is None
            or identity[0] != journal.identity
        ):
            return None

    def visit(node: dict[str, Any]) -> None:
        sid = node.get("id")
        if isinstance(sid, str):
            for message in node.get("messages") or []:
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                msg_id = message.get("id")
                if isinstance(msg_id, str):
                    note_workers(root_id, sid, msg_id, message.get("workers") or [])
        for child in node.get("forks") or []:
            if isinstance(child, dict):
                visit(child)

    if isinstance(root_snapshot, dict):
        visit(root_snapshot)
    with _lock(root_id), _connection(root_id, create=True) as conn:
        current = _journal_snapshot(root_id)
        indexed = conn.execute("SELECT value FROM meta WHERE key='indexed_end'").fetchone()
        identity = conn.execute("SELECT value FROM meta WHERE key='journal_identity'").fetchone()
        if indexed is None or int(indexed[0]) != current.size or identity is None or identity[0] != current.identity:
            raise ProjectionUnavailable("historical projection resume lost journal race")
        conn.execute("INSERT OR REPLACE INTO meta VALUES('indexed_end',?)", (str(current.size),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('journal_identity',?)", (current.identity,))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','1')")
        return [dict(row) for row in conn.execute(
            "SELECT sid,msg_id,root_node AS root_id,revision,direct_child_count,generation FROM messages"
        ).fetchall()]


def schedule_rebuild(root_id: str, root_snapshot: dict[str, Any] | None, *, priority: bool = True):
    if _shutdown:
        return
    with _locks_guard:
        if root_id in _rebuilding:
            _rebuild_pending[root_id] = (root_snapshot, priority)
            return None
        _rebuilding.add(root_id)

    def run() -> None:
        _rebuild_local.root_id = root_id
        started = time.perf_counter()
        state = "failed"
        manifests: list[dict[str, Any]] = []
        source = None
        def yield_to_priority() -> None:
            if priority:
                return
            with _locks_guard:
                pending = _rebuild_pending.get(root_id)
            if pending is not None and pending[1]:
                raise _RebuildPromoted()
        try:
            if _is_current(root_id):
                state = "already_current"
                return
            try:
                resumed = _resume_at_eof(root_id, root_snapshot)
            except (ProjectionUnavailable, ProjectionConflict, OSError, sqlite3.DatabaseError):
                resumed = None
            if resumed is not None:
                manifests = resumed
                state = "resumed_eof"
                return
            journal = _journal(root_id)
            import hydration_index_store
            with hydration_index_store.journal_guard(root_id, journal):
                scan_snapshot = _journal_snapshot(root_id)
                if _journal_snapshot(root_id) != scan_snapshot:
                    raise ProjectionUnavailable("historical projection rebuild lost journal race")
                if scan_snapshot.exists:
                    try:
                        source = journal.open("rb")
                    except FileNotFoundError as exc:
                        raise ProjectionUnavailable("historical projection rebuild lost journal race") from exc
                with _lock(root_id):
                    try:
                        with _connection(root_id, create=True) as conn:
                            _clear_projection(conn)
                    except _ProjectionInvalid:
                        _replace_invalid_projection(root_id)

            def scan(source) -> None:
                while True:
                    start = source.tell()
                    raw = source.readline()
                    if not raw:
                        return
                    if not raw.endswith(b"\n"):
                        raise ProjectionUnavailable("historical journal has a torn tail")
                    end = source.tell()
                    try:
                        entry = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(entry, dict):
                        note_event(root_id, entry, start, end)
                        yield_to_priority()

            if source is not None:
                scan(source)

            def visit(node: dict[str, Any]) -> None:
                sid = node.get("id")
                if isinstance(sid, str):
                    for message in node.get("messages") or []:
                        if not isinstance(message, dict) or message.get("role") != "assistant":
                            continue
                        msg_id = message.get("id")
                        if isinstance(msg_id, str):
                            note_workers(root_id, sid, msg_id, message.get("workers") or [])
                for child in node.get("forks") or []:
                    if isinstance(child, dict):
                        visit(child)

            if isinstance(root_snapshot, dict):
                yield_to_priority()
                visit(root_snapshot)
            yield_to_priority()

            with hydration_index_store.journal_guard(root_id, journal):
                current = _journal_snapshot(root_id)
                if current.exists != scan_snapshot.exists:
                    raise ProjectionUnavailable("historical projection rebuild lost journal race")
                if current.exists:
                    initial_file = scan_snapshot.identity.split(":", 4)[:3]
                    current_file = current.identity.split(":", 4)[:3]
                    if initial_file != current_file or source is None:
                        raise ProjectionUnavailable("historical projection rebuild lost journal race")
                    scan(source)
                    current = _journal_snapshot(root_id)
                    current_file = current.identity.split(":", 4)[:3]
                    source_stat = os.fstat(source.fileno())
                    source_file = ["present", str(source_stat.st_dev), str(source_stat.st_ino)]
                    if (
                        initial_file != current_file
                        or initial_file != source_file
                        or source.tell() != current.size
                    ):
                        raise ProjectionUnavailable("historical projection rebuild lost journal race")
                with _lock(root_id), _connection(root_id, create=True) as conn:
                    indexed = conn.execute("SELECT value FROM meta WHERE key='indexed_end'").fetchone()
                    if indexed is None or int(indexed[0]) != current.size:
                        raise ProjectionUnavailable("historical projection rebuild lost journal race")
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('journal_identity',?)", (current.identity,))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('ready','1')")
                    manifests = [dict(row) for row in conn.execute(
                        "SELECT sid,msg_id,root_node AS root_id,revision,direct_child_count,generation FROM messages"
                    ).fetchall()]
            state = "rebuilt"
        except _RebuildPromoted:
            state = "promoted"
        except Exception:
            logger.exception("historical rebuild failed root_id=%s state=%s", root_id, state)
            raise
        finally:
            if source is not None:
                source.close()
            _rebuild_local.root_id = None
            duration_ms = (time.perf_counter() - started) * 1000
            perf.record("historical.rebuild.duration", duration_ms)
            logger.info(
                "historical rebuild root_id=%s state=%s duration_ms=%.3f",
                root_id, state, duration_ms,
            )
            pending = None
            with _locks_guard:
                _rebuilding.discard(root_id)
                pending = _rebuild_pending.pop(root_id, None)
                dirty = root_id in _rebuild_dirty
                _rebuild_dirty.discard(root_id)
                if dirty and pending is None:
                    pending = (root_snapshot, priority)
            if state in {"already_current", "resumed_eof", "rebuilt"} and pending is None:
                _settle_current_waiters(root_id)
            elif state == "failed" and pending is None:
                _settle_current_waiters(
                    root_id,
                    ProjectionUnavailable("historical projection rebuild failed"),
                )
            for manifest in manifests:
                _notify_changed(root_id, manifest)
            if pending is not None and not _shutdown:
                schedule_rebuild(root_id, pending[0], priority=pending[1])

    lane = "ondemand" if priority else "startup"
    try:
        return _executor_pool.submit(root_id, run, lane=lane)
    except RuntimeError as exc:
        with _locks_guard:
            _rebuilding.discard(root_id)
        _settle_current_waiters(
            root_id,
            ProjectionUnavailable("historical projection executor is unavailable"),
        )
        raise ProjectionUnavailable(
            "historical projection executor is unavailable"
        ) from exc


def schedule_all(session_manager: Any) -> None:
    for summary in session_manager.list():
        root_id = summary.get("id") if isinstance(summary, dict) else None
        if not isinstance(root_id, str) or not root_id:
            continue
        if _is_current(root_id):
            continue
        future = schedule_rebuild(
            root_id, session_manager.get(root_id), priority=False,
        )
        if future is None:
            continue
        try:
            future.result()
        except Exception:
            logger.exception("historical startup rebuild did not complete root_id=%s", root_id)


def shutdown(*, wait: bool = True) -> None:
    global _shutdown, _change_observer
    with _locks_guard:
        _shutdown = True
    _change_observer = None
    with _locks_guard:
        waiting_roots = tuple(_current_waiters)
    for root_id in waiting_roots:
        _settle_current_waiters(
            root_id,
            ProjectionUnavailable("historical projection is shutting down"),
        )
    # schedule_rebuild's `_rebuilding` set already guarantees at most one
    # in-flight/queued run() per root, so -- like SessionProjectionDrainer
    # -- there is never cross-root queued work in either lane to cancel.
    _executor_pool.shutdown(wait=wait)
