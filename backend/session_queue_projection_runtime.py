from __future__ import annotations

import copy
import json
import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Optional

import perf
from paths import assert_state_root_safe


Fingerprint = dict[str, list[int]]
Record = dict[str, Any]
Mutation = tuple[int, Optional[Record]]
ProjectRoot = Callable[[dict[str, Any]], Iterable[Record]]

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StorageContext:
    identity: Path
    database_path: Path
    session_files: Callable[[], Iterable[Path]]
    enumerator_contract: str

    @classmethod
    def create(
        cls,
        identity: Path,
        *,
        session_files: Callable[[], Iterable[Path]],
        enumerator_contract: str,
        database_path: Optional[Path] = None,
    ) -> StorageContext:
        canonical = Path(identity)
        if not canonical.is_absolute():
            raise ValueError("session storage identity must be absolute")
        canonical = canonical.resolve()
        if canonical.name != "sessions":
            raise ValueError("session storage identity must be a sessions directory")
        assert_state_root_safe(canonical.parent)
        if not callable(session_files):
            raise TypeError("session_files must be callable")
        contract = enumerator_contract.strip()
        if not contract:
            raise ValueError("enumerator_contract is required")
        db_path = (
            canonical.parent / "queue_recovery_projection" / "projection.sqlite3"
            if database_path is None
            else Path(database_path)
        )
        if not db_path.is_absolute():
            raise ValueError("queue projection database path must be absolute")
        db_path = db_path.resolve()
        try:
            db_path.relative_to(canonical.parent)
        except ValueError as exc:
            raise ValueError(
                "queue projection database must stay inside the storage home"
            ) from exc
        return cls(canonical, db_path, session_files, contract)

    def equivalent_to(self, other: StorageContext) -> bool:
        return (
            self.identity == other.identity
            and self.database_path == other.database_path
            and self.enumerator_contract == other.enumerator_contract
        )

    def files(self) -> tuple[Path, ...]:
        home = self.identity.parent
        selected: list[Path] = []
        for raw_path in self.session_files():
            path = Path(raw_path)
            if not path.is_absolute():
                raise ValueError("session-file enumerator returned a relative path")
            canonical = path.resolve()
            try:
                canonical.relative_to(home)
            except ValueError as exc:
                raise ValueError(
                    "session-file enumerator escaped the storage home"
                ) from exc
            selected.append(canonical)
        return tuple(sorted(dict.fromkeys(selected), key=lambda item: item.as_posix()))

    def fingerprint(self, files: Optional[Iterable[Path]] = None) -> Fingerprint:
        home = self.identity.parent
        result: Fingerprint = {}
        for path in self.files() if files is None else files:
            try:
                stat = path.stat()
            except OSError:
                continue
            result[path.relative_to(home).as_posix()] = [
                int(stat.st_dev),
                int(stat.st_ino),
                int(stat.st_mtime_ns),
                int(stat.st_ctime_ns),
                int(stat.st_size),
            ]
        return result


def _decode_database(
    path_raw: str,
) -> tuple[dict[str, Record], dict[str, int], int, int, int, int]:
    records: dict[str, Record] = {}
    tombstones: dict[str, int] = {}
    durable_sequence = 0
    bytes_read = 0
    rows = 0
    rebuild_sequence = 0
    connection = sqlite3.connect(f"file:{path_raw}?mode=ro", uri=True)
    try:
        for sid, payload, sequence in connection.execute(
            "SELECT id, payload, sequence FROM records"
        ):
            rows += 1
            bytes_read += len(payload)
            record = json.loads(payload)
            if isinstance(record, dict) and record.get("id") == sid:
                records[sid] = record
                durable_sequence = max(durable_sequence, int(sequence))
        for sid, sequence in connection.execute(
            "SELECT id, sequence FROM tombstones"
        ):
            tombstones[sid] = int(sequence)
            durable_sequence = max(durable_sequence, int(sequence))
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='sequence'"
        ).fetchone()
        if row is not None:
            durable_sequence = max(durable_sequence, int(row[0]))
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='rebuild_sequence'"
        ).fetchone()
        if row is not None:
            rebuild_sequence = int(row[0])
            durable_sequence = max(durable_sequence, rebuild_sequence)
    finally:
        connection.close()
    return (
        records,
        tombstones,
        durable_sequence,
        rebuild_sequence,
        bytes_read,
        rows,
    )


class ProjectionRuntime:
    def __init__(self, context: StorageContext, epoch: int) -> None:
        self.context = context
        self._epoch = epoch
        self._lifecycle = threading.Condition(threading.Lock())
        self._state: Literal["open", "closing"] = "open"
        self._active_operations = 0

        self._data_cv = threading.Condition(threading.Lock())
        self._loaded = False
        self._loading = False
        self._records: dict[str, Record] = {}
        self._tombstones: dict[str, int] = {}
        self._sequence = 0
        self._durable_sequence = 0
        self._persisted_sequence = 0
        self._rebuild_sequence = 0
        self._journal: dict[str, Mutation] = {}
        self._mutation_log: dict[str, Mutation] = {}

        self._loader_lock = threading.Lock()
        self._load_executor: Optional[ProcessPoolExecutor] = None
        self._load_future: Optional[Future] = None
        self._loader_shutdown_thread: Optional[threading.Thread] = None
        self._loader_shutdown_failure: Optional[BaseException] = None

        self._write_cv = threading.Condition(threading.Lock())
        self._pending_writes: dict[str, Mutation] = {}
        self._pending_rebuild: Optional[tuple[int, dict[str, Record], Fingerprint]] = None
        self._active_writes = 0
        self._write_failure: Optional[BaseException] = None
        self._writer: Optional[threading.Thread] = None
        self._writer_stop = False

    @property
    def state(self) -> str:
        with self._lifecycle:
            return self._state

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def failure(self) -> Optional[BaseException]:
        with self._write_cv:
            return self._write_failure

    @contextmanager
    def operation(self) -> Iterator[ProjectionRuntime]:
        with self._lifecycle:
            if self._state != "open":
                raise RuntimeError("queue projection runtime is closing")
            epoch = self.epoch
            self._active_operations += 1
        try:
            yield self
            if epoch != self.epoch:
                raise RuntimeError("queue projection runtime epoch changed")
        finally:
            with self._lifecycle:
                self._active_operations -= 1
                self._lifecycle.notify_all()

    def _connect(self) -> sqlite3.Connection:
        path = self.context.database_path
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
            CREATE TABLE IF NOT EXISTS tombstones (
                id TEXT PRIMARY KEY,
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
            raise RuntimeError(
                "unsupported queue projection schema; wipe queue_recovery_projection"
            )
        return connection

    def _loader_executor_locked(self) -> ProcessPoolExecutor:
        if self._load_executor is None:
            self._load_executor = ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
            )
            perf.record_count("queue_projection.load.owner_started", 1)
        return self._load_executor

    def _load_candidate(
        self,
        epoch: int,
    ) -> tuple[dict[str, Record], dict[str, int], int, int]:
        started = time.perf_counter()
        self._connect().close()
        with self._loader_lock:
            future = self._load_future
            if future is None:
                queued = time.perf_counter()
                future = self._loader_executor_locked().submit(
                    _decode_database,
                    str(self.context.database_path),
                )
                self._load_future = future
                perf.record(
                    "queue_projection.load.owner_queue",
                    (time.perf_counter() - queued) * 1000.0,
                )
            else:
                perf.record_count("queue_projection.load.owner_coalesced", 1)
        held = time.perf_counter()
        failed = False
        try:
            (
                records,
                tombstones,
                durable,
                rebuild_sequence,
                bytes_read,
                rows,
            ) = future.result()
        except BaseException:
            failed = True
            raise
        finally:
            perf.record(
                "queue_projection.load.owner_hold",
                (time.perf_counter() - held) * 1000.0,
            )
            with self._loader_lock:
                if self._load_future is future:
                    self._load_future = None
                executor = self._load_executor if failed else None
                if failed:
                    self._load_executor = None
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
        if epoch != self.epoch:
            raise RuntimeError("stale queue projection loader result")
        perf.record_count("queue_projection.load.files", 1)
        perf.record_count("queue_projection.load.rows", rows)
        perf.record_count("queue_projection.load.bytes", bytes_read)
        perf.record(
            "queue_projection.load.build",
            (time.perf_counter() - started) * 1000.0,
        )
        return records, tombstones, durable, rebuild_sequence

    def _ensure_loaded(self) -> None:
        epoch = self.epoch
        started = time.perf_counter()
        with self._data_cv:
            while self._loading and not self._loaded:
                self._data_cv.wait()
            perf.record(
                "queue_projection.load.wait",
                (time.perf_counter() - started) * 1000.0,
            )
            if self._loaded:
                return
            self._loading = True
        try:
            records, tombstones, durable, rebuild_sequence = self._load_candidate(epoch)
        except BaseException:
            with self._data_cv:
                self._loading = False
                self._data_cv.notify_all()
            raise
        with self._data_cv:
            if epoch != self.epoch:
                self._loading = False
                self._data_cv.notify_all()
                raise RuntimeError("stale queue projection loader result")
            for sid, (sequence, record) in self._mutation_log.items():
                if record is None:
                    records.pop(sid, None)
                    tombstones[sid] = sequence
                else:
                    records[sid] = copy.deepcopy(record)
                    tombstones.pop(sid, None)
            self._records = records
            self._tombstones = tombstones
            self._durable_sequence = max(self._durable_sequence, durable)
            self._persisted_sequence = max(self._persisted_sequence, durable)
            self._rebuild_sequence = max(
                self._rebuild_sequence,
                rebuild_sequence,
            )
            self._sequence = max(self._sequence, durable)
            self._loaded = True
            self._loading = False
            self._data_cv.notify_all()

    def _ensure_writer_locked(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._writer_stop = False
        self._writer = threading.Thread(
            target=self._writer_loop,
            name=f"queue-projection-writer-{self.epoch}",
            daemon=True,
        )
        self._writer.start()

    def _enqueue(self, sid: str, sequence: int, record: Optional[Record]) -> None:
        with self._write_cv:
            current = self._pending_writes.get(sid)
            if current is None or sequence > current[0]:
                self._pending_writes[sid] = (sequence, copy.deepcopy(record))
            self._ensure_writer_locked()
            self._write_cv.notify_all()

    @staticmethod
    def _metadata_int(
        connection: sqlite3.Connection,
        key: str,
    ) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key=?",
            (key,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    @classmethod
    def _disk_sequence(cls, connection: sqlite3.Connection, sid: str) -> int:
        record = connection.execute(
            "SELECT sequence FROM records WHERE id=?",
            (sid,),
        ).fetchone()
        tombstone = connection.execute(
            "SELECT sequence FROM tombstones WHERE id=?",
            (sid,),
        ).fetchone()
        return max(
            int(record[0]) if record is not None else -1,
            int(tombstone[0]) if tombstone is not None else -1,
            cls._metadata_int(connection, "rebuild_sequence"),
        )

    def _write_batch(self, batch: dict[str, Mutation]) -> int:
        if not batch:
            return 0
        started = time.perf_counter()
        inserted = updated = deleted = fenced = 0
        with closing(self._connect()) as connection:
            waited = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            perf.record(
                "queue_projection.transaction.wait",
                (time.perf_counter() - waited) * 1000.0,
            )
            rebuild_sequence = self._metadata_int(
                connection,
                "rebuild_sequence",
            )
            for sid, (sequence, record) in batch.items():
                if max(
                    self._disk_sequence(connection, sid),
                    rebuild_sequence,
                ) >= sequence:
                    fenced += 1
                    continue
                if record is None:
                    deleted += connection.execute(
                        "DELETE FROM records WHERE id=?",
                        (sid,),
                    ).rowcount
                    connection.execute(
                        "INSERT OR REPLACE INTO tombstones(id, sequence) VALUES(?, ?)",
                        (sid, sequence),
                    )
                    continue
                payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
                existing = connection.execute(
                    "SELECT payload FROM records WHERE id=?",
                    (sid,),
                ).fetchone()
                inserted += int(existing is None)
                updated += int(existing is not None and existing[0] != payload)
                connection.execute("DELETE FROM tombstones WHERE id=?", (sid,))
                connection.execute(
                    "INSERT OR REPLACE INTO records(id, payload, sequence) VALUES(?, ?, ?)",
                    (sid, payload, sequence),
                )
            high_water = max(sequence for sequence, _record in batch.values())
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('sequence', ?)",
                (
                    str(
                        max(
                            high_water,
                            self._metadata_int(connection, "sequence"),
                            rebuild_sequence,
                        )
                    ),
                ),
            )
            connection.commit()
        with self._data_cv:
            self._durable_sequence = max(self._durable_sequence, high_water)
            for sid, (sequence, _record) in batch.items():
                current = self._journal.get(sid)
                if current is not None and current[0] <= sequence:
                    self._journal.pop(sid, None)
            if not self._journal:
                self._persisted_sequence = max(self._persisted_sequence, high_water)
            self._data_cv.notify_all()
        perf.record_count("queue_projection.transaction.inserted", inserted)
        perf.record_count("queue_projection.transaction.updated", updated)
        perf.record_count("queue_projection.transaction.deleted", deleted)
        perf.record_count("queue_projection.transaction.fenced", fenced)
        perf.record(
            "queue_projection.transaction.write",
            (time.perf_counter() - started) * 1000.0,
        )
        return high_water

    def _write_rebuild(
        self,
        fence: int,
        records: dict[str, Record],
        fingerprint: Fingerprint,
    ) -> int:
        started = time.perf_counter()
        applied = False
        persisted_rebuild_sequence = 0
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted_rebuild_sequence = self._metadata_int(
                connection,
                "rebuild_sequence",
            )
            if fence >= persisted_rebuild_sequence:
                connection.execute(
                    "DELETE FROM records WHERE sequence <= ?",
                    (fence,),
                )
                connection.execute(
                    "DELETE FROM tombstones WHERE sequence <= ?",
                    (fence,),
                )
                for sid, record in records.items():
                    if self._disk_sequence(connection, sid) > fence:
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO records(id, payload, sequence) "
                        "VALUES(?, ?, ?)",
                        (
                            sid,
                            json.dumps(
                                record,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            fence,
                        ),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) "
                    "VALUES('sequence', ?)",
                    (
                        str(
                            max(
                                fence,
                                self._metadata_int(connection, "sequence"),
                            )
                        ),
                    ),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) "
                    "VALUES('rebuild_sequence', ?)",
                    (str(fence),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) "
                    "VALUES('fingerprint', ?)",
                    (
                        json.dumps(
                            fingerprint,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                applied = True
            connection.commit()
        with self._data_cv:
            acknowledged = fence if applied else persisted_rebuild_sequence
            self._durable_sequence = max(self._durable_sequence, acknowledged)
            self._rebuild_sequence = max(
                self._rebuild_sequence,
                acknowledged,
            )
            if applied:
                for sid, (sequence, _record) in tuple(
                    self._mutation_log.items()
                ):
                    if sequence <= fence:
                        self._mutation_log.pop(sid, None)
            self._data_cv.notify_all()
        perf.record_count(
            "queue_projection.rebuild.rows",
            len(records) if applied else 0,
        )
        if not applied:
            perf.record_count("queue_projection.rebuild.fenced", 1)
        perf.record(
            "queue_projection.rebuild.transaction",
            (time.perf_counter() - started) * 1000.0,
        )
        return fence if applied else persisted_rebuild_sequence

    def _writer_loop(self) -> None:
        while True:
            with self._write_cv:
                while (
                    not self._writer_stop
                    and (
                        self._write_failure is not None
                        or (
                            not self._pending_writes
                            and self._pending_rebuild is None
                        )
                    )
                ):
                    self._write_cv.wait()
                if (
                    self._writer_stop
                    and not self._pending_writes
                    and self._pending_rebuild is None
                    and self._active_writes == 0
                ):
                    return
                rebuild = self._pending_rebuild
                self._pending_rebuild = None
                batch = dict(self._pending_writes)
                self._pending_writes.clear()
                self._active_writes += 1
            try:
                if rebuild is not None:
                    self._write_rebuild(*rebuild)
                self._write_batch(batch)
            except BaseException as exc:
                with self._write_cv:
                    if rebuild is not None and self._pending_rebuild is None:
                        self._pending_rebuild = rebuild
                    for sid, mutation in batch.items():
                        current = self._pending_writes.get(sid)
                        if current is None or mutation[0] > current[0]:
                            self._pending_writes[sid] = mutation
                    self._write_failure = exc
            finally:
                with self._write_cv:
                    self._active_writes -= 1
                    self._write_cv.notify_all()

    def retry_failed_writes(self) -> None:
        with self.operation():
            self._retry_failed_without_lease()

    def _retry_failed_without_lease(self) -> None:
        with self._write_cv:
            self._write_failure = None
            if self._pending_writes or self._pending_rebuild is not None:
                self._ensure_writer_locked()
            self._write_cv.notify_all()

    def _wait_durable(self, sequence: int) -> None:
        started = time.perf_counter()
        with self._data_cv:
            while self._durable_sequence < sequence:
                with self._write_cv:
                    failure = self._write_failure
                if failure is not None:
                    raise RuntimeError(
                        "queue projection durability failed"
                    ) from failure
                self._data_cv.wait()
        perf.record(
            "queue_projection.writer.durable_wait",
            (time.perf_counter() - started) * 1000.0,
        )

    def _mutate(self, sid: str, record: Optional[Record]) -> tuple[int, bool]:
        self._ensure_loaded()
        owned = copy.deepcopy(record)
        with self._data_cv:
            if (
                record is None
                and sid not in self._records
                and sid in self._tombstones
            ) or self._records.get(sid) == owned:
                current = self._journal.get(sid)
                return (
                    current[0] if current is not None else self._durable_sequence,
                    False,
                )
            self._sequence += 1
            sequence = self._sequence
            if owned is None:
                self._records.pop(sid, None)
                self._tombstones[sid] = sequence
            else:
                self._records[sid] = owned
                self._tombstones.pop(sid, None)
            self._journal[sid] = (sequence, owned)
            self._mutation_log[sid] = (sequence, owned)
            perf.record_count("queue_projection.journal.depth", len(self._journal))
            perf.record_count("queue_projection.journal.high_water", sequence)
            return sequence, True

    def upsert(self, record: Record, *, durable: bool = True) -> None:
        sid = record.get("id")
        if not isinstance(sid, str) or not sid:
            return
        with self.operation():
            sequence, changed = self._mutate(sid, record)
            if changed:
                self._enqueue(sid, sequence, record)
            if durable:
                self._wait_durable(sequence)

    def delete(self, session_ids: Iterable[str], *, durable: bool = True) -> None:
        with self.operation():
            sequences: list[int] = []
            for sid in dict.fromkeys(str(value) for value in session_ids if value):
                sequence, changed = self._mutate(sid, None)
                if changed:
                    self._enqueue(sid, sequence, None)
                sequences.append(sequence)
            if durable:
                for sequence in sequences:
                    self._wait_durable(sequence)

    def note_persisted(self, records: Iterable[Record]) -> int:
        with self.operation():
            self._ensure_loaded()
            owned_records = [copy.deepcopy(record) for record in records]
            changed: list[Record] = []
            with self._data_cv:
                self._sequence += 1
                high_water = self._sequence
                for record in owned_records:
                    sid = record.get("id")
                    if not isinstance(sid, str) or not sid:
                        continue
                    if self._records.get(sid) == record:
                        continue
                    self._records[sid] = record
                    self._tombstones.pop(sid, None)
                    self._journal[sid] = (high_water, record)
                    self._mutation_log[sid] = (high_water, record)
                    changed.append(record)
                self._persisted_sequence = high_water
            for record in changed:
                self._enqueue(record["id"], high_water, record)
            return high_water

    def get(self, sid: str) -> Optional[Record]:
        with self.operation():
            self._ensure_loaded()
            with self._data_cv:
                record = self._records.get(sid)
            return copy.deepcopy(record) if record is not None else None

    def get_many(self, session_ids: Iterable[str]) -> dict[str, Record]:
        ids = tuple(sid for sid in session_ids if sid)
        with self.operation():
            self._ensure_loaded()
            started = time.perf_counter()
            with self._data_cv:
                selected = {
                    sid: record
                    for sid in ids
                    if (record := self._records.get(sid)) is not None
                }
            perf.record(
                "queue_projection.get_many.lock_wait",
                (time.perf_counter() - started) * 1000.0,
            )
            return copy.deepcopy(selected)

    def records(
        self,
        predicate: Optional[Callable[[Record], bool]] = None,
    ) -> list[Record]:
        with self.operation():
            self._ensure_loaded()
            with self._data_cv:
                selected = [
                    record
                    for record in self._records.values()
                    if predicate is None or predicate(record)
                ]
            return copy.deepcopy(selected)

    def mark_dirty(self) -> int:
        with self.operation():
            with self._data_cv:
                self._sequence += 1
                return self._sequence

    def certification_generation(self) -> int:
        with self.operation():
            with self._data_cv:
                return self._persisted_sequence

    def projection_is_current(self) -> bool:
        with self.operation():
            try:
                with closing(self._connect()) as connection:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key='fingerprint'"
                    ).fetchone()
            except (OSError, sqlite3.Error, RuntimeError):
                return False
            if row is None:
                return False
            try:
                return json.loads(row[0]) == self.context.fingerprint()
            except json.JSONDecodeError:
                return False

    def _certify_closing(self, fingerprint: Optional[Fingerprint]) -> None:
        with self._lifecycle:
            if self._state != "closing" or self._active_operations:
                raise RuntimeError(
                    "queue projection certification requires quiescent closing"
                )
        with self._write_cv:
            if (
                self._pending_writes
                or self._pending_rebuild is not None
                or self._active_writes
                or self._write_failure is not None
            ):
                raise RuntimeError(
                    "queue projection certification requires durable writes"
                )
        with self._data_cv:
            if self._journal:
                raise RuntimeError(
                    "queue projection certification requires an empty journal"
                )
        current = (
            self.context.fingerprint()
            if fingerprint is None
            else copy.deepcopy(fingerprint)
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) "
                "VALUES('fingerprint', ?)",
                (
                    json.dumps(
                        current,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.commit()

    def rebuild(self, project_root: ProjectRoot) -> int:
        with self.operation():
            self._ensure_loaded()
            with self._data_cv:
                scan_start_sequence = self._sequence
            files = self.context.files()
            rebuilt: dict[str, Record] = {}
            for path in files:
                try:
                    root = json.loads(path.read_bytes())
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(root, dict):
                    continue
                for record in project_root(root):
                    sid = record.get("id")
                    if isinstance(sid, str) and sid:
                        rebuilt[sid] = copy.deepcopy(record)
            fingerprint = self.context.fingerprint(files)
            with self._data_cv:
                fence = self._sequence
                for sid, (sequence, record) in self._mutation_log.items():
                    if sequence <= scan_start_sequence:
                        continue
                    if record is None:
                        rebuilt.pop(sid, None)
                    else:
                        rebuilt[sid] = copy.deepcopy(record)
                self._records = copy.deepcopy(rebuilt)
                self._tombstones = {
                    sid: sequence
                    for sid, (sequence, record) in self._mutation_log.items()
                    if record is None
                }
                self._loaded = True
                self._persisted_sequence = max(self._persisted_sequence, fence)
            with self._write_cv:
                self._pending_rebuild = (fence, copy.deepcopy(rebuilt), fingerprint)
                self._ensure_writer_locked()
                self._write_cv.notify_all()
            return len(rebuilt)

    def ensure_current_or_rebuild(self, project_root: ProjectRoot) -> bool:
        if self.projection_is_current():
            with self.operation():
                with self._data_cv:
                    self._loaded = False
                    self._records.clear()
                    self._tombstones.clear()
                self._ensure_loaded()
            return False
        self.rebuild(project_root)
        return True

    def flush(self, timeout: Optional[float] = None) -> bool:
        with self.operation():
            return self._flush_without_lease(timeout)

    def _flush_without_lease(self, timeout: Optional[float]) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._write_cv:
            while (
                self._pending_writes
                or self._pending_rebuild is not None
                or self._active_writes
            ):
                if self._write_failure is not None:
                    raise RuntimeError(
                        "queue projection durability failed"
                    ) from self._write_failure
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._write_cv.wait(remaining)
            return True

    def _begin_shutdown(self) -> None:
        with self._lifecycle:
            self._state = "closing"
            self._lifecycle.notify_all()

    def _shutdown_loader(self, deadline: Optional[float]) -> None:
        with self._loader_lock:
            future = self._load_future
            if future is not None and not future.done():
                raise RuntimeError("queue projection loader is still active")
            executor = self._load_executor
            thread = self._loader_shutdown_thread
            if executor is None:
                return
            if thread is None:
                self._loader_shutdown_failure = None

                def close() -> None:
                    try:
                        executor.shutdown(wait=True, cancel_futures=True)
                    except BaseException as exc:
                        self._loader_shutdown_failure = exc

                thread = threading.Thread(
                    target=close,
                    name=f"queue-projection-loader-shutdown-{self.epoch}",
                    daemon=True,
                )
                self._loader_shutdown_thread = thread
                thread.start()
        remaining = None if deadline is None else max(
            0.0,
            deadline - time.monotonic(),
        )
        thread.join(remaining)
        if thread.is_alive():
            raise TimeoutError("queue projection loader did not stop")
        with self._loader_lock:
            failure = self._loader_shutdown_failure
            if failure is not None:
                raise RuntimeError(
                    "queue projection loader shutdown failed"
                ) from failure
            if self._load_executor is executor:
                self._load_executor = None
                self._load_future = None
                self._loader_shutdown_thread = None

    def _shutdown(
        self,
        timeout: Optional[float],
        *,
        certify: bool,
        fingerprint: Optional[Fingerprint],
    ) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        self._begin_shutdown()
        with self._lifecycle:
            while self._active_operations:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("queue projection operations did not quiesce")
                self._lifecycle.wait(remaining)

        self._retry_failed_without_lease()
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self._flush_without_lease(remaining):
            raise TimeoutError("queue projection writes did not drain")
        if certify:
            self._certify_closing(fingerprint)

        with self._write_cv:
            self._writer_stop = True
            self._write_cv.notify_all()
            writer = self._writer
        if writer is not None:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            writer.join(remaining)
            if writer.is_alive():
                raise TimeoutError("queue projection writer did not stop")

        self._shutdown_loader(deadline)

        with self._write_cv:
            if (
                self._pending_writes
                or self._pending_rebuild is not None
                or self._active_writes
                or (self._writer is not None and self._writer.is_alive())
            ):
                raise RuntimeError("queue projection writer shutdown was incomplete")
        with self._data_cv:
            self._records.clear()
            self._tombstones.clear()
            self._journal.clear()
            self._mutation_log.clear()
            self._loaded = False
            self._loading = False
            self._sequence = 0
            self._durable_sequence = 0
            self._persisted_sequence = 0
            self._rebuild_sequence = 0

    def debug_snapshot(self) -> dict[str, Any]:
        with self._lifecycle:
            state = self._state
            active_operations = self._active_operations
        with self._data_cv:
            data = {
                "loaded": self._loaded,
                "sequence": self._sequence,
                "durable_sequence": self._durable_sequence,
                "persisted_sequence": self._persisted_sequence,
                "rebuild_sequence": self._rebuild_sequence,
                "records": len(self._records),
                "tombstones": len(self._tombstones),
                "journal": len(self._journal),
            }
        with self._write_cv:
            writer = {
                "pending_writes": len(self._pending_writes),
                "pending_rebuild": self._pending_rebuild is not None,
                "active_writes": self._active_writes,
                "writer_alive": self._writer is not None
                and self._writer.is_alive(),
                "failure": (
                    None
                    if self._write_failure is None
                    else repr(self._write_failure)
                ),
            }
        with self._loader_lock:
            loader = {
                "loader_started": self._load_executor is not None,
                "loader_active": self._load_future is not None
                and not self._load_future.done(),
                "loader_shutdown_alive": self._loader_shutdown_thread
                is not None
                and self._loader_shutdown_thread.is_alive(),
            }
        return {
            "identity": str(self.context.identity),
            "epoch": self.epoch,
            "state": state,
            "active_operations": active_operations,
            **data,
            **writer,
            **loader,
        }


@dataclass
class _Binding:
    context: StorageContext
    runtime: ProjectionRuntime


class ProjectionRegistry:
    def __init__(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._binding: Optional[_Binding] = None
        self._next_epoch = 1

    def acquire(self, context: StorageContext) -> ProjectionRuntime:
        with self._cv:
            binding = self._binding
            if binding is None:
                runtime = ProjectionRuntime(context, self._next_epoch)
                self._next_epoch += 1
                self._binding = _Binding(context, runtime)
                return runtime
            if binding.context.identity != context.identity:
                raise RuntimeError(
                    "queue projection is bound to another storage identity"
                )
            if not binding.context.equivalent_to(context):
                raise RuntimeError(
                    "conflicting queue projection storage context"
                )
            if binding.runtime.state != "open":
                raise RuntimeError("queue projection runtime is closing")
            return binding.runtime

    def shutdown(
        self,
        context: StorageContext,
        *,
        timeout: Optional[float] = None,
        certify: bool = False,
        fingerprint: Optional[Fingerprint] = None,
    ) -> None:
        with self._cv:
            binding = self._binding
            if binding is None:
                return
            if binding.context.identity != context.identity:
                raise RuntimeError(
                    "queue projection is bound to another storage identity"
                )
            if not binding.context.equivalent_to(context):
                raise RuntimeError(
                    "conflicting queue projection storage context"
                )
            runtime = binding.runtime
        runtime._shutdown(
            timeout,
            certify=certify,
            fingerprint=fingerprint,
        )
        with self._cv:
            if self._binding is binding:
                self._binding = None
                self._cv.notify_all()

    def bound_identity(self) -> Optional[Path]:
        with self._cv:
            return (
                None
                if self._binding is None
                else self._binding.context.identity
            )


registry = ProjectionRegistry()
