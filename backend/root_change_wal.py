from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

import perf


ChangeKind = Literal["upsert", "delete"]


@dataclass(frozen=True)
class RootChange:
    seq: int
    kind: ChangeKind
    root_id: str
    path: Path
    signature: tuple[int, int, int] | None


ApplyChange = Callable[[RootChange], None]


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
            connection.commit()
            self._connection = connection

    def append(
        self,
        kind: ChangeKind,
        root_id: str,
        path: Path,
        signature: tuple[int, int, int] | None,
    ) -> int:
        if kind not in ("upsert", "delete"):
            raise ValueError("unsupported root change kind")
        if not root_id or Path(root_id).name != root_id:
            raise ValueError("root_id must be a non-empty path segment")
        payload = json.dumps(signature, separators=(",", ":")) if signature is not None else None
        started = time.perf_counter()
        with self._lock:
            connection = self._require_connection()
            cursor = connection.execute(
                "INSERT INTO root_changes(kind, root_id, path, signature, created_ns) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, root_id, os.fspath(path), payload, time.time_ns()),
            )
            connection.commit()
            seq = int(cursor.lastrowid)
        perf.record("store.session.root_change_wal.append", (time.perf_counter() - started) * 1000)
        return seq

    def read_after(self, seq: int, limit: int) -> list[RootChange]:
        if seq < 0 or limit < 1:
            raise ValueError("invalid WAL cursor or limit")
        with self._lock:
            rows = self._require_connection().execute(
                "SELECT seq, kind, root_id, path, signature FROM root_changes "
                "WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit),
            ).fetchall()
        return [
            RootChange(
                seq=int(row[0]),
                kind=row[1],
                root_id=row[2],
                path=Path(row[3]),
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

    def advance(self, consumer: str, seq: int) -> None:
        with self._lock:
            connection = self._require_connection()
            connection.execute(
                "INSERT INTO consumer_checkpoint(consumer, seq) VALUES (?, ?) "
                "ON CONFLICT(consumer) DO UPDATE SET seq = excluded.seq "
                "WHERE excluded.seq >= consumer_checkpoint.seq",
                (consumer, seq),
            )
            connection.commit()

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
        poll_interval_s: float = 0.25,
    ) -> None:
        if max_entries_per_tick < 1 or poll_interval_s <= 0:
            raise ValueError("watcher bounds must be positive")
        self._wal = wal
        self._roots = roots
        self._apply = apply
        self._accept_path = accept_path or (lambda path: path.suffix == ".json")
        self._consumer = consumer
        self._max_entries = max_entries_per_tick
        self._poll_interval = poll_interval_s
        self._known: dict[Path, tuple[int, int, int]] = {}
        self._scan_by_dir: dict[Path, os.ScandirIterator] = {}
        self._seen_by_dir: dict[Path, set[Path]] = {}
        self._pending_deletes: list[Path] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._wal.open()
        try:
            self.replay_once()
        except BaseException:
            self._wal.close()
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="session-root-change-owner", daemon=True)
        perf.register_queue("session-root-change-owner", self.pending_count)
        self._thread.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._stop.set()
            thread.join(timeout)
            if thread.is_alive():
                self._thread = thread
                raise TimeoutError("root change owner shutdown timed out")
            perf.unregister_queue("session-root-change-owner")
        for scanner in self._scan_by_dir.values():
            scanner.close()
        self._scan_by_dir.clear()
        self._seen_by_dir.clear()
        self._wal.close()

    def publish_durable_upsert(self, root_id: str, path: Path) -> int:
        signature = self._signature(path)
        if signature is None:
            raise FileNotFoundError(path)
        seq = self._wal.append("upsert", root_id, path, signature)
        self.replay_once()
        return seq

    def publish_durable_delete(self, root_id: str, path: Path) -> int:
        seq = self._wal.append("delete", root_id, path, None)
        self.replay_once()
        return seq

    def replay_once(self) -> int:
        cursor = self._wal.checkpoint(self._consumer)
        changes = self._wal.read_after(cursor, self._max_entries)
        for change in changes:
            self._apply(change)
            self._wal.advance(self._consumer, change.seq)
        perf.record_count("store.session.root_change_wal.replayed", len(changes))
        return len(changes)

    def poll_once(self) -> int:
        processed = 0
        while self._pending_deletes and processed < self._max_entries:
            path = self._pending_deletes.pop()
            if path not in self._known:
                continue
            self._known.pop(path, None)
            self._wal.append("delete", path.stem, path, None)
            processed += 1
        for directory in self._roots():
            if processed >= self._max_entries:
                break
            directory = Path(directory)
            scanner = self._scan_by_dir.get(directory)
            if scanner is None:
                try:
                    scanner = os.scandir(directory)
                except OSError:
                    continue
                self._scan_by_dir[directory] = scanner
                self._seen_by_dir[directory] = set()
            while processed < self._max_entries:
                try:
                    entry = next(scanner)
                except StopIteration:
                    scanner.close()
                    self._scan_by_dir.pop(directory, None)
                    seen = self._seen_by_dir.pop(directory, set())
                    self._pending_deletes.extend(
                        path for path in self._known if path.parent == directory and path not in seen
                    )
                    break
                except OSError:
                    scanner.close()
                    self._scan_by_dir.pop(directory, None)
                    self._seen_by_dir.pop(directory, None)
                    break
                path = Path(entry.path)
                processed += 1
                if not entry.is_file() or not self._accept_path(path):
                    continue
                self._seen_by_dir[directory].add(path)
                signature = self._signature(path)
                if signature is None or self._known.get(path) == signature:
                    continue
                self._known[path] = signature
                self._wal.append("upsert", path.stem, path, signature)
        self.replay_once()
        perf.record_count("store.session.root_change_watcher.entries", processed)
        return processed

    def pending_count(self) -> int:
        try:
            return int(bool(self._wal.read_after(self._wal.checkpoint(self._consumer), 1)))
        except (IndexError, RuntimeError):
            return 0

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            started = time.perf_counter()
            try:
                self.poll_once()
            except Exception:
                perf.record_count("store.session.root_change_watcher.failed")
            perf.record("store.session.root_change_watcher.tick", (time.perf_counter() - started) * 1000)

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return int(stat.st_mtime_ns), int(stat.st_size), int(getattr(stat, "st_ino", 0))
