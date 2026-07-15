from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
from dataclasses import asdict
from errno import EEXIST, ENOENT
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from chat_projection_store import (
    ChatProjectionStoreError, CommitResult, ProjectionCommit, SourceWatermark, StoredFact,
    StoredProjection, StoredRevision, TurnManifest,
)
from paths import ba_home


SCHEMA_VERSION = 2
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_READ_LIMIT = 10_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_JSON_LIST_ITEMS = 50_000
MAX_JSON_OBJECT_ITEMS = 50_000
MAX_TEXT_BYTES = 4_096
MAX_COMMIT_BYTES = MAX_JSON_BYTES
MAX_SQLITE_INTEGER = 2**63 - 1
MAX_IPC_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
IPC_TIMEOUT_SECONDS = 30
MIN_IPC_TIMEOUT_SECONDS = 0.05
MAX_IPC_TIMEOUT_SECONDS = 300

TABLE_DDL = {
    "selected_roots": "CREATE TABLE selected_roots(root_id TEXT PRIMARY KEY, root_generation INTEGER NOT NULL)",
    "root_generation_heads": "CREATE TABLE root_generation_heads(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, fact_sequence INTEGER NOT NULL, revision INTEGER NOT NULL, projection_cursor INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation))",
    "canonical_facts": "CREATE TABLE canonical_facts(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, fact_sequence INTEGER NOT NULL, event_id TEXT NOT NULL, content_hash TEXT NOT NULL, fact_json TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,fact_sequence), UNIQUE(root_id,root_generation,event_id,content_hash))",
    "render_nodes": "CREATE TABLE render_nodes(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, event_id TEXT NOT NULL, node_json TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,event_id))",
    "ownership": "CREATE TABLE ownership(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, event_id TEXT NOT NULL, turn_id TEXT NOT NULL, message_id TEXT, parent_event_id TEXT, owner_scope TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,event_id))",
    "turn_manifests": "CREATE TABLE turn_manifests(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, turn_id TEXT NOT NULL, event_count INTEGER NOT NULL, direct_child_count INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation,turn_id))",
    "revisions": "CREATE TABLE revisions(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, revision INTEGER NOT NULL, fact_sequence INTEGER NOT NULL, visible_delta_json TEXT NOT NULL, historical_json TEXT NOT NULL, PRIMARY KEY(root_id,root_generation,revision))",
    "source_watermarks": "CREATE TABLE source_watermarks(root_id TEXT NOT NULL, root_generation INTEGER NOT NULL, stream_id TEXT NOT NULL, source_generation INTEGER NOT NULL, source_sequence INTEGER NOT NULL, PRIMARY KEY(root_id,root_generation,stream_id))",
}
AUTOINDEX_COUNTS = {
    "selected_roots": 1, "root_generation_heads": 1, "canonical_facts": 2,
    "render_nodes": 1, "ownership": 1, "turn_manifests": 1, "revisions": 1,
    "source_watermarks": 1,
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


def _encode_json_bounded(payload: Any, limit: int) -> bytearray:
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = bytearray()
    try:
        for chunk in encoder.iterencode(payload):
            chunk_bytes = chunk.encode("utf-8")
            if len(encoded) + len(chunk_bytes) > limit:
                raise ChatProjectionStoreError("ipc_too_large", "projection owner frame limit exceeded")
            encoded.extend(chunk_bytes)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ChatProjectionStoreError("owner_protocol_error", "projection owner frame is invalid") from exc
    return encoded


def _send_frame(
    channel: socket.socket, payload: Mapping[str, Any], *, limit: int = MAX_IPC_BYTES,
) -> None:
    encoded = _encode_json_bounded(payload, limit)
    channel.sendall(struct.pack("!I", len(encoded)) + encoded)


def _receive_exact(channel: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = channel.recv(length - len(chunks))
        if not chunk:
            raise ChatProjectionStoreError("owner_unavailable", "projection owner exited")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive_frame(channel: socket.socket) -> Mapping[str, Any]:
    size = struct.unpack("!I", _receive_exact(channel, 4))[0]
    if size > MAX_IPC_BYTES:
        raise ChatProjectionStoreError("ipc_too_large", "projection owner frame limit exceeded")
    try:
        def strict_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        def reject_constant(value):
            raise ValueError(f"non-finite number: {value}")
        payload = json.loads(
            _receive_exact(channel, size).decode("utf-8"), object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ChatProjectionStoreError("owner_protocol_error", "invalid projection owner frame") from exc
    if not isinstance(payload, Mapping):
        raise ChatProjectionStoreError("owner_protocol_error", "invalid projection owner frame")
    return payload


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
        _owner_directory_fd: int | None = None,
        _owner_file_fd: int | None = None,
        _owner_basename: str | None = None,
        _ipc_timeout_seconds: float = IPC_TIMEOUT_SECONDS,
        _test_owner_fault: str | None = None,
    ) -> None:
        self._owner_client = _owner_directory_fd is None
        self._before_commit = before_commit
        self._after_commit = after_commit
        self._lock = threading.RLock()
        self._closed = False
        self._poisoned = False
        self._next_request_id = 1
        self._ipc_timeout_seconds = _ipc_timeout_seconds
        self._test_owner_fault = _test_owner_fault
        if self._owner_client:
            self._start_owner(path)
            return
        os.fchdir(_owner_directory_fd)
        self._path = Path(_owner_basename)
        self._connection = None
        self._parent_fd = None
        self._file_fd = None
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
        if (
            not isinstance(self._ipc_timeout_seconds, (int, float))
            or isinstance(self._ipc_timeout_seconds, bool)
            or not math.isfinite(self._ipc_timeout_seconds)
            or not MIN_IPC_TIMEOUT_SECONDS <= self._ipc_timeout_seconds <= MAX_IPC_TIMEOUT_SECONDS
        ):
            raise ChatProjectionStoreError(
                "invalid_input",
                f"IPC timeout must be {MIN_IPC_TIMEOUT_SECONDS}..{MAX_IPC_TIMEOUT_SECONDS} seconds",
            )
        if self._test_owner_fault not in (
            None, "post_commit_stop", "malformed_response", "semantic_mismatch",
        ):
            raise ChatProjectionStoreError("invalid_input", "unknown owner test fault")
        self._connection = None
        self._process = None
        self._channel = None
        self._path, self._parent_fd, self._file_fd, created = self._secure_open(path)
        parent_channel, child_channel = socket.socketpair()
        parent_channel.settimeout(self._ipc_timeout_seconds)
        launcher = "import os,runpy,sys;sys.argv=sys.argv[1:];sys.path.insert(0,os.path.dirname(sys.argv[0]));runpy.run_path(sys.argv[0],run_name='__main__')"
        command = [
            sys.executable, "-I", "-c", launcher, str(Path(__file__).resolve()), "--projection-owner",
            str(child_channel.fileno()), str(self._parent_fd), str(self._file_fd), self._path.name,
            self._test_owner_fault or "none",
        ]
        environment = {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}
        try:
            self._process = subprocess.Popen(
                command, pass_fds=(child_channel.fileno(), self._parent_fd, self._file_fd), env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            child_channel.close()
            self._channel = parent_channel
            response = _receive_frame(self._channel)
            if set(response) == {"error"} and isinstance(response["error"], Mapping) and set(response["error"]) == {"code", "detail"}:
                raise ChatProjectionStoreError(str(response["error"]["code"]), str(response["error"]["detail"]))
            if response != {"ready": True}:
                raise ChatProjectionStoreError("owner_protocol_error", "projection owner did not initialize")
        except BaseException:
            parent_channel.close()
            child_channel.close()
            if self._process is not None:
                self._process.kill()
                self._process.wait()
            if created:
                try:
                    os.unlink(self._path.name, dir_fd=self._parent_fd)
                except OSError:
                    pass
            self._close_secure_handles()
            raise

    @staticmethod
    def _verify_owner_file(file_fd: int, basename: str) -> None:
        expected = os.fstat(file_fd)
        visible = os.stat(basename, follow_symlinks=False)
        SQLiteChatProjectionStore._validate_secure_file_stat(expected)
        SQLiteChatProjectionStore._validate_secure_file_stat(visible)
        if (expected.st_dev, expected.st_ino) != (visible.st_dev, visible.st_ino):
            raise ChatProjectionStoreError("path_race", "chat store file changed during owner open")

    @staticmethod
    def _validate_secure_file_stat(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise ChatProjectionStoreError("insecure_store_file", "chat store must be a regular file")
        if metadata.st_uid != os.getuid():
            raise ChatProjectionStoreError("insecure_store_file", "chat store owner is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ChatProjectionStoreError("insecure_store_file", "chat store mode must be 0600")
        if metadata.st_nlink != 1:
            raise ChatProjectionStoreError("insecure_store_file", "chat store cannot be hard-linked")

    def _file_checkpoint(self) -> None:
        self._verify_owner_file(self._owner_identity_fd, self._owner_basename)

    def _rpc(self, operation: str, **arguments: Any) -> Any:
        if self._poisoned or self._closed:
            raise ChatProjectionStoreError("owner_unavailable", "projection owner exited")
        with self._lock:
            if self._poisoned or self._closed or self._channel is None or self._process is None or self._process.poll() is not None:
                self._poison_owner_locked()
                raise ChatProjectionStoreError("owner_unavailable", "projection owner exited")
            request_id = self._next_request_id
            if request_id > MAX_SQLITE_INTEGER:
                self._poison_owner_locked()
                raise ChatProjectionStoreError("owner_protocol_error", "request id exhausted")
            self._next_request_id += 1
            try:
                _send_frame(self._channel, {
                    "request_id": request_id, "operation": operation, "arguments": arguments,
                })
                response = _receive_frame(self._channel)
                return self._validate_rpc_response(response, request_id, operation, arguments)
            except ChatProjectionStoreError as exc:
                if exc.code not in ("owner_domain_error",):
                    self._poison_owner_locked()
                if exc.code == "owner_domain_error":
                    cause = exc.__cause__
                    if isinstance(cause, ChatProjectionStoreError):
                        if cause.code in {
                            "insecure_store_file", "path_race", "owner_protocol_error",
                            "owner_internal_error",
                        }:
                            self._poison_owner_locked()
                        raise cause
                if operation == "commit" and exc.code == "owner_unavailable":
                    raise ChatProjectionStoreError(
                        "commit_outcome_unknown", "owner response was lost after commit dispatch",
                    ) from exc
                raise
            except (OSError, TimeoutError, UnicodeError) as exc:
                self._poison_owner_locked()
                code = "commit_outcome_unknown" if operation == "commit" else "owner_unavailable"
                raise ChatProjectionStoreError(code, "projection owner response unavailable") from exc

    def _validate_rpc_response(
        self, response: Mapping[str, Any], request_id: int, operation: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        base = {"request_id", "operation"}
        if response.get("request_id") != request_id or response.get("operation") != operation:
            raise ChatProjectionStoreError("owner_protocol_error", "owner response correlation mismatch")
        if set(response) == base | {"error"}:
            error = response["error"]
            if not isinstance(error, Mapping) or set(error) != {"code", "detail"}:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid owner error envelope")
            self._wire_text(error.get("code"))
            self._wire_text(error.get("detail"))
            domain = ChatProjectionStoreError(error["code"], error["detail"])
            wrapped = ChatProjectionStoreError("owner_domain_error", "owner returned a domain error")
            wrapped.__cause__ = domain
            raise wrapped
        if set(response) != base | {"result"}:
            raise ChatProjectionStoreError("owner_protocol_error", "invalid owner result envelope")
        return self._validate_rpc_result(operation, response["result"], arguments)

    def _poison_owner_locked(self) -> None:
        self._poisoned = True
        channel, process = self._channel, self._process
        self._channel = None
        self._process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
        if channel is not None:
            channel.close()
        self._close_secure_handles()

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
                or not result["fact_sequence"] == result["revision"] == result["projection_cursor"]
            ):
                raise ChatProjectionStoreError("owner_protocol_error", "commit sequence mismatch")
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
                {"root_id", "root_generation", "revision", "fact_sequence", "visible_delta", "historical_revision"}
            )
            previous = arguments["after"]
            for row in rows:
                self._wire_mapping(row, expected)
                self._wire_correlation(row, arguments, ("root_id", "root_generation"))
                for key in expected & {"fact_sequence", "revision"}:
                    self._wire_integer(row[key])
                for key in expected & {"event_id", "content_hash"}:
                    self._wire_text(row[key])
                if operation == "read_facts" and (
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
            if rows and previous > cursor:
                raise ChatProjectionStoreError("owner_protocol_error", "owner cursor precedes page")
            return [
                {key: value for key, value in row.items() if key not in {"root_id", "root_generation"}}
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

    @classmethod
    def _secure_open(cls, path: Path | None) -> tuple[Path, int, int, bool]:
        declared_root = Path(os.path.abspath(ba_home().expanduser()))
        declared_candidate = (path or declared_root / "chat" / "selected.sqlite3").expanduser()
        if not declared_candidate.is_absolute():
            raise ChatProjectionStoreError("invalid_path", "chat store path must be absolute")
        declared_candidate = Path(os.path.abspath(declared_candidate))
        try:
            relative = declared_candidate.relative_to(declared_root)
        except ValueError as exc:
            raise ChatProjectionStoreError("path_escape", "store path escapes Better Agent home") from exc
        root = Path(os.path.realpath(declared_root))
        candidate = root / relative
        if not relative.parts or candidate.name in ("", ".", ".."):
            raise ChatProjectionStoreError("invalid_path", "chat store file name is required")
        parent_fd = cls._open_directory_chain(root, relative.parts[:-1])
        created = False
        flags = os.O_RDWR | os.O_NOFOLLOW
        try:
            file_fd = os.open(candidate.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            created = True
        except OSError as exc:
            if exc.errno != EEXIST:
                os.close(parent_fd)
                raise ChatProjectionStoreError("path_open_failed", "cannot securely create chat store") from exc
            try:
                file_fd = os.open(candidate.name, flags, dir_fd=parent_fd)
            except OSError as open_exc:
                os.close(parent_fd)
                code = "path_escape" if open_exc.errno != ENOENT else "path_race"
                raise ChatProjectionStoreError(code, "cannot securely open chat store") from open_exc
        try:
            cls._validate_secure_file_stat(os.fstat(file_fd))
            cls._validate_secure_file_stat(
                os.stat(candidate.name, dir_fd=parent_fd, follow_symlinks=False),
            )
        except BaseException:
            if created:
                try:
                    os.unlink(candidate.name, dir_fd=parent_fd)
                except OSError:
                    pass
            os.close(file_fd)
            os.close(parent_fd)
            raise
        return candidate, parent_fd, file_fd, created

    @staticmethod
    def _open_directory_chain(root: Path, relative_parts: tuple[str, ...]) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open("/", flags)
        try:
            for component in (*root.parts[1:], *relative_parts):
                try:
                    next_fd = os.open(component, flags, dir_fd=current)
                except OSError as exc:
                    if exc.errno != ENOENT:
                        raise ChatProjectionStoreError("path_escape", "directory path is not secure") from exc
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                    except OSError as mkdir_exc:
                        if mkdir_exc.errno != EEXIST:
                            raise ChatProjectionStoreError("path_open_failed", "cannot create store directory") from mkdir_exc
                    next_fd = os.open(component, flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except BaseException:
            os.close(current)
            raise

    def _close_secure_handles(self) -> None:
        for name in ("_file_fd", "_parent_fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

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
            for sql in TABLE_DDL.values()
        )
        self._connection.executescript(f"{install_sql};")
        self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._file_checkpoint()
        self._connection.commit()
        try:
            self._validate_schema()
        except ChatProjectionStoreError:
            self.close()
            raise

    def _validate_schema(self) -> None:
        expected_objects = self._expected_schema_objects()
        rows = self._connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "OR name LIKE 'sqlite_autoindex_%'"
        ).fetchall()
        actual_objects = {
            (row[0], row[1], row[2], self._normalize_sql(row[3])) for row in rows
            if row[0] in ("table", "index", "trigger", "view")
        }
        if actual_objects != expected_objects:
            raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
        if self._connection.execute("PRAGMA foreign_key_list('canonical_facts')").fetchall():
            raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
        for table, expected in self._TABLES.items():
            rows = self._connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            actual = tuple((row[1], row[2].upper(), int(row[3]), int(row[5]), row[4]) for row in rows)
            if actual != expected:
                raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
            expected_pk = tuple(name for name, _, _, pk, _ in sorted(expected, key=lambda item: item[3]) if pk)
            indexes = self._connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            unique_columns = set()
            for index in indexes:
                if int(index[2]) != 1:
                    continue
                columns = tuple(
                    row[2] for row in self._connection.execute(
                        f'PRAGMA index_info("{index[1]}")'
                    ).fetchall()
                )
                if index[3] == "pk":
                    if columns != expected_pk:
                        raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")
                else:
                    unique_columns.add(columns)
            if unique_columns != self._UNIQUE_INDEXES.get(table, set()):
                raise ChatProjectionStoreError("unsupported_schema", "wipe the selected chat store")

    @staticmethod
    def _normalize_sql(sql: str | None) -> str | None:
        if sql is None:
            return None
        return "".join(sql.lower().split())

    def _expected_schema_objects(self) -> set[tuple[str, str, str, str | None]]:
        objects = {
            ("table", name, name, self._normalize_sql(sql)) for name, sql in TABLE_DDL.items()
        }
        for table, count in AUTOINDEX_COUNTS.items():
            for number in range(1, count + 1):
                objects.add(("index", f"sqlite_autoindex_{table}_{number}", table, None))
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
        duplicate = self._connection.execute(
            "SELECT fact_sequence FROM canonical_facts WHERE root_id=? AND root_generation=? "
            "AND event_id=? AND content_hash=?",
            (request.root_id, request.root_generation, request.event_id, request.content_hash),
        ).fetchone()
        self._advance_watermark(request)
        if duplicate:
            return CommitResult(
                True, self._stored_int(duplicate[0]), self._stored_int(head[2]),
                self._stored_int(head[3]),
            )
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
        return CommitResult(False, fact_sequence, revision, cursor)

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
            page_bytes = 1024
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
                    row_bytes = len(_encode_json_bounded(asdict(item), MAX_RESPONSE_BYTES))
                    if page_bytes + row_bytes > MAX_RESPONSE_BYTES:
                        raise ChatProjectionStoreError("response_too_large", "fact page exceeds response budget")
                    results.append(item)
                    page_bytes += row_bytes
            finally:
                cursor.close()
        return results

    @_translate_sqlite("storage_read_failed")
    def read_revisions(
        self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000,
    ) -> list[StoredRevision]:
        self._read_args(root_id, root_generation, after, limit)
        if self._owner_client:
            rows = self._rpc("read_revisions", root_id=root_id, root_generation=root_generation,
                             after=after, limit=limit)
            return [StoredRevision(**row) for row in rows]
        with self._lock:
            cursor = self._connection.execute(
                "SELECT revision,fact_sequence,visible_delta_json,historical_json FROM revisions "
                "WHERE root_id=? AND root_generation=? AND revision>? ORDER BY revision LIMIT ?",
                (root_id, root_generation, after, limit),
            )
            results = []
            page_bytes = 1024
            previous = after
            try:
                for row in cursor:
                    item = StoredRevision(
                        self._stored_int(row[0]), self._stored_int(row[1]),
                        self._stored_json(row[2]), self._stored_json(row[3]),
                    )
                    if item.revision <= previous:
                        raise ChatProjectionStoreError("storage_corrupt", "persisted revisions are unordered")
                    previous = item.revision
                    row_bytes = len(_encode_json_bounded(asdict(item), MAX_RESPONSE_BYTES))
                    if page_bytes + row_bytes > MAX_RESPONSE_BYTES:
                        raise ChatProjectionStoreError("response_too_large", "revision page exceeds response budget")
                    results.append(item)
                    page_bytes += row_bytes
            finally:
                cursor.close()
        return results

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
                    "source_watermarks", "root_generation_heads", "selected_roots",
                ):
                    self._connection.execute(f'DELETE FROM "{table}" WHERE root_id=?', (root_id,))
                if self._before_commit:
                    self._before_commit()
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
            "source_watermarks", "root_generation_heads",
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
            with self._lock:
                if self._closed:
                    return
                close_error = None
                try:
                    if not self._poisoned:
                        self._rpc("close")
                except ChatProjectionStoreError as exc:
                    close_error = exc
                    if not self._poisoned:
                        self._poison_owner_locked()
                process = self._process
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=self._ipc_timeout_seconds)
                    except subprocess.TimeoutExpired as exc:
                        close_error = close_error or ChatProjectionStoreError(
                            "owner_unavailable", "projection owner did not close",
                        )
                        close_error.__cause__ = exc
                        self._poison_owner_locked()
                channel = self._channel
                self._channel = None
                self._process = None
                if channel is not None:
                    channel.close()
                self._close_secure_handles()
                self._closed = True
                if close_error is not None:
                    raise close_error
            return
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None
        self._close_secure_handles()


def _owner_dispatch(store: SQLiteChatProjectionStore, operation: str, arguments: Mapping[str, Any]) -> Any:
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
        rows = store.read_facts(**arguments)
        cursor = store.projection_cursor(arguments["root_id"], arguments["root_generation"])
        if rows and rows[-1].fact_sequence > cursor:
            raise ChatProjectionStoreError("storage_corrupt", "fact page exceeds projection head")
        return {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "after": arguments["after"], "projection_cursor": cursor, "rows": [
                {**asdict(item), "root_id": arguments["root_id"],
                 "root_generation": arguments["root_generation"]} for item in rows
            ],
        }
    if operation == "read_revisions":
        rows = store.read_revisions(**arguments)
        cursor = store.projection_cursor(arguments["root_id"], arguments["root_generation"])
        if rows and rows[-1].revision > cursor:
            raise ChatProjectionStoreError("storage_corrupt", "revision page exceeds projection head")
        return {
            "root_id": arguments["root_id"], "root_generation": arguments["root_generation"],
            "after": arguments["after"], "projection_cursor": cursor, "rows": [
                {**asdict(item), "root_id": arguments["root_id"],
                 "root_generation": arguments["root_generation"]} for item in rows
            ],
        }
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
    if operation == "close":
        store._file_checkpoint()
    method = getattr(store, operation)
    result = method(**arguments)
    if result is None:
        return None
    if isinstance(result, list):
        return [asdict(item) for item in result]
    if isinstance(result, (StoredProjection, SourceWatermark)):
        return asdict(result)
    return result


def _run_owner(
    channel_fd: int, directory_fd: int, file_fd: int, basename: str, test_fault: str,
) -> None:
    os.environ.clear()
    channel = socket.socket(fileno=channel_fd)
    store = None
    try:
        store = SQLiteChatProjectionStore(
            _owner_directory_fd=directory_fd, _owner_file_fd=file_fd, _owner_basename=basename,
        )
        _send_frame(channel, {"ready": True})
        while True:
            try:
                request = _receive_frame(channel)
            except ChatProjectionStoreError as exc:
                if exc.code == "owner_unavailable":
                    os._exit(1)
                raise
            if set(request) != {"request_id", "operation", "arguments"}:
                raise ChatProjectionStoreError("owner_protocol_error", "request shape is invalid")
            request_id = request["request_id"]
            operation = request["operation"]
            if type(request_id) is not int or not 0 <= request_id <= MAX_SQLITE_INTEGER:
                raise ChatProjectionStoreError("owner_protocol_error", "request id is invalid")
            if not isinstance(operation, str) or not isinstance(request["arguments"], Mapping):
                raise ChatProjectionStoreError("owner_protocol_error", "request shape is invalid")
            try:
                result = _owner_dispatch(store, operation, request["arguments"])
                if test_fault == "post_commit_stop" and operation == "commit":
                    os.kill(os.getpid(), signal.SIGSTOP)
                if test_fault == "malformed_response":
                    _send_frame(channel, {
                        "request_id": request_id + 1, "operation": operation,
                        "result": result, "unexpected": True,
                    })
                    test_fault = "none"
                    continue
                if test_fault == "semantic_mismatch" and isinstance(result, Mapping):
                    result = dict(result)
                    result["root_id"] = "mismatched-root"
                    test_fault = "none"
                try:
                    _send_frame(channel, {
                        "request_id": request_id, "operation": operation, "result": result,
                    }, limit=MAX_RESPONSE_BYTES)
                except ChatProjectionStoreError as exc:
                    if exc.code != "ipc_too_large":
                        raise
                    raise ChatProjectionStoreError(
                        "response_too_large", "owner result exceeds response budget",
                    ) from exc
                if operation == "close":
                    break
            except ChatProjectionStoreError as exc:
                _send_frame(channel, {
                    "request_id": request_id, "operation": operation,
                    "error": {"code": exc.code, "detail": exc.detail},
                })
            except BaseException:
                _send_frame(channel, {
                    "request_id": request_id, "operation": operation,
                    "error": {"code": "owner_internal_error", "detail": "owner operation failed"},
                })
    except ChatProjectionStoreError as exc:
        _send_frame(channel, {"error": {"code": exc.code, "detail": exc.detail}})
    except BaseException:
        try:
            _send_frame(channel, {"error": {"code": "owner_init_failed", "detail": "owner initialization failed"}})
        except BaseException:
            pass
    finally:
        if store is not None and store._connection is not None:
            store._connection.close()
        channel.close()
        os.close(file_fd)
        os.close(directory_fd)


if __name__ == "__main__" and len(sys.argv) == 7 and sys.argv[1] == "--projection-owner":
    import signal
    _run_owner(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6])
