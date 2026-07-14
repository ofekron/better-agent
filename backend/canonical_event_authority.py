from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


class AuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class RootAuthority:
    root_id: str
    root_generation: int
    authority: str
    canonical_through_seq: int
    journal_through_seq: int
    message_through_seq: int
    database_path: str | None


class RuntimeAuthorityCatalog:
    SCHEMA_VERSION = 2

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        existing = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='root_authority'"
        ).fetchone()
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if existing and version != self.SCHEMA_VERSION:
            self._connection.close()
            raise AuthorityError("unsupported canonical authority catalog schema; rebuild the catalog")
        if not existing:
            self._connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS root_authority (
            root_id TEXT NOT NULL,
            root_generation INTEGER NOT NULL,
            authority TEXT NOT NULL CHECK(authority IN ('jsonl', 'sqlite', 'deleting', 'deleted')),
            canonical_through_seq INTEGER NOT NULL,
            journal_through_seq INTEGER NOT NULL,
            message_through_seq INTEGER NOT NULL,
            database_path TEXT,
            PRIMARY KEY (root_id, root_generation)
        )""")
        self._connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_live_generation
            ON root_authority(root_id) WHERE authority != 'deleted'""")
        self._lock = threading.RLock()

    def current(self, root_id: str) -> RootAuthority | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT root_generation,authority,canonical_through_seq,journal_through_seq,message_through_seq,database_path "
                "FROM root_authority WHERE root_id=? AND authority!='deleted'",
                (root_id,),
            ).fetchone()
        return RootAuthority(root_id, *row) if row else None

    def create(self, root_id: str) -> RootAuthority:
        with self._lock, self._connection:
            current = self.current(root_id)
            if current is not None:
                return current
            row = self._connection.execute(
                "SELECT MAX(root_generation) FROM root_authority WHERE root_id=?", (root_id,),
            ).fetchone()
            generation = int(row[0]) + 1 if row and row[0] is not None else 0
            self._connection.execute(
                "INSERT INTO root_authority VALUES(?,?,?,?,?,?,NULL)",
                (root_id, generation, "jsonl", 0, -1, -1),
            )
        return RootAuthority(root_id, generation, "jsonl", 0, -1, -1, None)

    def commit_sqlite_cutover(
        self,
        root_id: str,
        root_generation: int,
        *,
        database_path: Path,
        canonical_through_seq: int,
        journal_through_seq: int,
        message_through_seq: int,
    ) -> RootAuthority:
        if not database_path.is_file():
            raise AuthorityError("canonical database must exist before authority cutover")
        with self._lock, self._connection:
            current = self.current(root_id)
            if current is None or current.root_generation != root_generation:
                raise AuthorityError("root generation changed during cutover")
            if current.authority != "jsonl":
                raise AuthorityError("root is not JSONL-authoritative")
            self._connection.execute(
                "UPDATE root_authority SET authority='sqlite',canonical_through_seq=?,journal_through_seq=?,message_through_seq=?,database_path=? "
                "WHERE root_id=? AND root_generation=? AND authority='jsonl'",
                (canonical_through_seq, journal_through_seq, message_through_seq, str(database_path), root_id, root_generation),
            )
        return RootAuthority(root_id, root_generation, "sqlite", canonical_through_seq, journal_through_seq, message_through_seq, str(database_path))

    def advance_coverage(
        self,
        root_id: str,
        root_generation: int,
        *,
        canonical_through_seq: int,
        journal_through_seq: int,
        message_through_seq: int,
    ) -> RootAuthority:
        with self._lock, self._connection:
            current = self.current(root_id)
            if current is None or current.root_generation != root_generation or current.authority != "sqlite":
                raise AuthorityError("canonical authority is not current")
            if (canonical_through_seq < current.canonical_through_seq
                    or journal_through_seq < current.journal_through_seq
                    or message_through_seq < current.message_through_seq):
                raise AuthorityError("canonical coverage cannot regress")
            self._connection.execute(
                "UPDATE root_authority SET canonical_through_seq=?,journal_through_seq=?,message_through_seq=? WHERE root_id=? AND root_generation=?",
                (canonical_through_seq, journal_through_seq, message_through_seq, root_id, root_generation),
            )
        return RootAuthority(
            root_id, root_generation, "sqlite", canonical_through_seq,
            journal_through_seq, message_through_seq, current.database_path,
        )

    def require_database(self, root_id: str) -> Path | None:
        current = self.current(root_id)
        if current is None or current.authority == "jsonl":
            return None
        if current.authority != "sqlite" or not current.database_path:
            raise AuthorityError("invalid canonical authority record")
        path = Path(current.database_path)
        if not path.is_file():
            raise AuthorityError("authoritative canonical database is missing")
        return path

    def begin_delete(self, root_id: str, root_generation: int) -> None:
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE root_authority SET authority='deleting' WHERE root_id=? AND root_generation=? AND authority IN ('jsonl','sqlite')",
                (root_id, root_generation),
            ).rowcount
            if changed != 1:
                raise AuthorityError("root generation is not current")

    def finish_delete(self, root_id: str, root_generation: int) -> None:
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE root_authority SET authority='deleted' WHERE root_id=? AND root_generation=? AND authority='deleting'",
                (root_id, root_generation),
            ).rowcount
            if changed != 1:
                raise AuthorityError("root deletion is not pending")

    def abort_delete(self, root_id: str, root_generation: int) -> None:
        with self._lock, self._connection:
            current = self.current(root_id)
            if current is None or current.root_generation != root_generation or current.authority != "deleting":
                raise AuthorityError("root deletion is not pending")
            authority = "sqlite" if current.database_path else "jsonl"
            self._connection.execute(
                "UPDATE root_authority SET authority=? WHERE root_id=? AND root_generation=? AND authority='deleting'",
                (authority, root_id, root_generation),
            )

    def deleting(self) -> list[RootAuthority]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT root_id,root_generation,authority,canonical_through_seq,journal_through_seq,message_through_seq,database_path "
                "FROM root_authority WHERE authority='deleting'"
            ).fetchall()
        return [RootAuthority(*row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
