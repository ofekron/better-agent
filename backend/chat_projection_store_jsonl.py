from __future__ import annotations

import hashlib
import json
import os
import fcntl
import stat
import sqlite3
import threading
import sys
from pathlib import Path
from typing import Any, Mapping

from chat_projection_store import (
    ChatProjectionStoreError, CommitResult, ProjectionCommit, SourceAdmission, SourceWatermark, StoredFact,
    StoredProjection, StoredRevision, TurnManifest, TurnWindowPage,
)
from chat_projection_store_owner import OwnerClient, serve_owner
from chat_projection_store_owner_path import secure_open, verify_anchored_file
from chat_projection_store_sqlite import (
    AUTOINDEX_COUNTS, MAX_RESPONSE_BYTES, MAX_TEXT_BYTES, TABLE_DDL,
    SQLiteChatProjectionStore, _owner_dispatch, canonical_json,
)
from paths import ba_home


JOURNAL_VERSION = 1
MAX_JSONL_ROW_BYTES = 16 * 1024 * 1024
INDEX_SLOTS = 4
CHECKPOINT_DDL = (
    "CREATE TABLE jsonl_checkpoint(slot_generation INTEGER NOT NULL,journal_dev INTEGER NOT NULL,"
    "journal_ino INTEGER NOT NULL,byte_offset INTEGER NOT NULL,record_sequence INTEGER NOT NULL,"
    "chain_head TEXT NOT NULL,prefix_digest TEXT NOT NULL,integrity_json TEXT NOT NULL)"
)
CHECKPOINT_SPEC = (
    ("slot_generation", "INTEGER", 1, 0, None), ("journal_dev", "INTEGER", 1, 0, None),
    ("journal_ino", "INTEGER", 1, 0, None), ("byte_offset", "INTEGER", 1, 0, None),
    ("record_sequence", "INTEGER", 1, 0, None), ("chain_head", "TEXT", 1, 0, None),
    ("prefix_digest", "TEXT", 1, 0, None), ("integrity_json", "TEXT", 1, 0, None),
)
INTEGRITY_DDL = (
    "CREATE TABLE jsonl_integrity(table_name TEXT PRIMARY KEY NOT NULL,row_count INTEGER NOT NULL,"
    "sum_a INTEGER NOT NULL,sum_b INTEGER NOT NULL) WITHOUT ROWID"
)
INTEGRITY_SPEC = (
    ("table_name", "TEXT", 1, 1, None), ("row_count", "INTEGER", 1, 0, None),
    ("sum_a", "INTEGER", 1, 0, None), ("sum_b", "INTEGER", 1, 0, None),
)
INTEGRITY_MODULUS_A = 9_223_372_036_854_775_783
INTEGRITY_MODULUS_B = 9_223_372_036_854_775_643


def _integrity_triggers() -> dict[str, tuple[str, str]]:
    triggers = {}
    for table in SQLiteChatProjectionStore._TABLES:
        columns = [column[0] for column in SQLiteChatProjectionStore._TABLES[table]]
        old_values = ",".join(f'OLD."{column}"' for column in columns)
        new_values = ",".join(f'NEW."{column}"' for column in columns)
        for operation, remove, add, count in (
            ("INSERT", "0", f"jsonl_row_a('{table}',{new_values})", 1),
            ("DELETE", f"jsonl_row_a('{table}',{old_values})", "0", -1),
            (
                "UPDATE", f"jsonl_row_a('{table}',{old_values})",
                f"jsonl_row_a('{table}',{new_values})", 0,
            ),
        ):
            name = f"jsonl_integrity_{table}_{operation.lower()}"
            remove_b = remove.replace("jsonl_row_a", "jsonl_row_b")
            add_b = add.replace("jsonl_row_a", "jsonl_row_b")
            ddl = (
                f'CREATE TRIGGER "{name}" AFTER {operation} ON "{table}" BEGIN '
                "UPDATE jsonl_integrity SET "
                f"row_count=row_count+({count}),sum_a=jsonl_accumulate_a(sum_a,{remove},{add}),"
                f"sum_b=jsonl_accumulate_b(sum_b,{remove_b},{add_b}) WHERE table_name='{table}'; "
                "DELETE FROM jsonl_checkpoint; END"
            )
            triggers[name] = (table, ddl)
    for operation in ("INSERT", "UPDATE", "DELETE"):
        name = f"jsonl_guard_jsonl_integrity_{operation.lower()}"
        triggers[name] = (
            "jsonl_integrity",
            f'CREATE TRIGGER "{name}" AFTER {operation} ON "jsonl_integrity" '
            "BEGIN DELETE FROM jsonl_checkpoint; END",
        )
    return triggers


INTEGRITY_TRIGGERS = _integrity_triggers()
JSONL_TABLE_DDL = {
    **TABLE_DDL, "jsonl_checkpoint": CHECKPOINT_DDL, "jsonl_integrity": INTEGRITY_DDL,
}
JSONL_TABLE_SPECS = {
    **SQLiteChatProjectionStore._TABLES,
    "jsonl_checkpoint": CHECKPOINT_SPEC, "jsonl_integrity": INTEGRITY_SPEC,
}
JSONL_UNIQUE_INDEXES = SQLiteChatProjectionStore._UNIQUE_INDEXES
JSONL_AUTOINDEX_COUNTS = AUTOINDEX_COUNTS


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
    def __init__(
        self, directory_fd: int, journal_fd: int, basename: str, test_owner_fault: str | None = None,
        force_rebuild: bool = False,
    ) -> None:
        self._journal_fd = journal_fd
        self._basename = basename
        self._test_owner_fault = test_owner_fault
        self._sequence = 0
        self._last_hash = "0" * 64
        try:
            fcntl.flock(journal_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ChatProjectionStoreError("writer_active", "JSONL journal already has an owner") from exc
        flags = fcntl.fcntl(journal_fd, fcntl.F_GETFL)
        fcntl.fcntl(journal_fd, fcntl.F_SETFL, flags | os.O_APPEND)
        verify_anchored_file(journal_fd, basename)
        checkpoint = None if force_rebuild else self._select_checkpoint()
        if checkpoint is None:
            index_name = self._absent_slot()
            start_offset, start_sequence, start_hash, prefix_digest = 0, 0, "0" * 64, "0" * 64
        else:
            index_name, start_offset, start_sequence, start_hash, prefix_digest = checkpoint
        _, index_directory_fd, index_fd, _ = secure_open(Path.cwd(), Path.cwd() / index_name)
        self._index_directory_fd = index_directory_fd
        self._index_fd = index_fd
        try:
            self._index = SQLiteChatProjectionStore(
                _owner_directory_fd=index_directory_fd, _owner_file_fd=index_fd,
                _owner_basename=index_name,
                _before_transaction_commit=lambda connection: self._write_checkpoint(
                    connection, standalone=False,
                ),
                _extra_table_ddl={
                    name: ddl for name, ddl in JSONL_TABLE_DDL.items() if name not in TABLE_DDL
                },
                _extra_table_specs={
                    name: spec for name, spec in JSONL_TABLE_SPECS.items()
                    if name not in SQLiteChatProjectionStore._TABLES
                },
                _extra_schema_objects=INTEGRITY_TRIGGERS,
            )
        except BaseException:
            os.close(index_fd)
            os.close(index_directory_fd)
            raise
        self._install_integrity_functions()
        self._checkpoint_rows_read = 0
        if checkpoint is None:
            self._clear_index()
        else:
            self._initialize_integrity()
        self._sequence = start_sequence
        self._last_hash = start_hash
        self._prefix_digest = prefix_digest
        self._startup_read_bytes = 0
        self._replay(start_offset)
        self._write_checkpoint()
        self._audit_lock = threading.Lock()
        self._audit_done = threading.Event()
        self._audit_status = "valid" if checkpoint is None else "pending"
        self._audit_fatal = False
        self._audit_offset = start_offset
        self._audit_expected = (start_sequence, start_hash, prefix_digest)
        if checkpoint is not None:
            threading.Thread(target=self._audit_worker, daemon=True).start()

    def _slot_names(self) -> list[str]:
        return [f"{self._basename}.index.{slot}.sqlite3" for slot in range(INDEX_SLOTS)]

    def _safe_slot(self, name: str) -> bool:
        try:
            metadata = os.stat(name, follow_symlinks=False)
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
        )

    @staticmethod
    def _row_integrity(component: int, table: str, *values: Any) -> int:
        encoded = canonical_json([table, *values]).encode("utf-8")
        digest = hashlib.sha256(encoded).digest()
        start = component * 16
        modulus = INTEGRITY_MODULUS_A if component == 0 else INTEGRITY_MODULUS_B
        return int.from_bytes(digest[start:start + 16], "big") % modulus

    def _install_integrity_functions(self) -> None:
        connection = self._index._connection
        connection.create_function(
            "jsonl_row_a", -1, lambda table, *values: self._row_integrity(0, table, *values),
            deterministic=True,
        )
        connection.create_function(
            "jsonl_row_b", -1, lambda table, *values: self._row_integrity(1, table, *values),
            deterministic=True,
        )
        connection.create_function(
            "jsonl_accumulate_a", 3,
            lambda current, remove, add: (current - remove + add) % INTEGRITY_MODULUS_A,
            deterministic=True,
        )
        connection.create_function(
            "jsonl_accumulate_b", 3,
            lambda current, remove, add: (current - remove + add) % INTEGRITY_MODULUS_B,
            deterministic=True,
        )

    def _initialize_integrity(self) -> None:
        with self._index._lock:
            self._index._connection.executemany(
                "INSERT OR IGNORE INTO jsonl_integrity VALUES(?,0,0,0)",
                ((table,) for table in sorted(SQLiteChatProjectionStore._TABLES)),
            )
            self._index._connection.commit()

    def _state_integrity(self, connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT table_name,row_count,sum_a,sum_b FROM jsonl_integrity ORDER BY table_name"
        ).fetchall()
        self._checkpoint_rows_read += len(rows)
        if len(rows) != len(SQLiteChatProjectionStore._TABLES):
            raise ChatProjectionStoreError("storage_corrupt", "projection integrity summary is incomplete")
        return canonical_json([list(row) for row in rows])

    def _select_checkpoint(self):
        journal = os.fstat(self._journal_fd)
        candidates = []
        for name in self._slot_names():
            if not self._safe_slot(name):
                continue
            try:
                connection = sqlite3.connect(f"file:{name}?mode=rw", uri=True)
                SQLiteChatProjectionStore._validate_schema_connection(
                    connection,
                    table_ddl=JSONL_TABLE_DDL,
                    table_specs=JSONL_TABLE_SPECS,
                    extra_schema_objects=INTEGRITY_TRIGGERS,
                    unique_indexes=JSONL_UNIQUE_INDEXES,
                    autoindex_counts=JSONL_AUTOINDEX_COUNTS,
                )
                row = connection.execute("SELECT * FROM jsonl_checkpoint").fetchone()
                if row is None or connection.execute("SELECT COUNT(*) FROM jsonl_checkpoint").fetchone()[0] != 1:
                    connection.close()
                    continue
                generation, dev, ino, offset, sequence, chain, prefix, integrity = row
                integrity_rows = connection.execute(
                        "SELECT table_name,row_count,sum_a,sum_b FROM jsonl_integrity ORDER BY table_name"
                    ).fetchall()
                actual_integrity = canonical_json([list(item) for item in integrity_rows])
                valid = (
                    type(generation) is int and type(offset) is int and type(sequence) is int
                    and (dev, ino) == (journal.st_dev, journal.st_ino) and 0 <= offset <= journal.st_size
                    and len(chain) == 64 and len(prefix) == 64
                    and all(character in "0123456789abcdef" for character in chain + prefix)
                    and len(integrity_rows) == len(SQLiteChatProjectionStore._TABLES)
                    and integrity == actual_integrity
                    and self._checkpoint_boundary_valid(offset, sequence, chain)
                )
                connection.close()
                if valid:
                    candidates.append((offset, sequence, generation, name, chain, prefix))
            except (ChatProjectionStoreError, sqlite3.Error, OSError, TypeError):
                continue
        if not candidates:
            return None
        offset, sequence, _generation, name, chain, prefix = max(candidates)
        return name, offset, sequence, chain, prefix

    def _checkpoint_boundary_valid(self, offset: int, sequence: int, chain_head: str) -> bool:
        if offset == 0:
            return sequence == 0 and chain_head == "0" * 64
        if os.pread(self._journal_fd, 1, offset - 1) != b"\n":
            return False
        size = min(offset, MAX_JSONL_ROW_BYTES + 2)
        window = os.pread(self._journal_fd, size, offset - size)
        content = window[:-1]
        separator = content.rfind(b"\n")
        raw = content[separator + 1:]
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(record, Mapping) or record.get("sequence") != sequence:
            return False
        payload = {key: record[key] for key in record if key != "checksum"}
        checksum = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return record.get("checksum") == checksum == chain_head

    def _audit_worker(self) -> None:
        sequence, offset = 0, 0
        chain = prefix = "0" * 64
        failed = False
        try:
            while offset < self._audit_offset:
                line = bytearray()
                while offset + len(line) < self._audit_offset:
                    remaining = min(64 * 1024, self._audit_offset - offset - len(line))
                    chunk = os.pread(self._journal_fd, remaining, offset + len(line))
                    if not chunk:
                        break
                    newline = chunk.find(b"\n")
                    line.extend(chunk if newline < 0 else chunk[:newline + 1])
                    if newline >= 0 or len(line) > MAX_JSONL_ROW_BYTES + 1:
                        break
                raw = bytes(line)
                if not raw.endswith(b"\n") or offset + len(raw) > self._audit_offset:
                    failed = True
                    break
                record = self._decode_record(raw[:-1], sequence + 1, chain)
                sequence += 1
                chain = record["checksum"]
                prefix = hashlib.sha256(bytes.fromhex(prefix) + raw).hexdigest()
                offset += len(raw)
            failed = failed or offset != self._audit_offset
            failed = failed or (sequence, chain, prefix) != self._audit_expected
        except BaseException:
            failed = True
        finally:
            with self._audit_lock:
                self._audit_status = "failed" if failed else "valid"
                self._audit_fatal = failed
                self._audit_done.set()

    def _ensure_healthy(self) -> None:
        with self._audit_lock:
            if self._audit_fatal:
                raise ChatProjectionStoreError("rebuild_required", "JSONL prefix audit failed")

    def _absent_slot(self) -> str:
        for name in self._slot_names():
            try:
                os.stat(name, follow_symlinks=False)
            except FileNotFoundError:
                return name
            except OSError:
                continue
        raise ChatProjectionStoreError("projection_slots_exhausted", "no safe projection index slot is available")

    def _clear_index(self) -> None:
        with self._index._lock:
            self._index._connection.execute("BEGIN IMMEDIATE")
            for table in SQLiteChatProjectionStore._TABLES:
                self._index._connection.execute(f'DELETE FROM "{table}"')
            self._index._connection.execute("DELETE FROM jsonl_integrity")
            self._index._connection.executemany(
                "INSERT INTO jsonl_integrity VALUES(?,0,0,0)",
                ((table,) for table in sorted(SQLiteChatProjectionStore._TABLES)),
            )
            self._index._connection.commit()

    def _write_checkpoint(
        self, connection: sqlite3.Connection | None = None, *, standalone: bool = True,
    ) -> None:
        connection = connection or self._index._connection
        journal = os.fstat(self._journal_fd)
        if standalone:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute(CHECKPOINT_DDL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        connection.execute("DELETE FROM jsonl_checkpoint")
        connection.execute(
            "INSERT INTO jsonl_checkpoint VALUES(?,?,?,?,?,?,?,?)",
            (self._sequence, journal.st_dev, journal.st_ino, os.lseek(self._journal_fd, 0, os.SEEK_END),
             self._sequence, self._last_hash, self._prefix_digest, self._state_integrity(connection)),
        )
        if standalone:
            connection.commit()

    def _decode_record(
        self, raw: bytes, expected_sequence: int, previous_hash: str,
    ) -> Mapping[str, Any]:
        if len(raw) > MAX_JSONL_ROW_BYTES:
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

    def _iter_records(
        self, start_offset: int = 0, start_sequence: int = 0,
        start_hash: str = "0" * 64, start_prefix: str = "0" * 64,
    ):
        os.lseek(self._journal_fd, start_offset, os.SEEK_SET)
        stream = os.fdopen(os.dup(self._journal_fd), "rb", buffering=1024 * 1024)
        previous_hash = start_hash
        prefix_digest = start_prefix
        sequence = start_sequence
        try:
            while True:
                start = stream.tell()
                raw = stream.readline(16 * 1024 * 1024 + 2)
                if not raw:
                    break
                self._startup_read_bytes += len(raw)
                terminated = raw.endswith(b"\n")
                content = raw[:-1] if terminated else raw
                if len(content) > MAX_JSONL_ROW_BYTES:
                    raise ChatProjectionStoreError("storage_corrupt", "JSONL journal row exceeds limit")
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
                prefix_digest = hashlib.sha256(bytes.fromhex(prefix_digest) + content + b"\n").hexdigest()
                yield record
        finally:
            stream.close()
            self._sequence = sequence
            self._last_hash = previous_hash
            self._prefix_digest = prefix_digest

    def _replay(self, start_offset: int) -> None:
        for record in self._iter_records(
            start_offset, self._sequence, self._last_hash, self._prefix_digest,
        ):
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
        self._prefix_digest = hashlib.sha256(bytes.fromhex(self._prefix_digest) + line).hexdigest()
        if self._test_owner_fault == "post_append_failure":
            self._test_owner_fault = None
            raise ChatProjectionStoreError(
                "storage_write_failed", "injected failure after journal durability",
            )

    def _apply(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        if operation == "commit":
            return self._index.commit(self._index._commit_from_dict(arguments["request"]))
        if operation == "duplicate_admitted":
            if set(arguments) != {
                "root_id", "root_generation", "stream_id", "source_generation", "source_sequence",
                "event_id", "content_hash", "fact_sequence", "revision", "projection_cursor",
            }:
                raise ChatProjectionStoreError("storage_corrupt", "duplicate admission shape is invalid")
            self._index._identity(arguments["root_id"], arguments["root_generation"])
            for name in ("stream_id", "event_id", "content_hash"):
                self._index._text(name, arguments[name])
            for name in (
                "source_generation", "source_sequence", "fact_sequence", "revision",
                "projection_cursor",
            ):
                self._index._integer(name, arguments[name])
            if (
                len(arguments["content_hash"]) != 64
                or any(character not in "0123456789abcdef" for character in arguments["content_hash"])
            ):
                raise ChatProjectionStoreError("storage_corrupt", "duplicate admission hash is invalid")
            selected = self._selected_generation(arguments["root_id"])
            head = self._index._connection.execute(
                "SELECT revision,projection_cursor FROM root_generation_heads "
                "WHERE root_id=? AND root_generation=?",
                (arguments["root_id"], arguments["root_generation"]),
            ).fetchone()
            if selected != arguments["root_generation"] or head is None:
                raise ChatProjectionStoreError("storage_corrupt", "duplicate admission generation is invalid")
            fact = self._index._connection.execute(
                "SELECT event_id,content_hash FROM canonical_facts WHERE root_id=? "
                "AND root_generation=? AND fact_sequence=?",
                (arguments["root_id"], arguments["root_generation"], arguments["fact_sequence"]),
            ).fetchone()
            if (
                fact is None or tuple(fact) != (arguments["event_id"], arguments["content_hash"])
                or tuple(map(self._index._stored_int, head)) != (
                    arguments["revision"], arguments["projection_cursor"],
                )
            ):
                raise ChatProjectionStoreError("storage_corrupt", "duplicate admission identity is invalid")
            connection = self._index._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                admission = connection.execute(
                    "SELECT event_id,content_hash,fact_sequence,revision,projection_cursor "
                    "FROM source_admissions WHERE root_id=? AND root_generation=? AND stream_id=? "
                    "AND source_generation=? AND source_sequence=?",
                    (
                        arguments["root_id"], arguments["root_generation"], arguments["stream_id"],
                        arguments["source_generation"], arguments["source_sequence"],
                    ),
                ).fetchone()
                expected = (
                    arguments["event_id"], arguments["content_hash"], arguments["fact_sequence"],
                    arguments["revision"], arguments["projection_cursor"],
                )
                if admission is not None and tuple(admission) != expected:
                    raise ChatProjectionStoreError("storage_corrupt", "duplicate admission conflicts")
                current = connection.execute(
                    "SELECT source_generation,source_sequence FROM source_watermarks "
                    "WHERE root_id=? AND root_generation=? AND stream_id=?",
                    (arguments["root_id"], arguments["root_generation"], arguments["stream_id"]),
                ).fetchone()
                candidate = (arguments["source_generation"], arguments["source_sequence"])
                if admission is None and current is not None and candidate < tuple(map(self._index._stored_int, current)):
                    raise ChatProjectionStoreError("storage_corrupt", "duplicate admission watermark regresses")
                connection.execute(
                    "INSERT INTO source_watermarks VALUES(?,?,?,?,?) "
                    "ON CONFLICT(root_id,root_generation,stream_id) DO UPDATE SET "
                    "source_generation=excluded.source_generation,source_sequence=excluded.source_sequence "
                    "WHERE (excluded.source_generation,excluded.source_sequence) > "
                    "(source_watermarks.source_generation,source_watermarks.source_sequence)",
                    (
                        arguments["root_id"], arguments["root_generation"], arguments["stream_id"],
                        *candidate,
                    ),
                )
                if admission is None:
                    connection.execute(
                        "INSERT INTO source_admissions VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            arguments["root_id"], arguments["root_generation"], arguments["stream_id"],
                            *candidate, *expected,
                        ),
                    )
                self._write_checkpoint(connection, standalone=False)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return None
        return getattr(self._index, operation)(**arguments)

    def _selected_generation(self, root_id: str) -> int | None:
        row = self._index._connection.execute(
            "SELECT root_generation FROM selected_roots WHERE root_id=?", (root_id,),
        ).fetchone()
        return self._index._stored_int(row[0]) if row else None

    def select_generation(self, root_id: str, root_generation: int) -> None:
        self._ensure_healthy()
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
        self._ensure_healthy()
        self._index._validate_commit(request)
        if self._selected_generation(request.root_id) != request.root_generation:
            raise ChatProjectionStoreError("stale_generation", "root generation is not selected")
        admission = self._index._connection.execute(
            "SELECT event_id,content_hash FROM source_admissions WHERE root_id=? "
            "AND root_generation=? AND stream_id=? AND source_generation=? AND source_sequence=?",
            (
                request.root_id, request.root_generation, request.watermark.stream_id,
                request.watermark.generation, request.watermark.sequence,
            ),
        ).fetchone()
        if admission is not None:
            if tuple(admission) != (request.event_id, request.content_hash):
                raise ChatProjectionStoreError(
                    "source_conflict", "source sequence carries different content",
                )
            return self._index.commit(request)
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
                    "event_id": request.event_id, "content_hash": request.content_hash,
                    "fact_sequence": self._index._stored_int(duplicate[0]),
                    "revision": self._index._stored_int(head[0]),
                    "projection_cursor": self._index._stored_int(head[1]),
                }
                self._append("duplicate_admitted", arguments)
                self._apply("duplicate_admitted", arguments)
            return CommitResult(
                True, self._index._stored_int(duplicate[0]), self._index._stored_int(head[0]),
                self._index._stored_int(head[1]),
            )
        arguments = {"request": self._index._commit_to_dict(request)}
        self._append("commit", arguments)
        result = self._apply("commit", arguments)
        return result

    def delete_generation(self, root_id: str, root_generation: int) -> None:
        self._ensure_healthy()
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
        self._ensure_healthy()
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
        for name in ("_index_fd", "_index_directory_fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                setattr(self, name, None)
                os.close(descriptor)

    def audit_prefix(self) -> None:
        self._audit_done.wait()
        self._ensure_healthy()

    def audit_status(self) -> str:
        with self._audit_lock:
            return self._audit_status

    def checkpoint_rows_read(self) -> int:
        return self._checkpoint_rows_read

    def __getattr__(self, name: str):
        value = getattr(self._index, name)
        if not callable(value):
            return value
        def guarded(*args, **kwargs):
            self._ensure_healthy()
            return value(*args, **kwargs)
        return guarded


class JsonlChatProjectionStore:
    def __init__(
        self, path: Path | None = None, *, _ipc_timeout_seconds: float = 30,
        _test_owner_fault: str | None = None, _force_rebuild: bool = False,
    ) -> None:
        if _test_owner_fault not in {None, "post_append_failure"}:
            raise ChatProjectionStoreError("invalid_input", "unknown owner test fault")
        if type(_force_rebuild) is not bool:
            raise ChatProjectionStoreError("invalid_input", "force rebuild flag is invalid")
        root = Path(os.path.abspath(ba_home().expanduser()))
        selected = path or root / "chat" / "selected.jsonl"
        validator = SQLiteChatProjectionStore.__new__(SQLiteChatProjectionStore)
        def validate_result(operation: str, result: Any, arguments: Mapping[str, Any]):
            if operation == "audit_prefix":
                if result is not None or arguments:
                    raise ChatProjectionStoreError("owner_protocol_error", "invalid audit result")
                return None
            if operation in {"startup_read_bytes", "checkpoint_rows_read"}:
                if type(result) is not int or result < 0 or arguments:
                    raise ChatProjectionStoreError("owner_protocol_error", "invalid startup metric")
                return result
            if operation == "audit_status":
                if result not in {"pending", "valid", "failed"} or arguments:
                    raise ChatProjectionStoreError("owner_protocol_error", "invalid audit status")
                return result
            return validator._validate_rpc_result(operation, result, arguments)
        self._owner = OwnerClient(
            root_path=root, path=selected, owner_script=Path(__file__),
            owner_arguments=(_test_owner_fault or "none", "rebuild" if _force_rebuild else "reuse"),
            validate_result=validate_result,
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

    def read_turn_window(
        self, root_id: str, root_generation: int, *, pane_id: str, turns: int,
        before_turn: str | None = None, after: int = 0, limit: int = 1000,
    ) -> TurnWindowPage:
        result = self._owner.rpc(
            "read_turn_window", root_id=root_id, root_generation=root_generation,
            pane_id=pane_id, turns=turns, before_turn=before_turn, after=after, limit=limit,
        )
        return TurnWindowPage(
            tuple(StoredFact(**row) for row in result["rows"]),
            result["cursor_found"], result["projection_cursor"],
        )

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

    def source_admission(
        self, root_id: str, root_generation: int, stream_id: str,
        source_generation: int, source_sequence: int,
    ) -> SourceAdmission | None:
        result = self._owner.rpc(
            "source_admission", root_id=root_id, root_generation=root_generation,
            stream_id=stream_id, source_generation=source_generation,
            source_sequence=source_sequence,
        )
        return SourceAdmission(**result) if result else None

    def close(self) -> None:
        self._owner.close()

    def audit_prefix(self) -> None:
        self._owner.rpc("audit_prefix")

    def startup_read_bytes(self) -> int:
        return self._owner.rpc("startup_read_bytes")

    def audit_status(self) -> str:
        return self._owner.rpc("audit_status")

    def checkpoint_rows_read(self) -> int:
        return self._owner.rpc("checkpoint_rows_read")


def _run_owner(
    channel_fd: int, directory_fd: int, file_fd: int, basename: str, test_owner_fault: str,
    rebuild_mode: str,
) -> None:
    if rebuild_mode not in {"reuse", "rebuild"}:
        raise ChatProjectionStoreError("owner_protocol_error", "invalid rebuild mode")
    def dispatch(store, operation: str, arguments: Mapping[str, Any], request_id: int):
        if operation == "audit_prefix":
            if arguments:
                raise ChatProjectionStoreError("owner_protocol_error", "audit arguments are invalid")
            return store.audit_prefix()
        if operation == "startup_read_bytes":
            if arguments:
                raise ChatProjectionStoreError("owner_protocol_error", "metric arguments are invalid")
            return store._startup_read_bytes
        if operation == "audit_status":
            if arguments:
                raise ChatProjectionStoreError("owner_protocol_error", "audit status arguments are invalid")
            return store.audit_status()
        if operation == "checkpoint_rows_read":
            if arguments:
                raise ChatProjectionStoreError("owner_protocol_error", "metric arguments are invalid")
            return store.checkpoint_rows_read()
        return _owner_dispatch(store, operation, arguments, request_id)
    serve_owner(
        channel_fd, directory_fd, file_fd, basename,
        lambda owner_directory_fd, owner_file_fd, owner_basename: _JsonlOwnerStore(
            owner_directory_fd, owner_file_fd, owner_basename,
            None if test_owner_fault == "none" else test_owner_fault,
            rebuild_mode == "rebuild",
        ),
        dispatch, lambda store: store.close(),
        lambda _channel, _request_id, _operation, result: (result, False), MAX_RESPONSE_BYTES,
    )


if __name__ == "__main__" and len(sys.argv) == 8 and sys.argv[1] == "--projection-owner":
    _run_owner(
        int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6], sys.argv[7],
    )
