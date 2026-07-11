from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import perf
import portable_lock
from paths import ba_home
from stores.sqlite_truth_base import (
    SqliteTruthStore,
    canonical_json,
    required_identifier,
)


SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
AUTHORITIES = ("user_stated", "agent_inferred")
SENSITIVITIES = ("normal", "sensitive", "secret")
MAX_TEXT_CHARS = 16_384
MAX_QUERY_CHARS = 1_024
MAX_SPAN_OFFSET = 1_000_000_000
_REDACTED_PAYLOAD = '{"redacted":true}'

_SCHEMA_OBJECTS = {
    "requirement_events": """CREATE TABLE requirement_events (
        commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        requirement_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        event_type TEXT NOT NULL
            CHECK (event_type IN ('registered', 'superseded', 'deleted')),
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (requirement_id, revision)
    )""",
    "requirements": """CREATE TABLE requirements (
        requirement_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'deleted')),
        text TEXT,
        kind TEXT,
        authority TEXT CHECK (authority IN ('user_stated', 'agent_inferred')),
        sensitivity TEXT CHECK (sensitivity IN ('normal', 'sensitive', 'secret')),
        source_session_id TEXT,
        source_message_id TEXT,
        span_start INTEGER,
        span_end INTEGER,
        source_sha256 TEXT,
        superseded_by TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (status = 'deleted' OR (
            text IS NOT NULL AND kind IS NOT NULL AND authority IS NOT NULL
            AND sensitivity IS NOT NULL AND source_session_id IS NOT NULL
            AND source_message_id IS NOT NULL AND span_start IS NOT NULL
            AND span_end IS NOT NULL AND source_sha256 IS NOT NULL)),
        CHECK (status = 'deleted' OR superseded_by IS NULL OR status = 'superseded')
    )""",
    "purge_markers": """CREATE TABLE purge_markers (
        requirement_id TEXT PRIMARY KEY,
        created_at REAL NOT NULL
    )""",
    "requirements_by_status": """CREATE INDEX requirements_by_status
        ON requirements(status, sensitivity)""",
}


class RequirementStoreError(RuntimeError):
    pass


class RevisionConflict(RequirementStoreError):
    pass


class RequirementIdempotencyConflict(RequirementStoreError):
    pass


class RequirementNotFound(RequirementStoreError):
    pass


class RequirementStateError(RequirementStoreError):
    pass


class CorruptEventLog(RequirementStoreError):
    pass


class PurgeIncomplete(RequirementStoreError):
    """The logical delete committed, but byte purge could not complete yet.

    Purge markers remain on disk, so the next open (or next delete) retries
    finalization until the residue is gone."""


@dataclass(frozen=True)
class RegisterResult:
    appended: bool
    requirement_id: str
    revision: int
    commit_seq: int


@dataclass(frozen=True)
class SupersedeResult:
    old_requirement_id: str
    old_revision: int
    new_requirement_id: str


def _validate_registration(
    *,
    requirement_id: str,
    text: str,
    kind: str,
    authority: str,
    sensitivity: str,
    source_session_id: str,
    source_message_id: str,
    span_start: int,
    span_end: int,
) -> dict[str, Any]:
    requirement_id = required_identifier("requirement_id", requirement_id)
    kind = required_identifier("kind", kind)
    source_session_id = required_identifier("source_session_id", source_session_id)
    source_message_id = required_identifier("source_message_id", source_message_id)
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text must be a non-empty string of at most {MAX_TEXT_CHARS} characters")
    if authority not in AUTHORITIES:
        raise ValueError(f"authority must be one of {AUTHORITIES}")
    if sensitivity not in SENSITIVITIES:
        raise ValueError(f"sensitivity must be one of {SENSITIVITIES}")
    for name, value in (("span_start", span_start), ("span_end", span_end)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_SPAN_OFFSET:
            raise ValueError(f"{name} must be an integer within [0, {MAX_SPAN_OFFSET}]")
    if span_end <= span_start:
        raise ValueError("span_end must be greater than span_start")
    return {
        "requirement_id": requirement_id,
        "text": text,
        "kind": kind,
        "authority": authority,
        "sensitivity": sensitivity,
        "source_session_id": source_session_id,
        "source_message_id": source_message_id,
        "span_start": span_start,
        "span_end": span_end,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _validate_authorization(authorized_sensitivities: Iterable[str]) -> frozenset[str]:
    if isinstance(authorized_sensitivities, str):
        raise ValueError("authorized_sensitivities must be a collection of sensitivity values")
    values = frozenset(authorized_sensitivities)
    if not values or not values.issubset(SENSITIVITIES):
        raise ValueError(f"authorized_sensitivities must be a non-empty subset of {SENSITIVITIES}")
    return values


def _fts_match_expression(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be a non-empty string of at most {MAX_QUERY_CHARS} characters")
    # Every token is wrapped as a quoted FTS5 string so caller input can never
    # inject MATCH syntax.
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in query.split())


def _project_events(rows: Iterable[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    """Replay the event log into the current-state rows, deterministically.

    Timestamps come from event created_at, never from the clock, so a rebuild
    reproduces the incremental projection byte for byte.
    """
    projected: dict[str, dict[str, Any]] = {}
    for row in rows:
        requirement_id = str(row["requirement_id"])
        event_type = str(row["event_type"])
        revision = int(row["revision"])
        created_at = float(row["created_at"])
        payload = json.loads(row["payload_json"])
        if event_type == "registered":
            if requirement_id in projected or revision != 1:
                raise CorruptEventLog(f"invalid registered event for {requirement_id}")
            if payload.get("redacted") is True:
                # Content was purged; only a later deleted event may resolve
                # this into a tombstone.
                projected[requirement_id] = {
                    "_pending_redacted": True,
                    "revision": revision,
                    "created_at": created_at,
                }
                continue
            projected[requirement_id] = {
                "requirement_id": requirement_id,
                "revision": revision,
                "status": "active",
                "text": payload["text"],
                "kind": payload["kind"],
                "authority": payload["authority"],
                "sensitivity": payload["sensitivity"],
                "source_session_id": payload["source_session_id"],
                "source_message_id": payload["source_message_id"],
                "span_start": payload["span_start"],
                "span_end": payload["span_end"],
                "source_sha256": payload["source_sha256"],
                "superseded_by": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
            continue
        current = projected.get(requirement_id)
        if current is None or int(current["revision"]) + 1 != revision:
            raise CorruptEventLog(f"event revision gap for {requirement_id}")
        if event_type == "superseded":
            current["revision"] = revision
            if current.get("_pending_redacted"):
                continue
            current["status"] = "superseded"
            current["superseded_by"] = payload["superseded_by"]
            current["updated_at"] = created_at
            continue
        if event_type == "deleted":
            projected[requirement_id] = {
                "requirement_id": requirement_id,
                "revision": revision,
                "status": "deleted",
                "text": None,
                "kind": None,
                "authority": None,
                "sensitivity": None,
                "source_session_id": None,
                "source_message_id": None,
                "span_start": None,
                "span_end": None,
                "source_sha256": None,
                "superseded_by": None,
                "created_at": float(current["created_at"]),
                "updated_at": created_at,
            }
            continue
        raise CorruptEventLog(f"unknown event type {event_type}")
    for requirement_id, row in projected.items():
        if row.get("_pending_redacted"):
            raise CorruptEventLog(f"redacted registration without deletion for {requirement_id}")
    return projected


class RequirementStore(SqliteTruthStore):
    """Truth-layer requirement registry with a disposable exact-search index.

    The events table is authoritative; the requirements table is its
    deterministic projection; the FTS index lives in a separate database file
    that is destroyed and rebuilt whenever it is stale, invalid, or a purge
    ran — it is never authoritative and every hit is re-verified and
    re-authorized against the truth rows before it is returned.
    """

    SCHEMA_VERSION = SCHEMA_VERSION
    SCHEMA_OBJECTS = _SCHEMA_OBJECTS
    LABEL = "requirement"
    PERF_PREFIX = "requirement_store"
    SECURE_DELETE = True

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or (ba_home() / "db" / "requirements.sqlite3"))
        # Every disposable index over this truth DB lives inside index_dir so
        # a purge can destroy them all wholesale, including index kinds this
        # process never attached. One shared lock fences every (re)build and
        # destruction.
        self.index_dir = self._path.with_name(self._path.stem + "_indexes")
        self.index_lock_path = self._path.with_name(self._path.stem + "_indexes.lock")
        self._index_path = self.index_dir / "fts.sqlite3"
        self._finalize_purges()

    # -- writes ------------------------------------------------------------

    def register(self, **fields: Any) -> RegisterResult:
        record = _validate_registration(**fields)
        payload_json = canonical_json(record, label="requirement registration")
        started = time.perf_counter()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT payload_json, commit_seq, revision FROM requirement_events "
                "WHERE requirement_id=? AND event_type='registered'",
                (record["requirement_id"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise RequirementIdempotencyConflict(
                        f"requirement {record['requirement_id']} was already registered "
                        "with different content"
                    )
                conn.commit()
                return RegisterResult(
                    appended=False,
                    requirement_id=record["requirement_id"],
                    revision=int(existing["revision"]),
                    commit_seq=int(existing["commit_seq"]),
                )
            now = time.time()
            cursor = self._append_event(
                conn,
                requirement_id=record["requirement_id"],
                revision=1,
                event_type="registered",
                payload_json=payload_json,
                created_at=now,
            )
            conn.execute(
                "INSERT INTO requirements (requirement_id, revision, status, text, kind, "
                "authority, sensitivity, source_session_id, source_message_id, span_start, "
                "span_end, source_sha256, superseded_by, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    record["requirement_id"],
                    1,
                    record["text"],
                    record["kind"],
                    record["authority"],
                    record["sensitivity"],
                    record["source_session_id"],
                    record["source_message_id"],
                    record["span_start"],
                    record["span_end"],
                    record["source_sha256"],
                    now,
                    now,
                ),
            )
            conn.commit()
            return RegisterResult(
                appended=True,
                requirement_id=record["requirement_id"],
                revision=1,
                commit_seq=int(cursor.lastrowid),
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
            perf.record("requirement_store.register", (time.perf_counter() - started) * 1000.0)

    def supersede(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        replacement: dict[str, Any],
    ) -> SupersedeResult:
        requirement_id = required_identifier("requirement_id", requirement_id)
        self._validate_revision(expected_revision)
        record = _validate_registration(**replacement)
        if record["requirement_id"] == requirement_id:
            raise ValueError("a requirement cannot supersede itself")
        payload_json = canonical_json(record, label="requirement registration")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = self._require_row(conn, requirement_id)
            if current["status"] != "active":
                raise RequirementStateError(
                    f"requirement {requirement_id} is {current['status']}, not active"
                )
            if int(current["revision"]) != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current['revision']}"
                )
            replacement_exists = conn.execute(
                "SELECT 1 FROM requirements WHERE requirement_id=?",
                (record["requirement_id"],),
            ).fetchone()
            if replacement_exists is not None:
                raise RequirementStateError(
                    f"replacement requirement {record['requirement_id']} already exists"
                )
            now = time.time()
            self._append_event(
                conn,
                requirement_id=record["requirement_id"],
                revision=1,
                event_type="registered",
                payload_json=payload_json,
                created_at=now,
            )
            conn.execute(
                "INSERT INTO requirements (requirement_id, revision, status, text, kind, "
                "authority, sensitivity, source_session_id, source_message_id, span_start, "
                "span_end, source_sha256, superseded_by, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    record["requirement_id"],
                    1,
                    record["text"],
                    record["kind"],
                    record["authority"],
                    record["sensitivity"],
                    record["source_session_id"],
                    record["source_message_id"],
                    record["span_start"],
                    record["span_end"],
                    record["source_sha256"],
                    now,
                    now,
                ),
            )
            self._append_event(
                conn,
                requirement_id=requirement_id,
                revision=expected_revision + 1,
                event_type="superseded",
                payload_json=canonical_json(
                    {"superseded_by": record["requirement_id"]},
                    label="requirement supersession",
                ),
                created_at=now,
            )
            conn.execute(
                "UPDATE requirements SET status='superseded', superseded_by=?, revision=?, "
                "updated_at=? WHERE requirement_id=? AND revision=?",
                (record["requirement_id"], expected_revision + 1, now, requirement_id, expected_revision),
            )
            conn.commit()
            return SupersedeResult(
                old_requirement_id=requirement_id,
                old_revision=expected_revision + 1,
                new_requirement_id=record["requirement_id"],
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def delete(self, requirement_id: str, *, expected_revision: int) -> None:
        """Purge a requirement: redact its content events, tombstone the row,
        destroy the disposable index, and truncate the WAL so no byte residue
        of the content survives."""
        requirement_id = required_identifier("requirement_id", requirement_id)
        self._validate_revision(expected_revision)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = self._require_row(conn, requirement_id)
            if current["status"] == "deleted":
                raise RequirementStateError(f"requirement {requirement_id} is already deleted")
            if int(current["revision"]) != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current['revision']}"
                )
            now = time.time()
            conn.execute(
                "UPDATE requirement_events SET payload_json=? "
                "WHERE requirement_id=? AND event_type='registered'",
                (_REDACTED_PAYLOAD, requirement_id),
            )
            self._append_event(
                conn,
                requirement_id=requirement_id,
                revision=expected_revision + 1,
                event_type="deleted",
                payload_json="{}",
                created_at=now,
            )
            conn.execute(
                "UPDATE requirements SET revision=?, status='deleted', text=NULL, kind=NULL, "
                "authority=NULL, sensitivity=NULL, source_session_id=NULL, "
                "source_message_id=NULL, span_start=NULL, span_end=NULL, source_sha256=NULL, "
                "superseded_by=NULL, updated_at=? WHERE requirement_id=? AND revision=?",
                (expected_revision + 1, now, requirement_id, expected_revision),
            )
            conn.execute(
                "INSERT INTO purge_markers (requirement_id, created_at) VALUES (?, ?)",
                (requirement_id, now),
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        self._finalize_purges()

    # -- reads -------------------------------------------------------------

    def get(
        self,
        requirement_id: str,
        *,
        authorized_sensitivities: Iterable[str],
    ) -> dict[str, Any] | None:
        requirement_id = required_identifier("requirement_id", requirement_id)
        authorized = _validate_authorization(authorized_sensitivities)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM requirements WHERE requirement_id=?",
                (requirement_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if row["status"] == "deleted":
            return {
                "requirement_id": requirement_id,
                "status": "deleted",
                "revision": int(row["revision"]),
                "deleted_at": float(row["updated_at"]),
            }
        if row["sensitivity"] not in authorized:
            return None
        return self._citation(row)

    def retrieve(
        self,
        query: str,
        *,
        authorized_sensitivities: Iterable[str],
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        match_expression = _fts_match_expression(query)
        authorized = _validate_authorization(authorized_sensitivities)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        started = time.perf_counter()
        self._ensure_index()
        results: list[dict[str, Any]] = []
        batch_size = max(limit * 4, 64)
        offset = 0
        index_conn = self._index_connect()
        conn = self._connect()
        try:
            # Page through every candidate: an authorized match must never be
            # starved out by a run of unauthorized hits ranked above it.
            while len(results) < limit:
                hits = index_conn.execute(
                    "SELECT requirement_id FROM requirement_fts WHERE requirement_fts MATCH ? "
                    "ORDER BY rank, requirement_id LIMIT ? OFFSET ?",
                    (match_expression, batch_size, offset),
                ).fetchall()
                for hit in hits:
                    if len(results) >= limit:
                        break
                    citation = self._qualify_hit(
                        conn, str(hit["requirement_id"]), authorized, include_superseded
                    )
                    if citation is not None:
                        results.append(citation)
                if len(hits) < batch_size:
                    break
                offset += batch_size
        finally:
            index_conn.close()
            conn.close()
            perf.record("requirement_store.retrieve", (time.perf_counter() - started) * 1000.0)
        return results

    def qualify_citations(
        self,
        requirement_ids: Iterable[str],
        *,
        authorized_sensitivities: Iterable[str],
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Re-verify candidate ids (from any disposable index) against the
        authoritative rows, preserving candidate order."""
        authorized = _validate_authorization(authorized_sensitivities)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        results: list[dict[str, Any]] = []
        conn = self._connect()
        try:
            for requirement_id in requirement_ids:
                if len(results) >= limit:
                    break
                citation = self._qualify_hit(
                    conn,
                    required_identifier("requirement_id", requirement_id),
                    authorized,
                    include_superseded,
                )
                if citation is not None:
                    results.append(citation)
            return results
        finally:
            conn.close()

    def indexable_rows(self) -> list[dict[str, Any]]:
        """Stable-ordered (requirement_id, text) rows a disposable index may
        embed or tokenize; deleted rows never appear."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT requirement_id, text FROM requirements "
                "WHERE status IN ('active', 'superseded') ORDER BY requirement_id"
            ).fetchall()
            return [
                {"requirement_id": row["requirement_id"], "text": row["text"]}
                for row in rows
            ]
        finally:
            conn.close()

    def rebuild_projection(self) -> int:
        """Deterministically rebuild the requirements table from the event log."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            events = conn.execute(
                "SELECT requirement_id, revision, event_type, payload_json, created_at "
                "FROM requirement_events ORDER BY commit_seq"
            ).fetchall()
            projected = _project_events(events)
            conn.execute("DELETE FROM requirements")
            for row in projected.values():
                conn.execute(
                    "INSERT INTO requirements (requirement_id, revision, status, text, kind, "
                    "authority, sensitivity, source_session_id, source_message_id, span_start, "
                    "span_end, source_sha256, superseded_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["requirement_id"],
                        row["revision"],
                        row["status"],
                        row["text"],
                        row["kind"],
                        row["authority"],
                        row["sensitivity"],
                        row["source_session_id"],
                        row["source_message_id"],
                        row["span_start"],
                        row["span_end"],
                        row["source_sha256"],
                        row["superseded_by"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
            conn.commit()
            return len(projected)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    # -- internals ---------------------------------------------------------

    def _validate_revision(self, expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")

    def _qualify_hit(
        self,
        conn: sqlite3.Connection,
        requirement_id: str,
        authorized: frozenset[str],
        include_superseded: bool,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM requirements WHERE requirement_id=?",
            (requirement_id,),
        ).fetchone()
        # Indexes are disposable projections: every hit must re-qualify
        # against the authoritative row or it is dropped.
        if row is None or row["status"] == "deleted":
            return None
        if row["status"] == "superseded" and not include_superseded:
            return None
        if row["sensitivity"] not in authorized:
            return None
        return self._citation(row)

    def _require_row(self, conn: sqlite3.Connection, requirement_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM requirements WHERE requirement_id=?",
            (requirement_id,),
        ).fetchone()
        if row is None:
            raise RequirementNotFound(f"requirement {requirement_id} does not exist")
        return row

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        requirement_id: str,
        revision: int,
        event_type: str,
        payload_json: str,
        created_at: float,
    ) -> sqlite3.Cursor:
        return conn.execute(
            "INSERT INTO requirement_events "
            "(event_id, requirement_id, revision, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), requirement_id, revision, event_type, payload_json, created_at),
        )

    def _citation(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "requirement_id": row["requirement_id"],
            "revision": int(row["revision"]),
            "status": row["status"],
            "text": row["text"],
            "kind": row["kind"],
            "authority": row["authority"],
            "sensitivity": row["sensitivity"],
            "superseded_by": row["superseded_by"],
            "source": {
                "session_id": row["source_session_id"],
                "message_id": row["source_message_id"],
                "span_start": int(row["span_start"]),
                "span_end": int(row["span_end"]),
                "sha256": row["source_sha256"],
            },
        }

    # -- purge finalization --------------------------------------------------

    def _finalize_purges(self) -> None:
        conn = self._connect()
        try:
            pending = conn.execute("SELECT COUNT(*) FROM purge_markers").fetchone()[0]
            if int(pending) == 0:
                return
            # Redacted pages are already committed; truncating the WAL removes
            # the pre-redaction frames that still hold the purged content. The
            # checkpoint result must be inspected: a busy checkpoint leaves
            # residue, so the markers stay and finalization retries later —
            # never fail open.
            busy = int(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
            if busy != 0:
                raise PurgeIncomplete(
                    "purge deferred: WAL truncation was blocked by a concurrent "
                    "reader; markers kept, finalization retries on next open"
                )
            # The rebuild lock fences an in-flight index rebuild that may have
            # read pre-delete truth rows: destroy strictly serializes after it.
            with self.index_rebuild_lock():
                self._destroy_indexes()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM purge_markers")
                conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    # -- disposable FTS index ------------------------------------------------

    def _index_connect(self) -> sqlite3.Connection:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._index_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    def _destroy_indexes(self) -> None:
        if self.index_dir.exists():
            shutil.rmtree(self.index_dir)

    @contextmanager
    def index_rebuild_lock(self):
        with self.index_lock_path.open("a+b") as lock_file:
            portable_lock.lock_ex(lock_file.fileno())
            try:
                yield
            finally:
                portable_lock.unlock(lock_file.fileno())

    def truth_watermark(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT MAX(commit_seq) FROM requirement_events").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    def _index_is_fresh(self, watermark: int) -> bool:
        if not self._index_path.exists():
            return False
        conn = self._index_connect()
        try:
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) != INDEX_SCHEMA_VERSION:
                return False
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key='last_commit_seq'"
            ).fetchone()
            return row is not None and int(row["value"]) == watermark
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _ensure_index(self) -> None:
        watermark = self.truth_watermark()
        if self._index_is_fresh(watermark):
            return
        with self.index_rebuild_lock():
            watermark = self.truth_watermark()
            if self._index_is_fresh(watermark):
                return
            self._rebuild_index(watermark)

    def _rebuild_index(self, watermark: int) -> None:
        started = time.perf_counter()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._index_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        rows = self.indexable_rows()
        conn = self._index_connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "CREATE VIRTUAL TABLE requirement_fts "
                "USING fts5(text, requirement_id UNINDEXED)"
            )
            conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for row in rows:
                conn.execute(
                    "INSERT INTO requirement_fts (text, requirement_id) VALUES (?, ?)",
                    (row["text"], row["requirement_id"]),
                )
            conn.execute(
                "INSERT INTO index_meta (key, value) VALUES ('last_commit_seq', ?)",
                (str(watermark),),
            )
            conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
            perf.record("requirement_store.rebuild_index", (time.perf_counter() - started) * 1000.0)
