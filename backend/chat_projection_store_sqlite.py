from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from chat_projection_store import (
    ChatProjectionStoreError, CommitResult, ProjectionCommit, SourceWatermark, StoredFact,
    StoredProjection, StoredRevision, TurnManifest,
)
from paths import ba_home, is_test_mode


SCHEMA_VERSION = 2
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_READ_LIMIT = 10_000


def _validate_json(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ChatProjectionStoreError("invalid_json", "JSON object keys must be strings")
        for nested in value.values():
            _validate_json(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_json(nested)
        return
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return
    raise ChatProjectionStoreError("invalid_json", "payload contains a non-JSON value")


def canonical_json(value: Mapping[str, Any]) -> str:
    _validate_json(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ChatProjectionStoreError("invalid_json", "payload must be canonical JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ChatProjectionStoreError("payload_too_large", "payload exceeds store admission limit")
    return encoded


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
    def __init__(
        self,
        path: Path | None = None,
        *,
        before_commit: Callable[[], None] | None = None,
        after_commit: Callable[[], None] | None = None,
    ) -> None:
        self._path = self._confined_path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._before_commit = before_commit
        self._after_commit = after_commit
        self._lock = threading.RLock()
        self._install_schema()

    @staticmethod
    def _confined_path(path: Path | None) -> Path:
        root = ba_home().resolve()
        candidate = (path or root / "chat" / "selected.sqlite3").expanduser()
        if not candidate.is_absolute():
            raise ChatProjectionStoreError("invalid_path", "chat store path must be absolute")
        if candidate.is_symlink():
            raise ChatProjectionStoreError("path_escape", "chat store path cannot be a symlink")
        resolved_parent = candidate.parent.resolve()
        resolved = candidate.resolve() if candidate.exists() else resolved_parent / candidate.name
        if resolved_parent != root and root not in resolved_parent.parents:
            mode = "test" if is_test_mode() else "runtime"
            raise ChatProjectionStoreError("path_escape", f"{mode} store path escapes Better Agent home")
        return resolved

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
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS selected_roots(
              root_id TEXT PRIMARY KEY, root_generation INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS root_generation_heads(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              fact_sequence INTEGER NOT NULL, revision INTEGER NOT NULL,
              projection_cursor INTEGER NOT NULL,
              PRIMARY KEY(root_id,root_generation)
            );
            CREATE TABLE IF NOT EXISTS canonical_facts(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              fact_sequence INTEGER NOT NULL, event_id TEXT NOT NULL,
              content_hash TEXT NOT NULL, fact_json TEXT NOT NULL,
              PRIMARY KEY(root_id,root_generation,fact_sequence),
              UNIQUE(root_id,root_generation,event_id,content_hash)
            );
            CREATE TABLE IF NOT EXISTS render_nodes(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              event_id TEXT NOT NULL, node_json TEXT NOT NULL,
              PRIMARY KEY(root_id,root_generation,event_id)
            );
            CREATE TABLE IF NOT EXISTS ownership(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              event_id TEXT NOT NULL, turn_id TEXT NOT NULL, message_id TEXT,
              parent_event_id TEXT, owner_scope TEXT NOT NULL,
              PRIMARY KEY(root_id,root_generation,event_id)
            );
            CREATE TABLE IF NOT EXISTS turn_manifests(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              turn_id TEXT NOT NULL, event_count INTEGER NOT NULL,
              direct_child_count INTEGER NOT NULL,
              PRIMARY KEY(root_id,root_generation,turn_id)
            );
            CREATE TABLE IF NOT EXISTS revisions(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              revision INTEGER NOT NULL, fact_sequence INTEGER NOT NULL,
              visible_delta_json TEXT NOT NULL, historical_json TEXT NOT NULL,
              PRIMARY KEY(root_id,root_generation,revision)
            );
            CREATE TABLE IF NOT EXISTS source_watermarks(
              root_id TEXT NOT NULL, root_generation INTEGER NOT NULL,
              stream_id TEXT NOT NULL, source_generation INTEGER NOT NULL,
              source_sequence INTEGER NOT NULL,
              PRIMARY KEY(root_id,root_generation,stream_id)
            );
        """)
        self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._connection.commit()
        try:
            self._validate_schema()
        except ChatProjectionStoreError:
            self.close()
            raise

    def _validate_schema(self) -> None:
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

    def select_generation(self, root_id: str, root_generation: int) -> None:
        self._identity(root_id, root_generation)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT root_generation FROM selected_roots WHERE root_id=?", (root_id,),
            ).fetchone()
            if row and root_generation <= int(row[0]):
                if root_generation == int(row[0]):
                    return
                raise ChatProjectionStoreError("stale_generation", "root generation is fenced")
            self._connection.execute(
                "INSERT INTO selected_roots VALUES(?,?) ON CONFLICT(root_id) DO UPDATE SET "
                "root_generation=excluded.root_generation", (root_id, root_generation),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO root_generation_heads VALUES(?,?,?,?,?)",
                (root_id, root_generation, 0, 0, 0),
            )

    def commit(self, request: ProjectionCommit) -> CommitResult:
        self._validate_commit(request)
        fact_json = canonical_json(request.canonical_fact)
        node_json = canonical_json(request.render_node)
        delta_json = canonical_json(request.visible_delta)
        historical_json = canonical_json(request.historical_revision)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                result = self._commit_transaction(
                    request, fact_json, node_json, delta_json, historical_json,
                )
                if self._before_commit:
                    self._before_commit()
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
        if head is None or int(head[0]) != request.root_generation:
            raise ChatProjectionStoreError("stale_generation", "root generation is not selected")
        duplicate = self._connection.execute(
            "SELECT fact_sequence FROM canonical_facts WHERE root_id=? AND root_generation=? "
            "AND event_id=? AND content_hash=?",
            (request.root_id, request.root_generation, request.event_id, request.content_hash),
        ).fetchone()
        self._advance_watermark(request)
        if duplicate:
            return CommitResult(True, int(duplicate[0]), int(head[2]), int(head[3]))
        fact_sequence, revision = int(head[1]) + 1, int(head[2]) + 1
        cursor = int(head[3]) + 1
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
        if current and candidate < (int(current[0]), int(current[1])):
            raise ChatProjectionStoreError("watermark_regression", "source watermark cannot regress")
        self._connection.execute(
            "INSERT INTO source_watermarks VALUES(?,?,?,?,?) "
            "ON CONFLICT(root_id,root_generation,stream_id) DO UPDATE SET "
            "source_generation=excluded.source_generation,source_sequence=excluded.source_sequence",
            (*values, *candidate),
        )

    def read_facts(
        self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000,
    ) -> list[StoredFact]:
        self._read_args(root_id, root_generation, after, limit)
        with self._lock:
            rows = self._connection.execute(
                "SELECT fact_sequence,event_id,content_hash,fact_json FROM canonical_facts "
                "WHERE root_id=? AND root_generation=? AND fact_sequence>? "
                "ORDER BY fact_sequence LIMIT ?", (root_id, root_generation, after, limit),
            ).fetchall()
        return [StoredFact(int(row[0]), row[1], row[2], json.loads(row[3])) for row in rows]

    def read_revisions(
        self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000,
    ) -> list[StoredRevision]:
        self._read_args(root_id, root_generation, after, limit)
        with self._lock:
            rows = self._connection.execute(
                "SELECT revision,fact_sequence,visible_delta_json,historical_json FROM revisions "
                "WHERE root_id=? AND root_generation=? AND revision>? ORDER BY revision LIMIT ?",
                (root_id, root_generation, after, limit),
            ).fetchall()
        return [StoredRevision(int(row[0]), int(row[1]), json.loads(row[2]), json.loads(row[3])) for row in rows]

    def projection_cursor(self, root_id: str, root_generation: int) -> int:
        self._identity(root_id, root_generation)
        with self._lock:
            row = self._connection.execute(
                "SELECT projection_cursor FROM root_generation_heads WHERE root_id=? AND root_generation=?",
                (root_id, root_generation),
            ).fetchone()
        return int(row[0]) if row else 0

    def delete_generation(self, root_id: str, root_generation: int) -> None:
        self._identity(root_id, root_generation)
        with self._lock:
            try:
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
                if selected and int(selected[0]) == root_generation:
                    raise ChatProjectionStoreError("current_generation", "selected generation cannot be deleted")
                self._delete_generation_rows(root_id, root_generation)
                if self._before_commit:
                    self._before_commit()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        if self._after_commit:
            self._after_commit()

    def delete_root(self, root_id: str) -> None:
        self._identity(root_id, 0)
        with self._lock:
            try:
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

    def read_projection(
        self, root_id: str, root_generation: int, event_id: str,
    ) -> StoredProjection | None:
        self._identity(root_id, root_generation)
        if not event_id:
            raise ChatProjectionStoreError("invalid_input", "event_id is required")
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
            event_id, json.loads(row[0]), row[1], row[2], row[3], row[4],
            TurnManifest(row[1], int(row[5]), int(row[6])),
        )

    def source_watermark(
        self, root_id: str, root_generation: int, stream_id: str,
    ) -> SourceWatermark | None:
        self._identity(root_id, root_generation)
        if not stream_id:
            raise ChatProjectionStoreError("invalid_input", "stream_id is required")
        with self._lock:
            row = self._connection.execute(
                "SELECT source_generation,source_sequence FROM source_watermarks "
                "WHERE root_id=? AND root_generation=? AND stream_id=?",
                (root_id, root_generation, stream_id),
            ).fetchone()
        return SourceWatermark(stream_id, int(row[0]), int(row[1])) if row else None

    @staticmethod
    def _validate_commit(request: ProjectionCommit) -> None:
        SQLiteChatProjectionStore._identity(request.root_id, request.root_generation)
        for name, value in (
            ("event_id", request.event_id), ("content_hash", request.content_hash),
            ("turn_id", request.turn_id), ("owner_scope", request.owner_scope),
            ("stream_id", request.watermark.stream_id),
        ):
            if not isinstance(value, str) or not value:
                raise ChatProjectionStoreError("invalid_input", f"{name} is required")
        for name, value in (
            ("message_id", request.message_id), ("parent_event_id", request.parent_event_id),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ChatProjectionStoreError("invalid_input", f"{name} must be a string or null")
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
        if request.content_hash != expected:
            raise ChatProjectionStoreError("hash_mismatch", "content hash does not match canonical fact")
        if request.manifest.turn_id != request.turn_id:
            raise ChatProjectionStoreError("invalid_input", "manifest turn does not match ownership")
        numbers = (
            request.manifest.event_count, request.manifest.direct_child_count,
            request.watermark.generation, request.watermark.sequence,
        )
        if any(type(value) is not int or value < 0 for value in numbers):
            raise ChatProjectionStoreError("invalid_input", "counts and watermarks must be non-negative")

    @staticmethod
    def _identity(root_id: str, root_generation: int) -> None:
        if not isinstance(root_id, str) or not root_id:
            raise ChatProjectionStoreError("invalid_input", "root_id is required")
        if type(root_generation) is not int or root_generation < 0:
            raise ChatProjectionStoreError("invalid_input", "root_generation must be non-negative")

    @staticmethod
    def _read_args(root_id: str, generation: int, after: int, limit: int) -> None:
        SQLiteChatProjectionStore._identity(root_id, generation)
        if type(after) is not int or after < 0:
            raise ChatProjectionStoreError("invalid_cursor", "cursor must be non-negative")
        if type(limit) is not int or not 1 <= limit <= MAX_READ_LIMIT:
            raise ChatProjectionStoreError("invalid_limit", f"limit must be 1..{MAX_READ_LIMIT}")

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None
