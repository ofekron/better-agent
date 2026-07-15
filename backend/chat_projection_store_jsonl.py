from __future__ import annotations

import hashlib
import json
import os
import fcntl
import secrets
import sys
from pathlib import Path
from typing import Any, Mapping

from chat_projection_store import (
    ChatProjectionStoreError, CommitResult, ProjectionCommit, SourceWatermark, StoredFact,
    StoredProjection, StoredRevision, TurnManifest,
)
from chat_projection_store_owner import OwnerClient, serve_owner
from chat_projection_store_owner_path import secure_open, verify_anchored_file
from chat_projection_store_sqlite import (
    MAX_RESPONSE_BYTES, MAX_TEXT_BYTES, SQLiteChatProjectionStore, _owner_dispatch, canonical_json,
)
from paths import ba_home


JOURNAL_VERSION = 1


def _record_payload(sequence: int, previous_hash: str, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": JOURNAL_VERSION, "sequence": sequence, "previous_hash": previous_hash,
        "operation": operation, "arguments": arguments,
    }


def _record_line(sequence: int, previous_hash: str, operation: str, arguments: Mapping[str, Any]) -> tuple[bytes, str]:
    payload = _record_payload(sequence, previous_hash, operation, arguments)
    checksum = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    encoded = canonical_json({**payload, "checksum": checksum}).encode("utf-8") + b"\n"
    return encoded, checksum


class _JsonlOwnerStore:
    def __init__(self, directory_fd: int, journal_fd: int, basename: str) -> None:
        self._journal_fd = journal_fd
        self._basename = basename
        self._sequence = 0
        self._last_hash = "0" * 64
        try:
            fcntl.flock(journal_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ChatProjectionStoreError("writer_active", "JSONL journal already has an owner") from exc
        flags = fcntl.fcntl(journal_fd, fcntl.F_GETFL)
        fcntl.fcntl(journal_fd, fcntl.F_SETFL, flags | os.O_APPEND)
        verify_anchored_file(journal_fd, basename)
        index_name = f"{basename}.index.{os.getpid()}.{secrets.token_hex(8)}.sqlite3"
        _, index_directory_fd, index_fd, _ = secure_open(Path.cwd(), Path.cwd() / index_name)
        try:
            self._index = SQLiteChatProjectionStore(
                _owner_directory_fd=index_directory_fd, _owner_file_fd=index_fd,
                _owner_basename=index_name,
            )
        except BaseException:
            os.close(index_fd)
            os.close(index_directory_fd)
            raise
        self._rebuild()

    def _decode_record(
        self, raw: bytes, expected_sequence: int, previous_hash: str,
    ) -> Mapping[str, Any]:
        if len(raw) > 16 * 1024 * 1024:
            raise ChatProjectionStoreError("storage_corrupt", "JSONL journal row exceeds limit")
        def strict_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        try:
            record = json.loads(
                raw.decode("utf-8"), object_pairs_hook=strict_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ChatProjectionStoreError("storage_corrupt", "JSONL journal row is invalid") from exc
        if not isinstance(record, Mapping) or set(record) != {
            "version", "sequence", "previous_hash", "operation", "arguments", "checksum",
        }:
            raise ChatProjectionStoreError("storage_corrupt", "JSONL journal row shape is invalid")
        if record["version"] != JOURNAL_VERSION or record["sequence"] != expected_sequence:
            raise ChatProjectionStoreError("storage_corrupt", "JSONL journal sequence is invalid")
        if record["previous_hash"] != previous_hash:
            raise ChatProjectionStoreError("storage_corrupt", "JSONL journal chain is invalid")
        for key in ("previous_hash", "checksum"):
            if (
                not isinstance(record[key], str) or len(record[key]) != 64
                or any(character not in "0123456789abcdef" for character in record[key])
            ):
                raise ChatProjectionStoreError("storage_corrupt", "JSONL journal hash is invalid")
        payload = {key: record[key] for key in record if key != "checksum"}
        checksum = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        if record["checksum"] != checksum:
            raise ChatProjectionStoreError("storage_corrupt", "JSONL journal checksum is invalid")
        return record

    def _iter_records(self):
        os.lseek(self._journal_fd, 0, os.SEEK_SET)
        stream = os.fdopen(os.dup(self._journal_fd), "rb", buffering=1024 * 1024)
        previous_hash = "0" * 64
        sequence = 0
        try:
            while True:
                start = stream.tell()
                raw = stream.readline(16 * 1024 * 1024 + 2)
                if not raw:
                    break
                terminated = raw.endswith(b"\n")
                content = raw[:-1] if terminated else raw
                try:
                    record = self._decode_record(content, sequence + 1, previous_hash)
                except ChatProjectionStoreError:
                    if terminated:
                        raise
                    os.ftruncate(self._journal_fd, start)
                    os.fsync(self._journal_fd)
                    break
                if not terminated:
                    os.write(self._journal_fd, b"\n")
                    os.fsync(self._journal_fd)
                sequence += 1
                previous_hash = record["checksum"]
                yield record
        finally:
            stream.close()
            self._sequence = sequence
            self._last_hash = previous_hash

    def _rebuild(self) -> None:
        with self._index._lock:
            self._index._connection.execute("BEGIN IMMEDIATE")
            for table in self._index._TABLES:
                self._index._connection.execute(f'DELETE FROM "{table}"')
            self._index._connection.commit()
        for record in self._iter_records():
            try:
                self._apply(record["operation"], record["arguments"])
            except BaseException as exc:
                raise ChatProjectionStoreError(
                    "storage_corrupt", "JSONL journal violates projection state",
                ) from exc

    def _append(self, operation: str, arguments: Mapping[str, Any]) -> None:
        line, checksum = _record_line(self._sequence + 1, self._last_hash, operation, arguments)
        os.lseek(self._journal_fd, 0, os.SEEK_END)
        view = memoryview(line)
        while view:
            written = os.write(self._journal_fd, view)
            if written <= 0:
                raise ChatProjectionStoreError("storage_write_failed", "JSONL journal append failed")
            view = view[written:]
        os.fsync(self._journal_fd)
        self._sequence += 1
        self._last_hash = checksum

    def _apply(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        if operation == "commit":
            return self._index.commit(self._index._commit_from_dict(arguments["request"]))
        if operation == "watermark_advanced":
            if set(arguments) != {
                "root_id", "root_generation", "stream_id", "source_generation", "source_sequence",
            }:
                raise ChatProjectionStoreError("storage_corrupt", "watermark control shape is invalid")
            self._index._identity(arguments["root_id"], arguments["root_generation"])
            self._index._text("stream_id", arguments["stream_id"])
            self._index._integer("source_generation", arguments["source_generation"])
            self._index._integer("source_sequence", arguments["source_sequence"])
            values = (
                arguments["root_id"], arguments["root_generation"], arguments["stream_id"],
                arguments["source_generation"], arguments["source_sequence"],
            )
            self._index._connection.execute(
                "INSERT INTO source_watermarks VALUES(?,?,?,?,?) ON CONFLICT(root_id,root_generation,stream_id) "
                "DO UPDATE SET source_generation=excluded.source_generation,source_sequence=excluded.source_sequence",
                values,
            )
            self._index._connection.commit()
            return None
        return getattr(self._index, operation)(**arguments)

    def _selected_generation(self, root_id: str) -> int | None:
        row = self._index._connection.execute(
            "SELECT root_generation FROM selected_roots WHERE root_id=?", (root_id,),
        ).fetchone()
        return self._index._stored_int(row[0]) if row else None

    def select_generation(self, root_id: str, root_generation: int) -> None:
        self._index._identity(root_id, root_generation)
        current = self._selected_generation(root_id)
        if current is not None and root_generation < current:
            raise ChatProjectionStoreError("stale_generation", "root generation is fenced")
        if current == root_generation:
            return
        arguments = {"root_id": root_id, "root_generation": root_generation}
        self._append("select_generation", arguments)
        self._apply("select_generation", arguments)

    def commit(self, request: ProjectionCommit) -> CommitResult:
        self._index._validate_commit(request)
        if self._selected_generation(request.root_id) != request.root_generation:
            raise ChatProjectionStoreError("stale_generation", "root generation is not selected")
        duplicate = self._index._connection.execute(
            "SELECT fact_sequence FROM canonical_facts WHERE root_id=? AND root_generation=? AND event_id=? AND content_hash=?",
            (request.root_id, request.root_generation, request.event_id, request.content_hash),
        ).fetchone()
        current_watermark = self._index._connection.execute(
            "SELECT source_generation,source_sequence FROM source_watermarks WHERE root_id=? AND root_generation=? AND stream_id=?",
            (request.root_id, request.root_generation, request.watermark.stream_id),
        ).fetchone()
        candidate = (request.watermark.generation, request.watermark.sequence)
        current = tuple(map(self._index._stored_int, current_watermark)) if current_watermark else None
        if current is not None and candidate < current:
            raise ChatProjectionStoreError("watermark_regression", "source watermark cannot regress")
        if duplicate:
            head = self._index._connection.execute(
                "SELECT revision,projection_cursor FROM root_generation_heads WHERE root_id=? AND root_generation=?",
                (request.root_id, request.root_generation),
            ).fetchone()
            if current is None or candidate > current:
                arguments = {
                    "root_id": request.root_id, "root_generation": request.root_generation,
                    "stream_id": request.watermark.stream_id,
                    "source_generation": request.watermark.generation,
                    "source_sequence": request.watermark.sequence,
                }
                self._append("watermark_advanced", arguments)
                self._apply("watermark_advanced", arguments)
            return CommitResult(
                True, self._index._stored_int(duplicate[0]), self._index._stored_int(head[0]),
                self._index._stored_int(head[1]),
            )
        arguments = {"request": self._index._commit_to_dict(request)}
        self._append("commit", arguments)
        return self._apply("commit", arguments)

    def delete_generation(self, root_id: str, root_generation: int) -> None:
        self._index._identity(root_id, root_generation)
        exists = self._index._connection.execute(
            "SELECT 1 FROM root_generation_heads WHERE root_id=? AND root_generation=?",
            (root_id, root_generation),
        ).fetchone()
        if exists is None:
            raise ChatProjectionStoreError("missing_generation", "root generation does not exist")
        if self._selected_generation(root_id) == root_generation:
            raise ChatProjectionStoreError("current_generation", "selected generation cannot be deleted")
        arguments = {"root_id": root_id, "root_generation": root_generation}
        self._append("delete_generation", arguments)
        self._apply("delete_generation", arguments)

    def delete_root(self, root_id: str) -> None:
        self._index._identity(root_id, 0)
        exists = self._index._connection.execute(
            "SELECT 1 FROM selected_roots WHERE root_id=? UNION SELECT 1 FROM root_generation_heads WHERE root_id=? LIMIT 1",
            (root_id, root_id),
        ).fetchone()
        if exists is None:
            raise ChatProjectionStoreError("missing_root", "root does not exist")
        arguments = {"root_id": root_id}
        self._append("delete_root", arguments)
        self._apply("delete_root", arguments)

    def close(self) -> None:
        self._index.close()

    def __getattr__(self, name: str):
        return getattr(self._index, name)


class JsonlChatProjectionStore:
    def __init__(self, path: Path | None = None, *, _ipc_timeout_seconds: float = 30) -> None:
        root = Path(os.path.abspath(ba_home().expanduser()))
        selected = path or root / "chat" / "selected.jsonl"
        validator = SQLiteChatProjectionStore.__new__(SQLiteChatProjectionStore)
        self._owner = OwnerClient(
            root_path=root, path=selected, owner_script=Path(__file__), owner_arguments=(),
            validate_result=validator._validate_rpc_result,
            ipc_timeout_seconds=_ipc_timeout_seconds, max_error_text_bytes=MAX_TEXT_BYTES,
            require_sqlite_header=False,
        )

    def select_generation(self, root_id: str, root_generation: int) -> None:
        self._owner.rpc("select_generation", root_id=root_id, root_generation=root_generation)

    def commit(self, request: ProjectionCommit) -> CommitResult:
        payload = SQLiteChatProjectionStore._commit_to_dict(request)
        return CommitResult(**self._owner.rpc("commit", request=payload))

    def read_facts(self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000):
        rows = self._owner.rpc("read_facts", root_id=root_id, root_generation=root_generation, after=after, limit=limit)
        return [StoredFact(**row) for row in rows]

    def read_revisions(self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000):
        rows = self._owner.rpc("read_revisions", root_id=root_id, root_generation=root_generation, after=after, limit=limit)
        return [StoredRevision(**row) for row in rows]

    def projection_cursor(self, root_id: str, root_generation: int) -> int:
        return self._owner.rpc("projection_cursor", root_id=root_id, root_generation=root_generation)

    def delete_generation(self, root_id: str, root_generation: int) -> None:
        self._owner.rpc("delete_generation", root_id=root_id, root_generation=root_generation)

    def delete_root(self, root_id: str) -> None:
        self._owner.rpc("delete_root", root_id=root_id)

    def read_projection(self, root_id: str, root_generation: int, event_id: str):
        result = self._owner.rpc("read_projection", root_id=root_id, root_generation=root_generation, event_id=event_id)
        if result is None:
            return None
        result["manifest"] = TurnManifest(**result["manifest"])
        return StoredProjection(**result)

    def source_watermark(self, root_id: str, root_generation: int, stream_id: str):
        result = self._owner.rpc("source_watermark", root_id=root_id, root_generation=root_generation, stream_id=stream_id)
        return SourceWatermark(**result) if result else None

    def close(self) -> None:
        self._owner.close()


def _run_owner(channel_fd: int, directory_fd: int, file_fd: int, basename: str) -> None:
    serve_owner(
        channel_fd, directory_fd, file_fd, basename,
        lambda owner_directory_fd, owner_file_fd, owner_basename: _JsonlOwnerStore(
            owner_directory_fd, owner_file_fd, owner_basename,
        ),
        _owner_dispatch, lambda store: store.close(),
        lambda _channel, _request_id, _operation, result: (result, False), MAX_RESPONSE_BYTES,
    )


if __name__ == "__main__" and len(sys.argv) == 6 and sys.argv[1] == "--projection-owner":
    _run_owner(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
