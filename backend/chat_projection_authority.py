from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from paths import ba_home


PROVIDERS = frozenset({"claude", "codex", "gemini"})
STORE_KINDS = frozenset({"jsonl", "sqlite"})


class ProjectionAuthorityError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ProjectionAuthority:
    authority_id: str
    provider: str
    session_id: str
    root_id: str
    root_generation: int
    store_kind: str
    store_path: Path


class ProjectionAuthorityRegistry:
    SCHEMA_VERSION = 1
    _DDL = (
        "CREATE TABLE projection_authority("
        "authority_id TEXT PRIMARY KEY,provider TEXT NOT NULL,session_id TEXT NOT NULL UNIQUE,"
        "root_id TEXT NOT NULL UNIQUE,root_generation INTEGER NOT NULL,store_kind TEXT NOT NULL,"
        "CHECK(provider IN ('claude','codex','gemini')),"
        "CHECK(store_kind IN ('jsonl','sqlite')),CHECK(root_generation>=0))"
    )

    def __init__(self, path: Path | None = None) -> None:
        root = Path(os.path.abspath(ba_home().expanduser()))
        self._projection_root = root / "chat" / "canonical-projections"
        selected = path or root / "chat" / "projection-authority.sqlite3"
        selected = Path(os.path.abspath(selected.expanduser()))
        if not selected.resolve().is_relative_to(root.resolve()):
            raise ProjectionAuthorityError("invalid_authority_path", "registry must be under BETTER_AGENT_HOME")
        selected.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(selected, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._lock = threading.RLock()
        self._closed = False
        self._install_schema()

    def _install_schema(self) -> None:
        rows = self._connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if rows and version != self.SCHEMA_VERSION:
            self._connection.close()
            raise ProjectionAuthorityError("unsupported_authority_schema", "rebuild the authority registry")
        if not rows:
            self._connection.execute(self._DDL)
            self._connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self._connection.commit()
        actual = self._connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        expected_sql = "".join(self._DDL.lower().split())
        if len(actual) != 1 or actual[0][:3] != ("table", "projection_authority", "projection_authority"):
            self._connection.close()
            raise ProjectionAuthorityError("unsupported_authority_schema", "rebuild the authority registry")
        if "".join((actual[0][3] or "").lower().split()) != expected_sql:
            self._connection.close()
            raise ProjectionAuthorityError("unsupported_authority_schema", "rebuild the authority registry")
        columns = tuple(
            (row[1], row[2].upper(), int(row[3]), int(row[5]))
            for row in self._connection.execute("PRAGMA table_info('projection_authority')")
        )
        if columns != (
            ("authority_id", "TEXT", 0, 1), ("provider", "TEXT", 1, 0),
            ("session_id", "TEXT", 1, 0), ("root_id", "TEXT", 1, 0),
            ("root_generation", "INTEGER", 1, 0), ("store_kind", "TEXT", 1, 0),
        ):
            self._connection.close()
            raise ProjectionAuthorityError("unsupported_authority_schema", "rebuild the authority registry")
        unique_indexes = set()
        for index in self._connection.execute("PRAGMA index_list('projection_authority')"):
            if int(index[2]) != 1:
                continue
            names = tuple(
                row[2] for row in self._connection.execute(
                    f'PRAGMA index_info("{index[1]}")'
                )
            )
            unique_indexes.add((index[3], names))
        if unique_indexes != {
            ("pk", ("authority_id",)), ("u", ("session_id",)), ("u", ("root_id",)),
        }:
            self._connection.close()
            raise ProjectionAuthorityError("unsupported_authority_schema", "rebuild the authority registry")

    @staticmethod
    def _text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
            raise ProjectionAuthorityError("invalid_authority", f"{name} is invalid")
        return value

    @classmethod
    def _validate_selection(
        cls, provider: str, session_id: str, root_id: str, root_generation: int, store_kind: str,
    ) -> None:
        cls._text("session_id", session_id)
        cls._text("root_id", root_id)
        if provider not in PROVIDERS or store_kind not in STORE_KINDS:
            raise ProjectionAuthorityError("invalid_authority", "provider or store kind is unsupported")
        if type(root_generation) is not int or not 0 <= root_generation <= 9_223_372_036_854_775_807:
            raise ProjectionAuthorityError("invalid_authority", "root_generation is invalid")

    @staticmethod
    def _authority_id(provider: str, session_id: str, root_id: str) -> str:
        return hashlib.sha256(f"{provider}\0{session_id}\0{root_id}".encode("utf-8")).hexdigest()

    def _selection(self, row: tuple) -> ProjectionAuthority:
        authority_id, provider, session_id, root_id, generation, store_kind = row
        suffix = "jsonl" if store_kind == "jsonl" else "sqlite3"
        path = self._projection_root / f"{authority_id}.{suffix}"
        return ProjectionAuthority(
            authority_id, provider, session_id, root_id, int(generation), store_kind, path,
        )

    def register(
        self, *, provider: str, session_id: str, root_id: str,
        root_generation: int, store_kind: str,
    ) -> ProjectionAuthority:
        self._validate_selection(provider, session_id, root_id, root_generation, store_kind)
        authority_id = self._authority_id(provider, session_id, root_id)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT authority_id,provider,session_id,root_id,root_generation,store_kind "
                "FROM projection_authority WHERE session_id=? OR root_id=?",
                (session_id, root_id),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise ProjectionAuthorityError("mixed_authority", "session and root resolve differently")
                current = self._selection(rows[0])
                expected = (authority_id, provider, session_id, root_id, root_generation, store_kind)
                if rows[0] != expected:
                    raise ProjectionAuthorityError("authority_conflict", "authority is already assigned")
                return current
            with self._connection:
                self._connection.execute(
                    "INSERT INTO projection_authority VALUES(?,?,?,?,?,?)",
                    (authority_id, provider, session_id, root_id, root_generation, store_kind),
                )
            return self._selection((authority_id, provider, session_id, root_id, root_generation, store_kind))

    def require(self, authority: ProjectionAuthority) -> ProjectionAuthority:
        if not isinstance(authority, ProjectionAuthority):
            raise ProjectionAuthorityError("invalid_authority", "authority capability is invalid")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT authority_id,provider,session_id,root_id,root_generation,store_kind "
                "FROM projection_authority WHERE authority_id=?",
                (authority.authority_id,),
            ).fetchone()
        if row is None:
            raise ProjectionAuthorityError("authority_missing", "authority is not registered")
        current = self._selection(row)
        if current != authority:
            raise ProjectionAuthorityError("authority_mismatch", "authority capability is stale or mixed")
        return current

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProjectionAuthorityError("authority_closed", "authority registry is closed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()
