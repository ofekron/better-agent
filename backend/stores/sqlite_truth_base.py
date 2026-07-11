from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import perf
import portable_lock


MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_ERROR_TEXT_CHARS = 4_096
SQLITE_BUSY_TIMEOUT_MS = 5_000


class SchemaVersionError(RuntimeError):
    pass


def canonical_json(value: Any, *, label: str = "document") -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{label} exceeds the maximum encoded size")
    return encoded


def required_identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be a non-empty string of at most 512 characters")
    return value


def required_error_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    # Diagnostic text is bounded storage, not an identifier: keep the head
    # instead of refusing to record the failure at all.
    return value[:MAX_ERROR_TEXT_CHARS]


class SqliteTruthStore:
    """Fail-closed SQLite-WAL owner store.

    Truth-layer contract shared by every authoritative store: WAL journal,
    synchronous=FULL, cross-process locked initialization, and exact
    canonical-DDL validation that rejects both mismatched and unexpected
    objects. Subclasses define SCHEMA_VERSION, SCHEMA_OBJECTS, LABEL and
    PERF_PREFIX; SECURE_DELETE=True additionally zeroes freed page content
    so purged rows leave no byte residue.
    """

    SCHEMA_VERSION: int
    SCHEMA_OBJECTS: dict[str, str]
    LABEL: str
    PERF_PREFIX: str
    SECURE_DELETE = False

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=FULL")
        if self.SECURE_DELETE:
            conn.execute("PRAGMA secure_delete=ON")
        return conn

    def _initialize(self) -> None:
        started = time.perf_counter()
        lock_path = self._path.with_suffix(self._path.suffix + ".init.lock")
        with lock_path.open("a+b") as lock_file:
            portable_lock.lock_ex(lock_file.fileno())
            try:
                self._initialize_locked()
            finally:
                portable_lock.unlock(lock_file.fileno())
        perf.record(f"{self.PERF_PREFIX}.initialize", (time.perf_counter() - started) * 1000.0)

    def _initialize_locked(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            stored = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if stored not in (0, self.SCHEMA_VERSION):
                raise SchemaVersionError(
                    f"unsupported {self.LABEL} database schema {stored}; "
                    f"expected {self.SCHEMA_VERSION}"
                )
            if stored == self.SCHEMA_VERSION:
                self._validate_schema(conn)
                conn.commit()
                return
            for statement in self.SCHEMA_OBJECTS.values():
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self._validate_schema(conn)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _validate_schema(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT name, sql FROM sqlite_master").fetchall()
        actual = {str(row["name"]): str(row["sql"] or "") for row in rows}
        for name in actual:
            # SQLite-owned internals (sqlite_autoindex_*, sqlite_sequence, ...)
            # are the only objects allowed beyond the canonical schema.
            if name not in self.SCHEMA_OBJECTS and not name.startswith("sqlite_"):
                raise SchemaVersionError(
                    f"{self.LABEL} database contains unexpected object {name}"
                )
        for name, expected_sql in self.SCHEMA_OBJECTS.items():
            actual_sql = actual.get(name)
            if actual_sql is None or " ".join(actual_sql.split()) != " ".join(expected_sql.split()):
                raise SchemaVersionError(
                    f"{self.LABEL} database object {name} has unexpected schema"
                )
