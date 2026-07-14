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
    database_path: str | None


class RuntimeAuthorityCatalog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS root_authority (
            root_id TEXT NOT NULL,
            root_generation INTEGER NOT NULL,
            authority TEXT NOT NULL CHECK(authority IN ('jsonl', 'sqlite', 'deleted')),
            canonical_through_seq INTEGER NOT NULL,
            database_path TEXT,
            PRIMARY KEY (root_id, root_generation)
        )""")
        self._connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_live_generation
            ON root_authority(root_id) WHERE authority != 'deleted'""")
        self._lock = threading.RLock()

    def current(self, root_id: str) -> RootAuthority | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT root_generation,authority,canonical_through_seq,database_path "
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
                "INSERT INTO root_authority VALUES(?,?,?,?,NULL)",
                (root_id, generation, "jsonl", 0),
            )
        return RootAuthority(root_id, generation, "jsonl", 0, None)

    def commit_sqlite_cutover(
        self,
        root_id: str,
        root_generation: int,
        *,
        database_path: Path,
        canonical_through_seq: int,
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
                "UPDATE root_authority SET authority='sqlite',canonical_through_seq=?,database_path=? "
                "WHERE root_id=? AND root_generation=? AND authority='jsonl'",
                (canonical_through_seq, str(database_path), root_id, root_generation),
            )
        return RootAuthority(root_id, root_generation, "sqlite", canonical_through_seq, str(database_path))

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

    def delete(self, root_id: str, root_generation: int) -> None:
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE root_authority SET authority='deleted' WHERE root_id=? AND root_generation=? AND authority!='deleted'",
                (root_id, root_generation),
            ).rowcount
            if changed != 1:
                raise AuthorityError("root generation is not current")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
