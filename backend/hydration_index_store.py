import hashlib
import json
import multiprocessing
import os
import sqlite3
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor, TimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
import threading

import perf
from paths import ba_home


SCHEMA = 1
BOUNDARY_BYTES = 4096
BUILD_TIMEOUT_SECONDS = 300
_WORKER_COUNT = 2
_pool_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_generation: object = None
_builds: dict[Path, tuple[Future, Path]] = {}
_shutdown = threading.Event()
_receipts_lock = threading.Lock()
_append_receipts: dict[str, tuple[int, int, int, int, int, int]] = {}


def _db_path(root_id: str) -> Path:
    name = hashlib.sha256(root_id.encode("utf-8")).hexdigest() + ".sqlite3"
    return ba_home() / "cache" / "render-hydration" / name


def _boundary(path: Path, offset: int) -> str:
    start = max(0, offset - BOUNDARY_BYTES)
    with path.open("rb") as file:
        file.seek(start)
        return hashlib.sha256(file.read(offset - start)).hexdigest()


def _create(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE offsets(sid TEXT NOT NULL, offset INTEGER NOT NULL);"
        "CREATE INDEX offsets_sid ON offsets(sid, offset);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    return conn


def _scan(conn: sqlite3.Connection, journal: Path, start: int) -> tuple[int, int, int]:
    rows: list[tuple[str, int]] = []
    inserted = 0
    scanned = 0
    committed = start
    with journal.open("rb") as file:
        file.seek(start)
        while True:
            offset = file.tell()
            line = file.readline()
            if not line:
                break
            scanned += len(line)
            if not line.endswith(b"\n"):
                break
            committed = file.tell()
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            sid = row.get("sid") if isinstance(row, dict) else None
            if isinstance(sid, str) and sid:
                rows.append((sid, offset))
            if len(rows) >= 4096:
                inserted += len(rows)
                conn.executemany("INSERT INTO offsets VALUES (?, ?)", rows)
                rows.clear()
    if rows:
        inserted += len(rows)
        conn.executemany("INSERT INTO offsets VALUES (?, ?)", rows)
    return committed, scanned, inserted


def _cold_build(journal_raw: str, output_raw: str) -> None:
    journal = Path(journal_raw)
    output = Path(output_raw)
    stat = journal.stat()
    conn = _create(output)
    try:
        committed, scanned, rows = _scan(conn, journal, 0)
        end = journal.stat()
        if (stat.st_dev, stat.st_ino) != (end.st_dev, end.st_ino) or end.st_size < committed:
            raise RuntimeError("journal changed during hydration index build")
        values = {
            "schema": str(SCHEMA), "dev": str(end.st_dev), "ino": str(end.st_ino),
            "offset": str(committed), "boundary": _boundary(journal, committed),
            "mtime_ns": str(end.st_mtime_ns), "ctime_ns": str(end.st_ctime_ns),
            "scanned": str(scanned), "rows": str(rows),
        }
        conn.executemany("INSERT INTO meta VALUES (?, ?)", values.items())
        conn.commit()
    finally:
        conn.close()


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta"))


def note_authoritative_append(
    root_id: str, journal: Path, start: int, end: int,
    before_mtime_ns: int | None = None, before_ctime_ns: int | None = None,
) -> None:
    stat = journal.stat()
    with _receipts_lock:
        prior = _append_receipts.get(root_id)
        chained = prior is not None and prior[:2] == (stat.st_dev, stat.st_ino) and prior[3] == start
        origin = prior[2] if chained else start
        origin_mtime = prior[4] if chained else int(before_mtime_ns if before_mtime_ns is not None else stat.st_mtime_ns)
        origin_ctime = prior[5] if chained else int(before_ctime_ns if before_ctime_ns is not None else stat.st_ctime_ns)
        _append_receipts[root_id] = (stat.st_dev, stat.st_ino, origin, end, origin_mtime, origin_ctime)


def _growth_is_authoritative(root_id: str, meta: dict[str, str], stat: os.stat_result, offset: int) -> bool:
    with _receipts_lock:
        receipt = _append_receipts.get(root_id)
        return (
            receipt is not None
            and receipt[:2] == (stat.st_dev, stat.st_ino)
            and receipt[2] <= offset
            and receipt[3] >= stat.st_size
            and receipt[4] == int(meta["mtime_ns"])
            and receipt[5] == int(meta["ctime_ns"])
        )


def _acknowledge_growth(root_id: str, committed: int, stat: os.stat_result) -> None:
    with _receipts_lock:
        receipt = _append_receipts.get(root_id)
        if receipt is None or receipt[2] > committed or receipt[3] < committed:
            return
        if receipt[3] == committed:
            _append_receipts.pop(root_id, None)
        else:
            _append_receipts[root_id] = (
                receipt[0], receipt[1], committed, receipt[3], stat.st_mtime_ns, stat.st_ctime_ns,
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
        with _pool_lock:
            if _shutdown.is_set():
                raise RuntimeError("hydration index store is shutting down")
            build = _builds.get(target)
            if build is None:
                temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                future = _ensure_pool_locked().submit(_cold_build, str(journal), str(temp))
                build = (future, temp)
                _builds[target] = build
                perf.record_count("hydrate.worker.submitted", 1)
            else:
                perf.record_count("hydrate.worker.coalesced", 1)
        future, temp = build
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
            raise RuntimeError("hydration index worker build failed") from exc
        with _pool_lock:
            if _shutdown.is_set():
                temp.unlink(missing_ok=True)
                raise RuntimeError("hydration index build cancelled by shutdown")
            if _builds.get(target) == build:
                os.replace(temp, target)
                _builds.pop(target, None)
        return


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
    for _, temp in stale_builds:
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
    for _, temp in builds:
        temp.unlink(missing_ok=True)


def load(
    root_id: str,
    journal: Path,
    base_offsets: dict[str, tuple[int, ...]] | None = None,
    base_checkpoint: int = 0,
) -> tuple[dict[str, tuple[int, ...]], dict[str, int]]:
    apply_runtime_generation()
    target = _db_path(root_id)
    started = time.perf_counter()
    cold = False
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
        cold = True
        _publish_cold(journal, target)
        conn = sqlite3.connect(target)
        meta = _meta(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _meta(conn)
        if not _valid_append(root_id, meta, journal):
            conn.rollback()
            conn.close()
            conn = None
            cold = True
            _publish_cold(journal, target)
            conn = sqlite3.connect(target)
            conn.execute("BEGIN IMMEDIATE")
            meta = _meta(conn)
        start = int(meta["offset"])
        current_checkpoint = start
        scanned = int(meta.get("scanned", 0)) if cold else 0
        if journal.stat().st_size > start:
            committed, scanned, _ = _scan(conn, journal, start)
            stat = journal.stat()
            conn.execute("UPDATE meta SET value=? WHERE key='offset'", (str(committed),))
            conn.execute("UPDATE meta SET value=? WHERE key='boundary'", (_boundary(journal, committed),))
            conn.execute("UPDATE meta SET value=? WHERE key='scanned'", (str(scanned),))
            conn.execute("UPDATE meta SET value=? WHERE key='mtime_ns'", (str(stat.st_mtime_ns),))
            conn.execute("UPDATE meta SET value=? WHERE key='ctime_ns'", (str(stat.st_ctime_ns),))
            current_checkpoint = committed
        conn.commit()
        _acknowledge_growth(root_id, current_checkpoint, journal.stat())
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
