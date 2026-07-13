from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import perf
import portable_lock


IndexKey = tuple[str, str, int, int, int, int]
_SCHEMA_VERSION = 1
_LEGACY_IMPORT_VERSION = 1
_UPSERT_SQL = (
    "INSERT INTO markers(run_id, provider_kind, ingestion_version, marker_size, "
    "marker_mtime_ns, marker_inode, row_json) VALUES(?,?,?,?,?,?,?) "
    "ON CONFLICT(run_id) DO UPDATE SET provider_kind=excluded.provider_kind, "
    "ingestion_version=excluded.ingestion_version, marker_size=excluded.marker_size, "
    "marker_mtime_ns=excluded.marker_mtime_ns, marker_inode=excluded.marker_inode, "
    "row_json=excluded.row_json WHERE markers.provider_kind <> excluded.provider_kind "
    "OR markers.ingestion_version <> excluded.ingestion_version "
    "OR markers.marker_size <> excluded.marker_size "
    "OR markers.marker_mtime_ns <> excluded.marker_mtime_ns "
    "OR markers.marker_inode <> excluded.marker_inode"
)


def row_key(row: dict[str, Any]) -> IndexKey:
    run_id = row.get("run_id")
    provider_kind = row.get("provider_kind")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("invalid reconciled-marker run_id")
    if not isinstance(provider_kind, str) or not provider_kind:
        raise ValueError("invalid reconciled-marker provider_kind")

    def bounded_int(name: str) -> int:
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"invalid reconciled-marker {name}")
        if value < 0 or value > (2**63 - 1):
            raise ValueError(f"out-of-range reconciled-marker {name}")
        return value

    return (
        run_id,
        provider_kind,
        bounded_int("ingestion_version"),
        bounded_int("marker_size"),
        bounded_int("marker_mtime_ns"),
        bounded_int("marker_inode"),
    )


class ReconciledMarkerIndex:
    def __init__(self, path: Path) -> None:
        self.path = path.with_suffix(".sqlite3")
        self.legacy_path = path
        self._lock = threading.RLock()

    def load_latest(self) -> dict[str, dict[str, Any]]:
        return self.load_latest_for(None)

    def load_latest_for(
        self, run_ids: Iterable[str] | None,
    ) -> dict[str, dict[str, Any]]:
        ids = None if run_ids is None else tuple(dict.fromkeys(run_ids))
        if ids == ():
            return {}
        started = time.perf_counter()
        with self._lock, self._connect() as connection:
            if ids is None:
                rows = connection.execute("SELECT row_json FROM markers").fetchall()
            else:
                rows = []
                for offset in range(0, len(ids), 900):
                    batch = ids[offset:offset + 900]
                    placeholders = ",".join("?" for _ in batch)
                    rows.extend(connection.execute(
                        f"SELECT row_json FROM markers WHERE run_id IN ({placeholders})",
                        batch,
                    ).fetchall())
        perf.record(
            "reconciled_marker_index.query",
            (time.perf_counter() - started) * 1000.0,
        )
        result: dict[str, dict[str, Any]] = {}
        for (raw,) in rows:
            try:
                row = json.loads(raw)
                key = row_key(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            result[key[0]] = row
        perf.record_count("reconciled_marker_index.query_rows", len(result))
        return result

    def load_keys(self) -> set[IndexKey]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, provider_kind, ingestion_version, marker_size, "
                "marker_mtime_ns, marker_inode FROM markers"
            ).fetchall()
        return {tuple(row) for row in rows}  # type: ignore[misc]

    def append(self, row: dict[str, Any]) -> bool:
        return self.append_many([row]) == 1

    def append_many(self, rows: list[dict[str, Any]]) -> int:
        owned = [dict(row) for row in rows]
        if not owned:
            return 0
        values = []
        for row in owned:
            key = row_key(row)
            values.append((*key, json.dumps(row, separators=(",", ":"))))
        started = time.perf_counter()
        with self._lock, self._connect() as connection:
            lock_started = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            perf.record(
                "reconciled_marker_index.lock_wait",
                (time.perf_counter() - lock_started) * 1000.0,
            )
            before = connection.total_changes
            connection.executemany(_UPSERT_SQL, values)
            appended = connection.total_changes - before
        perf.record("reconciled_marker_index.append", (time.perf_counter() - started) * 1000.0)
        perf.record_count("reconciled_marker_index.appended", appended)
        perf.record_count("reconciled_marker_index.duplicate", len(owned) - appended)
        return appended

    def remove(self, run_id: str) -> bool:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("invalid reconciled-marker run_id")
        with self._lock, self._connect() as connection:
            lock_started = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            perf.record(
                "reconciled_marker_index.lock_wait",
                (time.perf_counter() - lock_started) * 1000.0,
            )
            cursor = connection.execute("DELETE FROM markers WHERE run_id = ?", (run_id,))
            return cursor.rowcount == 1

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_projection_path()
        started = time.perf_counter()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            self._ensure_schema(connection)
            self._import_legacy_once(connection)
            return connection
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            if getattr(error, "sqlite_errorcode", None) not in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            }:
                raise
            return self._rebuild_corrupt_projection()
        finally:
            perf.record("reconciled_marker_index.open", (time.perf_counter() - started) * 1000.0)

    def _validate_projection_path(self) -> None:
        parent = self.path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise OSError("reconciled-marker projection parent is not a safe directory")
        if self.path.exists() or self.path.is_symlink():
            st = self.path.lstat()
            if self.path.is_symlink() or not stat.S_ISREG(st.st_mode):
                raise OSError("reconciled-marker projection is not a safe regular file")
        if self.path.parent.resolve() != self.legacy_path.parent.resolve():
            raise OSError("reconciled-marker projection escapes runs root")

    def _rebuild_corrupt_projection(self) -> sqlite3.Connection:
        lock_path = self.path.with_suffix(self.path.suffix + ".rebuild.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        with os.fdopen(lock_fd, "a+b") as lock_file:
            if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                raise OSError("reconciled-marker rebuild lock is not a regular file")
            wait_started = time.perf_counter()
            portable_lock.lock_ex(lock_file.fileno())
            perf.record(
                "reconciled_marker_index.rebuild_lock_wait",
                (time.perf_counter() - wait_started) * 1000.0,
            )
            try:
                try:
                    connection = sqlite3.connect(
                        self.path, timeout=30.0, isolation_level=None,
                    )
                    connection.execute("PRAGMA schema_version").fetchone()
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    self._ensure_schema(connection)
                    self._import_legacy_once(connection)
                    return connection
                except sqlite3.DatabaseError as error:
                    if 'connection' in locals():
                        connection.close()
                    if getattr(error, "sqlite_errorcode", None) not in {
                        sqlite3.SQLITE_CORRUPT,
                        sqlite3.SQLITE_NOTADB,
                    }:
                        raise
                for candidate in (
                    self.path,
                    self.path.with_name(self.path.name + "-wal"),
                    self.path.with_name(self.path.name + "-shm"),
                ):
                    candidate.unlink(missing_ok=True)
                perf.record_count("reconciled_marker_index.rebuild", 1)
                connection = sqlite3.connect(
                    self.path, timeout=30.0, isolation_level=None,
                )
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                self._ensure_schema(connection)
                self._import_legacy_once(connection)
                self._restore_authoritative_markers(connection)
                return connection
            finally:
                portable_lock.unlock(lock_file.fileno())

    def _restore_authoritative_markers(self, connection: sqlite3.Connection) -> None:
        started = time.perf_counter()
        rows = []
        root = self.path.parent
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                marker = Path(entry.path) / "reconciled.marker"
                dir_fd = None
                try:
                    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    dir_flags |= getattr(os, "O_NOFOLLOW", 0)
                    dir_fd = os.open(entry.path, dir_flags)
                    fd = os.open(
                        "reconciled.marker",
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=dir_fd,
                    )
                    with os.fdopen(fd, "rb") as stream:
                        st = os.fstat(stream.fileno())
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        data = json.load(stream)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                finally:
                    if dir_fd is not None:
                        os.close(dir_fd)
                provider_kind = data.get("provider_kind") if isinstance(data, dict) else None
                ingestion_version = data.get("ingestion_version") if isinstance(data, dict) else None
                if (
                    not isinstance(provider_kind, str)
                    or not provider_kind
                    or isinstance(ingestion_version, bool)
                    or not isinstance(ingestion_version, int)
                ):
                    continue
                row = {
                    "run_id": entry.name,
                    "marker_path": str(marker),
                    "provider_kind": provider_kind,
                    "ingestion_version": ingestion_version,
                    "marker_size": int(st.st_size),
                    "marker_mtime_ns": int(st.st_mtime_ns),
                    "marker_inode": int(getattr(st, "st_ino", 0) or 0),
                    "written_at": time.time(),
                }
                key = row_key(row)
                rows.append((*key, json.dumps(row, separators=(",", ":"))))
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(_UPSERT_SQL, rows)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        perf.record_count("reconciled_marker_index.authoritative_rows", len(rows))
        perf.record(
            "reconciled_marker_index.authoritative_rebuild",
            (time.perf_counter() - started) * 1000.0,
        )

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if version is not None and version[0] != _SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported reconciled-marker schema version: {version[0]}"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS markers("
            "run_id TEXT PRIMARY KEY, provider_kind TEXT NOT NULL, "
            "ingestion_version INTEGER NOT NULL, marker_size INTEGER NOT NULL, "
            "marker_mtime_ns INTEGER NOT NULL, marker_inode INTEGER NOT NULL, "
            "row_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
            (_SCHEMA_VERSION,),
        )
        try:
            os.chmod(Path(connection.execute("PRAGMA database_list").fetchone()[2]), 0o600)
        except OSError:
            pass

    def _import_legacy_once(self, connection: sqlite3.Connection) -> None:
        imported = connection.execute(
            "SELECT value FROM metadata WHERE key='legacy_import_version'"
        ).fetchone()
        if imported is not None and imported[0] == _LEGACY_IMPORT_VERSION:
            return
        started = time.perf_counter()
        malformed = 0
        imported_rows = 0
        bytes_read = 0
        connection.execute("BEGIN IMMEDIATE")
        try:
            imported = connection.execute(
                "SELECT value FROM metadata WHERE key='legacy_import_version'"
            ).fetchone()
            if imported is not None and imported[0] == _LEGACY_IMPORT_VERSION:
                connection.execute("COMMIT")
                return
            if self.legacy_path.exists() and not self.legacy_path.is_symlink():
                batch = []
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.legacy_path, flags)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise OSError("legacy reconciled-marker index is not a regular file")
                    for raw in stream:
                        bytes_read += len(raw)
                        try:
                            row = json.loads(raw)
                            key = row_key(row)
                        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                            malformed += 1
                            continue
                        batch.append((*key, json.dumps(row, separators=(",", ":"))))
                        if len(batch) == 500:
                            connection.executemany(_UPSERT_SQL, batch)
                            imported_rows += len(batch)
                            batch.clear()
                if batch:
                    connection.executemany(_UPSERT_SQL, batch)
                    imported_rows += len(batch)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('legacy_import_version',?)",
                (_LEGACY_IMPORT_VERSION,),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            perf.record(
                "reconciled_marker_index.legacy_import",
                (time.perf_counter() - started) * 1000.0,
            )
            perf.record_count("reconciled_marker_index.legacy_import_rows", imported_rows)
            perf.record_count("reconciled_marker_index.legacy_import_bytes", bytes_read)
            if malformed:
                perf.record_count("reconciled_marker_index.malformed", malformed)


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, ReconciledMarkerIndex] = {}


def for_path(path: Path) -> ReconciledMarkerIndex:
    key = str(path.absolute())
    with _REGISTRY_LOCK:
        found = _REGISTRY.get(key)
        if found is None:
            found = ReconciledMarkerIndex(path)
            _REGISTRY[key] = found
        return found
