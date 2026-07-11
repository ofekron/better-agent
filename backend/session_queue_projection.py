from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import perf
from paths import ba_home

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_load_cv = threading.Condition(_lock)
_loaded = False
_loading = False
_records: dict[str, dict[str, Any]] = {}
_sequence = 0
_durable_sequence = 0
_persisted_sequence = 0
_journal: dict[str, tuple[int, Optional[dict[str, Any]]]] = {}
_mutation_log: dict[str, tuple[int, Optional[dict[str, Any]]]] = {}

_write_cv = threading.Condition()
_pending_writes: dict[str, tuple[int, Optional[dict[str, Any]]]] = {}
_pending_rebuild: Optional[tuple[int, dict[str, dict[str, Any]], dict[str, list[int]]]] = None
_active_writes = 0
_write_failure: Optional[BaseException] = None
_writer_started = False

_SCHEMA_VERSION = 1


def _projection_dir() -> Path:
    return ba_home() / "queue_recovery_projection"


def _database_path() -> Path:
    return _projection_dir() / "projection.sqlite3"


def _record_path(session_id: str, generation: Optional[str] = None) -> Path:
    del generation
    return _database_path()


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS records (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            sequence INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    version = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if version is None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        connection.commit()
    elif version[0] != str(_SCHEMA_VERSION):
        connection.close()
        raise RuntimeError("unsupported queue projection schema; wipe queue_recovery_projection")
    return connection


def _session_files_fingerprint() -> dict[str, list[int]]:
    import session_store

    fingerprint: dict[str, list[int]] = {}
    home = ba_home()
    for path in session_store._session_json_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint[path.relative_to(home).as_posix()] = [
            int(stat.st_dev), int(stat.st_ino), int(stat.st_mtime_ns),
            int(stat.st_ctime_ns), int(stat.st_size),
        ]
    return fingerprint


def _metadata(connection: sqlite3.Connection, key: str) -> Optional[str]:
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def projection_is_current() -> bool:
    try:
        with _connect() as connection:
            raw = _metadata(connection, "fingerprint")
    except (OSError, sqlite3.Error, RuntimeError):
        return False
    if raw is None:
        return False
    try:
        return json.loads(raw) == _session_files_fingerprint()
    except json.JSONDecodeError:
        return False


def certification_generation() -> int:
    with _lock:
        return _persisted_sequence


def mark_dirty() -> int:
    global _sequence
    with _lock:
        _sequence += 1
        return _sequence


def mark_current_if_generation(
    expected_generation: int,
    expected_fingerprint: Optional[dict[str, list[int]]] = None,
    projection_generation: Optional[str] = None,
) -> bool:
    del projection_generation
    fingerprint = expected_fingerprint or _session_files_fingerprint()
    with _lock:
        if expected_generation != _persisted_sequence or _journal:
            return False
    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('fingerprint', ?)",
                (json.dumps(fingerprint, sort_keys=True, separators=(",", ":")),),
            )
            connection.commit()
    except sqlite3.Error:
        logger.exception("failed to certify queue projection")
        return False
    return True


def _compact_ack(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    client_id = message.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        return None
    ack: dict[str, Any] = {"client_id": client_id}
    for key in ("id", "lifecycle_msg_id", "seq", "timestamp"):
        if message.get(key) is not None:
            ack[key] = copy.deepcopy(message[key])
    return ack


def _compact_loaded_record(record: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(record)


def _load_candidate() -> tuple[dict[str, dict[str, Any]], int]:
    started = time.perf_counter()
    loaded: dict[str, dict[str, Any]] = {}
    bytes_read = 0
    durable = 0
    with _connect() as connection:
        for session_id, payload, sequence in connection.execute(
            "SELECT id, payload, sequence FROM records"
        ):
            bytes_read += len(payload)
            record = json.loads(payload)
            if isinstance(record, dict) and record.get("id") == session_id:
                loaded[session_id] = record
                durable = max(durable, int(sequence))
        raw_sequence = _metadata(connection, "sequence")
        if raw_sequence is not None:
            durable = max(durable, int(raw_sequence))
    perf.record_count("queue_projection.load.files", 1)
    perf.record_count("queue_projection.load.rows", len(loaded))
    perf.record_count("queue_projection.load.bytes", bytes_read)
    perf.record("queue_projection.load.build", (time.perf_counter() - started) * 1000.0)
    return loaded, durable


def _ensure_loaded() -> None:
    global _loaded, _loading, _durable_sequence, _sequence, _persisted_sequence
    wait_started = time.perf_counter()
    with _load_cv:
        while _loading and not _loaded:
            _load_cv.wait()
        perf.record("queue_projection.load.wait", (time.perf_counter() - wait_started) * 1000.0)
        if _loaded:
            return
        _loading = True
    try:
        candidate, durable = _load_candidate()
    except BaseException:
        with _load_cv:
            _loading = False
            _load_cv.notify_all()
        raise
    with _load_cv:
        for sid, (_sequence_value, record) in _mutation_log.items():
            if record is None:
                candidate.pop(sid, None)
            else:
                candidate[sid] = record
        _records.clear()
        _records.update(candidate)
        _durable_sequence = max(_durable_sequence, durable)
        _persisted_sequence = max(_persisted_sequence, durable)
        _sequence = max(_sequence, durable)
        _loaded = True
        _loading = False
        _load_cv.notify_all()


def _reset_and_load() -> None:
    global _loaded
    with _load_cv:
        _records.clear()
        _loaded = False
    _ensure_loaded()


def _ensure_writer_locked() -> None:
    global _writer_started
    if _writer_started:
        return
    _writer_started = True
    threading.Thread(target=_writer_loop, name="queue-projection-writer", daemon=True).start()


def _enqueue_write(session_id: str, sequence: int, record: Optional[dict[str, Any]]) -> None:
    with _write_cv:
        current = _pending_writes.get(session_id)
        if current is None or sequence > current[0]:
            _pending_writes[session_id] = (sequence, copy.deepcopy(record))
        _ensure_writer_locked()
        _write_cv.notify_all()


def _compact_batch(batch: dict[str, tuple[int, Optional[dict[str, Any]]]]) -> None:
    global _durable_sequence, _persisted_sequence
    if not batch:
        return
    started = time.perf_counter()
    inserted = updated = deleted = 0
    with _connect() as connection:
        wait_started = time.perf_counter()
        connection.execute("BEGIN IMMEDIATE")
        perf.record("queue_projection.transaction.wait", (time.perf_counter() - wait_started) * 1000.0)
        for sid, (sequence, record) in batch.items():
            if record is None:
                deleted += connection.execute("DELETE FROM records WHERE id=?", (sid,)).rowcount
                continue
            payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
            existing = connection.execute("SELECT payload FROM records WHERE id=?", (sid,)).fetchone()
            if existing is None:
                inserted += 1
            elif existing[0] != payload:
                updated += 1
            else:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO records(id, payload, sequence) VALUES(?, ?, ?)",
                (sid, payload, sequence),
            )
        high_water = max(sequence for sequence, _record in batch.values())
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('sequence', ?)",
            (str(high_water),),
        )
        commit_started = time.perf_counter()
        connection.commit()
        perf.record("queue_projection.transaction.commit", (time.perf_counter() - commit_started) * 1000.0)
    with _lock:
        _durable_sequence = max(_durable_sequence, high_water)
        for sid, (sequence, _record) in batch.items():
            current = _journal.get(sid)
            if current is not None and current[0] <= sequence:
                _journal.pop(sid, None)
        if not _journal:
            _persisted_sequence = max(_persisted_sequence, high_water)
    perf.record_count("queue_projection.transaction.inserted", inserted)
    perf.record_count("queue_projection.transaction.updated", updated)
    perf.record_count("queue_projection.transaction.deleted", deleted)
    perf.record_count("queue_projection.transaction.unchanged", len(batch) - inserted - updated - deleted)
    perf.record("queue_projection.transaction.write", (time.perf_counter() - started) * 1000.0)


def _compact_rebuild(
    sequence: int,
    records: dict[str, dict[str, Any]],
    fingerprint: dict[str, list[int]],
) -> None:
    global _durable_sequence
    started = time.perf_counter()
    with _connect() as connection:
        wait_started = time.perf_counter()
        connection.execute("BEGIN IMMEDIATE")
        perf.record("queue_projection.rebuild.transaction_wait", (time.perf_counter() - wait_started) * 1000.0)
        connection.execute("DELETE FROM records")
        connection.executemany(
            "INSERT INTO records(id, payload, sequence) VALUES(?, ?, ?)",
            (
                (sid, json.dumps(record, separators=(",", ":"), ensure_ascii=False), sequence)
                for sid, record in records.items()
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('sequence', ?)",
            (str(sequence),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('fingerprint', ?)",
            (json.dumps(fingerprint, sort_keys=True, separators=(",", ":")),),
        )
        commit_started = time.perf_counter()
        connection.commit()
        perf.record("queue_projection.rebuild.commit", (time.perf_counter() - commit_started) * 1000.0)
    with _lock:
        _durable_sequence = max(_durable_sequence, sequence)
    perf.record_count("queue_projection.rebuild.rows", len(records))
    perf.record("queue_projection.rebuild.transaction", (time.perf_counter() - started) * 1000.0)


def _writer_loop() -> None:
    global _active_writes, _pending_rebuild, _write_failure
    while True:
        with _write_cv:
            while not _pending_writes and _pending_rebuild is None:
                _write_cv.wait()
            rebuild = _pending_rebuild
            _pending_rebuild = None
            batch = dict(_pending_writes)
            _pending_writes.clear()
            _active_writes += 1
        try:
            if rebuild is not None:
                _compact_rebuild(*rebuild)
            _compact_batch(batch)
            _write_failure = None
        except BaseException as exc:
            logger.exception("queue projection transaction failed")
            with _write_cv:
                _write_failure = exc
        finally:
            with _write_cv:
                _active_writes -= 1
                _write_cv.notify_all()


def _wait_durable(sequence: int) -> None:
    started = time.perf_counter()
    with _write_cv:
        while True:
            with _lock:
                if _durable_sequence >= sequence:
                    break
            if _write_failure is not None:
                raise RuntimeError("queue projection durability failed") from _write_failure
            _write_cv.wait()
    perf.record("queue_projection.writer.durable_wait", (time.perf_counter() - started) * 1000.0)


def _apply_mutation(session_id: str, record: Optional[dict[str, Any]]) -> tuple[int, bool]:
    global _sequence
    _ensure_loaded()
    owned = copy.deepcopy(record)
    with _lock:
        if (record is None and session_id not in _records) or _records.get(session_id) == owned:
            current = _journal.get(session_id)
            return (current[0] if current else _durable_sequence), False
        _sequence += 1
        sequence = _sequence
        if owned is None:
            _records.pop(session_id, None)
        else:
            _records[session_id] = owned
        _journal[session_id] = (sequence, owned)
        _mutation_log[session_id] = (sequence, owned)
        perf.record_count("queue_projection.journal.depth", len(_journal))
        perf.record_count("queue_projection.journal.high_water", sequence)
    return sequence, True


def upsert_record(record: dict[str, Any]) -> None:
    sid = record.get("id")
    if not isinstance(sid, str) or not sid:
        return
    sequence, changed = _apply_mutation(sid, _compact_loaded_record(record))
    if changed:
        _enqueue_write(sid, sequence, record)
        _wait_durable(sequence)


def upsert_record_background(record: dict[str, Any]) -> None:
    sid = record.get("id")
    if not isinstance(sid, str) or not sid:
        return
    sequence, changed = _apply_mutation(sid, _compact_loaded_record(record))
    if changed:
        _enqueue_write(sid, sequence, record)


def note_persisted_tree(root: dict[str, Any]) -> int:
    global _persisted_sequence, _sequence
    records = [record for node in _walk_nodes(root) if (record := project_session(node)) is not None]
    _ensure_loaded()
    with _lock:
        _sequence += 1
        high_water = _sequence
        changed_records: list[dict[str, Any]] = []
        for record in records:
            sid = record["id"]
            owned = copy.deepcopy(record)
            if _records.get(sid) == owned:
                continue
            _records[sid] = owned
            _journal[sid] = (high_water, owned)
            _mutation_log[sid] = (high_water, owned)
            changed_records.append(owned)
        _persisted_sequence = high_water
        perf.record_count("queue_projection.persisted.high_water", _persisted_sequence)
        perf.record_count("queue_projection.persisted.changed_rows", len(changed_records))
    for record in changed_records:
        _enqueue_write(record["id"], high_water, record)
    return high_water


def upsert_from_session(session: dict[str, Any]) -> None:
    record = project_session(session)
    if record is not None:
        upsert_record(record)


def delete_records(session_ids: Iterable[str]) -> None:
    writes: list[tuple[str, int]] = []
    for sid in dict.fromkeys(str(value) for value in session_ids if value):
        sequence, changed = _apply_mutation(sid, None)
        if changed:
            writes.append((sid, sequence))
            _enqueue_write(sid, sequence, None)
    for _sid, sequence in writes:
        _wait_durable(sequence)


def delete_record(session_id: str) -> None:
    delete_records((session_id,))


def flush_pending_writes(timeout: Optional[float] = None) -> bool:
    deadline = None if timeout is None else time.monotonic() + timeout
    with _write_cv:
        while _pending_writes or _pending_rebuild is not None or _active_writes:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            _write_cv.wait(remaining)
    return True


def get(session_id: str) -> Optional[dict[str, Any]]:
    _ensure_loaded()
    with _lock:
        record = _records.get(session_id)
    return copy.deepcopy(record) if record is not None else None


def get_many(session_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [sid for sid in session_ids if sid]
    _ensure_loaded()
    started = time.perf_counter()
    with _lock:
        perf.record("queue_projection.get_many.lock_wait", (time.perf_counter() - started) * 1000.0)
        selected = {sid: record for sid in ids if (record := _records.get(sid)) is not None}
    return copy.deepcopy(selected)


def queued_prompts(session_id: str) -> list[dict[str, Any]]:
    record = get(session_id)
    return [item for item in (record or {}).get("queued_prompts", []) if isinstance(item, dict)]


def _user_message_projection(messages: Iterable[Any]) -> dict[str, Any]:
    acks: dict[str, dict[str, Any]] = {}
    lifecycle_ids: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        ack = _compact_ack(message)
        if ack is not None:
            acks[ack["client_id"]] = ack
        lifecycle_id = message.get("lifecycle_msg_id")
        if isinstance(lifecycle_id, str) and lifecycle_id:
            lifecycle_ids.append(lifecycle_id)
    return {"user_message_acks": acks, "user_lifecycle_msg_ids": list(dict.fromkeys(lifecycle_ids))}


def project_session(session: dict[str, Any]) -> Optional[dict[str, Any]]:
    sid = session.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    users = _user_message_projection(session.get("messages") or [])
    client_ids = set(users["user_message_acks"])
    lifecycle_ids = set(users["user_lifecycle_msg_ids"])
    queued = []
    for prompt in session.get("queued_prompts") or []:
        if not isinstance(prompt, dict):
            continue
        if prompt.get("client_id") in client_ids or prompt.get("lifecycle_msg_id") in lifecycle_ids:
            continue
        queued.append(copy.deepcopy(prompt))
    return {"id": sid, "model": session.get("model"), "cwd": session.get("cwd"), "queued_prompts": queued, **users}


def _walk_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for fork in node.get("forks") or []:
        if isinstance(fork, dict):
            yield from _walk_nodes(fork)


def _scan_complete_snapshot() -> tuple[dict[str, dict[str, Any]], dict[str, list[int]]]:
    import session_store

    started = time.perf_counter()
    rebuilt: dict[str, dict[str, Any]] = {}
    files = bytes_read = 0
    for path in session_store._session_json_files():
        try:
            raw = path.read_bytes()
            root = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        files += 1
        bytes_read += len(raw)
        if isinstance(root, dict):
            for node in _walk_nodes(root):
                record = project_session(node)
                if record is not None:
                    rebuilt[record["id"]] = record
    perf.record_count("queue_projection.rebuild.scan.files", files)
    perf.record_count("queue_projection.rebuild.scan.bytes", bytes_read)
    perf.record("queue_projection.rebuild.scan", (time.perf_counter() - started) * 1000.0)
    return rebuilt, _session_files_fingerprint()


def rebuild_from_disk() -> int:
    global _loaded, _pending_rebuild, _persisted_sequence
    rebuilt, fingerprint = _scan_complete_snapshot()
    with _lock:
        for sid, (_sequence_value, record) in _mutation_log.items():
            if record is None:
                rebuilt.pop(sid, None)
            else:
                rebuilt[sid] = copy.deepcopy(record)
        _records.clear()
        _records.update(rebuilt)
        _loaded = True
        _persisted_sequence = max(_persisted_sequence, _sequence)
        snapshot_sequence = _sequence
        for sid, (sequence, _record) in tuple(_mutation_log.items()):
            if sequence <= snapshot_sequence:
                _mutation_log.pop(sid, None)
    with _write_cv:
        _pending_rebuild = (snapshot_sequence, copy.deepcopy(rebuilt), fingerprint)
        _ensure_writer_locked()
        _write_cv.notify_all()
    with _lock:
        newer = {sid: item for sid, item in _journal.items() if item[0] > snapshot_sequence}
    for sid, (sequence, record) in newer.items():
        _enqueue_write(sid, sequence, record)
    return len(rebuilt)


def ensure_current_or_rebuild() -> bool:
    if projection_is_current():
        _reset_and_load()
        return False
    rebuild_from_disk()
    return True


def list_queued_records() -> list[dict[str, Any]]:
    _ensure_loaded()
    with _lock:
        selected = [record for _sid, record in sorted(_records.items()) if record.get("queued_prompts")]
    return copy.deepcopy(selected)


def queued_counts() -> dict[str, int]:
    _ensure_loaded()
    with _lock:
        return {sid: len(record.get("queued_prompts") or []) for sid, record in _records.items() if record.get("queued_prompts")}
