from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from chat_projection_store import ChatProjectionStoreError
from chat_projection_store_owner import OwnerClient, serve_owner
from chat_projection_store_owner_path import verify_anchored_file
from paths import ba_home


PROVIDERS = frozenset({"claude", "codex", "gemini"})
STORE_KINDS = frozenset({"jsonl", "sqlite"})
SCHEMA_VERSION = 1
DDL = (
    "CREATE TABLE projection_authority("
    "authority_id TEXT PRIMARY KEY,provider TEXT NOT NULL,session_id TEXT NOT NULL UNIQUE,"
    "root_id TEXT NOT NULL UNIQUE,root_generation INTEGER NOT NULL,store_kind TEXT NOT NULL,"
    "CHECK(provider IN ('claude','codex','gemini')),"
    "CHECK(store_kind IN ('jsonl','sqlite')),CHECK(root_generation>=0))"
)
ROW_KEYS = {
    "authority_id", "provider", "session_id", "root_id", "root_generation", "store_kind",
}


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


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectionAuthorityError("invalid_authority", f"{name} is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ProjectionAuthorityError("invalid_authority", f"{name} is invalid") from exc
    if size > 4096:
        raise ProjectionAuthorityError("invalid_authority", f"{name} is invalid")
    return value


def _validate_selection(
    provider: str, session_id: str, root_id: str, root_generation: int, store_kind: str,
) -> None:
    _text("session_id", session_id)
    _text("root_id", root_id)
    if provider not in PROVIDERS or store_kind not in STORE_KINDS:
        raise ProjectionAuthorityError("invalid_authority", "provider or store kind is unsupported")
    if type(root_generation) is not int or not 0 <= root_generation <= 9_223_372_036_854_775_807:
        raise ProjectionAuthorityError("invalid_authority", "root_generation is invalid")


def _authority_id(provider: str, session_id: str, root_id: str) -> str:
    return hashlib.sha256(f"{provider}\0{session_id}\0{root_id}".encode("utf-8")).hexdigest()


class _AuthorityOwner:
    def __init__(
        self, directory_fd: int, file_fd: int, basename: str,
        connect_swap_basename: str | None = None,
    ) -> None:
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._basename = basename
        verify_anchored_file(file_fd, basename)
        if connect_swap_basename is not None:
            os.replace(basename, f"{basename}.anchored")
            os.replace(connect_swap_basename, basename)
        uri = f"file:{quote(basename, safe='')}?mode=rw"
        try:
            self._connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            databases = self._connection.execute("PRAGMA database_list").fetchall()
            main = [row for row in databases if row[1] == "main"]
            if len(main) != 1:
                raise ChatProjectionStoreError(
                    "path_race", "authority database identity is unavailable",
                )
            connected = os.stat(main[0][2], follow_symlinks=False)
            anchored = os.fstat(file_fd)
            if (connected.st_dev, connected.st_ino) != (anchored.st_dev, anchored.st_ino):
                raise ChatProjectionStoreError(
                    "path_race", "authority database changed during SQLite connect",
                )
            verify_anchored_file(file_fd, basename)
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._install_schema()
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
        except ChatProjectionStoreError:
            raise
        except sqlite3.Error as exc:
            raise ChatProjectionStoreError(
                "authority_storage_failed", "authority registry initialization failed",
            ) from exc

    def _install_schema(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, SCHEMA_VERSION):
            raise ChatProjectionStoreError(
                "unsupported_authority_schema", "rebuild the authority registry",
            )
        rows = self._connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not rows:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, SCHEMA_VERSION):
                    raise ChatProjectionStoreError(
                        "unsupported_authority_schema", "rebuild the authority registry",
                    )
                if not rows:
                    self._connection.execute(DDL)
                    self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        self._validate_schema()

    def _validate_schema(self) -> None:
        if int(self._connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise ChatProjectionStoreError(
                "unsupported_authority_schema", "rebuild the authority registry",
            )
        actual = self._connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        expected_sql = "".join(DDL.lower().split())
        if (
            len(actual) != 1
            or actual[0][:3] != ("table", "projection_authority", "projection_authority")
            or "".join((actual[0][3] or "").lower().split()) != expected_sql
        ):
            raise ChatProjectionStoreError(
                "unsupported_authority_schema", "rebuild the authority registry",
            )
        columns = tuple(
            (row[1], row[2].upper(), int(row[3]), int(row[5]))
            for row in self._connection.execute("PRAGMA table_info('projection_authority')")
        )
        if columns != (
            ("authority_id", "TEXT", 0, 1), ("provider", "TEXT", 1, 0),
            ("session_id", "TEXT", 1, 0), ("root_id", "TEXT", 1, 0),
            ("root_generation", "INTEGER", 1, 0), ("store_kind", "TEXT", 1, 0),
        ):
            raise ChatProjectionStoreError(
                "unsupported_authority_schema", "rebuild the authority registry",
            )
        unique_indexes = set()
        for index in self._connection.execute("PRAGMA index_list('projection_authority')"):
            if int(index[2]) != 1:
                continue
            columns = tuple(
                row[2] for row in self._connection.execute(f'PRAGMA index_info("{index[1]}")')
            )
            unique_indexes.add((index[3], columns))
        if unique_indexes != {
            ("pk", ("authority_id",)), ("u", ("session_id",)), ("u", ("root_id",)),
        }:
            raise ChatProjectionStoreError(
                "unsupported_authority_schema", "rebuild the authority registry",
            )

    @staticmethod
    def _row(row: tuple) -> dict[str, Any]:
        return dict(zip((
            "authority_id", "provider", "session_id", "root_id", "root_generation", "store_kind",
        ), row))

    def register(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        verify_anchored_file(self._file_fd, self._basename)
        expected = (
            arguments["authority_id"], arguments["provider"], arguments["session_id"],
            arguments["root_id"], arguments["root_generation"], arguments["store_kind"],
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT authority_id,provider,session_id,root_id,root_generation,store_kind "
                "FROM projection_authority WHERE session_id=? OR root_id=?",
                (arguments["session_id"], arguments["root_id"]),
            ).fetchall()
            if rows:
                self._connection.rollback()
                if len(rows) != 1:
                    raise ChatProjectionStoreError(
                        "mixed_authority", "session and root resolve differently",
                    )
                if rows[0] != expected:
                    raise ChatProjectionStoreError(
                        "authority_conflict", "authority is already assigned",
                    )
                return self._row(rows[0])
            self._connection.execute(
                "INSERT INTO projection_authority VALUES(?,?,?,?,?,?)", expected,
            )
            self._connection.commit()
            return self._row(expected)
        except ChatProjectionStoreError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ChatProjectionStoreError(
                "authority_busy" if "locked" in str(exc).lower() else "authority_storage_failed",
                "authority registration failed",
            ) from exc

    def require(self, authority_id: str) -> dict[str, Any]:
        verify_anchored_file(self._file_fd, self._basename)
        try:
            row = self._connection.execute(
                "SELECT authority_id,provider,session_id,root_id,root_generation,store_kind "
                "FROM projection_authority WHERE authority_id=?", (authority_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ChatProjectionStoreError(
                "authority_storage_failed", "authority lookup failed",
            ) from exc
        if row is None:
            raise ChatProjectionStoreError("authority_missing", "authority is not registered")
        return self._row(row)

    def close(self) -> None:
        self._connection.close()


class ProjectionAuthorityRegistry:
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(
        self, path: Path | None = None, *, _test_connect_swap_basename: str | None = None,
    ) -> None:
        root = Path(os.path.abspath(ba_home().expanduser()))
        self._projection_root = root / "chat" / "canonical-projections"
        selected = path or root / "chat" / "projection-authority.sqlite3"
        selected = Path(os.path.abspath(selected.expanduser()))
        if _test_connect_swap_basename is not None and (
            not _test_connect_swap_basename
            or Path(_test_connect_swap_basename).name != _test_connect_swap_basename
        ):
            raise ProjectionAuthorityError("invalid_authority", "connect swap basename is invalid")
        try:
            self._owner = OwnerClient(
                root_path=root, path=selected, owner_script=Path(__file__),
                owner_arguments=(_test_connect_swap_basename or "none",),
                validate_result=self._validate_result, require_sqlite_header=True,
            )
        except ChatProjectionStoreError as exc:
            raise ProjectionAuthorityError(exc.code, exc.detail) from exc
        self._closed = False
        self._lock = threading.RLock()

    @staticmethod
    def _validate_result(operation: str, result: Any, arguments: Mapping[str, Any]) -> Any:
        if operation == "close":
            if result is not None or arguments:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid close result")
            return None
        if operation not in {"register", "require"} or not isinstance(result, Mapping):
            raise ChatProjectionStoreError("owner_protocol_error", "invalid authority result")
        if set(result) != ROW_KEYS:
            raise ChatProjectionStoreError("owner_protocol_error", "invalid authority result shape")
        if (
            not all(isinstance(result[key], str) for key in ROW_KEYS - {"root_generation"})
            or type(result["root_generation"]) is not int
        ):
            raise ChatProjectionStoreError("owner_protocol_error", "invalid authority result values")
        if operation == "register" and any(result[key] != arguments[key] for key in ROW_KEYS):
            raise ChatProjectionStoreError("owner_protocol_error", "authority result mismatch")
        if operation == "require" and result["authority_id"] != arguments["authority_id"]:
            raise ChatProjectionStoreError("owner_protocol_error", "authority result mismatch")
        return dict(result)

    def _selection(self, row: Mapping[str, Any]) -> ProjectionAuthority:
        suffix = "jsonl" if row["store_kind"] == "jsonl" else "sqlite3"
        path = self._projection_root / f"{row['authority_id']}.{suffix}"
        return ProjectionAuthority(
            row["authority_id"], row["provider"], row["session_id"], row["root_id"],
            row["root_generation"], row["store_kind"], path,
        )

    @staticmethod
    def _translate(exc: ChatProjectionStoreError) -> ProjectionAuthorityError:
        return ProjectionAuthorityError(exc.code, exc.detail)

    def register(
        self, *, provider: str, session_id: str, root_id: str,
        root_generation: int, store_kind: str,
    ) -> ProjectionAuthority:
        _validate_selection(provider, session_id, root_id, root_generation, store_kind)
        arguments = {
            "authority_id": _authority_id(provider, session_id, root_id), "provider": provider,
            "session_id": session_id, "root_id": root_id, "root_generation": root_generation,
            "store_kind": store_kind,
        }
        with self._lock:
            self._ensure_open()
            try:
                return self._selection(self._owner.rpc("register", **arguments))
            except ChatProjectionStoreError as exc:
                raise self._translate(exc) from exc

    def require(self, authority: ProjectionAuthority) -> ProjectionAuthority:
        if not isinstance(authority, ProjectionAuthority):
            raise ProjectionAuthorityError("invalid_authority", "authority capability is invalid")
        with self._lock:
            self._ensure_open()
            try:
                current = self._selection(self._owner.rpc(
                    "require", authority_id=authority.authority_id,
                ))
            except ChatProjectionStoreError as exc:
                raise self._translate(exc) from exc
        if current != authority:
            raise ProjectionAuthorityError(
                "authority_mismatch", "authority capability is stale or mixed",
            )
        return current

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProjectionAuthorityError("authority_closed", "authority registry is closed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._owner.close()
            except ChatProjectionStoreError as exc:
                raise self._translate(exc) from exc


def _run_owner(
    channel_fd: int, directory_fd: int, file_fd: int, basename: str,
    connect_swap_basename: str,
) -> None:
    def dispatch(store: _AuthorityOwner, operation: str, arguments: Mapping[str, Any], _request_id: int):
        if operation == "register" and set(arguments) == ROW_KEYS:
            return store.register(arguments)
        if operation == "require" and set(arguments) == {"authority_id"}:
            return store.require(arguments["authority_id"])
        if operation == "close" and not arguments:
            return None
        raise ChatProjectionStoreError("owner_protocol_error", "operation is not allowed")
    serve_owner(
        channel_fd, directory_fd, file_fd, basename,
        lambda owner_directory_fd, owner_file_fd, owner_basename: _AuthorityOwner(
            owner_directory_fd, owner_file_fd, owner_basename,
            None if connect_swap_basename == "none" else connect_swap_basename,
        ),
        dispatch, lambda store: store.close(),
        lambda _channel, _request_id, _operation, result: (result, False), 64 * 1024,
    )


if __name__ == "__main__" and len(sys.argv) == 7 and sys.argv[1] == "--projection-owner":
    _run_owner(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6])
