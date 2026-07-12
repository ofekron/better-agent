from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional, Sequence

import perf


ChangeKind = Literal["upsert", "delete"]
FileSignature = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class RootChange:
    seq: int
    kind: ChangeKind
    root_id: str
    path: Path
    signature: FileSignature | None


ApplyChange = Callable[[RootChange], bool | None]


@dataclass(frozen=True)
class LocalMutation:
    mutation_seq: int
    kind: ChangeKind
    root_id: str
    path: Path


class RootChangeWal:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS root_changes ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL "
                "CHECK(kind IN ('upsert','delete')), root_id TEXT NOT NULL, "
                "path TEXT NOT NULL, signature TEXT, created_ns INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS consumer_checkpoint ("
                "consumer TEXT PRIMARY KEY, seq INTEGER NOT NULL CHECK(seq >= 0))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS owner_signatures ("
                "consumer TEXT NOT NULL, path TEXT NOT NULL, root_id TEXT NOT NULL, "
                "signature TEXT NOT NULL, PRIMARY KEY(consumer, path))"
            )
            connection.commit()
            self._connection = connection

    def append(
        self,
        kind: ChangeKind,
        root_id: str,
        path: Path,
        signature: FileSignature | None,
    ) -> int:
        return self.append_many(((kind, root_id, path, signature),))[0].seq

    def append_many(
        self,
        changes: Sequence[tuple[ChangeKind, str, Path, FileSignature | None]],
    ) -> list[RootChange]:
        if not changes:
            return []
        for kind, root_id, _path, _signature in changes:
            if kind not in ("upsert", "delete"):
                raise ValueError("unsupported root change kind")
            if not root_id or Path(root_id).name != root_id:
                raise ValueError("root_id must be a non-empty path segment")
        started = time.perf_counter()
        rows: list[RootChange] = []
        with self._lock:
            connection = self._require_connection()
            with connection:
                for kind, root_id, path, signature in changes:
                    payload = (
                        json.dumps(signature, separators=(",", ":"))
                        if signature is not None else None
                    )
                    cursor = connection.execute(
                        "INSERT INTO root_changes(kind, root_id, path, signature, created_ns) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (kind, root_id, os.fspath(path), payload, time.time_ns()),
                    )
                    rows.append(RootChange(int(cursor.lastrowid), kind, root_id, Path(path), signature))
        perf.record("store.session.root_change_wal.append", (time.perf_counter() - started) * 1000)
        perf.record_count("store.session.root_change_wal.append_batch_size", len(rows))
        return rows

    def read_after(self, seq: int, limit: int) -> list[RootChange]:
        if seq < 0 or limit < 1:
            raise ValueError("invalid WAL cursor or limit")
        with self._lock:
            rows = self._require_connection().execute(
                "SELECT seq, kind, root_id, path, signature FROM root_changes "
                "WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit),
            ).fetchall()
        return [
            RootChange(
                seq=int(row[0]), kind=row[1], root_id=row[2], path=Path(row[3]),
                signature=tuple(json.loads(row[4])) if row[4] is not None else None,
            )
            for row in rows
        ]

    def checkpoint(self, consumer: str) -> int:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT seq FROM consumer_checkpoint WHERE consumer = ?", (consumer,)
            ).fetchone()
        return int(row[0]) if row else 0

    def owner_signatures(self, consumer: str) -> dict[Path, tuple[str, FileSignature]]:
        with self._lock:
            rows = self._require_connection().execute(
                "SELECT path, root_id, signature FROM owner_signatures WHERE consumer = ?",
                (consumer,),
            ).fetchall()
        return {
            Path(path): (root_id, tuple(json.loads(signature)))
            for path, root_id, signature in rows
        }

    def commit_projection(self, consumer: str, changes: Sequence[RootChange]) -> None:
        if not changes:
            return
        started = time.perf_counter()
        with self._lock:
            connection = self._require_connection()
            with connection:
                for change in changes:
                    if change.kind == "delete":
                        connection.execute(
                            "DELETE FROM owner_signatures WHERE consumer = ? AND path = ?",
                            (consumer, os.fspath(change.path)),
                        )
                    elif change.signature is not None:
                        connection.execute(
                            "INSERT INTO owner_signatures(consumer,path,root_id,signature) "
                            "VALUES(?,?,?,?) ON CONFLICT(consumer,path) DO UPDATE SET "
                            "root_id=excluded.root_id, signature=excluded.signature",
                            (
                                consumer, os.fspath(change.path), change.root_id,
                                json.dumps(change.signature, separators=(",", ":")),
                            ),
                        )
                connection.execute(
                    "INSERT INTO consumer_checkpoint(consumer, seq) VALUES (?, ?) "
                    "ON CONFLICT(consumer) DO UPDATE SET seq = excluded.seq "
                    "WHERE excluded.seq >= consumer_checkpoint.seq",
                    (consumer, changes[-1].seq),
                )
        perf.record("store.session.root_change_wal.projection_commit", (time.perf_counter() - started) * 1000)

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("root change WAL is not open")
        return self._connection


class RootChangeOwner:
    def __init__(
        self,
        *,
        wal: RootChangeWal,
        roots: Callable[[], Iterable[Path]],
        apply: ApplyChange,
        accept_path: Callable[[Path], bool] | None = None,
        consumer: str = "session-root-projection",
        max_entries_per_tick: int = 128,
        max_tick_wall_ms: float = 20.0,
        max_tick_cpu_ms: float = 10.0,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float | None = None,
        idle_backoff_factor: float = 2.0,
    ) -> None:
        if max_poll_interval_s is None:
            max_poll_interval_s = max(30.0, poll_interval_s * 2.0)
        if (max_entries_per_tick < 1 or poll_interval_s <= 0
                or max_poll_interval_s < poll_interval_s or idle_backoff_factor <= 1
                or max_tick_wall_ms <= 0 or max_tick_cpu_ms <= 0):
            raise ValueError("watcher bounds must be positive")
        self._wal = wal
        self._roots = roots
        self._apply = apply
        self._accept_path = accept_path or (lambda path: path.suffix == ".json")
        self._consumer = consumer
        self._max_entries = max_entries_per_tick
        self._max_tick_wall_ms = max_tick_wall_ms
        self._max_tick_cpu_ms = max_tick_cpu_ms
        self._poll_interval = poll_interval_s
        self._max_poll_interval = max_poll_interval_s
        self._idle_backoff_factor = idle_backoff_factor
        self._known: dict[Path, tuple[str, FileSignature]] = {}
        self._operation_lock = threading.RLock()
        self._mutation_seq = 0
        self._local_journal: dict[Path, tuple[int, ChangeKind, str, FileSignature | None]] = {}
        self._ready = threading.Event()
        self._startup_failure: BaseException | None = None
        self._observation = threading.Condition()
        self._observation_generation = 0
        self._scheduler = threading.Condition()
        self._scan_requested = False
        self._scan_in_progress = False
        self._local_activity_generation = 0
        self._cycle_dirs: tuple[Path, ...] = ()
        self._cycle_dir_index = 0
        self._cycle_scanner: os.ScandirIterator | None = None
        self._cycle_snapshot: dict[Path, tuple[str, FileSignature]] = {}
        self._cycle_start_seq = 0
        self._cycle_started_at = 0.0
        self._delete_candidates: set[Path] = set()
        self._completed_snapshot: dict[Path, tuple[str, FileSignature]] | None = None
        self._last_cycle_change_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._wal.open()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="session-root-change-owner", daemon=True)
        perf.register_queue("session-root-change-owner", self.pending_count)
        self._thread.start()

    def wait_ready(self, timeout: Optional[float] = None) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("root change owner readiness timed out")
        if self._startup_failure is not None:
            raise RuntimeError("root change owner startup failed") from self._startup_failure

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._stop.set()
            with self._scheduler:
                self._scheduler.notify_all()
            thread.join(timeout)
            if thread.is_alive():
                self._thread = thread
                raise TimeoutError("root change owner shutdown timed out")
            perf.unregister_queue("session-root-change-owner")
        if self._cycle_scanner is not None:
            self._cycle_scanner.close()
            self._cycle_scanner = None
        self._wal.close()

    @property
    def observation_generation(self) -> int:
        with self._observation:
            return self._observation_generation

    def wait_for_observation(self, generation: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._scheduler:
            if self._observation_generation <= generation:
                if self._scan_requested or self._scan_in_progress:
                    perf.record_count("store.session.root_change_watcher.observation_coalesced")
                else:
                    self._scan_requested = True
                    perf.record_count("store.session.root_change_watcher.observation_demand")
                self._scheduler.notify()
        with self._observation:
            while self._observation_generation <= generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._observation.wait(remaining)
            return True

    def begin_local_upsert(self, root_id: str, path: Path) -> LocalMutation:
        return self._begin_local("upsert", root_id, path)

    def begin_local_delete(self, root_id: str, path: Path) -> LocalMutation:
        return self._begin_local("delete", root_id, path)

    def _begin_local(self, kind: ChangeKind, root_id: str, path: Path) -> LocalMutation:
        self.wait_ready()
        with self._operation_lock:
            self._mutation_seq += 1
            token = LocalMutation(self._mutation_seq, kind, root_id, path)
            self._local_journal[path] = (token.mutation_seq, kind, root_id, None)
            perf.record_count("store.session.root_change_watcher.local_intent")
            self._signal_local_activity()
            return token

    def _signal_local_activity(self) -> None:
        with self._scheduler:
            self._local_activity_generation += 1
            self._scheduler.notify()

    def durable_local(
        self,
        mutation: LocalMutation,
        signature: FileSignature | None = None,
    ) -> RootChange:
        if mutation.kind == "delete":
            signature = None
        if mutation.kind == "upsert" and signature is None:
            raise ValueError("durable upsert requires the committed file signature")
        change = self._wal.append_many(((
            mutation.kind, mutation.root_id, mutation.path, signature,
        ),))[0]
        with self._operation_lock:
            current = self._local_journal.get(mutation.path)
            if current is not None and current[0] == mutation.mutation_seq:
                self._local_journal[mutation.path] = (
                    mutation.mutation_seq, mutation.kind, mutation.root_id, signature,
                )
        return change

    def complete_local(self, change: RootChange) -> None:
        self._wal.commit_projection(self._consumer, (change,))
        with self._operation_lock:
            if change.kind == "delete":
                self._known.pop(change.path, None)
            elif change.signature is not None:
                self._known[change.path] = (change.root_id, change.signature)
            current = self._local_journal.get(change.path)
            if current is not None and current[1:] == (
                change.kind, change.root_id, change.signature,
            ):
                self._local_journal.pop(change.path, None)

    def abandon_local(self, mutation: LocalMutation) -> None:
        with self._operation_lock:
            current = self._local_journal.get(mutation.path)
            if current is not None and current[0] == mutation.mutation_seq:
                self._local_journal.pop(mutation.path, None)

    def replay_once(self) -> int:
        cursor = self._wal.checkpoint(self._consumer)
        changes = self._wal.read_after(cursor, self._max_entries)
        if not changes:
            return 0
        for change in changes:
            if self._apply(change) is False:
                raise RuntimeError(f"root change projection rejected {change.root_id}")
        self._wal.commit_projection(self._consumer, changes)
        with self._operation_lock:
            for change in changes:
                if change.kind == "delete":
                    self._known.pop(change.path, None)
                elif change.signature is not None:
                    self._known[change.path] = (change.root_id, change.signature)
        perf.record_count("store.session.root_change_wal.replayed", len(changes))
        return len(changes)

    def poll_once(self) -> int:
        wall_started = time.perf_counter()
        cpu_started = time.thread_time()
        if self._completed_snapshot is not None:
            self._commit_completed_cycle()
            return 0
        if not self._cycle_dirs:
            self._cycle_started_at = wall_started
            self._cycle_dirs = tuple(Path(path) for path in self._roots())
            self._cycle_dir_index = 0
            self._cycle_snapshot = {}
            with self._operation_lock:
                self._cycle_start_seq = self._mutation_seq
        processed = 0
        while processed < self._max_entries and self._cycle_dir_index < len(self._cycle_dirs):
                if ((time.perf_counter() - wall_started) * 1000 >= self._max_tick_wall_ms
                        or (time.thread_time() - cpu_started) * 1000 >= self._max_tick_cpu_ms):
                    break
                directory = self._cycle_dirs[self._cycle_dir_index]
                if self._cycle_scanner is None:
                    try:
                        self._cycle_scanner = os.scandir(directory)
                    except OSError:
                        self._cycle_dir_index += 1
                        continue
                try:
                    entry = next(self._cycle_scanner)
                except StopIteration:
                    self._cycle_scanner.close()
                    self._cycle_scanner = None
                    self._cycle_dir_index += 1
                    continue
                except OSError:
                    self._cycle_scanner.close()
                    self._cycle_scanner = None
                    self._cycle_dir_index += 1
                    continue
                processed += 1
                path = Path(entry.path)
                if not entry.is_file() or not self._accept_path(path):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                self._cycle_snapshot[path] = (path.stem, self._signature_from_stat(stat))
        if self._cycle_dir_index >= len(self._cycle_dirs):
            self._completed_snapshot = self._cycle_snapshot
            self._commit_completed_cycle()
        perf.record_count("store.session.root_change_watcher.entries", processed)
        perf.record("store.session.root_change_watcher.scan_wall", (time.perf_counter() - wall_started) * 1000)
        perf.record("store.session.root_change_watcher.scan_cpu", (time.thread_time() - cpu_started) * 1000)
        return processed

    def _commit_completed_cycle(self) -> None:
        assert self._completed_snapshot is not None
        while self.replay_once():
            pass
        lock_started = time.perf_counter()
        with self._operation_lock:
            perf.record("store.session.root_change_watcher.reconcile_lock_wait", (time.perf_counter() - lock_started) * 1000)
            disk = dict(self._completed_snapshot)
            for path, (seq, kind, root_id, signature) in self._local_journal.items():
                if seq <= self._cycle_start_seq:
                    continue
                if kind == "delete":
                    disk.pop(path, None)
                elif signature is not None:
                    disk[path] = (root_id, signature)
            known = dict(self._known)
            mutation_seq = self._mutation_seq
            verified_deletes = self._delete_candidates & (set(known) - set(disk))
            self._delete_candidates = set(known) - set(disk)
        perf.record("store.session.root_change_watcher.reconcile_lock_hold", (time.perf_counter() - lock_started) * 1000)
        self._last_cycle_change_count = self._reconcile_snapshot(
            disk, known, verified_deletes, mutation_seq,
        )
        self._completed_snapshot = None
        self._cycle_dirs = ()
        self._cycle_snapshot = {}
        with self._observation:
            self._observation_generation += 1
            self._observation.notify_all()
        if self._cycle_started_at:
            perf.record(
                "store.session.root_change_watcher.authority_cycle_wall",
                (time.perf_counter() - self._cycle_started_at) * 1000,
            )
            self._cycle_started_at = 0.0

    def _reconcile_snapshot(self, disk, known=None, verified_deletes=None, mutation_seq=None) -> int:
        known = dict(self._known) if known is None else known
        verified_deletes = set(known) - set(disk) if verified_deletes is None else verified_deletes
        changes: list[tuple[ChangeKind, str, Path, FileSignature | None]] = []
        for path, (root_id, signature) in disk.items():
            if known.get(path) != (root_id, signature):
                changes.append(("upsert", root_id, path, signature))
        for path, (root_id, _signature) in known.items():
            if path in verified_deletes:
                changes.append(("delete", root_id, path, None))
        if changes:
            appended = self._wal.append_many(changes)
            for change in appended:
                if self._apply(change) is False:
                    raise RuntimeError(f"root change projection rejected {change.root_id}")
            self._wal.commit_projection(self._consumer, appended)
            with self._operation_lock:
                if mutation_seq is None or self._mutation_seq == mutation_seq:
                    retained = {
                        path: value for path, value in known.items()
                        if path not in disk and path not in verified_deletes
                    }
                    self._known = {**disk, **retained}
                else:
                    perf.record_count("store.session.root_change_watcher.cas_conflict")
        elif verified_deletes:
            with self._operation_lock:
                for path in verified_deletes:
                    self._known.pop(path, None)
        perf.record_count("store.session.root_change_watcher.changes", len(changes))
        return len(changes)

    def pending_count(self) -> int:
        try:
            return int(bool(self._wal.read_after(self._wal.checkpoint(self._consumer), 1)))
        except (IndexError, RuntimeError):
            return 0

    def _run(self) -> None:
        try:
            with self._operation_lock:
                self._known = self._wal.owner_signatures(self._consumer)
            while self.replay_once():
                pass
            # Readiness requires one complete unchanged pass after reconciliation.
            # A mutation observed by the verification pass starts another pass.
            while True:
                disk = self._disk_snapshot()
                changed = self._reconcile_snapshot(disk)
                if changed == 0:
                    break
        except BaseException as exc:
            self._startup_failure = exc
            self._ready.set()
            return
        with self._observation:
            self._observation_generation += 1
            self._observation.notify_all()
        self._ready.set()
        interval = self._poll_interval
        deadline = time.monotonic() + interval
        seen_local_activity = self._local_activity_generation
        while not self._stop.is_set():
            with self._scheduler:
                while not self._stop.is_set():
                    now = time.monotonic()
                    local_activity = self._local_activity_generation != seen_local_activity
                    if self._scan_requested or local_activity or now >= deadline:
                        break
                    self._scheduler.wait(deadline - now)
                if self._stop.is_set():
                    break
                local_activity = self._local_activity_generation != seen_local_activity
                seen_local_activity = self._local_activity_generation
                requested = self._scan_requested
                self._scan_requested = False
            if local_activity and not requested:
                interval = self._poll_interval
                deadline = min(deadline, time.monotonic() + interval)
                perf.record_count("store.session.root_change_watcher.wake.local_activity")
                perf.record("store.session.root_change_watcher.idle_interval", interval * 1000)
                continue
            wake_reason = "observation" if requested else "authority_deadline"
            perf.record_count(f"store.session.root_change_watcher.wake.{wake_reason}")
            with self._scheduler:
                self._scan_in_progress = True
            started = time.perf_counter()
            failed = False
            try:
                self.poll_once()
            except Exception:
                failed = True
                perf.record_count("store.session.root_change_watcher.failed")
            finally:
                with self._scheduler:
                    self._scan_in_progress = False
            perf.record("store.session.root_change_watcher.tick", (time.perf_counter() - started) * 1000)
            if failed:
                interval = self._poll_interval
                deadline = time.monotonic() + interval
                perf.record_count("store.session.root_change_watcher.wake.retry_backoff")
                continue
            if self._cycle_dirs or self._completed_snapshot is not None:
                deadline = time.monotonic()
                perf.record_count("store.session.root_change_watcher.wake.cycle_continuation")
                continue
            if self._last_cycle_change_count or requested:
                interval = self._poll_interval
            else:
                interval = min(
                    self._max_poll_interval,
                    interval * self._idle_backoff_factor,
                )
            deadline = time.monotonic() + interval
            perf.record("store.session.root_change_watcher.idle_interval", interval * 1000)

    def _disk_snapshot(self) -> dict[Path, tuple[str, FileSignature]]:
        snapshot: dict[Path, tuple[str, FileSignature]] = {}
        for directory in self._roots():
            try:
                scanner = os.scandir(directory)
            except OSError:
                continue
            with scanner:
                for entry in scanner:
                    path = Path(entry.path)
                    if not entry.is_file() or not self._accept_path(path):
                        continue
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    snapshot[path] = (path.stem, self._signature_from_stat(stat))
        return snapshot

    @staticmethod
    def _signature(path: Path) -> FileSignature | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return RootChangeOwner._signature_from_stat(stat)

    @staticmethod
    def _signature_from_stat(stat: os.stat_result) -> FileSignature:
        return (
            int(stat.st_dev), int(stat.st_ino), int(stat.st_ctime_ns),
            int(stat.st_mtime_ns), int(stat.st_size),
        )
