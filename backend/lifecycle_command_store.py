from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lifecycle_command_model import (
    LifecycleCommand,
    LifecycleEffect,
    LifecycleSnapshot,
    TransitionPlan,
    materialize_json,
    validate_identifier,
)
from paths import ba_home
from process_identity import ProcessIdentity, process_identity_is_proven_dead


SCHEMA_VERSION = 4
_STATUSES = {
    "planned",
    "effects_applied",
    "committed",
    "notification_attempted",
}
_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[Path] = set()
_OPEN_CONNECTIONS = 0
_OPEN_CONNECTIONS_LOCK = threading.Lock()


class TransitionConflict(RuntimeError):
    pass


class TransitionBusy(TransitionConflict):
    pass


class SnapshotChanged(TransitionConflict):
    pass


class AuthorityLeaseHeld(RuntimeError):
    pass


def _path() -> Path:
    return ba_home() / "lifecycle-command-state.sqlite3"


def store_path() -> Path:
    return _path().resolve()


def initialize() -> None:
    path = store_path()
    with _INIT_LOCK:
        if path in _INITIALIZED_PATHS:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=2.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            _migrate(connection)
        finally:
            connection.close()
        _INITIALIZED_PATHS.add(path)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = store_path()
    with _INIT_LOCK:
        initialized = path in _INITIALIZED_PATHS
    if not initialized:
        raise RuntimeError("lifecycle command store is not initialized")
    database = sqlite3.connect(
        f"file:{path}?mode=rw",
        timeout=2.0,
        isolation_level=None,
        uri=True,
    )
    global _OPEN_CONNECTIONS
    try:
        with _OPEN_CONNECTIONS_LOCK:
            _OPEN_CONNECTIONS += 1
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA synchronous = FULL")
        yield database
    finally:
        database.close()
        with _OPEN_CONNECTIONS_LOCK:
            _OPEN_CONNECTIONS -= 1


def _migrate(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN EXCLUSIVE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if type(version) is not int or version < 0:
            raise RuntimeError("invalid lifecycle command schema version")
        if version > SCHEMA_VERSION:
            raise RuntimeError("unsupported lifecycle command schema")
        if version == 0:
            schema = """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL,
                identity_json TEXT,
                revision INTEGER NOT NULL CHECK (revision >= 0),
                execution_json TEXT,
                execution_policy TEXT,
                completed_execution_count INTEGER NOT NULL DEFAULT 0
            ) STRICT;
            CREATE TABLE transitions (
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                command_json TEXT NOT NULL,
                source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
                next_snapshot_json TEXT NOT NULL,
                notification_type TEXT NOT NULL,
                notification_payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'planned',
                        'effects_applied',
                        'committed',
                        'notification_attempted'
                    )
                ),
                PRIMARY KEY (session_id, request_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            ) STRICT;
            CREATE TABLE effects (
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                effect_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT,
                PRIMARY KEY (session_id, request_id, ordinal),
                UNIQUE (effect_id),
                FOREIGN KEY (session_id, request_id)
                    REFERENCES transitions(session_id, request_id)
            ) STRICT;
            CREATE UNIQUE INDEX one_unfinished_transition_per_session
                ON transitions(session_id)
                WHERE status IN ('planned', 'effects_applied', 'committed');
            CREATE INDEX transitions_by_status
                ON transitions(status, session_id, request_id);
            CREATE TABLE authority_owner (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                owner_id TEXT NOT NULL,
                pid INTEGER NOT NULL CHECK (pid > 0),
                create_time REAL NOT NULL
            ) STRICT;
            CREATE TABLE pending_terminal_renders (
                session_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                lifecycle_message_id TEXT NOT NULL,
                execution_turn_id TEXT NOT NULL,
                assistant_message_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('complete', 'stopped', 'failed')
                ),
                FOREIGN KEY (session_id, request_id)
                    REFERENCES transitions(session_id, request_id)
            ) STRICT;
            """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("PRAGMA user_version = 4")
        elif version == 1:
            _migrate_v1_to_v2(connection)
            version = 2
        if version == 2:
            _migrate_v2_to_v3(connection)
            version = 3
        if version == 3:
            _migrate_v3_to_v4(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if version == 0:
        connection.execute("PRAGMA journal_mode = WAL")


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE authority_owner (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            owner_id TEXT NOT NULL,
            pid INTEGER NOT NULL CHECK (pid > 0),
            create_time REAL NOT NULL
        ) STRICT
        """
    )
    session_rows = connection.execute(
        "SELECT session_id, identity_json FROM sessions WHERE identity_json IS NOT NULL"
    ).fetchall()
    for row in session_rows:
        identity = _logical_identity(_load_object(row["identity_json"], "turn identity"))
        connection.execute(
            "UPDATE sessions SET identity_json = ? WHERE session_id = ?",
            (_dump(identity), row["session_id"]),
        )
    transition_rows = connection.execute(
        """
        SELECT session_id, request_id, command_json, next_snapshot_json,
               notification_payload_json
        FROM transitions
        """
    ).fetchall()
    for row in transition_rows:
        command = _load_object(row["command_json"], "command")
        command["identity"] = _logical_identity(command["identity"])
        command.update({
            "execution_identity": None,
            "provider_run_id": None,
            "execution_policy": (
                "single" if command.get("kind") == "begin_turn" else None
            ),
        })
        next_snapshot = _load_object(row["next_snapshot_json"], "next snapshot")
        if next_snapshot.get("identity") is not None:
            next_snapshot["identity"] = _logical_identity(next_snapshot["identity"])
        next_snapshot.update({
            "execution": None,
            "execution_policy": (
                "single" if next_snapshot.get("phase") != "idle" else None
            ),
            "completed_execution_count": 0,
        })
        notification = _load_object(
            row["notification_payload_json"],
            "notification payload",
        )
        notification["identity"] = _logical_identity(notification["identity"])
        fingerprint = LifecycleCommand.from_dict(command).fingerprint()
        connection.execute(
            """
            UPDATE transitions
            SET command_json = ?, fingerprint = ?, next_snapshot_json = ?,
                notification_payload_json = ?
            WHERE session_id = ? AND request_id = ?
            """,
            (
                _dump(command),
                fingerprint,
                _dump(next_snapshot),
                _dump(notification),
                row["session_id"],
                row["request_id"],
            ),
        )
    effect_rows = connection.execute(
        "SELECT session_id, request_id, ordinal, payload_json FROM effects"
    ).fetchall()
    for row in effect_rows:
        payload = _load_object(row["payload_json"], "effect payload")
        payload["identity"] = _logical_identity(payload["identity"])
        connection.execute(
            """
            UPDATE effects SET payload_json = ?
            WHERE session_id = ? AND request_id = ? AND ordinal = ?
            """,
            (
                _dump(payload),
                row["session_id"],
                row["request_id"],
                row["ordinal"],
            ),
        )
    connection.execute("PRAGMA user_version = 2")


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE sessions ADD COLUMN execution_json TEXT")
    connection.execute("ALTER TABLE sessions ADD COLUMN execution_policy TEXT")
    connection.execute(
        "ALTER TABLE sessions ADD COLUMN completed_execution_count INTEGER NOT NULL DEFAULT 0"
    )
    rows = connection.execute(
        """
        SELECT session_id, request_id, command_json, next_snapshot_json
        FROM transitions
        """
    ).fetchall()
    for row in rows:
        command = _load_object(row["command_json"], "command")
        command.setdefault("execution_identity", None)
        command.setdefault("provider_run_id", None)
        command.setdefault(
            "execution_policy",
            "single" if command.get("kind") == "begin_turn" else None,
        )
        snapshot = _load_object(row["next_snapshot_json"], "next snapshot")
        snapshot.setdefault("execution", None)
        snapshot.setdefault(
            "execution_policy",
            "single" if snapshot.get("phase") != "idle" else None,
        )
        snapshot.setdefault("completed_execution_count", 0)
        fingerprint = LifecycleCommand.from_dict(command).fingerprint()
        connection.execute(
            """
            UPDATE transitions
            SET command_json = ?, fingerprint = ?, next_snapshot_json = ?
            WHERE session_id = ? AND request_id = ?
            """,
            (
                _dump(command),
                fingerprint,
                _dump(snapshot),
                row["session_id"],
                row["request_id"],
            ),
        )
    connection.execute("PRAGMA user_version = 3")


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE pending_terminal_renders (
            session_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            lifecycle_message_id TEXT NOT NULL,
            execution_turn_id TEXT NOT NULL,
            assistant_message_id TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (
                outcome IN ('complete', 'stopped', 'failed')
            ),
            FOREIGN KEY (session_id, request_id)
                REFERENCES transitions(session_id, request_id)
        ) STRICT
        """
    )
    connection.execute("PRAGMA user_version = 4")


def _logical_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError("invalid persisted turn identity")
    user_turn_id = value.get("user_turn_id")
    lifecycle_message_id = value.get("lifecycle_message_id")
    validate_identifier(user_turn_id, "user_turn_id")
    validate_identifier(lifecycle_message_id, "lifecycle_message_id")
    return {
        "user_turn_id": user_turn_id,
        "lifecycle_message_id": lifecycle_message_id,
    }


def session_snapshot(session_id: str) -> LifecycleSnapshot:
    validate_identifier(session_id, "session_id")
    with connection() as database:
        row = database.execute(
            """
            SELECT session_id, phase, identity_json, revision,
                   execution_json, execution_policy, completed_execution_count
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    return _snapshot_from_row(row) if row is not None else LifecycleSnapshot()


def active_session_snapshots() -> list[tuple[str, LifecycleSnapshot]]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT session_id, phase, identity_json, revision,
                   execution_json, execution_policy, completed_execution_count
            FROM sessions
            WHERE phase != 'idle'
            ORDER BY session_id
            """
        ).fetchall()
    return [
        (str(row["session_id"]), _snapshot_from_row(row))
        for row in rows
    ]


def pending_terminal_renders() -> list[dict[str, str]]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT session_id, request_id, lifecycle_message_id,
                   execution_turn_id, assistant_message_id, outcome
            FROM pending_terminal_renders
            ORDER BY session_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def pending_terminal_render(session_id: str) -> dict[str, str] | None:
    validate_identifier(session_id, "session_id")
    with connection() as database:
        row = database.execute(
            """
            SELECT session_id, request_id, lifecycle_message_id,
                   execution_turn_id, assistant_message_id, outcome
            FROM pending_terminal_renders
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def acknowledge_terminal_render(session_id: str, request_id: str) -> bool:
    validate_identifier(session_id, "session_id")
    validate_identifier(request_id, "request_id")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            DELETE FROM pending_terminal_renders
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        )
        database.commit()
    return cursor.rowcount == 1


def acquire_authority(
    owner_id: str,
    process_identity: ProcessIdentity,
) -> None:
    validate_identifier(owner_id, "owner_id")
    if type(process_identity.pid) is not int or process_identity.pid <= 0:
        raise ValueError("authority pid must be a positive integer")
    if (
        type(process_identity.create_time) is not float
        or not math.isfinite(process_identity.create_time)
    ):
        raise ValueError("authority create_time must be finite")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            "SELECT owner_id, pid, create_time FROM authority_owner WHERE singleton = 1"
        ).fetchone()
        if existing is not None:
            if (
                existing["owner_id"] == owner_id
                and existing["pid"] == process_identity.pid
                and existing["create_time"] == process_identity.create_time
            ):
                database.commit()
                return
            recorded = ProcessIdentity(
                pid=existing["pid"],
                create_time=existing["create_time"],
            )
            if not process_identity_is_proven_dead(recorded):
                database.rollback()
                raise AuthorityLeaseHeld(
                    "lifecycle authority is owned by a live or unverifiable process"
                )
        database.execute(
            """
            INSERT INTO authority_owner(singleton, owner_id, pid, create_time)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                owner_id = excluded.owner_id,
                pid = excluded.pid,
                create_time = excluded.create_time
            """,
            (
                owner_id,
                process_identity.pid,
                process_identity.create_time,
            ),
        )
        database.commit()


def release_authority(
    owner_id: str,
    process_identity: ProcessIdentity,
) -> None:
    validate_identifier(owner_id, "owner_id")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            DELETE FROM authority_owner
            WHERE singleton = 1 AND owner_id = ? AND pid = ? AND create_time = ?
            """,
            (
                owner_id,
                process_identity.pid,
                process_identity.create_time,
            ),
        )
        database.commit()


def authority_owner() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT owner_id, pid, create_time FROM authority_owner WHERE singleton = 1"
        ).fetchone()
    return dict(row) if row is not None else None


def transition_for(session_id: str, request_id: str) -> dict[str, Any] | None:
    validate_identifier(session_id, "session_id")
    validate_identifier(request_id, "request_id")
    with connection() as database:
        row = database.execute(
            """
            SELECT *
            FROM transitions
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        ).fetchone()
        if row is None:
            return None
        effects = database.execute(
            """
            SELECT effect_id, kind, payload_json, result_json
            FROM effects
            WHERE session_id = ? AND request_id = ?
            ORDER BY ordinal
            """,
            (session_id, request_id),
        ).fetchall()
    return _transition_from_rows(row, effects)


def required_transition(session_id: str, request_id: str) -> dict[str, Any]:
    transition = transition_for(session_id, request_id)
    if transition is None:
        raise RuntimeError("lifecycle command transition disappeared")
    return transition


def unfinished_transitions() -> tuple[tuple[str, str], ...]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT session_id, request_id
            FROM transitions
            WHERE status IN ('planned', 'effects_applied', 'committed')
            ORDER BY session_id, request_id
            """
        ).fetchall()
    return tuple((row["session_id"], row["request_id"]) for row in rows)


def persist_plan(
    command: LifecycleCommand,
    snapshot: LifecycleSnapshot,
    plan: TransitionPlan,
) -> str:
    command_json = _dump(command.to_dict())
    next_snapshot_json = _dump(plan.next_snapshot.to_dict())
    notification_payload_json = _dump(materialize_json(plan.fact_payload))
    effects = tuple(
        (
            ordinal,
            effect.effect_id,
            effect.kind,
            _dump(effect.to_dict()["payload"]),
        )
        for ordinal, effect in enumerate(plan.effects)
    )
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            """
            SELECT fingerprint
            FROM transitions
            WHERE session_id = ? AND request_id = ?
            """,
            (command.session_id, command.request_id),
        ).fetchone()
        if existing is not None:
            database.commit()
            if existing["fingerprint"] != command.fingerprint():
                raise TransitionConflict(
                    "request_id is already bound to another command"
                )
            return "existing"
        database.execute(
            """
            INSERT INTO sessions(
                session_id, phase, identity_json, revision,
                execution_json, execution_policy, completed_execution_count
            )
            VALUES (?, 'idle', NULL, 0, NULL, NULL, 0)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (command.session_id,),
        )
        current = database.execute(
            """
            SELECT session_id, phase, identity_json, revision,
                   execution_json, execution_policy, completed_execution_count
            FROM sessions
            WHERE session_id = ?
            """,
            (command.session_id,),
        ).fetchone()
        if _snapshot_from_row(current) != snapshot:
            database.rollback()
            raise SnapshotChanged("lifecycle snapshot changed before intent")
        try:
            database.execute(
                """
                INSERT INTO transitions(
                    session_id,
                    request_id,
                    fingerprint,
                    command_json,
                    source_revision,
                    next_snapshot_json,
                    notification_type,
                    notification_payload_json,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned')
                """,
                (
                    command.session_id,
                    command.request_id,
                    command.fingerprint(),
                    command_json,
                    snapshot.revision,
                    next_snapshot_json,
                    plan.fact_type,
                    notification_payload_json,
                ),
            )
            database.executemany(
                """
                INSERT INTO effects(
                    session_id,
                    request_id,
                    ordinal,
                    effect_id,
                    kind,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        command.session_id,
                        command.request_id,
                        ordinal,
                        effect_id,
                        kind,
                        payload_json,
                    )
                    for ordinal, effect_id, kind, payload_json in effects
                ),
            )
        except sqlite3.IntegrityError as exc:
            database.rollback()
            raise TransitionBusy(
                "another lifecycle transition is unfinished"
            ) from exc
        database.commit()
        return "inserted"


def record_effect_result(
    session_id: str,
    request_id: str,
    ordinal: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("effect ordinal must be a non-negative integer")
    result_json = _dump(result)
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT result_json
            FROM effects
            WHERE session_id = ? AND request_id = ? AND ordinal = ?
            """,
            (session_id, request_id, ordinal),
        ).fetchone()
        if row is None:
            database.rollback()
            raise RuntimeError("lifecycle effect disappeared")
        if row["result_json"] is not None:
            database.commit()
            return _load_object(row["result_json"], "effect result")
        database.execute(
            """
            UPDATE effects
            SET result_json = ?
            WHERE session_id = ? AND request_id = ? AND ordinal = ?
            """,
            (result_json, session_id, request_id, ordinal),
        )
        remaining = database.execute(
            """
            SELECT 1
            FROM effects
            WHERE session_id = ? AND request_id = ? AND result_json IS NULL
            LIMIT 1
            """,
            (session_id, request_id),
        ).fetchone()
        if remaining is None:
            database.execute(
                """
                UPDATE transitions
                SET status = 'effects_applied'
                WHERE session_id = ? AND request_id = ? AND status = 'planned'
                """,
                (session_id, request_id),
            )
        database.commit()
    return result


def commit_transition(session_id: str, request_id: str) -> LifecycleSnapshot:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT source_revision, next_snapshot_json, status
            FROM transitions
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        ).fetchone()
        if row is None:
            database.rollback()
            raise RuntimeError("lifecycle transition disappeared")
        next_snapshot = LifecycleSnapshot.from_dict(
            _load_object(row["next_snapshot_json"], "next snapshot")
        )
        if row["status"] in {"committed", "notification_attempted"}:
            database.commit()
            return next_snapshot
        if row["status"] != "effects_applied":
            database.rollback()
            raise RuntimeError("cannot commit lifecycle transition before effects")
        cursor = database.execute(
            """
            UPDATE sessions
            SET phase = ?, identity_json = ?, revision = ?,
                execution_json = ?, execution_policy = ?,
                completed_execution_count = ?
            WHERE session_id = ? AND revision = ?
            """,
            (
                next_snapshot.phase,
                _identity_json(next_snapshot),
                next_snapshot.revision,
                (
                    _dump(next_snapshot.execution.to_dict())
                    if next_snapshot.execution else None
                ),
                next_snapshot.execution_policy,
                next_snapshot.completed_execution_count,
                session_id,
                row["source_revision"],
            ),
        )
        if cursor.rowcount != 1:
            database.rollback()
            raise SnapshotChanged("lifecycle revision changed before commit")
        database.execute(
            """
            UPDATE transitions
            SET status = 'committed'
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        )
        if next_snapshot.phase == "idle":
            effect_row = database.execute(
                """
                SELECT payload_json
                FROM effects
                WHERE session_id = ? AND request_id = ? AND ordinal = 0
                """,
                (session_id, request_id),
            ).fetchone()
            payload = (
                _load_object(effect_row["payload_json"], "effect payload")
                if effect_row is not None
                else {}
            )
            identity = payload.get("identity")
            execution_identity = payload.get("execution_identity")
            outcome = payload.get("outcome")
            if (
                isinstance(identity, dict)
                and isinstance(execution_identity, dict)
                and outcome in {"complete", "stopped", "failed"}
            ):
                database.execute(
                    """
                    INSERT INTO pending_terminal_renders(
                        session_id, request_id, lifecycle_message_id,
                        execution_turn_id, assistant_message_id, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        request_id = excluded.request_id,
                        lifecycle_message_id = excluded.lifecycle_message_id,
                        execution_turn_id = excluded.execution_turn_id,
                        assistant_message_id = excluded.assistant_message_id,
                        outcome = excluded.outcome
                    """,
                    (
                        session_id,
                        request_id,
                        identity["lifecycle_message_id"],
                        execution_identity["execution_turn_id"],
                        execution_identity["assistant_message_id"],
                        outcome,
                    ),
                )
        database.commit()
    return next_snapshot


def mark_notification_attempted(session_id: str, request_id: str) -> bool:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            UPDATE transitions
            SET status = 'notification_attempted'
            WHERE session_id = ? AND request_id = ? AND status = 'committed'
            """,
            (session_id, request_id),
        )
        database.commit()
    return cursor.rowcount == 1


def table_counts() -> dict[str, int]:
    with connection() as database:
        return {
            table: database.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "sessions",
                "transitions",
                "effects",
                "pending_terminal_renders",
            )
        }


def open_connection_count() -> int:
    with _OPEN_CONNECTIONS_LOCK:
        return _OPEN_CONNECTIONS


def _snapshot_from_row(row: sqlite3.Row) -> LifecycleSnapshot:
    identity = (
        _load_object(row["identity_json"], "turn identity")
        if row["identity_json"] is not None
        else None
    )
    return LifecycleSnapshot.from_dict({
        "phase": row["phase"],
        "identity": identity,
        "revision": row["revision"],
        "execution": (
            _load_object(row["execution_json"], "execution")
            if row["execution_json"] is not None else None
        ),
        "execution_policy": row["execution_policy"],
        "completed_execution_count": row["completed_execution_count"],
    })


def _transition_from_rows(
    row: sqlite3.Row,
    effect_rows: list[sqlite3.Row],
) -> dict[str, Any]:
    if row["status"] not in _STATUSES:
        raise RuntimeError("invalid lifecycle transition status")
    command = LifecycleCommand.from_dict(
        _load_object(row["command_json"], "command")
    )
    if command.fingerprint() != row["fingerprint"]:
        raise RuntimeError("lifecycle transition fingerprint mismatch")
    effects = []
    results = []
    for effect_row in effect_rows:
        effect = LifecycleEffect(
            effect_id=effect_row["effect_id"],
            kind=effect_row["kind"],
            payload=_load_object(effect_row["payload_json"], "effect payload"),
        )
        effects.append(effect.to_dict())
        if effect_row["result_json"] is not None:
            results.append(
                _load_object(effect_row["result_json"], "effect result")
            )
    return {
        "command": command.to_dict(),
        "fingerprint": row["fingerprint"],
        "source_revision": row["source_revision"],
        "next_snapshot": _load_object(
            row["next_snapshot_json"],
            "next snapshot",
        ),
        "effects": effects,
        "effect_results": results,
        "notification_type": row["notification_type"],
        "notification_payload": _load_object(
            row["notification_payload_json"],
            "notification payload",
        ),
        "status": row["status"],
    }


def _identity_json(snapshot: LifecycleSnapshot) -> str | None:
    return _dump(snapshot.identity.to_dict()) if snapshot.identity else None


def _dump(value: Any) -> str:
    return json.dumps(
        materialize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load_object(raw: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid lifecycle {name} JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid lifecycle {name}")
    return value
