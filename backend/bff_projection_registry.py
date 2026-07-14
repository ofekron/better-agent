from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


class ProjectionMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionState:
    root_id: str
    root_generation: int
    schema_version: int
    epoch: str
    revision: int
    canonical_through_seq: int
    checksum: str


class ProjectionRegistry:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS projection_roots (
            root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, schema_version INTEGER NOT NULL, epoch TEXT NOT NULL,
            revision INTEGER NOT NULL, canonical_through_seq INTEGER NOT NULL, checksum TEXT NOT NULL
            , PRIMARY KEY (root_id, root_generation)
        )""")
        self._lock = threading.Lock()

    def get(self, root_id: str, root_generation: int) -> ProjectionState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT schema_version,epoch,revision,canonical_through_seq,checksum FROM projection_roots WHERE root_id=? AND root_generation=?",
                (root_id, root_generation),
            ).fetchone()
        return ProjectionState(root_id, root_generation, *row) if row else None

    def publish(self, root_id: str, root_generation: int, *, canonical_through_seq: int, checksum: str, schema_version: int) -> ProjectionState:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT schema_version,epoch,revision,canonical_through_seq,checksum FROM projection_roots WHERE root_id=? AND root_generation=?",
                (root_id, root_generation),
            ).fetchone()
            if row and row[0] == schema_version and canonical_through_seq < row[3]:
                raise ProjectionMismatch("projection canonical sequence regressed")
            if row and row[0] == schema_version and row[3] == canonical_through_seq:
                if row[4] != checksum:
                    raise ProjectionMismatch("projection rebuild checksum mismatch")
                return ProjectionState(root_id, root_generation, *row)
            epoch = row[1] if row and row[0] == schema_version else str(uuid.uuid4())
            revision = row[2] + 1 if row and row[0] == schema_version else 1
            self._connection.execute(
                "INSERT INTO projection_roots VALUES(?,?,?,?,?,?,?) ON CONFLICT(root_id, root_generation) DO UPDATE SET schema_version=excluded.schema_version, epoch=excluded.epoch, revision=excluded.revision, canonical_through_seq=excluded.canonical_through_seq, checksum=excluded.checksum",
                (root_id, root_generation, schema_version, epoch, revision, canonical_through_seq, checksum),
            )
        return ProjectionState(root_id, root_generation, schema_version, epoch, revision, canonical_through_seq, checksum)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
