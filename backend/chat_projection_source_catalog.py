from __future__ import annotations

import sqlite3
import threading
import os
from dataclasses import dataclass
from pathlib import Path

from paths import ba_home
from chat_projection_store import ChatProjectionStoreError
from chat_projection_store_owner import OwnerClient, serve_owner
from chat_projection_store_owner_path import verify_anchored_file


SCHEMA_VERSION = 1
DDL = (
    "CREATE TABLE root_generations(root_id TEXT PRIMARY KEY,generation INTEGER NOT NULL CHECK(generation>=1))",
    "CREATE TABLE streams(root_id TEXT NOT NULL,stream_id TEXT NOT NULL,provider TEXT NOT NULL,generation INTEGER NOT NULL,next_sequence INTEGER NOT NULL,PRIMARY KEY(root_id,stream_id),UNIQUE(root_id,generation))",
    "CREATE TABLE admissions(root_id TEXT NOT NULL,stream_id TEXT NOT NULL,event_id TEXT NOT NULL,content_hash TEXT NOT NULL,generation INTEGER NOT NULL,sequence INTEGER NOT NULL,PRIMARY KEY(root_id,stream_id,event_id,content_hash),UNIQUE(root_id,stream_id,generation,sequence))",
)


@dataclass(frozen=True)
class SourceIdentity:
    provider: str
    stream_id: str
    generation: int
    sequence: int


class SourceCatalogError(RuntimeError):
    pass


class _SourceCatalogOwner:
    def __init__(self, directory_fd: int, file_fd: int, basename: str) -> None:
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._basename = basename
        verify_anchored_file(file_fd, basename)
        try:
            self._connection = sqlite3.connect(basename, timeout=30, check_same_thread=False)
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._install_schema()
            verify_anchored_file(file_fd, basename)
        except sqlite3.Error as exc:
            raise ChatProjectionStoreError("source_catalog_failed", "source catalog initialization failed") from exc
        self._lock = threading.RLock()

    def _install_schema(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, SCHEMA_VERSION):
            raise SourceCatalogError("unsupported source catalog schema")
        if version == 0:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
                if version == 0:
                    for statement in DDL:
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise SourceCatalogError("unsupported source catalog schema")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        actual = self._connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        expected = sorted(
            (statement.split("(", 1)[0].split()[-1], "".join(statement.lower().split()))
            for statement in DDL
        )
        normalized = sorted((name, "".join((sql or "").lower().split())) for name, sql in actual)
        if normalized != expected:
            raise SourceCatalogError("source catalog schema mismatch")

    @staticmethod
    def _text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
            raise SourceCatalogError(f"invalid {name}")
        return value

    def root_generation(self, root_id: str) -> int:
        self._text("root_id", root_id)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO root_generations VALUES(?,1)", (root_id,),
            )
            return int(self._connection.execute(
                "SELECT generation FROM root_generations WHERE root_id=?", (root_id,),
            ).fetchone()[0])

    def advance_root_generation(self, root_id: str) -> int:
        self._text("root_id", root_id)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO root_generations VALUES(?,1) ON CONFLICT(root_id) DO UPDATE SET generation=generation+1",
                (root_id,),
            )
            return int(self._connection.execute(
                "SELECT generation FROM root_generations WHERE root_id=?", (root_id,),
            ).fetchone()[0])

    def admit(
        self, *, root_id: str, provider: str, stream_id: str,
        event_id: str, content_hash: str,
    ) -> SourceIdentity:
        for name, value in (
            ("root_id", root_id), ("provider", provider), ("stream_id", stream_id),
            ("event_id", event_id), ("content_hash", content_hash),
        ):
            self._text(name, value)
        if provider not in {"claude", "codex", "gemini"}:
            raise SourceCatalogError("unsupported provider")
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise SourceCatalogError("invalid content_hash")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                stream = self._connection.execute(
                    "SELECT provider,generation,next_sequence FROM streams WHERE root_id=? AND stream_id=?",
                    (root_id, stream_id),
                ).fetchone()
                if stream is None:
                    generation = int(self._connection.execute(
                        "SELECT COALESCE(MAX(generation),0)+1 FROM streams WHERE root_id=?",
                        (root_id,),
                    ).fetchone()[0])
                    self._connection.execute(
                        "INSERT INTO streams VALUES(?,?,?,?,1)",
                        (root_id, stream_id, provider, generation),
                    )
                    next_sequence = 1
                else:
                    if str(stream[0]) != provider:
                        raise SourceCatalogError("stream provider conflict")
                    generation, next_sequence = int(stream[1]), int(stream[2])
                existing = self._connection.execute(
                    "SELECT generation,sequence FROM admissions WHERE root_id=? AND stream_id=? AND event_id=? AND content_hash=?",
                    (root_id, stream_id, event_id, content_hash),
                ).fetchone()
                if existing is not None:
                    self._connection.commit()
                    return SourceIdentity(provider, stream_id, int(existing[0]), int(existing[1]))
                self._connection.execute(
                    "INSERT INTO admissions VALUES(?,?,?,?,?,?)",
                    (root_id, stream_id, event_id, content_hash, generation, next_sequence),
                )
                self._connection.execute(
                    "UPDATE streams SET next_sequence=? WHERE root_id=? AND stream_id=?",
                    (next_sequence + 1, root_id, stream_id),
                )
                self._connection.commit()
                return SourceIdentity(provider, stream_id, generation, next_sequence)
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class ChatProjectionSourceCatalog:
    def __init__(self, path: Path | None = None) -> None:
        root = Path(os.path.abspath(ba_home().expanduser()))
        selected = path or root / "chat" / "canonical-source-catalog.sqlite3"
        try:
            self._owner = OwnerClient(
                root_path=root,
                path=Path(os.path.abspath(selected.expanduser())),
                owner_script=Path(__file__),
                owner_arguments=(),
                validate_result=self._validate_result,
                require_sqlite_header=True,
            )
        except ChatProjectionStoreError as exc:
            raise SourceCatalogError(exc.detail) from exc

    @staticmethod
    def _validate_result(operation, result, arguments):
        if operation == "close":
            if result is not None:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid close response")
            return None
        if operation in {"root_generation", "advance_root_generation"}:
            if type(result) is not int or result < 1:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid generation response")
            return result
        if operation == "admit":
            if not isinstance(result, dict) or set(result) != {"provider", "stream_id", "generation", "sequence"}:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid admission response")
            if result["provider"] != arguments["provider"] or result["stream_id"] != arguments["stream_id"]:
                raise ChatProjectionStoreError("owner_protocol_error", "admission correlation mismatch")
            if type(result["generation"]) is not int or type(result["sequence"]) is not int:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid admission coordinates")
            return result
        raise ChatProjectionStoreError("owner_protocol_error", "invalid source operation")

    def root_generation(self, root_id: str) -> int:
        return int(self._owner.rpc("root_generation", root_id=root_id))

    def advance_root_generation(self, root_id: str) -> int:
        return int(self._owner.rpc("advance_root_generation", root_id=root_id))

    def admit(self, **arguments) -> SourceIdentity:
        return SourceIdentity(**self._owner.rpc("admit", **arguments))

    def close(self) -> None:
        self._owner.close()


def _run_owner(channel_fd: int, directory_fd: int, file_fd: int, basename: str) -> None:
    def dispatch(store, operation, arguments, _request_id):
        if operation in {"root_generation", "advance_root_generation"} and set(arguments) == {"root_id"}:
            return getattr(store, operation)(**arguments)
        if operation == "admit" and set(arguments) == {"root_id", "provider", "stream_id", "event_id", "content_hash"}:
            return store.admit(**arguments).__dict__
        if operation == "close" and not arguments:
            return None
        raise ChatProjectionStoreError("owner_protocol_error", "source operation is not allowed")

    serve_owner(
        channel_fd, directory_fd, file_fd, basename, _SourceCatalogOwner,
        dispatch, lambda store: store.close(),
        lambda _channel, _request_id, _operation, result: (result, False), 64 * 1024,
    )


if __name__ == "__main__" and len(__import__("sys").argv) == 6 and __import__("sys").argv[1] == "--projection-owner":
    argv = __import__("sys").argv
    _run_owner(int(argv[2]), int(argv[3]), int(argv[4]), argv[5])
