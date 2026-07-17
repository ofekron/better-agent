import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import Future, ProcessPoolExecutor, TimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
import threading

import perf
import portable_lock
from paths import ba_home


logger = logging.getLogger(__name__)


SCHEMA = 2
BOUNDARY_BYTES = 4096
BUILD_TIMEOUT_SECONDS = 300
_WORKER_COUNT = 2
_pool_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_generation: object = None
_builds: dict[
    Path, tuple[Future, Path, tuple[tuple[int, int, int, int, int], str]]
] = {}
_shutdown = threading.Event()
_receipts_lock = threading.Lock()
_append_receipts: dict[str, tuple[int, int, int, int, str, str]] = {}
_journal_guard_locks: dict[str, threading.RLock] = {}
_journal_guard_locks_guard = threading.Lock()
_journal_guard_local = threading.local()


class _StaleProjection(Exception):
    pass


class WriterProjectionError(RuntimeError):
    pass


def _safe_root_dir(root_id: str) -> Path:
    if (
        not isinstance(root_id, str) or not root_id
        or root_id in {".", ".."} or "/" in root_id or "\\" in root_id
        or "\x00" in root_id
    ):
        raise ValueError("invalid hydration root id")
    sessions = (ba_home() / "sessions").resolve()
    root = sessions / root_id
    if root.resolve(strict=False).parent != sessions:
        raise ValueError("hydration root escapes sessions directory")
    return root


def _db_path(root_id: str) -> Path:
    return _safe_root_dir(root_id) / "render_hydration.sqlite3"


def _legacy_db_path(root_id: str) -> Path:
    _safe_root_dir(root_id)
    name = hashlib.sha256(root_id.encode("utf-8")).hexdigest() + ".sqlite3"
    return ba_home() / "cache" / "render-hydration" / name


def _ack_path(journal: Path) -> Path:
    return journal.parent / "hydration_index_ack.json"


def _receipt_path(journal: Path) -> Path:
    return journal.parent / "hydration_append_receipt.json"


def _validate_journal(root_id: str, journal: Path) -> Path:
    root = _safe_root_dir(root_id)
    candidate = Path(journal)
    if (
        candidate.name != "events.jsonl"
        or candidate.parent.resolve(strict=False) != root
        or candidate.is_symlink()
    ):
        raise ValueError("hydration journal is outside its root")
    return candidate


def _write_ack(journal: Path, offset: int, digest: str) -> None:
    target = _ack_path(journal)
    stat = journal.stat()
    payload = {
        "version": SCHEMA, "dev": int(stat.st_dev), "ino": int(stat.st_ino),
        "offset": int(offset), "digest": digest,
    }
    try:
        if json.loads(target.read_text(encoding="utf-8")) == payload:
            return
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def journal_guard(root_id: str, journal: Path | None = None):
    authority = _validate_journal(
        root_id, journal or (_safe_root_dir(root_id) / "events.jsonl"),
    )
    with _journal_guard_locks_guard:
        local_lock = _journal_guard_locks.setdefault(root_id, threading.RLock())
    with local_lock:
        held = getattr(_journal_guard_local, "held", {})
        existing = held.get(root_id)
        if existing is not None:
            held[root_id] = (existing[0], existing[1] + 1)
            try:
                yield
            finally:
                held[root_id] = (existing[0], existing[1])
            return
        lock_path = authority.parent / ".render_hydration.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        portable_lock.lock_ex(lock_file.fileno())
        held[root_id] = (lock_file, 1)
        _journal_guard_local.held = held
        try:
            yield
        finally:
            held.pop(root_id, None)
            portable_lock.unlock(lock_file.fileno())
            lock_file.close()


def _boundary(path: Path, offset: int) -> str:
    start = max(0, offset - BOUNDARY_BYTES)
    with path.open("rb") as file:
        file.seek(start)
        return hashlib.sha256(file.read(offset - start)).hexdigest()


def _journal_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
        int(stat.st_mtime_ns), int(stat.st_ctime_ns),
    )


def _create(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE offsets(sid TEXT NOT NULL, offset INTEGER NOT NULL);"
        "CREATE INDEX offsets_sid ON offsets(sid, offset);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    return conn


def _scan(
    conn: sqlite3.Connection, journal: Path, start: int, digest: str,
    stop: int | None = None,
) -> tuple[int, int, int, str]:
    rows: list[tuple[str, int]] = []
    inserted = 0
    scanned = 0
    committed = start
    valid_rows = 0
    invalid_rows = 0
    with journal.open("rb") as file:
        file.seek(start)
        while True:
            offset = file.tell()
            remaining = None if stop is None else stop - offset
            if remaining is not None and remaining <= 0:
                break
            line = file.readline(remaining)
            if not line:
                break
            scanned += len(line)
            if not line.endswith(b"\n"):
                break
            committed = file.tell()
            chain_hash = hashlib.sha256(bytes.fromhex(digest))
            chain_hash.update(line)
            digest = chain_hash.hexdigest()
            try:
                row = json.loads(line)
                valid_rows += 1
            except (json.JSONDecodeError, UnicodeDecodeError):
                sid = None
                invalid_rows += 1
            else:
                sid = row.get("sid") if isinstance(row, dict) else None
                if not isinstance(sid, str) or not sid:
                    sid = None
            if sid is not None:
                rows.append((sid, offset))
            if len(rows) >= 4096:
                inserted += len(rows)
                conn.executemany("INSERT INTO offsets VALUES (?, ?)", rows)
                rows.clear()
    if rows:
        inserted += len(rows)
        conn.executemany("INSERT INTO offsets VALUES (?, ?)", rows)
    perf.record_count("hydrate.index_scan.valid_rows", valid_rows)
    perf.record_count("hydrate.index_scan.invalid_rows", invalid_rows)
    return committed, scanned, inserted, digest


def _cold_build(
    journal_raw: str, output_raw: str,
    snapshot: tuple[tuple[int, int, int, int, int], str],
) -> None:
    journal = Path(journal_raw)
    output = Path(output_raw)
    captured, captured_boundary = snapshot
    conn = _create(output)
    try:
        committed, scanned, rows, digest = _scan(
            conn, journal, 0, bytes(32).hex(), captured[2],
        )
        end = journal.stat()
        if (
            (int(end.st_dev), int(end.st_ino)) != captured[:2]
            or int(end.st_size) < captured[2]
            or committed != captured[2]
            or _boundary(journal, captured[2]) != captured_boundary
        ):
            raise RuntimeError("journal changed during hydration index build")
        values = {
            "schema": str(SCHEMA), "dev": str(captured[0]), "ino": str(captured[1]),
            "offset": str(committed), "boundary": _boundary(journal, committed),
            "mtime_ns": str(captured[3]), "ctime_ns": str(captured[4]),
            "scanned": str(scanned), "rows": str(rows), "digest": digest,
            "reconciled_seq": "0",
        }
        conn.executemany("INSERT INTO meta VALUES (?, ?)", values.items())
        conn.commit()
    finally:
        conn.close()


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta"))


def note_authoritative_append(
    root_id: str, journal: Path, start: int, end: int,
    before_digest: str, after_digest: str, sid: str | None = None,
) -> None:
    stat = journal.stat()
    with _receipts_lock:
        prior = _append_receipts.get(root_id)
        chained = (
            prior is not None
            and prior[:2] == (stat.st_dev, stat.st_ino)
            and prior[3] == start
            and prior[5] == before_digest
        )
        origin = prior[2] if chained else start
        origin_digest = prior[4] if chained else before_digest
        _append_receipts[root_id] = (
            stat.st_dev, stat.st_ino, origin, end, origin_digest, after_digest,
        )


def prepare_durable_append_receipt(
    root_id: str, journal: Path, end: int, digest: str,
) -> None:
    journal = _validate_journal(root_id, journal)
    predecessor_size = 0
    predecessor_digest = bytes(32).hex()
    try:
        ack = json.loads(_ack_path(journal).read_text(encoding="utf-8"))
        predecessor_size = int(ack["offset"])
        predecessor_digest = str(ack["digest"])
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    stat = journal.stat()
    payload = {
        "version": 1, "dev": int(stat.st_dev), "ino": int(stat.st_ino),
        "predecessor_size": predecessor_size,
        "predecessor_digest": predecessor_digest,
        "size": int(end), "digest": digest,
    }
    target = _receipt_path(journal)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)


def _install_legacy_projection(root_id: str, journal: Path) -> None:
    target = _db_path(root_id)
    if target.exists():
        return
    legacy = _legacy_db_path(root_id)
    if not legacy.exists():
        return
    try:
        with sqlite3.connect(legacy) as conn:
            meta = _meta(conn)
            if not _valid_append(root_id, meta, journal):
                return
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, target)
        perf.record_count("hydrate.writer_projection.migrated", 1)
    except (OSError, sqlite3.Error, KeyError, ValueError):
        return


def flush_writer_projection(root_id: str, journal: Path) -> None:
    with journal_guard(root_id, journal):
        _install_legacy_projection(root_id, journal)
        target = _db_path(root_id)
        conn: sqlite3.Connection | None = None
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                conn = _create(target)
                stat = journal.stat()
                zero = bytes(32).hex()
                conn.executemany("INSERT INTO meta VALUES (?, ?)", {
                    "schema": str(SCHEMA), "dev": str(stat.st_dev),
                    "ino": str(stat.st_ino), "offset": "0",
                    "boundary": _boundary(journal, 0),
                    "mtime_ns": str(stat.st_mtime_ns),
                    "ctime_ns": str(stat.st_ctime_ns), "scanned": "0",
                    "rows": "0", "digest": zero, "reconciled_seq": "0",
                }.items())
                conn.commit()
            else:
                conn = sqlite3.connect(target)
            conn.execute("BEGIN IMMEDIATE")
            meta = _meta(conn)
            if not _valid_append(root_id, meta, journal):
                conn.rollback()
                raise WriterProjectionError("projection prefix is not authoritative")
            start = int(meta["offset"])
            stat = journal.stat()
            if stat.st_size == start:
                conn.rollback()
                return
            expected, scanned, rows, digest = _scan(
                conn, journal, start, meta["digest"], stat.st_size,
            )
            if expected != stat.st_size:
                raise WriterProjectionError("journal tail is incomplete")
            receipt_digest = _receipt_growth_digest(root_id, meta, stat, start)
            if receipt_digest is not None and digest != receipt_digest:
                raise WriterProjectionError("journal tail digest mismatches receipt")
            updates = {
                "offset": str(expected), "boundary": _boundary(journal, expected),
                "mtime_ns": str(stat.st_mtime_ns), "ctime_ns": str(stat.st_ctime_ns),
                "digest": digest,
                "rows": str(int(meta.get("rows", 0)) + rows),
                "scanned": str(scanned),
            }
            conn.executemany(
                "UPDATE meta SET value=? WHERE key=?",
                ((value, key) for key, value in updates.items()),
            )
            conn.commit()
            _write_ack(journal, expected, digest)
            perf.record_count("hydrate.writer_projection.rows", rows)
        except WriterProjectionError:
            if conn is not None:
                conn.rollback()
            raise
        except (OSError, sqlite3.Error, KeyError, ValueError) as exc:
            if conn is not None:
                conn.rollback()
            perf.record_count("hydrate.writer_projection.failed", 1)
            logger.error("writer hydration projection failed for %s", root_id, exc_info=True)
            raise WriterProjectionError("writer hydration projection failed") from exc
        finally:
            if conn is not None:
                conn.close()


def _receipt_growth_digest(
    root_id: str, meta: dict[str, str], stat: os.stat_result, offset: int,
) -> str | None:
    try:
        receipt = json.loads(
            (_safe_root_dir(root_id) / "hydration_append_receipt.json").read_text(
                encoding="utf-8",
            )
        )
        if (
            int(receipt.get("dev", -1)) == stat.st_dev
            and int(receipt.get("ino", -1)) == stat.st_ino
            and int(receipt.get("predecessor_size", -1)) == offset
            and receipt.get("predecessor_digest") == meta["digest"]
            and int(receipt.get("size", -1)) == stat.st_size
            and isinstance(receipt.get("digest"), str)
        ):
            return str(receipt["digest"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _growth_is_authoritative(root_id: str, meta: dict[str, str], stat: os.stat_result, offset: int) -> bool:
    with _receipts_lock:
        receipt = _append_receipts.get(root_id)
        in_memory = (
            receipt is not None
            and receipt[:2] == (stat.st_dev, stat.st_ino)
            and receipt[2] == offset
            and receipt[3] >= stat.st_size
            and receipt[4] == meta["digest"]
        )
    if in_memory:
        return True
    if _receipt_growth_digest(root_id, meta, stat, offset) is not None:
        perf.record_count("hydrate.append_authority.receipt", 1)
        return True
    try:
        payload = json.loads((ba_home() / "sessions" / root_id / "event_chain.json").read_text(
            encoding="utf-8",
        ))
        authority = payload.get("append_authority")
        identity = tuple(int(value) for value in payload.get("identity", ()))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    current_identity = (
        int(stat.st_dev), int(stat.st_ino), int(stat.st_ctime_ns),
        int(stat.st_mtime_ns), int(stat.st_size),
    )
    valid = (
        isinstance(authority, dict)
        and identity == current_identity
        and int(authority.get("predecessor_size", -1)) == offset
        and authority.get("predecessor_digest") == meta["digest"]
        and int(authority.get("size", -1)) == stat.st_size
        and authority.get("digest") == payload.get("digest")
    )
    if valid:
        perf.record_count("hydrate.append_authority.durable", 1)
    return valid


def _acknowledge_growth(
    root_id: str, committed: int, digest: str, stat: os.stat_result,
) -> None:
    with _receipts_lock:
        receipt = _append_receipts.get(root_id)
        if receipt is None or receipt[2] > committed or receipt[3] < committed:
            return
        if receipt[3] == committed:
            _append_receipts.pop(root_id, None)
        else:
            _append_receipts[root_id] = (
                receipt[0], receipt[1], committed, receipt[3], digest, receipt[5],
            )


def _valid_append(root_id: str, meta: dict[str, str], journal: Path) -> bool:
    if int(meta.get("schema", -1)) != SCHEMA:
        return False
    stat = journal.stat()
    offset = int(meta["offset"])
    base_valid = (
        int(meta["dev"]) == stat.st_dev
        and int(meta["ino"]) == stat.st_ino
        and stat.st_size >= offset
        and meta["boundary"] == _boundary(journal, offset)
    )
    if not base_valid:
        return False
    if stat.st_size > offset:
        return _growth_is_authoritative(root_id, meta, stat, offset)
    return int(meta["mtime_ns"]) == stat.st_mtime_ns and int(meta["ctime_ns"]) == stat.st_ctime_ns


def _publish_cold(journal: Path, target: Path) -> None:
    if _shutdown.is_set():
        raise RuntimeError("hydration index store is shutting down")
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        root_id = journal.parent.name
        try:
            with journal_guard(root_id, journal):
                captured = _journal_identity(journal)
                snapshot = (captured, _boundary(journal, captured[2]))
        except OSError as exc:
            raise RuntimeError("hydration journal unavailable") from exc
        with _pool_lock:
            if _shutdown.is_set():
                raise RuntimeError("hydration index store is shutting down")
            build = _builds.get(target)
            if build is None:
                temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                future = _ensure_pool_locked().submit(
                    _cold_build, str(journal), str(temp), snapshot,
                )
                build = (future, temp, snapshot)
                _builds[target] = build
                perf.record_count("hydrate.worker.submitted", 1)
            else:
                perf.record_count("hydrate.worker.coalesced", 1)
        future, temp, snapshot = build
        captured, captured_boundary = snapshot
        try:
            future.result(timeout=BUILD_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            _discard_pool()
            raise RuntimeError("hydration index build timed out") from exc
        except BrokenProcessPool:
            _discard_pool()
            if attempt == 0:
                perf.record_count("hydrate.worker.crash_replaced", 1)
                continue
            raise RuntimeError("hydration index worker pool crashed after replacement")
        except BaseException as exc:
            with _pool_lock:
                if _builds.get(target) == build:
                    _builds.pop(target, None)
            temp.unlink(missing_ok=True)
            if attempt == 0:
                perf.record_count("hydrate.worker.snapshot_retry", 1)
                continue
            raise RuntimeError("hydration index worker build failed") from exc
        with journal_guard(root_id, journal):
            current = _journal_identity(journal)
            prefix_valid = current == captured
            if (
                not prefix_valid
                and current[:2] == captured[:2]
                and current[2] > captured[2]
                and _boundary(journal, captured[2]) == captured_boundary
            ):
                try:
                    with sqlite3.connect(temp) as temp_conn:
                        built_meta = _meta(temp_conn)
                    prefix_valid = _growth_is_authoritative(
                        root_id, built_meta, journal.stat(), captured[2],
                    )
                except (OSError, sqlite3.Error, KeyError, ValueError):
                    prefix_valid = False
            with _pool_lock:
                if _shutdown.is_set():
                    temp.unlink(missing_ok=True)
                    raise RuntimeError("hydration index build cancelled by shutdown")
                if _builds.get(target) == build:
                    _builds.pop(target, None)
                    if prefix_valid:
                        os.replace(temp, target)
                    else:
                        temp.unlink(missing_ok=True)
            if prefix_valid:
                return
        perf.record_count("hydrate.worker.snapshot_retry", 1)


def _ensure_pool_locked() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = _new_pool()
        perf.record_count("hydrate.worker.pool_started", 1)
    return _pool


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
            max_workers=_WORKER_COUNT,
            mp_context=multiprocessing.get_context("spawn"),
    )


def _discard_pool() -> None:
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
        stale_builds = tuple(_builds.values())
        _builds.clear()
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)
    for _, temp, _ in stale_builds:
        temp.unlink(missing_ok=True)


def set_generation(generation: object) -> None:
    """Recycle workers when runtime configuration/extension code changes."""
    global _pool_generation
    with _pool_lock:
        if generation == _pool_generation:
            return
        _pool_generation = generation
    _discard_pool()


def apply_runtime_generation() -> None:
    set_generation((SCHEMA, str(ba_home().resolve())))


def shutdown() -> None:
    _shutdown.set()
    _discard_pool()
    with _pool_lock:
        builds = tuple(_builds.values())
        _builds.clear()
    for _, temp, _ in builds:
        temp.unlink(missing_ok=True)


def invalidate(root_id: str, journal: Path | None = None) -> None:
    """Discard a projection when the journal writer detects non-append mutation."""
    with journal_guard(root_id, journal):
        with _receipts_lock:
            _append_receipts.pop(root_id, None)
        _db_path(root_id).unlink(missing_ok=True)
        _legacy_db_path(root_id).unlink(missing_ok=True)


def _load(
    root_id: str,
    journal: Path,
    base_offsets: dict[str, tuple[int, ...]] | None = None,
    base_checkpoint: int = 0,
    *,
    cold: bool = False,
    started: float | None = None,
) -> tuple[dict[str, tuple[int, ...]], dict[str, int]]:
    apply_runtime_generation()
    target = _db_path(root_id)
    started = time.perf_counter() if started is None else started
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(target)
        meta = _meta(conn)
        if not _valid_append(root_id, meta, journal):
            conn.close()
            conn = None
            raise ValueError("stale projection")
    except (OSError, sqlite3.Error, KeyError, ValueError):
        if conn is not None:
            conn.close()
        raise _StaleProjection
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _meta(conn)
        if not _valid_append(root_id, meta, journal):
            conn.rollback()
            conn.close()
            conn = None
            raise _StaleProjection
        start = int(meta["offset"])
        current_checkpoint = start
        scanned = int(meta.get("scanned", 0)) if cold else 0
        if journal.stat().st_size > start:
            committed, scanned, _, digest = _scan(
                conn, journal, start, meta["digest"],
            )
            stat = journal.stat()
            receipt_digest = _receipt_growth_digest(root_id, meta, stat, start)
            if receipt_digest is not None and digest != receipt_digest:
                raise _StaleProjection
            conn.execute("UPDATE meta SET value=? WHERE key='offset'", (str(committed),))
            conn.execute("UPDATE meta SET value=? WHERE key='boundary'", (_boundary(journal, committed),))
            conn.execute("UPDATE meta SET value=? WHERE key='scanned'", (str(scanned),))
            conn.execute("UPDATE meta SET value=? WHERE key='mtime_ns'", (str(stat.st_mtime_ns),))
            conn.execute("UPDATE meta SET value=? WHERE key='ctime_ns'", (str(stat.st_ctime_ns),))
            conn.execute("UPDATE meta SET value=? WHERE key='digest'", (digest,))
            current_checkpoint = committed
        else:
            digest = meta["digest"]
        conn.commit()
        _acknowledge_growth(root_id, current_checkpoint, digest, journal.stat())
        _write_ack(journal, current_checkpoint, digest)
        can_merge = not cold and base_offsets is not None and base_checkpoint <= int(meta["offset"])
        offsets = {sid: list(values) for sid, values in (base_offsets or {}).items()} if can_merge else {}
        query = "SELECT sid, offset FROM offsets WHERE offset >= ? ORDER BY offset" if can_merge else "SELECT sid, offset FROM offsets ORDER BY offset"
        args = (base_checkpoint,) if can_merge else ()
        for sid, offset in conn.execute(query, args):
            offsets.setdefault(sid, []).append(offset)
        metrics = {
            "cold": int(cold), "scanned_bytes": scanned,
            "rows": sum(map(len, offsets.values())),
            "checkpoint": current_checkpoint,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
        return {sid: tuple(values) for sid, values in offsets.items()}, metrics
    except BaseException:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


def load(
    root_id: str,
    journal: Path,
    base_offsets: dict[str, tuple[int, ...]] | None = None,
    base_checkpoint: int = 0,
) -> tuple[dict[str, tuple[int, ...]], dict[str, int]]:
    started = time.perf_counter()
    cold = False
    while True:
        with journal_guard(root_id, journal):
            _install_legacy_projection(root_id, journal)
        try:
            with journal_guard(root_id, journal):
                return _load(
                    root_id, journal, base_offsets, base_checkpoint,
                    cold=cold, started=started,
                )
        except _StaleProjection:
            cold = True
        _publish_cold(journal, _db_path(root_id))


def reconcile_cursor(root_id: str, journal: Path) -> int:
    """Return the durable render-reconcile high-water after validating the index."""
    started = time.perf_counter()
    load(root_id, journal)
    with journal_guard(root_id, journal):
        with sqlite3.connect(_db_path(root_id)) as conn:
            cursor = int(_meta(conn).get("reconciled_seq", 0))
    perf.record("hydrate.reconcile_cursor.read", (time.perf_counter() - started) * 1000)
    return cursor


def mark_reconciled(root_id: str, journal: Path, seq: int) -> None:
    """Durably advance the reconcile high-water on the validated append projection."""
    started = time.perf_counter()
    load(root_id, journal)
    with journal_guard(root_id, journal):
        with sqlite3.connect(_db_path(root_id)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = int(_meta(conn).get("reconciled_seq", 0))
            if seq > current:
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='reconciled_seq'",
                    (str(seq),),
                )
            conn.commit()
    perf.record("hydrate.reconcile_cursor.write", (time.perf_counter() - started) * 1000)
