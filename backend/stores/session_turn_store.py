from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import perf
from paths import ba_home
from stores.sqlite_truth_base import (
    MAX_DOCUMENT_BYTES,
    MAX_ERROR_TEXT_CHARS,
    SQLITE_BUSY_TIMEOUT_MS,
    SchemaVersionError,
    SqliteTruthStore,
    canonical_json,
    required_error_text,
    required_identifier,
)


SCHEMA_VERSION = 1

_SCHEMA_OBJECTS = {
    "turn_aggregates": """CREATE TABLE turn_aggregates (
        root_id TEXT NOT NULL,
        sid TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        aggregate_version INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (root_id, sid, turn_id)
    )""",
    "domain_events": """CREATE TABLE domain_events (
        commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        root_id TEXT NOT NULL,
        sid TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        aggregate_version INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        causation_id TEXT,
        correlation_id TEXT,
        idempotency_key TEXT NOT NULL,
        command_hash TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (root_id, sid, turn_id, aggregate_version),
        UNIQUE (root_id, sid, turn_id, idempotency_key),
        FOREIGN KEY (root_id, sid, turn_id)
            REFERENCES turn_aggregates(root_id, sid, turn_id)
    )""",
    "outbox": """CREATE TABLE outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        topic TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        dispatched_at REAL,
        claimed_by TEXT,
        claim_epoch INTEGER NOT NULL DEFAULT 0,
        lease_expires_at REAL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        FOREIGN KEY (event_id) REFERENCES domain_events(event_id)
    )""",
    "domain_events_aggregate": """CREATE INDEX domain_events_aggregate
        ON domain_events(root_id, sid, turn_id, aggregate_version)""",
    "outbox_pending": """CREATE INDEX outbox_pending
        ON outbox(outbox_id) WHERE dispatched_at IS NULL""",
    "import_checkpoints": """CREATE TABLE import_checkpoints (
        root_id TEXT PRIMARY KEY,
        journal_cursor INTEGER NOT NULL,
        turn_count INTEGER NOT NULL,
        imported_at REAL NOT NULL
    )""",
    "owner_authority": """CREATE TABLE owner_authority (
        root_id TEXT PRIMARY KEY,
        authority TEXT NOT NULL CHECK (authority IN ('legacy', 'sqlite')),
        flipped_at REAL NOT NULL
    )""",
}

OWNER_AUTHORITIES = ("legacy", "sqlite")

# Fired with (root_id, authority) after a committed authority flip — a fact
# for subscribers (e.g. SessionManager's write-fence cache), never a command.
_authority_listeners: list[Callable[[str, str], None]] = []


def register_authority_listener(listener: Callable[[str, str], None]) -> None:
    # Idempotent: re-registering the same bound method (e.g. a rebuilt
    # SessionManager in tests) must not stack duplicate fan-outs.
    if listener not in _authority_listeners:
        _authority_listeners.append(listener)


def default_store_path() -> Path:
    return ba_home() / "db" / "better_agent.sqlite3"


class SessionTurnStoreError(RuntimeError):
    pass


class AggregateVersionConflict(SessionTurnStoreError):
    pass


class IdempotencyConflict(SessionTurnStoreError):
    pass


class OutboxLeaseConflict(SessionTurnStoreError):
    pass


class AuthorityConflict(SessionTurnStoreError):
    pass


@dataclass(frozen=True)
class ApplyResult:
    appended: bool
    aggregate_version: int
    commit_seq: int
    outbox_id: int


_canonical_json = canonical_json
_required_identifier = required_identifier
_required_error_text = required_error_text


class SessionTurnStore(SqliteTruthStore):
    SCHEMA_VERSION = SCHEMA_VERSION
    SCHEMA_OBJECTS = _SCHEMA_OBJECTS
    LABEL = "session turn"
    PERF_PREFIX = "session_turn_store"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or default_store_path())

    def apply_command(
        self,
        *,
        root_id: str,
        sid: str,
        turn_id: str,
        expected_version: int,
        event_type: str,
        payload: dict[str, Any],
        new_state: dict[str, Any],
        idempotency_key: str,
        outbox_topic: str,
        event_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        event_schema_version: int = 1,
    ) -> ApplyResult:
        root_id = _required_identifier("root_id", root_id)
        sid = _required_identifier("sid", sid)
        turn_id = _required_identifier("turn_id", turn_id)
        event_type = _required_identifier("event_type", event_type)
        idempotency_key = _required_identifier("idempotency_key", idempotency_key)
        outbox_topic = _required_identifier("outbox_topic", outbox_topic)
        event_id = _required_identifier("event_id", event_id or str(uuid.uuid4()))
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if isinstance(event_schema_version, bool) or not isinstance(event_schema_version, int) or event_schema_version < 1:
            raise ValueError("event_schema_version must be a positive integer")

        payload_json = _canonical_json(payload)
        state_json = _canonical_json(new_state)
        outbox_json = _canonical_json(
            {
                "event_id": event_id,
                "root_id": root_id,
                "sid": sid,
                "turn_id": turn_id,
                "event_type": event_type,
                "aggregate_version": expected_version + 1,
            }
        )
        command_json = _canonical_json(
            {
                "sid": sid,
                "event_type": event_type,
                "event_schema_version": event_schema_version,
                "payload": payload,
                "new_state": new_state,
                "outbox_topic": outbox_topic,
                "causation_id": causation_id,
                "correlation_id": correlation_id,
            }
        )
        command_hash = hashlib.sha256(command_json.encode("utf-8")).hexdigest()
        started = time.perf_counter()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT command_hash, aggregate_version, commit_seq "
                "FROM domain_events WHERE root_id=? AND sid=? AND turn_id=? AND idempotency_key=?",
                (root_id, sid, turn_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                if duplicate["command_hash"] != command_hash:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different command"
                    )
                outbox = conn.execute(
                    "SELECT outbox_id FROM outbox WHERE event_id=("
                    "SELECT event_id FROM domain_events "
                    "WHERE root_id=? AND sid=? AND turn_id=? AND idempotency_key=?)",
                    (root_id, sid, turn_id, idempotency_key),
                ).fetchone()
                conn.commit()
                return ApplyResult(
                    appended=False,
                    aggregate_version=int(duplicate["aggregate_version"]),
                    commit_seq=int(duplicate["commit_seq"]),
                    outbox_id=int(outbox["outbox_id"]),
                )

            current = conn.execute(
                "SELECT aggregate_version FROM turn_aggregates "
                "WHERE root_id=? AND sid=? AND turn_id=?",
                (root_id, sid, turn_id),
            ).fetchone()
            current_version = int(current["aggregate_version"]) if current else 0
            if current_version != expected_version:
                raise AggregateVersionConflict(
                    f"expected aggregate version {expected_version}, found {current_version}"
                )

            now = time.time()
            next_version = expected_version + 1
            if current is None:
                conn.execute(
                    "INSERT INTO turn_aggregates "
                    "(root_id, sid, turn_id, aggregate_version, state_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (root_id, sid, turn_id, next_version, state_json, now, now),
                )
            else:
                conn.execute(
                    "UPDATE turn_aggregates SET sid=?, aggregate_version=?, state_json=?, updated_at=? "
                    "WHERE root_id=? AND sid=? AND turn_id=? AND aggregate_version=?",
                    (sid, next_version, state_json, now, root_id, sid, turn_id, expected_version),
                )
            cursor = conn.execute(
                "INSERT INTO domain_events "
                "(event_id, root_id, sid, turn_id, aggregate_version, event_type, schema_version, "
                "payload_json, causation_id, correlation_id, idempotency_key, command_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    root_id,
                    sid,
                    turn_id,
                    next_version,
                    event_type,
                    event_schema_version,
                    payload_json,
                    causation_id,
                    correlation_id,
                    idempotency_key,
                    command_hash,
                    now,
                ),
            )
            outbox_cursor = conn.execute(
                "INSERT INTO outbox(event_id, topic, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (event_id, outbox_topic, outbox_json, now),
            )
            conn.commit()
            return ApplyResult(
                appended=True,
                aggregate_version=next_version,
                commit_seq=int(cursor.lastrowid),
                outbox_id=int(outbox_cursor.lastrowid),
            )
        except (AggregateVersionConflict, IdempotencyConflict):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            perf.record("session_turn_store.apply_command", (time.perf_counter() - started) * 1000.0)

    def get_turn(self, root_id: str, sid: str, turn_id: str) -> dict[str, Any] | None:
        root_id = _required_identifier("root_id", root_id)
        sid = _required_identifier("sid", sid)
        turn_id = _required_identifier("turn_id", turn_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT sid, aggregate_version, state_json FROM turn_aggregates "
                "WHERE root_id=? AND sid=? AND turn_id=?",
                (root_id, sid, turn_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "root_id": root_id,
                "sid": row["sid"],
                "turn_id": turn_id,
                "aggregate_version": int(row["aggregate_version"]),
                "state": json.loads(row["state_json"]),
            }
        finally:
            conn.close()

    def list_turn_keys(self, root_id: str) -> list[dict[str, Any]]:
        root_id = _required_identifier("root_id", root_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT sid, turn_id, aggregate_version FROM turn_aggregates "
                "WHERE root_id=? ORDER BY sid, turn_id",
                (root_id,),
            ).fetchall()
            return [
                {
                    "sid": row["sid"],
                    "turn_id": row["turn_id"],
                    "aggregate_version": int(row["aggregate_version"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def record_import_checkpoint(
        self,
        *,
        root_id: str,
        journal_cursor: int,
        turn_count: int,
    ) -> None:
        root_id = _required_identifier("root_id", root_id)
        for name, value in (("journal_cursor", journal_cursor), ("turn_count", turn_count)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO import_checkpoints (root_id, journal_cursor, turn_count, imported_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(root_id) DO UPDATE SET "
                "journal_cursor=excluded.journal_cursor, turn_count=excluded.turn_count, "
                "imported_at=excluded.imported_at "
                "WHERE excluded.journal_cursor >= import_checkpoints.journal_cursor",
                (root_id, journal_cursor, turn_count, time.time()),
            )
        finally:
            conn.close()

    def get_owner_authority(self, root_id: str) -> str:
        """Which store owns this root's message/turn state. Roots with no
        recorded flip are legacy-owned by definition."""
        root_id = _required_identifier("root_id", root_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT authority FROM owner_authority WHERE root_id=?",
                (root_id,),
            ).fetchone()
            return str(row["authority"]) if row is not None else "legacy"
        finally:
            conn.close()

    def set_owner_authority(
        self,
        root_id: str,
        *,
        authority: str,
        expected_authority: str,
    ) -> None:
        root_id = _required_identifier("root_id", root_id)
        for name, value in (("authority", authority), ("expected_authority", expected_authority)):
            if value not in OWNER_AUTHORITIES:
                raise ValueError(f"{name} must be one of {OWNER_AUTHORITIES}")
        if authority == expected_authority:
            raise ValueError("authority flip must change the authority")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT authority FROM owner_authority WHERE root_id=?",
                (root_id,),
            ).fetchone()
            current = str(row["authority"]) if row is not None else "legacy"
            if current != expected_authority:
                raise AuthorityConflict(
                    f"root {root_id} owner authority is {current}, expected {expected_authority}"
                )
            conn.execute(
                "INSERT INTO owner_authority (root_id, authority, flipped_at) "
                "VALUES (?, ?, ?) ON CONFLICT(root_id) DO UPDATE SET "
                "authority=excluded.authority, flipped_at=excluded.flipped_at",
                (root_id, authority, time.time()),
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        for listener in list(_authority_listeners):
            listener(root_id, authority)

    def get_import_checkpoint(self, root_id: str) -> dict[str, Any] | None:
        root_id = _required_identifier("root_id", root_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT journal_cursor, turn_count, imported_at "
                "FROM import_checkpoints WHERE root_id=?",
                (root_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "root_id": root_id,
                "journal_cursor": int(row["journal_cursor"]),
                "turn_count": int(row["turn_count"]),
                "imported_at": float(row["imported_at"]),
            }
        finally:
            conn.close()

    def pending_outbox(self, *, limit: int) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT outbox_id, event_id, topic, payload_json, created_at "
                "FROM outbox WHERE dispatched_at IS NULL ORDER BY outbox_id LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "outbox_id": int(row["outbox_id"]),
                    "event_id": row["event_id"],
                    "topic": row["topic"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": float(row["created_at"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def claim_outbox(
        self,
        *,
        consumer_id: str,
        limit: int,
        lease_seconds: float,
    ) -> list[dict[str, Any]]:
        consumer_id = _required_identifier("consumer_id", consumer_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT outbox_id FROM outbox WHERE dispatched_at IS NULL "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?) "
                "ORDER BY outbox_id LIMIT ?",
                (now, limit),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                outbox_id = int(row["outbox_id"])
                conn.execute(
                    "UPDATE outbox SET claimed_by=?, claim_epoch=claim_epoch+1, "
                    "lease_expires_at=?, attempts=attempts+1, last_error=NULL WHERE outbox_id=?",
                    (consumer_id, now + float(lease_seconds), outbox_id),
                )
                claimed_row = conn.execute(
                    "SELECT outbox_id, event_id, topic, payload_json, claim_epoch, attempts "
                    "FROM outbox WHERE outbox_id=?",
                    (outbox_id,),
                ).fetchone()
                claimed.append({
                    "outbox_id": outbox_id,
                    "event_id": claimed_row["event_id"],
                    "topic": claimed_row["topic"],
                    "payload": json.loads(claimed_row["payload_json"]),
                    "claim_epoch": int(claimed_row["claim_epoch"]),
                    "attempts": int(claimed_row["attempts"]),
                })
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def acknowledge_outbox(
        self,
        *,
        outbox_id: int,
        consumer_id: str,
        claim_epoch: int,
    ) -> None:
        consumer_id = _required_identifier("consumer_id", consumer_id)
        if isinstance(outbox_id, bool) or not isinstance(outbox_id, int) or outbox_id < 1:
            raise ValueError("outbox_id must be a positive integer")
        if isinstance(claim_epoch, bool) or not isinstance(claim_epoch, int) or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET dispatched_at=?, lease_expires_at=NULL "
                "WHERE outbox_id=? AND dispatched_at IS NULL AND claimed_by=? AND claim_epoch=? "
                "AND lease_expires_at>?",
                (time.time(), outbox_id, consumer_id, claim_epoch, time.time()),
            )
            if cursor.rowcount != 1:
                raise OutboxLeaseConflict("outbox acknowledgement does not own the current lease")
        finally:
            conn.close()

    def fail_outbox(
        self,
        *,
        outbox_id: int,
        consumer_id: str,
        claim_epoch: int,
        error: str,
    ) -> None:
        consumer_id = _required_identifier("consumer_id", consumer_id)
        error = _required_error_text("error", error)
        if isinstance(outbox_id, bool) or not isinstance(outbox_id, int) or outbox_id < 1:
            raise ValueError("outbox_id must be a positive integer")
        if isinstance(claim_epoch, bool) or not isinstance(claim_epoch, int) or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET claimed_by=NULL, lease_expires_at=NULL, last_error=? "
                "WHERE outbox_id=? AND dispatched_at IS NULL AND claimed_by=? AND claim_epoch=? "
                "AND lease_expires_at>?",
                (error, outbox_id, consumer_id, claim_epoch, time.time()),
            )
            if cursor.rowcount != 1:
                raise OutboxLeaseConflict("outbox failure does not own the current lease")
        finally:
            conn.close()
