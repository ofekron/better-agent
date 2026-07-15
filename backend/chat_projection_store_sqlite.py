from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from chat_projection_store_owner import (
    DEFAULT_FRAME_BYTES, DEFAULT_TIMEOUT_SECONDS, MAX_REQUEST_ID, MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS, OwnerClient, encode_frame, send_frame, serve_owner,
)
from chat_projection_store_owner_path import verify_anchored_file

from chat_projection_store import (
    ChatProjectionStoreError, CommitResult, ProjectionCommit, SourceAdmission, SourceWatermark, StoredFact,
    StoredProjection, StoredRevision, TurnManifest,
)
from paths import ba_home


SCHEMA_VERSION = 3
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_READ_LIMIT = 10_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_JSON_LIST_ITEMS = 50_000
MAX_JSON_OBJECT_ITEMS = 50_000
MAX_TEXT_BYTES = 4_096
MAX_COMMIT_BYTES = MAX_JSON_BYTES
MAX_SQLITE_INTEGER = MAX_REQUEST_ID
MAX_IPC_BYTES = DEFAULT_FRAME_BYTES
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
IPC_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS
STARTUP_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS
MIN_IPC_TIMEOUT_SECONDS = MIN_TIMEOUT_SECONDS
MAX_IPC_TIMEOUT_SECONDS = MAX_TIMEOUT_SECONDS

TABLE_DDL = {
    "selected_roots": "CREATE TABLE selected_roots(root_id TEXT PRIMARY KEY, root_generation INTEGER NOT NULL)",
    "root_generation_heads": "CREATE TABLE root_generation_heads(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, fact_sequence INTEGER NOT NULL, revision INTEGER NOT NULL, projection_cursor INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation))",
    "canonical_facts": "CREATE TABLE canonical_facts(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, fact_sequence INTEGER NOT NULL, event_id TEXT NOT NULL, content_hash TEXT NOT NULL, fact_json TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,fact_sequence), UNIQUE(root_id,root_generation,event_id,content_hash))",
    "render_nodes": "CREATE TABLE render_nodes(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, event_id TEXT NOT NULL, node_json TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,event_id))",
    "ownership": "CREATE TABLE ownership(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, event_id TEXT NOT NULL, turn_id TEXT NOT NULL, message_id TEXT, parent_event_id TEXT, owner_scope TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,event_id))",
    "turn_manifests": "CREATE TABLE turn_manifests(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, turn_id TEXT NOT NULL, event_count INTEGER NOT NULL, direct_child_count INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation,turn_id))",
    "revisions": "CREATE TABLE revisions(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, revision INTEGER NOT NULL, fact_sequence INTEGER NOT NULL, visible_delta_json TEXT NOT NULL, historical_json TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,revision))",
    "source_watermarks": "CREATE TABLE source_watermarks(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, stream_id TEXT NOT NULL, source_generation INTEGER NOT NULL, source_sequence INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation,stream_id))",
    "source_admissions": "CREATE TABLE source_admissions(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, stream_id TEXT NOT NULL, source_generation INTEGER NOT NULL, source_sequence INTEGER NOT NULL, event_id TEXT NOT NULL, content_hash TEXT NOT NULL, fact_sequence INTEGER NOT NULL, revision INTEGER NOT NULL, projection_cursor INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation,stream_id,source_generation,source_sequence))",
}
AUTOINDEX_COUNTS = {
    "selected_roots": 1, "root_generation_heads": 1, "canonical_facts": 2,
    "render_nodes": 1, "ownership": 1, "turn_manifests": 1, "revisions": 1,
    "source_watermarks": 1,
    "source_admissions": 1,
}


def _validate_json(value: Any) -> None:
    active: set[int] = set()
    nodes = 0
    stack: list[tuple[str, Any, int]] = [("visit", value, 0)]
    while stack:
        action, current, depth = stack.pop()
        if action == "leave":
            active.remove(id(current))
            continue
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ChatProjectionStoreError("json_node_limit", "JSON node limit exceeded")
        if depth > MAX_JSON_DEPTH:
            raise ChatProjectionStoreError("json_depth_limit", "JSON depth limit exceeded")
        if isinstance(current, Mapping):
            if len(current) > MAX_JSON_OBJECT_ITEMS:
                raise ChatProjectionStoreError("json_object_limit", "JSON object limit exceeded")
            if id(current) in active:
                raise ChatProjectionStoreError("json_cycle", "JSON payload contains a cycle")
            if any(not isinstance(key, str) for key in current):
                raise ChatProjectionStoreError("invalid_json", "JSON object keys must be strings")
            active.add(id(current))
            stack.append(("leave", current, depth))
            stack.extend(("visit", nested, depth + 1) for nested in reversed(tuple(current.values())))
            continue
        if isinstance(current, list):
            if len(current) > MAX_JSON_LIST_ITEMS:
                raise ChatProjectionStoreError("json_list_limit", "JSON array limit exceeded")
            if id(current) in active:
                raise ChatProjectionStoreError("json_cycle", "JSON payload contains a cycle")
            active.add(id(current))
            stack.append(("leave", current, depth))
            stack.extend(("visit", nested, depth + 1) for nested in reversed(current))
            continue
        if current is None or isinstance(current, (str, bool)) or type(current) is int:
            continue
        if isinstance(current, float) and current == current and current not in (float("inf"), float("-inf")):
            continue
        raise ChatProjectionStoreError("invalid_json", "payload contains a non-JSON value")


def _translate_sqlite(code: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except ChatProjectionStoreError:
                raise
            except UnicodeError as exc:
                raise ChatProjectionStoreError("invalid_input", "text is not valid UTF-8") from exc
            except sqlite3.Error as exc:
                raise ChatProjectionStoreError(code, "SQLite projection store operation failed") from exc
        return wrapped
    return decorate


def canonical_json(value: Mapping[str, Any]) -> str:
    _validate_json(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ChatProjectionStoreError("invalid_json", "payload must be canonical JSON") from exc
    try:
        encoded_bytes = encoded.encode("utf-8")
    except UnicodeError as exc:
        raise ChatProjectionStoreError("invalid_input", "text is not valid UTF-8") from exc
    if len(encoded_bytes) > MAX_JSON_BYTES:
        raise ChatProjectionStoreError("payload_too_large", "payload exceeds store admission limit")
    return encoded


_encode_json_bounded = encode_frame
_send_frame = send_frame


class SQLiteChatProjectionStore:
    _TABLES = {
        "selected_roots": (
            ("root_id", "TEXT", 0, 1, None), ("root_generation", "INTEGER", 1, 0, None),
        ),
        "root_generation_heads": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("fact_sequence", "INTEGER", 1, 0, None), ("revision", "INTEGER", 1, 0, None),
            ("projection_cursor", "INTEGER", 1, 0, None),
        ),
        "canonical_facts": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("fact_sequence", "INTEGER", 1, 3, None), ("event_id", "TEXT", 1, 0, None),
            ("content_hash", "TEXT", 1, 0, None), ("fact_json", "TEXT", 1, 0, None),
        ),
        "render_nodes": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("event_id", "TEXT", 1, 3, None), ("node_json", "TEXT", 1, 0, None),
        ),
        "ownership": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("event_id", "TEXT", 1, 3, None), ("turn_id", "TEXT", 1, 0, None),
            ("message_id", "TEXT", 0, 0, None), ("parent_event_id", "TEXT", 0, 0, None),
            ("owner_scope", "TEXT", 1, 0, None),
        ),
        "turn_manifests": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("turn_id", "TEXT", 1, 3, None), ("event_count", "INTEGER", 1, 0, None),
            ("direct_child_count", "INTEGER", 1, 0, None),
        ),
        "revisions": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("revision", "INTEGER", 1, 3, None), ("fact_sequence", "INTEGER", 1, 0, None),
            ("visible_delta_json", "TEXT", 1, 0, None),
            ("historical_json", "TEXT", 1, 0, None),
        ),
        "source_watermarks": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("stream_id", "TEXT", 1, 3, None), ("source_generation", "INTEGER", 1, 0, None),
            ("source_sequence", "INTEGER", 1, 0, None),
        ),
        "source_admissions": (
            ("root_id", "TEXT", 1, 1, None), ("root_generation", "INTEGER", 1, 2, None),
            ("stream_id", "TEXT", 1, 3, None), ("source_generation", "INTEGER", 1, 4, None),
            ("source_sequence", "INTEGER", 1, 5, None), ("event_id", "TEXT", 1, 0, None),
            ("content_hash", "TEXT", 1, 0, None), ("fact_sequence", "INTEGER", 1, 0, None),
            ("revision", "INTEGER", 1, 0, None), ("projection_cursor", "INTEGER", 1, 0, None),
        ),
    }
    _UNIQUE_INDEXES = {
        "canonical_facts": {("root_id", "root_generation", "event_id", "content_hash")},
    }
    @_translate_sqlite("storage_init_failed")
    def __init__(
        self,
        path: Path | None = None,
        *,
        before_commit: Callable[[], None] | None = None,
        after_commit: Callable[[], None] | None = None,
        _before_transaction_commit: Callable[[sqlite3.Connection], None] | None = None,
        _extra_table_ddl: Mapping[str, str] | None = None,
        _extra_table_specs: Mapping[str, tuple[tuple[str, str, int, int, str | None], ...]] | None = None,
        _extra_schema_objects: Mapping[str, tuple[str, str]] | None = None,
        _owner_directory_fd: int | None = None,
        _owner_file_fd: int | None = None,
        _owner_basename: str | None = None,
        _ipc_timeout_seconds: float = IPC_TIMEOUT_SECONDS,
        _startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
        _test_owner_fault: str | None = None,
    ) -> None:
        self._owner_client = _owner_directory_fd is None
        self._before_commit = before_commit
        self._after_commit = after_commit
        self._before_transaction_commit = _before_transaction_commit
        self._table_ddl = {**TABLE_DDL, **(_extra_table_ddl or {})}
        self._TABLES = {**type(self)._TABLES, **(_extra_table_specs or {})}
        self._extra_schema_objects = dict(_extra_schema_objects or {})
        self._lock = threading.RLock()
        self._closed = False
        self._ipc_timeout_seconds = _ipc_timeout_seconds
        self._startup_timeout_seconds = _startup_timeout_seconds
        self._test_owner_fault = _test_owner_fault
        if self._owner_client:
            self._start_owner(path)
            return
        self._store_path = Path(_owner_basename)
        self._connection = None
        self._owner_identity_fd = _owner_file_fd
        self._owner_basename = _owner_basename
        self._file_checkpoint()
        uri = f"file:{quote(_owner_basename, safe='')}?mode=rw"
        self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._verify_owner_file(_owner_file_fd, _owner_basename)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._install_schema()

    def _start_owner(self, path: Path | None) -> None:
        if self._test_owner_fault not in (
            None, "post_commit_stop", "malformed_response", "semantic_mismatch",
            "malformed_commit_response", "revision_pair_mismatch",
        ):
            raise ChatProjectionStoreError("invalid_input", "unknown owner test fault")
        self._connection = None
        root = Path(os.path.abspath(ba_home().expanduser()))
        selected = path or root / "chat" / "selected.sqlite3"
        self._owner = OwnerClient(
            root_path=root, path=selected, owner_script=Path(__file__),
            owner_arguments=(self._test_owner_fault or "none",),
            validate_result=self._validate_rpc_result,
            ipc_timeout_seconds=self._ipc_timeout_seconds,
            startup_timeout_seconds=self._startup_timeout_seconds,
            max_error_text_bytes=MAX_TEXT_BYTES,
        )

    @property
    def _path(self) -> Path:
        return self._owner.path if self._owner_client else self._store_path

    @property
    def _process(self):
        return self._owner.process

    @staticmethod
    def _verify_owner_file(file_fd: int, basename: str) -> None:
        verify_anchored_file(file_fd, basename)

    def _file_checkpoint(self) -> None:
        self._verify_owner_file(self._owner_identity_fd, self._owner_basename)

    def _rpc(self, operation: str, **arguments: Any) -> Any:
        return self._owner.rpc(operation, **arguments)

    def _validate_rpc_result(
        self, operation: str, result: Any, arguments: Mapping[str, Any],
    ) -> Any:
        if operation in {"select_generation", "delete_generation", "delete_root", "close"}:
            if result is not None:
                raise ChatProjectionStoreError("owner_protocol_error", "owner result must be null")
            return None
        if operation == "projection_cursor":
            self._wire_mapping(result, {"root_id", "root_generation", "projection_cursor"})
            self._wire_correlation(result, arguments, ("root_id", "root_generation"))
            return self._wire_integer(result["projection_cursor"])
        if operation == "commit":
            self._wire_mapping(result, {
                "duplicate", "fact_sequence", "revision", "projection_cursor", "root_id",
                "root_generation", "event_id", "content_hash", "source_generation",
                "source_sequence",
            })
            if type(result["duplicate"]) is not bool:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid commit duplicate flag")
            for key in (
                "fact_sequence", "revision", "projection_cursor", "root_generation",
                "source_generation", "source_sequence",
            ):
                self._wire_integer(result[key])
            request = arguments["request"]
            for key in ("root_id", "root_generation", "event_id", "content_hash"):
                if result[key] != request[key]:
                    raise ChatProjectionStoreError("owner_protocol_error", "commit correlation mismatch")
            if (
                result["source_generation"] != request["watermark"]["generation"]
                or result["source_sequence"] != request["watermark"]["sequence"]
            ):
                raise ChatProjectionStoreError("owner_protocol_error", "commit sequence mismatch")
            if result["revision"] != result["projection_cursor"]:
                raise ChatProjectionStoreError("owner_protocol_error", "commit head mismatch")
            if result["duplicate"]:
                if not 1 <= result["fact_sequence"] <= result["projection_cursor"]:
                    raise ChatProjectionStoreError("owner_protocol_error", "duplicate fact sequence mismatch")
            elif result["fact_sequence"] != result["projection_cursor"]:
                raise ChatProjectionStoreError("owner_protocol_error", "inserted fact sequence mismatch")
            return {key: result[key] for key in ("duplicate", "fact_sequence", "revision", "projection_cursor")}
        if operation in {"read_facts", "read_revisions"}:
            self._wire_mapping(result, {
                "root_id", "root_generation", "after", "projection_cursor", "rows",
            })
            self._wire_correlation(result, arguments, ("root_id", "root_generation", "after"))
            rows = result["rows"]
            cursor = self._wire_integer(result["projection_cursor"])
            if not isinstance(rows, list) or len(rows) > arguments["limit"]:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid bounded read result")
            expected = (
                {"root_id", "root_generation", "fact_sequence", "event_id", "content_hash", "canonical_fact"}
                if operation == "read_facts" else
                {"root_id", "root_generation", "revision", "fact_sequence", "event_id", "content_hash", "visible_delta", "historical_revision"}
            )
            previous = arguments["after"]
            for row in rows:
                self._wire_mapping(row, expected)
                self._wire_correlation(row, arguments, ("root_id", "root_generation"))
                for key in expected & {"fact_sequence", "revision"}:
                    self._wire_integer(row[key])
                for key in expected & {"event_id", "content_hash"}:
                    self._wire_text(row[key])
                if "content_hash" in row and (
                    len(row["content_hash"]) != 64
                    or any(character not in "0123456789abcdef" for character in row["content_hash"])
                ):
                    raise ChatProjectionStoreError("owner_protocol_error", "owner hash is invalid")
                for key in expected & {"canonical_fact", "visible_delta", "historical_revision"}:
                    self._wire_json(row[key])
                sequence_key = "fact_sequence" if operation == "read_facts" else "revision"
                if row[sequence_key] <= previous:
                    raise ChatProjectionStoreError("owner_protocol_error", "owner rows are not strictly ordered")
                previous = row[sequence_key]
                if operation == "read_facts":
                    expected_hash = hashlib.sha256(canonical_json(row["canonical_fact"]).encode("utf-8")).hexdigest()
                    if row["content_hash"] != expected_hash or row["canonical_fact"].get("event_id") != row["event_id"]:
                        raise ChatProjectionStoreError("owner_protocol_error", "owner fact hash mismatch")
                else:
                    if row["revision"] != row["fact_sequence"]:
                        raise ChatProjectionStoreError(
                            "owner_protocol_error", "owner revision fact pairing mismatch",
                        )
                    self._validate_delta_identity(
                        row["visible_delta"], row["historical_revision"], row["event_id"],
                        row["content_hash"], "owner_protocol_error",
                    )
            if rows and previous > cursor:
                raise ChatProjectionStoreError("owner_protocol_error", "owner cursor precedes page")
            transport_only = {"root_id", "root_generation"}
            if operation == "read_revisions":
                transport_only |= {"event_id", "content_hash"}
            return [
                {key: value for key, value in row.items() if key not in transport_only}
                for row in rows
            ]
        if operation == "read_projection":
            self._wire_mapping(result, {"root_id", "root_generation", "event_id", "projection"})
            self._wire_correlation(result, arguments, ("root_id", "root_generation", "event_id"))
            result = result["projection"]
            if result is None:
                return None
            self._wire_mapping(result, {
                "event_id", "render_node", "turn_id", "message_id", "parent_event_id",
                "owner_scope", "manifest",
            })
            for key in ("event_id", "turn_id", "owner_scope"):
                self._wire_text(result[key])
            if result["event_id"] != arguments["event_id"]:
                raise ChatProjectionStoreError("owner_protocol_error", "owner projection id mismatch")
            for key in ("message_id", "parent_event_id"):
                if result[key] is not None:
                    self._wire_text(result[key])
            self._wire_json(result["render_node"])
            self._wire_mapping(result["manifest"], {"turn_id", "event_count", "direct_child_count"})
            self._wire_text(result["manifest"]["turn_id"])
            self._wire_integer(result["manifest"]["event_count"])
            self._wire_integer(result["manifest"]["direct_child_count"])
            if result["manifest"]["turn_id"] != result["turn_id"]:
                raise ChatProjectionStoreError("owner_protocol_error", "owner manifest ownership mismatch")
            return result
        if operation == "source_watermark":
            self._wire_mapping(result, {
                "root_id", "root_generation", "stream_id", "watermark",
            })
            self._wire_correlation(result, arguments, ("root_id", "root_generation", "stream_id"))
            result = result["watermark"]
            if result is None:
                return None
            self._wire_mapping(result, {"stream_id", "generation", "sequence"})
            self._wire_text(result["stream_id"])
            if result["stream_id"] != arguments["stream_id"]:
                raise ChatProjectionStoreError("owner_protocol_error", "owner stream id mismatch")
            self._wire_integer(result["generation"])
            self._wire_integer(result["sequence"])
            return result
        if operation == "source_admission":
            self._wire_mapping(result, {
                "root_id", "root_generation", "stream_id", "source_generation",
                "source_sequence", "admission",
            })
            self._wire_correlation(
                result, arguments,
                ("root_id", "root_generation", "stream_id", "source_generation", "source_sequence"),
            )
            admission = result["admission"]
            if admission is None:
                return None
            self._wire_mapping(admission, {
                "event_id", "content_hash", "fact_sequence", "revision", "projection_cursor",
            })
            self._wire_text(admission["event_id"])
            self._wire_text(admission["content_hash"])
            for key in ("fact_sequence", "revision", "projection_cursor"):
                self._wire_integer(admission[key])
            return admission
        raise ChatProjectionStoreError("owner_protocol_error", "unknown owner result operation")

    @staticmethod
    def _wire_correlation(
        result: Mapping[str, Any], arguments: Mapping[str, Any], keys: tuple[str, ...],
    ) -> None:
        if any(result[key] != arguments[key] for key in keys):
            raise ChatProjectionStoreError("owner_protocol_error", "owner result correlation mismatch")

    @staticmethod
    def _wire_mapping(value: Any, keys: set[str]) -> None:
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ChatProjectionStoreError("owner_protocol_error", "owner result shape is invalid")

    @staticmethod
    def _wire_integer(value: Any) -> int:
        if type(value) is not int or not 0 <= value <= MAX_SQLITE_INTEGER:
            raise ChatProjectionStoreError("owner_protocol_error", "owner integer is invalid")
        return value

    @staticmethod
    def _wire_text(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ChatProjectionStoreError("owner_protocol_error", "owner text is invalid")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ChatProjectionStoreError("owner_protocol_error", "owner text is invalid") from exc
        if size > MAX_TEXT_BYTES:
            raise ChatProjectionStoreError("owner_protocol_error", "owner text exceeds limit")
        return value

    @staticmethod
    def _wire_json(value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ChatProjectionStoreError("owner_protocol_error", "owner JSON object is invalid")
        try:
            _validate_json(value)
            canonical_json(value)
        except ChatProjectionStoreError as exc:
            raise ChatProjectionStoreError("owner_protocol_error", "owner JSON object is invalid") from exc
        return value

    def _install_schema(self) -> None:
        existing_tables = {
            row[0] for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if existing_tables and (version != SCHEMA_VERSION or existing_tables != set(self._TABLES)):
            self.close()
            raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
        if not existing_tables and version not in (0, SCHEMA_VERSION):
            self.close()
            raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
        install_sql = ";".join(
            sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
            for sql in self._table_ddl.values()
        )
        self._connection.executescript(f"{install_sql};")
        if self._extra_schema_objects:
            existing = {
                row[0] for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            missing = [
                ddl for name, (_table, ddl) in self._extra_schema_objects.items() if name not in existing
            ]
            if missing:
                self._connection.executescript(";".join(missing) + ";")
        self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._file_checkpoint()
        self._connection.commit()
        try:
            self._validate_schema()
        except ChatProjectionStoreError:
            self.close()
            raise

    def _validate_schema(self) -> None:
        self._validate_schema_connection(
            self._connection, table_ddl=self._table_ddl, table_specs=self._TABLES,
            extra_schema_objects=self._extra_schema_objects,
            unique_indexes=self._UNIQUE_INDEXES, autoindex_counts=AUTOINDEX_COUNTS,
        )

    @classmethod
    def _validate_schema_connection(
        cls, connection: sqlite3.Connection, *, table_ddl: Mapping[str, str],
        table_specs: Mapping[str, tuple[tuple[str, str, int, int, str | None], ...]],
        extra_schema_objects: Mapping[str, tuple[str, str]],
        unique_indexes: Mapping[str, set[tuple[str, ...]]],
        autoindex_counts: Mapping[str, int],
    ) -> None:
        expected_objects = cls._expected_schema_objects_for(
            table_ddl, extra_schema_objects, autoindex_counts,
        )
        rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "OR name LIKE 'sqlite_autoindex_%'"
        ).fetchall()
        actual_objects = {
            (row[0], row[1], row[2], cls._normalize_sql(row[3])) for row in rows
            if row[0] in ("table", "index", "trigger", "view")
        }
        if actual_objects != expected_objects:
            raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
        for table, expected in table_specs.items():
            if connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            actual = tuple((row[1], row[2].upper(), int(row[3]), int(row[5]), row[4]) for row in rows)
            if actual != expected:
                raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
            expected_pk = tuple(name for name, _, _, pk, _ in sorted(expected, key=lambda item: item[3]) if pk)
            indexes = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            unique_columns = set()
            for index in indexes:
                if int(index[2]) != 1:
                    continue
                columns = tuple(
                    row[2] for row in connection.execute(
                        f'PRAGMA index_info("{index[1]}")'
                    ).fetchall()
                )
                if index[3] == "pk":
                    if columns != expected_pk:
                        raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
                else:
                    unique_columns.add(columns)
            if unique_columns != unique_indexes.get(table, set()):
                raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")

    @staticmethod
    def _normalize_sql(sql: str | None) -> str | None:
        if sql is None:
            return None
        return "".join(sql.lower().split())

    def _expected_schema_objects(self) -> set[tuple[str, str, str, str | None]]:
        return self._expected_schema_objects_for(
            self._table_ddl, self._extra_schema_objects, AUTOINDEX_COUNTS,
        )

    @classmethod
    def _expected_schema_objects_for(
        cls, table_ddl: Mapping[str, str], extra_schema_objects: Mapping[str, tuple[str, str]],
        autoindex_counts: Mapping[str, int],
    ) -> set[tuple[str, str, str, str | None]]:
        objects = {
            ("table", name, name, cls._normalize_sql(sql)) for name, sql in table_ddl.items()
        }
        for table, count in autoindex_counts.items():
            for number in range(1, count + 1):
                objects.add(("index", f"sqlite_autoindex_{table}_{number}", table, None))
        for name, (table, ddl) in extra_schema_objects.items():
            objects.add(("trigger", name, table, cls._normalize_sql(ddl)))
        return objects

    @_translate_sqlite("storage_write_failed")
    def select_generation(self, root_id: str, root_generation: int) -> None:
        self._identity(root_id, root_generation)
        if self._owner_client:
            self._rpc("select_generation", root_id=root_id, root_generation=root_generation)
            return
        with self._lock:
            try:
                self._file_checkpoint()
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT root_generation FROM selected_roots WHERE root_id=?", (root_id,),
                ).fetchone()
                current_generation = self._stored_int(row[0]) if row else None
                if current_generation is not None and root_generation < current_generation:
                    raise ChatProjectionStoreError("stale_generation", "root generation is fenced")
                if row is None:
                    self._connection.execute("INSERT INTO selected_roots VALUES(?,?)", (root_id, root_generation))
                elif root_generation > current_generation:
                    self._connection.execute(
                        "UPDATE selected_roots SET root_generation=? WHERE root_id=? AND root_generation<?",
                        (root_generation, root_id, root_generation),
                    )
                self._connection.execute(
                    "INSERT OR IGNORE INTO root_generation_heads VALUES(?,?,?,?,?)",
                    (root_id, root_generation, 0, 0, 0),
                )
                self._file_checkpoint()
                if self._before_transaction_commit:
                    self._before_transaction_commit(self._connection)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    @_translate_sqlite("storage_write_failed")
    def commit(self, request: ProjectionCommit) -> CommitResult:
        if self._owner_client:
            self._validate_commit(request)
            if self._before_commit:
                self._before_commit()
            result = self._rpc("commit", request=self._commit_to_dict(request))
            if self._after_commit:
                self._after_commit()
            return CommitResult(**result)
        self._validate_commit(request)
        fact_json = canonical_json(request.canonical_fact)
        node_json = canonical_json(request.render_node)
        delta_json = canonical_json(request.visible_delta)
        historical_json = canonical_json(request.historical_revision)
        text_values = (
            request.root_id, request.event_id, request.content_hash, request.turn_id,
            request.message_id or "", request.parent_event_id or "", request.owner_scope,
            request.manifest.turn_id, request.watermark.stream_id,
        )
        aggregate_bytes = sum(len(value.encode("utf-8")) for value in text_values)
        aggregate_bytes += sum(
            len(value.encode("utf-8")) for value in (fact_json, node_json, delta_json, historical_json)
        )
        if aggregate_bytes > MAX_COMMIT_BYTES:
            raise ChatProjectionStoreError("commit_too_large", "aggregate commit limit exceeded")
        with self._lock:
            try:
                self._file_checkpoint()
                self._connection.execute("BEGIN IMMEDIATE")
                result = self._commit_transaction(
                    request, fact_json, node_json, delta_json, historical_json,
                )
                if self._before_commit:
                    self._before_commit()
                self._file_checkpoint()
                if self._before_transaction_commit:
                    self._before_transaction_commit(self._connection)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        if self._after_commit:
            self._after_commit()
        return result

    def _commit_transaction(
        self, request: ProjectionCommit, fact_json: str, node_json: str,
        delta_json: str, historical_json: str,
    ) -> CommitResult:
        head = self._connection.execute(
            "SELECT h.root_generation,h.fact_sequence,h.revision,h.projection_cursor "
            "FROM selected_roots s JOIN root_generation_heads h USING(root_id,root_generation) "
            "WHERE s.root_id=?", (request.root_id,),
        ).fetchone()
        if head is None or self._stored_int(head[0]) != request.root_generation:
            raise ChatProjectionStoreError("stale_generation", "root generation is not selected")
        admission = self._connection.execute(
            "SELECT event_id,content_hash,fact_sequence,revision,projection_cursor "
            "FROM source_admissions WHERE root_id=? AND root_generation=? AND stream_id=? "
            "AND source_generation=? AND source_sequence=?",
            (
                request.root_id, request.root_generation, request.watermark.stream_id,
                request.watermark.generation, request.watermark.sequence,
            ),
        ).fetchone()
        if admission is not None:
            if (self._stored_text(admission[0]), self._stored_text(admission[1])) != (
                request.event_id, request.content_hash,
            ):
                raise ChatProjectionStoreError(
                    "source_conflict", "source sequence carries different content",
                )
            return CommitResult(
                True, self._stored_int(admission[2]), self._stored_int(admission[3]),
                self._stored_int(admission[4]),
            )
        duplicate = self._connection.execute(
            "SELECT fact_sequence FROM canonical_facts WHERE root_id=? AND root_generation=? "
            "AND event_id=? AND content_hash=?",
            (request.root_id, request.root_generation, request.event_id, request.content_hash),
        ).fetchone()
        self._advance_watermark(request)
        if duplicate:
            result = CommitResult(
                True, self._stored_int(duplicate[0]), self._stored_int(head[2]),
                self._stored_int(head[3]),
            )
            self._record_source_admission(request, result)
            return result
        fact_sequence = self._increment_stored(head[1], "fact sequence")
        revision = self._increment_stored(head[2], "revision")
        cursor = self._increment_stored(head[3], "projection cursor")
        values = (request.root_id, request.root_generation)
        self._connection.execute(
            "INSERT INTO canonical_facts VALUES(?,?,?,?,?,?)",
            (*values, fact_sequence, request.event_id, request.content_hash, fact_json),
        )
        self._connection.execute(
            "INSERT INTO render_nodes VALUES(?,?,?,?) ON CONFLICT(root_id,root_generation,event_id) "
            "DO UPDATE SET node_json=excluded.node_json",
            (*values, request.event_id, node_json),
        )
        self._connection.execute(
            "INSERT INTO ownership VALUES(?,?,?,?,?,?,?) ON CONFLICT(root_id,root_generation,event_id) "
            "DO UPDATE SET turn_id=excluded.turn_id,message_id=excluded.message_id,"
            "parent_event_id=excluded.parent_event_id,owner_scope=excluded.owner_scope",
            (*values, request.event_id, request.turn_id, request.message_id,
             request.parent_event_id, request.owner_scope),
        )
        self._connection.execute(
            "INSERT INTO turn_manifests VALUES(?,?,?,?,?) ON CONFLICT(root_id,root_generation,turn_id) "
            "DO UPDATE SET event_count=excluded.event_count,direct_child_count=excluded.direct_child_count",
            (*values, request.manifest.turn_id, request.manifest.event_count,
             request.manifest.direct_child_count),
        )
        self._connection.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?)",
            (*values, revision, fact_sequence, delta_json, historical_json),
        )
        self._connection.execute(
            "UPDATE root_generation_heads SET fact_sequence=?,revision=?,projection_cursor=? "
            "WHERE root_id=? AND root_generation=?",
            (fact_sequence, revision, cursor, *values),
        )
        result = CommitResult(False, fact_sequence, revision, cursor)
        self._record_source_admission(request, result)
        return result

    def _record_source_admission(
        self, request: ProjectionCommit, result: CommitResult,
    ) -> None:
        self._connection.execute(
            "INSERT INTO source_admissions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                request.root_id, request.root_generation, request.watermark.stream_id,
                request.watermark.generation, request.watermark.sequence,
                request.event_id, request.content_hash, result.fact_sequence,
                result.revision, result.projection_cursor,
            ),
        )

    def _advance_watermark(self, request: ProjectionCommit) -> None:
        values = (request.root_id, request.root_generation, request.watermark.stream_id)
        current = self._connection.execute(
            "SELECT source_generation,source_sequence FROM source_watermarks "
            "WHERE root_id=? AND root_generation=? AND stream_id=?", values,
        ).fetchone()
        candidate = (request.watermark.generation, request.watermark.sequence)
        if current and candidate < (self._stored_int(current[0]), self._stored_int(current[1])):
            raise ChatProjectionStoreError("watermark_regression", "source watermark cannot regress")
        self._connection.execute(
            "INSERT INTO source_watermarks VALUES(?,?,?,?,?) "
            "ON CONFLICT(root_id,root_generation,stream_id) DO UPDATE SET "
            "source_generation=excluded.source_generation,source_sequence=excluded.source_sequence",
            (*values, *candidate),
        )

    @_translate_sqlite("storage_read_failed")
    def read_facts(
        self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000,
        _page_base_bytes: int = 0,
    ) -> list[StoredFact]:
        self._read_args(root_id, root_generation, after, limit)
        if self._owner_client:
            rows = self._rpc("read_facts", root_id=root_id, root_generation=root_generation,
                             after=after, limit=limit)
            return [StoredFact(**row) for row in rows]
        with self._lock:
            cursor = self._connection.execute(
                "SELECT fact_sequence,event_id,content_hash,fact_json FROM canonical_facts "
                "WHERE root_id=? AND root_generation=? AND fact_sequence>? "
                "ORDER BY fact_sequence LIMIT ?", (root_id, root_generation, after, limit),
            )
            results = []
            page_bytes = _page_base_bytes
            previous = after
            try:
                for row in cursor:
                    item = StoredFact(
                        self._stored_int(row[0]), self._stored_text(row[1]),
                        self._stored_text(row[2]), self._stored_json(row[3]),
                    )
                    expected_hash = hashlib.sha256(
                        canonical_json(item.canonical_fact).encode("utf-8"),
                    ).hexdigest()
                    if item.content_hash != expected_hash or item.canonical_fact.get("event_id") != item.event_id:
                        raise ChatProjectionStoreError("storage_corrupt", "persisted fact identity is invalid")
                    if item.fact_sequence <= previous:
                        raise ChatProjectionStoreError("storage_corrupt", "persisted facts are unordered")
                    previous = item.fact_sequence
                    wire_item = {
                        **asdict(item), "root_id": root_id, "root_generation": root_generation,
                    }
                    row_bytes = len(_encode_json_bounded(wire_item, MAX_RESPONSE_BYTES))
                    added_bytes = row_bytes + (1 if results else 0)
                    if page_bytes + added_bytes > MAX_RESPONSE_BYTES:
                        raise ChatProjectionStoreError("response_too_large", "fact page exceeds response budget")
                    results.append(item)
                    page_bytes += added_bytes
            finally:
                cursor.close()
        return results

    @_translate_sqlite("storage_read_failed")
    def read_revisions(
        self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000,
        _page_base_bytes: int = 0,
    ) -> list[StoredRevision]:
        self._read_args(root_id, root_generation, after, limit)
        if self._owner_client:
            rows = self._rpc("read_revisions", root_id=root_id, root_generation=root_generation,
                             after=after, limit=limit)
            return [StoredRevision(**row) for row in rows]
        with self._lock:
            head_cursor = self.projection_cursor(root_id, root_generation)
            cursor = self._connection.execute(
                "SELECT r.revision,r.fact_sequence,r.visible_delta_json,r.historical_json,"
                "f.fact_sequence,f.event_id,f.content_hash,f.fact_json FROM revisions r LEFT JOIN canonical_facts f ON "
                "f.root_id=r.root_id AND f.root_generation=r.root_generation "
                "AND f.fact_sequence=r.fact_sequence "
                "WHERE r.root_id=? AND r.root_generation=? AND r.revision>? "
                "ORDER BY r.revision LIMIT ?",
                (root_id, root_generation, after, limit),
            )
            results = []
            page_bytes = _page_base_bytes
            previous = after
            try:
                for row in cursor:
                    item = StoredRevision(
                        self._stored_int(row[0]), self._stored_int(row[1]),
                        self._stored_json(row[2]), self._stored_json(row[3]),
                    )
                    if row[4] is None or not 1 <= item.fact_sequence <= head_cursor:
                        raise ChatProjectionStoreError(
                            "storage_corrupt", "revision canonical fact reference is invalid",
                        )
                    if item.revision != item.fact_sequence:
                        raise ChatProjectionStoreError(
                            "storage_corrupt", "revision and canonical fact sequences diverge",
                        )
                    event_id = self._stored_text(row[5])
                    content_hash = self._stored_text(row[6])
                    canonical_fact = self._stored_json(row[7])
                    expected_hash = hashlib.sha256(
                        canonical_json(canonical_fact).encode("utf-8"),
                    ).hexdigest()
                    if content_hash != expected_hash or canonical_fact.get("event_id") != event_id:
                        raise ChatProjectionStoreError(
                            "storage_corrupt", "revision canonical fact identity is invalid",
                        )
                    self._validate_delta_identity(
                        item.visible_delta, item.historical_revision, event_id, content_hash,
                        "storage_corrupt",
                    )
                    if item.revision <= previous:
                        raise ChatProjectionStoreError("storage_corrupt", "persisted revisions are unordered")
                    previous = item.revision
                    wire_item = {
                        **asdict(item), "root_id": root_id, "root_generation": root_generation,
                        "event_id": event_id, "content_hash": content_hash,
                    }
                    row_bytes = len(_encode_json_bounded(wire_item, MAX_RESPONSE_BYTES))
                    added_bytes = row_bytes + (1 if results else 0)
                    if page_bytes + added_bytes > MAX_RESPONSE_BYTES:
                        raise ChatProjectionStoreError("response_too_large", "revision page exceeds response budget")
                    results.append(item)
                    page_bytes += added_bytes
            finally:
                cursor.close()
        return results

    def _revision_identity(
        self, root_id: str, root_generation: int, fact_sequence: int,
    ) -> tuple[str, str]:
        row = self._connection.execute(
            "SELECT event_id,content_hash FROM canonical_facts WHERE root_id=? "
            "AND root_generation=? AND fact_sequence=?",
            (root_id, root_generation, fact_sequence),
        ).fetchone()
        if row is None:
            raise ChatProjectionStoreError("storage_corrupt", "revision canonical fact is missing")
        return self._stored_text(row[0]), self._stored_text(row[1])

    @staticmethod
    def _validate_delta_identity(
        visible: Mapping[str, Any], historical: Mapping[str, Any], event_id: str,
        content_hash: str, code: str,
    ) -> None:
        for payload in (visible, historical):
            if "event_id" in payload and payload["event_id"] != event_id:
                raise ChatProjectionStoreError(code, "revision event identity mismatch")
            if "content_hash" in payload and payload["content_hash"] != content_hash:
                raise ChatProjectionStoreError(code, "revision content identity mismatch")
        if "replace" in visible and visible["replace"] != event_id:
            raise ChatProjectionStoreError(code, "revision replacement identity mismatch")

    @_translate_sqlite("storage_read_failed")
    def projection_cursor(self, root_id: str, root_generation: int) -> int:
        self._identity(root_id, root_generation)
        if self._owner_client:
            result = self._rpc("projection_cursor", root_id=root_id, root_generation=root_generation)
            return self._stored_int(result)
        with self._lock:
            row = self._connection.execute(
                "SELECT projection_cursor FROM root_generation_heads WHERE root_id=? AND root_generation=?",
                (root_id, root_generation),
            ).fetchone()
        return self._stored_int(row[0]) if row else 0

    @_translate_sqlite("storage_write_failed")
    def delete_generation(self, root_id: str, root_generation: int) -> None:
        self._identity(root_id, root_generation)
        if self._owner_client:
            if self._before_commit:
                self._before_commit()
            self._rpc("delete_generation", root_id=root_id, root_generation=root_generation)
            if self._after_commit:
                self._after_commit()
            return
        with self._lock:
            try:
                self._file_checkpoint()
                self._connection.execute("BEGIN IMMEDIATE")
                selected = self._connection.execute(
                    "SELECT root_generation FROM selected_roots WHERE root_id=?", (root_id,),
                ).fetchone()
                exists = self._connection.execute(
                    "SELECT 1 FROM root_generation_heads WHERE root_id=? AND root_generation=?",
                    (root_id, root_generation),
                ).fetchone()
                if exists is None:
                    raise ChatProjectionStoreError("missing_generation", "root generation does not exist")
                if selected and self._stored_int(selected[0]) == root_generation:
                    raise ChatProjectionStoreError("current_generation", "selected generation cannot be deleted")
                self._delete_generation_rows(root_id, root_generation)
                if self._before_commit:
                    self._before_commit()
                if self._before_transaction_commit:
                    self._before_transaction_commit(self._connection)
                self._file_checkpoint()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        if self._after_commit:
            self._after_commit()

    @_translate_sqlite("storage_write_failed")
    def delete_root(self, root_id: str) -> None:
        self._identity(root_id, 0)
        if self._owner_client:
            if self._before_commit:
                self._before_commit()
            self._rpc("delete_root", root_id=root_id)
            if self._after_commit:
                self._after_commit()
            return
        with self._lock:
            try:
                self._file_checkpoint()
                self._connection.execute("BEGIN IMMEDIATE")
                exists = self._connection.execute(
                    "SELECT 1 FROM selected_roots WHERE root_id=? UNION SELECT 1 FROM "
                    "root_generation_heads WHERE root_id=? LIMIT 1", (root_id, root_id),
                ).fetchone()
                if exists is None:
                    raise ChatProjectionStoreError("missing_root", "root does not exist")
                for table in (
                    "canonical_facts", "render_nodes", "ownership", "turn_manifests", "revisions",
                    "source_watermarks", "source_admissions", "root_generation_heads", "selected_roots",
                ):
                    self._connection.execute(f'DELETE FROM "{table}" WHERE root_id=?', (root_id,))
                if self._before_commit:
                    self._before_commit()
                if self._before_transaction_commit:
                    self._before_transaction_commit(self._connection)
                self._file_checkpoint()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        if self._after_commit:
            self._after_commit()

    def _delete_generation_rows(self, root_id: str, root_generation: int) -> None:
        for table in (
            "canonical_facts", "render_nodes", "ownership", "turn_manifests", "revisions",
            "source_watermarks", "source_admissions", "root_generation_heads",
        ):
            self._connection.execute(
                f'DELETE FROM "{table}" WHERE root_id=? AND root_generation=?',
                (root_id, root_generation),
            )

    @_translate_sqlite("storage_read_failed")
    def read_projection(
        self, root_id: str, root_generation: int, event_id: str,
    ) -> StoredProjection | None:
        self._identity(root_id, root_generation)
        self._text("event_id", event_id)
        if self._owner_client:
            result = self._rpc("read_projection", root_id=root_id,
                               root_generation=root_generation, event_id=event_id)
            if result is None:
                return None
            manifest = TurnManifest(**result.pop("manifest"))
            return StoredProjection(manifest=manifest, **result)
        with self._lock:
            row = self._connection.execute(
                "SELECT n.node_json,o.turn_id,o.message_id,o.parent_event_id,o.owner_scope,"
                "m.event_count,m.direct_child_count FROM render_nodes n JOIN ownership o "
                "USING(root_id,root_generation,event_id) JOIN turn_manifests m "
                "ON m.root_id=o.root_id AND m.root_generation=o.root_generation AND m.turn_id=o.turn_id "
                "WHERE n.root_id=? AND n.root_generation=? AND n.event_id=?",
                (root_id, root_generation, event_id),
            ).fetchone()
        if row is None:
            return None
        return StoredProjection(
            event_id, self._stored_json(row[0]), self._stored_text(row[1]),
            self._stored_nullable_text(row[2]), self._stored_nullable_text(row[3]),
            self._stored_text(row[4]),
            TurnManifest(self._stored_text(row[1]), self._stored_int(row[5]), self._stored_int(row[6])),
        )

    @_translate_sqlite("storage_read_failed")
    def source_watermark(
        self, root_id: str, root_generation: int, stream_id: str,
    ) -> SourceWatermark | None:
        self._identity(root_id, root_generation)
        self._text("stream_id", stream_id)
        if self._owner_client:
            result = self._rpc("source_watermark", root_id=root_id,
                               root_generation=root_generation, stream_id=stream_id)
            return SourceWatermark(**result) if result is not None else None
        with self._lock:
            row = self._connection.execute(
                "SELECT source_generation,source_sequence FROM source_watermarks "
                "WHERE root_id=? AND root_generation=? AND stream_id=?",
                (root_id, root_generation, stream_id),
            ).fetchone()
        return SourceWatermark(
            stream_id, self._stored_int(row[0]), self._stored_int(row[1]),
        ) if row else None

    @_translate_sqlite("storage_read_failed")
    def source_admission(
        self, root_id: str, root_generation: int, stream_id: str,
        source_generation: int, source_sequence: int,
    ) -> SourceAdmission | None:
        self._identity(root_id, root_generation)
        self._text("stream_id", stream_id)
        self._integer("source_generation", source_generation)
        self._integer("source_sequence", source_sequence)
        if self._owner_client:
            result = self._rpc(
                "source_admission", root_id=root_id, root_generation=root_generation,
                stream_id=stream_id, source_generation=source_generation,
                source_sequence=source_sequence,
            )
            return SourceAdmission(**result) if result is not None else None
        with self._lock:
            row = self._connection.execute(
                "SELECT event_id,content_hash,fact_sequence,revision,projection_cursor "
                "FROM source_admissions WHERE root_id=? AND root_generation=? AND stream_id=? "
                "AND source_generation=? AND source_sequence=?",
                (root_id, root_generation, stream_id, source_generation, source_sequence),
            ).fetchone()
        if row is None:
            return None
        return SourceAdmission(
            self._stored_text(row[0]), self._stored_text(row[1]), self._stored_int(row[2]),
            self._stored_int(row[3]), self._stored_int(row[4]),
        )

    @staticmethod
    def _validate_commit(request: ProjectionCommit) -> None:
        SQLiteChatProjectionStore._identity(request.root_id, request.root_generation)
        for name, value in (
            ("event_id", request.event_id), ("content_hash", request.content_hash),
            ("turn_id", request.turn_id), ("owner_scope", request.owner_scope),
            ("stream_id", request.watermark.stream_id),
        ):
            SQLiteChatProjectionStore._text(name, value)
        for name, value in (
            ("message_id", request.message_id), ("parent_event_id", request.parent_event_id),
        ):
            if value is not None:
                SQLiteChatProjectionStore._text(name, value)
        SQLiteChatProjectionStore._text("manifest.turn_id", request.manifest.turn_id)
        for name, value in (
            ("canonical_fact", request.canonical_fact), ("render_node", request.render_node),
            ("visible_delta", request.visible_delta),
            ("historical_revision", request.historical_revision),
        ):
            if not isinstance(value, Mapping):
                raise ChatProjectionStoreError("invalid_input", f"{name} must be an object")
        if request.canonical_fact.get("event_id") != request.event_id:
            raise ChatProjectionStoreError("invalid_input", "canonical fact event_id does not match")
        expected = hashlib.sha256(canonical_json(request.canonical_fact).encode("utf-8")).hexdigest()
        if len(request.content_hash) != 64 or any(character not in "0123456789abcdef" for character in request.content_hash):
            raise ChatProjectionStoreError("invalid_input", "content_hash must be lowercase SHA-256")
        if request.content_hash != expected:
            raise ChatProjectionStoreError("hash_mismatch", "content hash does not match canonical fact")
        if request.manifest.turn_id != request.turn_id:
            raise ChatProjectionStoreError("invalid_input", "manifest turn does not match ownership")
        for name, value in (
            ("manifest.event_count", request.manifest.event_count),
            ("manifest.direct_child_count", request.manifest.direct_child_count),
            ("watermark.generation", request.watermark.generation),
            ("watermark.sequence", request.watermark.sequence),
        ):
            SQLiteChatProjectionStore._integer(name, value)

    @staticmethod
    def _commit_to_dict(request: ProjectionCommit) -> dict[str, Any]:
        return {
            "root_id": request.root_id, "root_generation": request.root_generation,
            "event_id": request.event_id, "content_hash": request.content_hash,
            "canonical_fact": request.canonical_fact, "render_node": request.render_node,
            "turn_id": request.turn_id, "message_id": request.message_id,
            "parent_event_id": request.parent_event_id, "owner_scope": request.owner_scope,
            "manifest": {
                "turn_id": request.manifest.turn_id, "event_count": request.manifest.event_count,
                "direct_child_count": request.manifest.direct_child_count,
            },
            "visible_delta": request.visible_delta,
            "historical_revision": request.historical_revision,
            "watermark": {
                "stream_id": request.watermark.stream_id,
                "generation": request.watermark.generation,
                "sequence": request.watermark.sequence,
            },
        }

    @staticmethod
    def _commit_from_dict(payload: Mapping[str, Any]) -> ProjectionCommit:
        if not isinstance(payload, Mapping):
            raise ChatProjectionStoreError("owner_protocol_error", "commit payload must be an object")
        expected = {
            "root_id", "root_generation", "event_id", "content_hash", "canonical_fact",
            "render_node", "turn_id", "message_id", "parent_event_id", "owner_scope",
            "manifest", "visible_delta", "historical_revision", "watermark",
        }
        if set(payload) != expected or not isinstance(payload["manifest"], Mapping) or not isinstance(payload["watermark"], Mapping):
            raise ChatProjectionStoreError("owner_protocol_error", "commit payload shape is invalid")
        if set(payload["manifest"]) != {"turn_id", "event_count", "direct_child_count"}:
            raise ChatProjectionStoreError("owner_protocol_error", "manifest payload shape is invalid")
        if set(payload["watermark"]) != {"stream_id", "generation", "sequence"}:
            raise ChatProjectionStoreError("owner_protocol_error", "watermark payload shape is invalid")
        values = dict(payload)
        values["manifest"] = TurnManifest(**payload["manifest"])
        values["watermark"] = SourceWatermark(**payload["watermark"])
        return ProjectionCommit(**values)

    @staticmethod
    def _identity(root_id: str, root_generation: int) -> None:
        SQLiteChatProjectionStore._text("root_id", root_id)
        SQLiteChatProjectionStore._integer("root_generation", root_generation)

    @staticmethod
    def _integer(name: str, value: int, *, code: str = "invalid_input") -> None:
        if type(value) is not int or not 0 <= value <= MAX_SQLITE_INTEGER:
            raise ChatProjectionStoreError(code, f"{name} must fit a non-negative SQLite integer")

    @staticmethod
    def _stored_int(value: Any) -> int:
        if type(value) is not int or not 0 <= value <= MAX_SQLITE_INTEGER:
            raise ChatProjectionStoreError("storage_corrupt", "persisted integer is invalid")
        return value

    @staticmethod
    def _increment_stored(value: Any, name: str) -> int:
        current = SQLiteChatProjectionStore._stored_int(value)
        if current == MAX_SQLITE_INTEGER:
            raise ChatProjectionStoreError("storage_corrupt", f"persisted {name} is exhausted")
        return current + 1

    @staticmethod
    def _stored_text(value: Any) -> str:
        if not isinstance(value, str):
            raise ChatProjectionStoreError("storage_corrupt", "persisted text is invalid")
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ChatProjectionStoreError("storage_corrupt", "persisted text is invalid") from exc
        return value

    @staticmethod
    def _stored_nullable_text(value: Any) -> str | None:
        return None if value is None else SQLiteChatProjectionStore._stored_text(value)

    @staticmethod
    def _stored_json(value: Any) -> Mapping[str, Any]:
        text = SQLiteChatProjectionStore._stored_text(value)
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError) as exc:
            raise ChatProjectionStoreError("storage_corrupt", "persisted JSON is invalid") from exc
        if not isinstance(decoded, Mapping):
            raise ChatProjectionStoreError("storage_corrupt", "persisted JSON is not an object")
        try:
            _validate_json(decoded)
        except ChatProjectionStoreError as exc:
            raise ChatProjectionStoreError("storage_corrupt", "persisted JSON violates store limits") from exc
        return decoded

    @staticmethod
    def _text(name: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ChatProjectionStoreError("invalid_input", f"{name} is required")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ChatProjectionStoreError("invalid_input", f"{name} is not valid UTF-8") from exc
        if size > MAX_TEXT_BYTES:
            raise ChatProjectionStoreError("text_too_large", f"{name} exceeds UTF-8 byte limit")

    @staticmethod
    def _read_args(root_id: str, generation: int, after: int, limit: int) -> None:
        SQLiteChatProjectionStore._identity(root_id, generation)
        SQLiteChatProjectionStore._integer("cursor", after, code="invalid_cursor")
        if type(limit) is not int or not 1 <= limit <= min(MAX_READ_LIMIT, MAX_SQLITE_INTEGER):
            raise ChatProjectionStoreError("invalid_limit", f"limit must be 1..{MAX_READ_LIMIT}")

    @_translate_sqlite("storage_close_failed")
    def close(self) -> None:
        if self._owner_client:
            self._owner.close()
            self._closed = True
            return
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None


def _owner_dispatch(
    store: SQLiteChatProjectionStore, operation: str, arguments: Mapping[str, Any],
    request_id: int,
) -> Any:
    allowed = {
        "select_generation": {"root_id", "root_generation"},
        "commit": {"request"},
        "read_facts": {"root_id", "root_generation", "after", "limit"},
        "read_revisions": {"root_id", "root_generation", "after", "limit"},
        "projection_cursor": {"root_id", "root_generation"},
        "delete_generation": {"root_id", "root_generation"},
        "delete_root": {"root_id"},
        "read_projection": {"root_id", "root_generation", "event_id"},
        "source_watermark": {"root_id", "root_generation", "stream_id"},
        "source_admission": {
            "root_id", "root_generation", "stream_id", "source_generation", "source_sequence",
        },
        "close": set(),
    }
    if operation not in allowed or set(arguments) != allowed[operation]:
        raise ChatProjectionStoreError("owner_protocol_error", "operation is not allowed")
    if operation == "commit":
        request = store._commit_from_dict(arguments["request"])
        result = asdict(store.commit(request))
        result.update({
            "root_id": request.root_id, "root_generation": request.root_generation,
            "event_id": request.event_id, "content_hash": request.content_hash,
            "source_generation": request.watermark.generation,
            "source_sequence": request.watermark.sequence,
        })
        return result
    if operation == "read_facts":
        cursor = store.projection_cursor(arguments["root_id"], arguments["root_generation"])
        result = {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "after": arguments["after"], "projection_cursor": cursor, "rows": [],
        }
        base_bytes = len(_encode_json_bounded({
            "request_id": request_id, "operation": operation, "result": result,
        }, MAX_RESPONSE_BYTES))
        rows = store.read_facts(**arguments, _page_base_bytes=base_bytes)
        if rows and rows[-1].fact_sequence > cursor:
            raise ChatProjectionStoreError("storage_corrupt", "fact page exceeds projection head")
        result["rows"] = [
            {**asdict(item), "root_id": arguments["root_id"],
             "root_generation": arguments["root_generation"]} for item in rows
        ]
        return result
    if operation == "read_revisions":
        cursor = store.projection_cursor(arguments["root_id"], arguments["root_generation"])
        result = {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "after": arguments["after"], "projection_cursor": cursor, "rows": [],
        }
        base_bytes = len(_encode_json_bounded({
            "request_id": request_id, "operation": operation, "result": result,
        }, MAX_RESPONSE_BYTES))
        rows = store.read_revisions(**arguments, _page_base_bytes=base_bytes)
        if rows and rows[-1].revision > cursor:
            raise ChatProjectionStoreError("storage_corrupt", "revision page exceeds projection head")
        result["rows"] = []
        for item in rows:
            event_id, content_hash = store._revision_identity(
                arguments["root_id"], arguments["root_generation"], item.fact_sequence,
            )
            result["rows"].append({
                **asdict(item), "root_id": arguments["root_id"],
                "root_generation": arguments["root_generation"],
                "event_id": event_id, "content_hash": content_hash,
            })
        return result
    if operation == "projection_cursor":
        return {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "projection_cursor": store.projection_cursor(**arguments),
        }
    if operation == "read_projection":
        result = store.read_projection(**arguments)
        return {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "event_id": arguments["event_id"], "projection": asdict(result) if result else None,
        }
    if operation == "source_watermark":
        result = store.source_watermark(**arguments)
        return {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "stream_id": arguments["stream_id"], "watermark": asdict(result) if result else None,
        }
    if operation == "source_admission":
        result = store.source_admission(**arguments)
        return {
            **arguments, "admission": asdict(result) if result else None,
        }
    if operation == "close":
        store._file_checkpoint()
    method = getattr(store, operation)
    result = method(**arguments)
    if result is None:
        return None
    if isinstance(result, list):
        return [asdict(item) for item in result]
    if isinstance(result, (StoredProjection, SourceWatermark, SourceAdmission)):
        return asdict(result)
    return result


def _run_owner(
    channel_fd: int, directory_fd: int, file_fd: int, basename: str, test_fault: str,
) -> None:
    def create_store(owner_directory_fd: int, owner_file_fd: int, owner_basename: str):
        store = SQLiteChatProjectionStore(
            _owner_directory_fd=owner_directory_fd, _owner_file_fd=owner_file_fd,
            _owner_basename=owner_basename,
        )
        return store

    fault = [test_fault]
    def mutate_result(channel, request_id: int, operation: str, result: Any) -> tuple[Any, bool]:
        if fault[0] == "post_commit_stop" and operation == "commit":
            os.kill(os.getpid(), signal.SIGSTOP)
        if fault[0] == "malformed_response":
            _send_frame(channel, {
                "request_id": request_id + 1, "operation": operation,
                "result": result, "unexpected": True,
            })
            fault[0] = "none"
            return result, True
        if fault[0] == "malformed_commit_response" and operation == "commit":
            _send_frame(channel, {
                "request_id": request_id, "operation": operation,
                "result": {"duplicate": "invalid"},
            })
            fault[0] = "none"
            return result, True
        if fault[0] == "semantic_mismatch" and isinstance(result, Mapping):
            result = dict(result)
            result["root_id"] = "mismatched-root"
            fault[0] = "none"
        if (
            fault[0] == "revision_pair_mismatch" and operation == "read_revisions"
            and isinstance(result, Mapping) and result.get("rows")
        ):
            result = dict(result)
            result["rows"] = [dict(row) for row in result["rows"]]
            result["rows"][0]["fact_sequence"] += 1
            fault[0] = "none"
        return result, False

    serve_owner(
        channel_fd, directory_fd, file_fd, basename, create_store, _owner_dispatch,
        lambda store: store.close(), mutate_result, MAX_RESPONSE_BYTES,
    )


if __name__ == "__main__" and len(sys.argv) == 7 and sys.argv[1] == "--projection-owner":
    import signal
    _run_owner(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6])
