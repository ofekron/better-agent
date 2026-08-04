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
    SelectorAttemptDecision,
    SelectorAuthoritySnapshot,
    SelectorIdentity,
    TransitionPlan,
    materialize_json,
    validate_identifier,
)
from paths import ba_home
from process_identity import ProcessIdentity, process_identity_is_proven_dead


SCHEMA_VERSION = 8
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
                owner_incarnation TEXT,
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
            CREATE TABLE selector_authorities (
                session_id TEXT PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                pending_projection_json TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                    ON DELETE CASCADE
            ) STRICT;
            CREATE TABLE selector_handoff_acceptances (
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                acceptance_json TEXT NOT NULL,
                PRIMARY KEY (session_id, request_id),
                FOREIGN KEY (session_id, request_id)
                    REFERENCES transitions(session_id, request_id)
                    ON DELETE CASCADE
            ) STRICT;
            CREATE TABLE execution_selector_roles (
                session_id TEXT NOT NULL,
                execution_turn_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('primary', 'supervisor')),
                PRIMARY KEY (session_id, execution_turn_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                    ON DELETE CASCADE
            ) STRICT;
            CREATE TABLE execution_selector_attempts (
                session_id TEXT NOT NULL,
                execution_turn_id TEXT NOT NULL,
                provider_run_id TEXT NOT NULL,
                selector_generation INTEGER NOT NULL CHECK (selector_generation >= 0),
                role TEXT NOT NULL CHECK (role IN ('primary', 'supervisor')),
                selector_identity_json TEXT NOT NULL,
                native_sid_compatibility_json TEXT,
                PRIMARY KEY (session_id, provider_run_id),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                    ON DELETE CASCADE
            ) STRICT;
            """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("PRAGMA user_version = 8")
        elif version == 1:
            _migrate_v1_to_v2(connection)
            version = 2
        if version == 2:
            _migrate_v2_to_v3(connection)
            version = 3
        if version == 3:
            _migrate_v3_to_v4(connection)
            version = 4
        if version == 4:
            _migrate_v4_to_v5(connection)
            version = 5
        if version == 5:
            _migrate_v5_to_v6(connection)
            version = 6
        if version == 6:
            _migrate_v6_to_v7(connection)
            version = 7
        if version == 7:
            _migrate_v7_to_v8(connection)
            version = 8
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


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE sessions ADD COLUMN owner_incarnation TEXT")
    connection.execute("PRAGMA user_version = 5")


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE selector_authorities (
            session_id TEXT PRIMARY KEY,
            snapshot_json TEXT NOT NULL,
            pending_projection_json TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                ON DELETE CASCADE
        ) STRICT
        """
    )
    connection.execute("PRAGMA user_version = 6")


def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE selector_handoff_acceptances (
            session_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            acceptance_json TEXT NOT NULL,
            PRIMARY KEY (session_id, request_id),
            FOREIGN KEY (session_id, request_id)
                REFERENCES transitions(session_id, request_id)
                ON DELETE CASCADE
        ) STRICT
        """
    )
    connection.execute(
        """
        CREATE TABLE execution_selector_roles (
            session_id TEXT NOT NULL,
            execution_turn_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('primary', 'supervisor')),
            PRIMARY KEY (session_id, execution_turn_id),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                ON DELETE CASCADE
        ) STRICT
        """
    )
    connection.execute("PRAGMA user_version = 7")


def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE execution_selector_attempts (
            session_id TEXT NOT NULL,
            execution_turn_id TEXT NOT NULL,
            provider_run_id TEXT NOT NULL,
            selector_generation INTEGER NOT NULL CHECK (selector_generation >= 0),
            role TEXT NOT NULL CHECK (role IN ('primary', 'supervisor')),
            selector_identity_json TEXT NOT NULL,
            native_sid_compatibility_json TEXT,
            PRIMARY KEY (session_id, provider_run_id),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                ON DELETE CASCADE
        ) STRICT
        """
    )
    connection.execute("PRAGMA user_version = 8")


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


def selector_authority_snapshot(
    session_id: str,
) -> SelectorAuthoritySnapshot | None:
    validate_identifier(session_id, "session_id")
    with connection() as database:
        row = database.execute(
            "SELECT snapshot_json FROM selector_authorities WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return SelectorAuthoritySnapshot.from_dict(
        _load_object(row["snapshot_json"], "selector authority snapshot")
    )


def persist_selector_transition(
    session_id: str,
    *,
    target: SelectorIdentity,
    projection_updates: dict[str, Any],
    primary_native_sid: str | None,
    supervisor_native_sid: str | None,
    primary_legacy_native_sid_compatibility: dict[str, Any] | None,
    supervisor_legacy_native_sid_compatibility: dict[str, Any] | None,
) -> SelectorAuthoritySnapshot:
    validate_identifier(session_id, "session_id")
    if type(projection_updates) is not dict:
        raise ValueError("selector projection updates must be an object")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        session = database.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            database.rollback()
            raise TransitionConflict("selector authority requires a lifecycle session")
        row = database.execute(
            "SELECT snapshot_json FROM selector_authorities WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            current = SelectorAuthoritySnapshot(
                primary_native_sid=primary_native_sid,
                supervisor_native_sid=supervisor_native_sid,
                primary_native_sid_compatibility=(
                    primary_legacy_native_sid_compatibility
                ),
                supervisor_native_sid_compatibility=(
                    supervisor_legacy_native_sid_compatibility
                ),
            )
        else:
            current = SelectorAuthoritySnapshot.from_dict(
                _load_object(row["snapshot_json"], "selector authority snapshot")
            )
            current = current.merge_missing_native_sid_evidence(
                primary_native_sid=primary_native_sid,
                supervisor_native_sid=supervisor_native_sid,
                primary_native_sid_compatibility=(
                    primary_legacy_native_sid_compatibility
                ),
                supervisor_native_sid_compatibility=(
                    supervisor_legacy_native_sid_compatibility
                ),
            )
        next_snapshot = current.transition(target)
        projection_json = _dump({
            "updates": projection_updates,
            "target": target.to_dict(),
            "clear_native_sids": current.identity != target,
        })
        database.execute(
            """
            INSERT INTO selector_authorities(
                session_id, snapshot_json, pending_projection_json
            ) VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                pending_projection_json = excluded.pending_projection_json
            """,
            (session_id, _dump(next_snapshot.to_dict()), projection_json),
        )
        database.commit()
    return next_snapshot


def pending_selector_projections() -> tuple[tuple[str, dict[str, Any]], ...]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT session_id, pending_projection_json
            FROM selector_authorities
            WHERE pending_projection_json IS NOT NULL
            ORDER BY session_id
            """
        ).fetchall()
    return tuple(
        (
            row["session_id"],
            _load_object(row["pending_projection_json"], "selector projection"),
        )
        for row in rows
    )


def pending_selector_projection(session_id: str) -> dict[str, Any] | None:
    validate_identifier(session_id, "session_id")
    with connection() as database:
        row = database.execute(
            """
            SELECT pending_projection_json
            FROM selector_authorities
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    if row is None or row["pending_projection_json"] is None:
        return None
    return _load_object(
        row["pending_projection_json"],
        "selector projection",
    )


def execution_selector_role(
    session_id: str,
    execution_turn_id: str,
) -> str | None:
    validate_identifier(session_id, "session_id")
    validate_identifier(execution_turn_id, "execution_turn_id")
    with connection() as database:
        row = database.execute(
            """
            SELECT role
            FROM execution_selector_roles
            WHERE session_id = ? AND execution_turn_id = ?
            """,
            (session_id, execution_turn_id),
        ).fetchone()
    return row["role"] if row is not None else None


def record_execution_selector_attempt(
    session_id: str,
    *,
    execution_turn_id: str,
    provider_run_id: str,
    selector_generation: int,
    role: str,
    selector: SelectorIdentity,
    native_sid_compatibility: dict[str, Any] | None,
) -> None:
    validate_identifier(session_id, "session_id")
    validate_identifier(execution_turn_id, "execution_turn_id")
    validate_identifier(provider_run_id, "provider_run_id")
    if type(selector_generation) is not int or selector_generation < 0:
        raise ValueError("selector_generation is invalid")
    if role not in {"primary", "supervisor"}:
        raise ValueError("selector attempt role is invalid")
    if not isinstance(selector, SelectorIdentity):
        raise ValueError("selector attempt identity is invalid")
    if native_sid_compatibility is not None and type(
        native_sid_compatibility
    ) is not dict:
        raise ValueError("native SID compatibility must be an object")
    compatibility_json = (
        _dump(native_sid_compatibility)
        if native_sid_compatibility is not None
        else None
    )
    selector_identity_json = _dump(selector.to_dict())
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            """
            SELECT execution_turn_id, selector_generation, role,
                   selector_identity_json,
                   native_sid_compatibility_json
            FROM execution_selector_attempts
            WHERE session_id = ? AND provider_run_id = ?
            """,
            (session_id, provider_run_id),
        ).fetchone()
        expected = (
            execution_turn_id,
            selector_generation,
            role,
            selector_identity_json,
            compatibility_json,
        )
        if existing is not None:
            actual = (
                existing["execution_turn_id"],
                existing["selector_generation"],
                existing["role"],
                existing["selector_identity_json"],
                existing["native_sid_compatibility_json"],
            )
            database.commit()
            if actual != expected:
                raise TransitionConflict(
                    "provider run is already bound to another selector attempt"
                )
            return
        database.execute(
            """
            INSERT INTO execution_selector_attempts(
                session_id, execution_turn_id, provider_run_id,
                selector_generation, role, selector_identity_json,
                native_sid_compatibility_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                execution_turn_id,
                provider_run_id,
                selector_generation,
                role,
                selector_identity_json,
                compatibility_json,
            ),
        )
        database.commit()


def execution_selector_attempt(
    session_id: str,
    provider_run_id: str,
) -> dict[str, Any] | None:
    validate_identifier(session_id, "session_id")
    validate_identifier(provider_run_id, "provider_run_id")
    with connection() as database:
        row = database.execute(
            """
            SELECT execution_turn_id, selector_generation, role,
                   selector_identity_json,
                   native_sid_compatibility_json
            FROM execution_selector_attempts
            WHERE session_id = ? AND provider_run_id = ?
            """,
            (session_id, provider_run_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "execution_turn_id": row["execution_turn_id"],
        "provider_run_id": provider_run_id,
        "selector_generation": row["selector_generation"],
        "role": row["role"],
        "selector": SelectorIdentity.from_dict(
            _load_object(
                row["selector_identity_json"],
                "execution selector attempt identity",
            )
        ),
        "native_sid_compatibility": (
            _load_object(
                row["native_sid_compatibility_json"],
                "execution selector attempt compatibility",
            )
            if row["native_sid_compatibility_json"] is not None
            else None
        ),
    }


def persist_admitted_selector_attempt(
    session_id: str,
    *,
    target: SelectorIdentity,
    native_sid_compatibility: dict[str, Any] | None,
    primary_native_sid: str | None,
    supervisor_native_sid: str | None,
    primary_native_sid_compatibility: dict[str, Any] | None,
    supervisor_native_sid_compatibility: dict[str, Any] | None,
) -> tuple[SelectorAuthoritySnapshot, SelectorAttemptDecision]:
    validate_identifier(session_id, "session_id")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT snapshot_json FROM selector_authorities WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            current = SelectorAuthoritySnapshot()
        else:
            current = SelectorAuthoritySnapshot.from_dict(
                _load_object(row["snapshot_json"], "selector authority snapshot")
            )
        next_snapshot, decision = current.admit_attempt(
            target,
            native_sid_compatibility,
            primary_native_sid=primary_native_sid,
            supervisor_native_sid=supervisor_native_sid,
            primary_native_sid_compatibility=(
                primary_native_sid_compatibility
            ),
            supervisor_native_sid_compatibility=(
                supervisor_native_sid_compatibility
            ),
        )
        if decision == "stale":
            database.rollback()
            return current, decision
        database.execute(
            """
            INSERT INTO selector_authorities(
                session_id, snapshot_json, pending_projection_json
            ) VALUES (?, ?, NULL)
            ON CONFLICT(session_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json
            """,
            (session_id, _dump(next_snapshot.to_dict())),
        )
        database.commit()
    return next_snapshot, decision


def acknowledge_selector_projection(
    session_id: str,
    target: SelectorIdentity,
) -> bool:
    validate_identifier(session_id, "session_id")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT pending_projection_json
            FROM selector_authorities
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None or row["pending_projection_json"] is None:
            database.rollback()
            return False
        pending = _load_object(
            row["pending_projection_json"],
            "selector projection",
        )
        if pending.get("target") != target.to_dict():
            database.rollback()
            return False
        database.execute(
            """
            UPDATE selector_authorities
            SET pending_projection_json = NULL
            WHERE session_id = ?
            """,
            (session_id,),
        )
        database.commit()
    return True


def attach_selector_native_sid(
    session_id: str,
    *,
    expected_generation: int,
    role: str,
    native_sid: str,
) -> tuple[SelectorAuthoritySnapshot, dict[str, Any]] | None:
    validate_identifier(session_id, "session_id")
    validate_identifier(native_sid, "native_sid")
    return _persist_selector_native_sid(
        session_id,
        expected_generation=expected_generation,
        role=role,
        native_sid=native_sid,
        expected_native_sid=None,
        require_expected_native_sid=False,
    )


def clear_selector_native_sid(
    session_id: str,
    *,
    expected_generation: int,
    role: str,
    expected_native_sid: str,
) -> tuple[SelectorAuthoritySnapshot, dict[str, Any]] | None:
    validate_identifier(session_id, "session_id")
    validate_identifier(expected_native_sid, "expected_native_sid")
    return _persist_selector_native_sid(
        session_id,
        expected_generation=expected_generation,
        role=role,
        native_sid=None,
        expected_native_sid=expected_native_sid,
        require_expected_native_sid=True,
    )


def _persist_selector_native_sid(
    session_id: str,
    *,
    expected_generation: int,
    role: str,
    native_sid: str | None,
    expected_native_sid: str | None,
    require_expected_native_sid: bool,
) -> tuple[SelectorAuthoritySnapshot, dict[str, Any]] | None:
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("selector generation is invalid")
    if role not in {"primary", "supervisor"}:
        raise ValueError("selector native SID role is invalid")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT snapshot_json FROM selector_authorities WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            database.rollback()
            return None
        snapshot = SelectorAuthoritySnapshot.from_dict(
            _load_object(row["snapshot_json"], "selector authority snapshot")
        )
        if (
            snapshot.generation != expected_generation
            or snapshot.identity is None
            or snapshot.native_sid_compatibility is None
        ):
            database.rollback()
            return None
        current_native_sid = (
            snapshot.supervisor_native_sid
            if role == "supervisor"
            else snapshot.primary_native_sid
        )
        if require_expected_native_sid and current_native_sid != expected_native_sid:
            database.rollback()
            return None
        updated = SelectorAuthoritySnapshot(
            generation=snapshot.generation,
            identity=snapshot.identity,
            native_sid_compatibility=snapshot.native_sid_compatibility,
            primary_native_sid=(
                native_sid if role == "primary" else snapshot.primary_native_sid
            ),
            supervisor_native_sid=(
                native_sid
                if role == "supervisor"
                else snapshot.supervisor_native_sid
            ),
            primary_native_sid_compatibility=(
                (
                    snapshot.native_sid_compatibility
                    if native_sid is not None
                    else None
                )
                if role == "primary"
                else snapshot.primary_native_sid_compatibility
            ),
            supervisor_native_sid_compatibility=(
                (
                    snapshot.native_sid_compatibility
                    if native_sid is not None
                    else None
                )
                if role == "supervisor"
                else snapshot.supervisor_native_sid_compatibility
            ),
            handoff=snapshot.handoff,
        )
        projection = {
            "kind": "native_sid",
            "generation": updated.generation,
            "identity": updated.identity.to_dict(),
            "role": role,
            "native_sid": native_sid,
        }
        database.execute(
            """
            UPDATE selector_authorities
            SET snapshot_json = ?, pending_projection_json = ?
            WHERE session_id = ?
            """,
            (_dump(updated.to_dict()), _dump(projection), session_id),
        )
        database.commit()
    return updated, projection


def _acknowledge_exact_selector_projection(
    session_id: str,
    projection: dict[str, Any],
    *,
    label: str,
) -> bool:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT pending_projection_json
            FROM selector_authorities
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None or row["pending_projection_json"] is None:
            database.rollback()
            return False
        pending = _load_object(row["pending_projection_json"], label)
        if pending != projection:
            database.rollback()
            return False
        database.execute(
            """
            UPDATE selector_authorities
            SET pending_projection_json = NULL
            WHERE session_id = ?
            """,
            (session_id,),
        )
        database.commit()
    return True


def acknowledge_native_sid_projection(
    session_id: str,
    projection: dict[str, Any],
) -> bool:
    validate_identifier(session_id, "session_id")
    if type(projection) is not dict or projection.get("kind") != "native_sid":
        raise ValueError("native SID projection is invalid")
    return _acknowledge_exact_selector_projection(
        session_id,
        projection,
        label="native SID projection",
    )


def acknowledge_handoff_projection(
    session_id: str,
    projection: dict[str, Any],
) -> bool:
    validate_identifier(session_id, "session_id")
    if type(projection) is not dict or projection.get("kind") != "handoff_consumed":
        raise ValueError("handoff projection is invalid")
    return _acknowledge_exact_selector_projection(
        session_id,
        projection,
        label="handoff projection",
    )


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


def align_session_owner(session_id: str, owner_incarnation: str) -> None:
    validate_identifier(session_id, "session_id")
    validate_identifier(owner_incarnation, "owner_incarnation")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT owner_incarnation FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            database.execute(
                """
                INSERT INTO sessions(
                    session_id, owner_incarnation, phase, identity_json,
                    revision, execution_json, execution_policy,
                    completed_execution_count
                ) VALUES (?, ?, 'idle', NULL, 0, NULL, NULL, 0)
                """,
                (session_id, owner_incarnation),
            )
        elif row["owner_incarnation"] is None:
            database.execute(
                "UPDATE sessions SET owner_incarnation = ? WHERE session_id = ?",
                (owner_incarnation, session_id),
            )
        elif row["owner_incarnation"] != owner_incarnation:
            _delete_session_rows(database, session_id)
            database.execute(
                """
                INSERT INTO sessions(
                    session_id, owner_incarnation, phase, identity_json,
                    revision, execution_json, execution_policy,
                    completed_execution_count
                ) VALUES (?, ?, 'idle', NULL, 0, NULL, NULL, 0)
                """,
                (session_id, owner_incarnation),
            )
        database.commit()


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


def retire_session(
    session_id: str,
    *,
    expected_terminal_request_id: str | None = None,
    expected_owner_incarnation: str | None = None,
    allow_unbound_owner: bool = False,
) -> bool:
    validate_identifier(session_id, "session_id")
    if expected_terminal_request_id is not None:
        validate_identifier(
            expected_terminal_request_id,
            "expected_terminal_request_id",
        )
    if expected_owner_incarnation is not None:
        validate_identifier(
            expected_owner_incarnation,
            "expected_owner_incarnation",
        )
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        if expected_terminal_request_id is not None:
            pending = database.execute(
                """
                SELECT 1
                FROM pending_terminal_renders
                WHERE session_id = ? AND request_id = ?
                """,
                (session_id, expected_terminal_request_id),
            ).fetchone()
            if pending is None:
                database.rollback()
                return False
        if expected_owner_incarnation is not None:
            owner = database.execute(
                """
                SELECT 1 FROM sessions
                WHERE session_id = ?
                  AND (
                    owner_incarnation = ?
                    OR (? AND owner_incarnation IS NULL)
                  )
                """,
                (
                    session_id,
                    expected_owner_incarnation,
                    allow_unbound_owner,
                ),
            ).fetchone()
            if owner is None:
                database.rollback()
                return False
        deleted = _delete_session_rows(database, session_id)
        database.commit()
    return deleted


def _delete_session_rows(
    database: sqlite3.Connection,
    session_id: str,
) -> bool:
    database.execute(
        "DELETE FROM pending_terminal_renders WHERE session_id = ?",
        (session_id,),
    )
    database.execute(
        "DELETE FROM effects WHERE session_id = ?",
        (session_id,),
    )
    database.execute(
        "DELETE FROM transitions WHERE session_id = ?",
        (session_id,),
    )
    cursor = database.execute(
        "DELETE FROM sessions WHERE session_id = ?",
        (session_id,),
    )
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
        acceptance_row = database.execute(
            """
            SELECT acceptance_json
            FROM selector_handoff_acceptances
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        ).fetchone()
    transition = _transition_from_rows(row, effects)
    transition["selector_handoff_acceptance"] = (
        _load_object(
            acceptance_row["acceptance_json"],
            "selector handoff acceptance",
        )
        if acceptance_row is not None
        else None
    )
    return transition


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


def _validated_selector_handoff_acceptance(
    command: LifecycleCommand,
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {
        "generation", "role", "source_sid", "target", "provider_run_id",
    }:
        raise ValueError("selector handoff acceptance is invalid")
    if (
        command.kind not in {"finish_execution", "finish_execution_and_turn"}
        or command.provider_run_id != value["provider_run_id"]
    ):
        raise ValueError("selector handoff acceptance requires exact success")
    generation = value["generation"]
    if type(generation) is not int or generation < 0:
        raise ValueError("selector handoff generation is invalid")
    role = value["role"]
    if role not in {"primary", "supervisor"}:
        raise ValueError("selector handoff role is invalid")
    validate_identifier(value["source_sid"], "selector handoff source_sid")
    validate_identifier(value["provider_run_id"], "selector handoff provider_run_id")
    target = SelectorIdentity.from_dict(value["target"])
    return {
        "generation": generation,
        "role": role,
        "source_sid": value["source_sid"],
        "target": target.to_dict(),
        "provider_run_id": value["provider_run_id"],
    }


def _validated_execution_selector_role(
    command: LifecycleCommand,
    role: str | None,
) -> str | None:
    if role is None:
        return None
    if command.kind != "start_execution" or role not in {"primary", "supervisor"}:
        raise ValueError("execution selector role is invalid")
    return role


def persist_plan(
    command: LifecycleCommand,
    snapshot: LifecycleSnapshot,
    plan: TransitionPlan,
    *,
    selector_handoff_acceptance: dict[str, Any] | None = None,
    execution_selector_role: str | None = None,
    execution_selector_attempt: dict[str, Any] | None = None,
) -> str:
    acceptance = _validated_selector_handoff_acceptance(
        command,
        selector_handoff_acceptance,
    )
    if acceptance is not None and plan.fact_payload.get("outcome") != "complete":
        raise ValueError("selector handoff acceptance requires terminal success")
    selector_role = _validated_execution_selector_role(
        command,
        execution_selector_role,
    )
    selector_attempt = None
    if execution_selector_attempt is not None:
        if (
            command.kind != "bind_execution_run"
            or command.execution_identity is None
            or command.provider_run_id is None
            or type(execution_selector_attempt) is not dict
            or set(execution_selector_attempt) != {
                "selector_generation",
                "role",
                "selector",
                "native_sid_compatibility",
            }
        ):
            raise ValueError("execution selector attempt is invalid")
        generation = execution_selector_attempt["selector_generation"]
        role = execution_selector_attempt["role"]
        if type(generation) is not int or generation < 0:
            raise ValueError("execution selector attempt generation is invalid")
        if role not in {"primary", "supervisor"}:
            raise ValueError("execution selector attempt role is invalid")
        selector = SelectorIdentity.from_dict(execution_selector_attempt["selector"])
        compatibility = execution_selector_attempt["native_sid_compatibility"]
        if compatibility is not None and type(compatibility) is not dict:
            raise ValueError("execution selector attempt compatibility is invalid")
        selector_attempt = {
            "execution_turn_id": command.execution_identity.execution_turn_id,
            "provider_run_id": command.provider_run_id,
            "selector_generation": generation,
            "role": role,
            "selector_identity_json": _dump(selector.to_dict()),
            "native_sid_compatibility_json": (
                _dump(compatibility) if compatibility is not None else None
            ),
        }
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
            existing_acceptance = database.execute(
                """
                SELECT acceptance_json
                FROM selector_handoff_acceptances
                WHERE session_id = ? AND request_id = ?
                """,
                (command.session_id, command.request_id),
            ).fetchone()
            existing_role = None
            if command.kind == "start_execution":
                existing_role = database.execute(
                    """
                    SELECT role
                    FROM execution_selector_roles
                    WHERE session_id = ? AND execution_turn_id = ?
                    """,
                    (
                        command.session_id,
                        command.execution_identity.execution_turn_id,
                    ),
                ).fetchone()
            existing_attempt = None
            if command.kind == "bind_execution_run":
                existing_attempt = database.execute(
                    """
                    SELECT execution_turn_id, provider_run_id,
                           selector_generation, role, selector_identity_json,
                           native_sid_compatibility_json
                    FROM execution_selector_attempts
                    WHERE session_id = ? AND provider_run_id = ?
                    """,
                    (command.session_id, command.provider_run_id),
                ).fetchone()
            database.commit()
            if existing["fingerprint"] != command.fingerprint():
                raise TransitionConflict(
                    "request_id is already bound to another command"
                )
            persisted_acceptance = (
                _load_object(
                    existing_acceptance["acceptance_json"],
                    "selector handoff acceptance",
                )
                if existing_acceptance is not None
                else None
            )
            if persisted_acceptance != acceptance:
                raise TransitionConflict(
                    "request_id is already bound to another selector acceptance"
                )
            persisted_role = (
                existing_role["role"] if existing_role is not None else None
            )
            if command.kind == "start_execution" and persisted_role != selector_role:
                raise TransitionConflict(
                    "request_id is already bound to another selector role"
                )
            persisted_attempt = (
                dict(existing_attempt) if existing_attempt is not None else None
            )
            if persisted_attempt != selector_attempt:
                raise TransitionConflict(
                    "request_id is already bound to another selector attempt"
                )
            return "existing"
        database.execute(
            """
            INSERT INTO sessions(
                session_id, owner_incarnation, phase, identity_json, revision,
                execution_json, execution_policy, completed_execution_count
            )
            VALUES (?, NULL, 'idle', NULL, 0, NULL, NULL, 0)
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
        if selector_attempt is not None:
            authority_row = database.execute(
                "SELECT snapshot_json FROM selector_authorities WHERE session_id = ?",
                (command.session_id,),
            ).fetchone()
            role_row = database.execute(
                """
                SELECT role FROM execution_selector_roles
                WHERE session_id = ? AND execution_turn_id = ?
                """,
                (
                    command.session_id,
                    selector_attempt["execution_turn_id"],
                ),
            ).fetchone()
            authority = (
                SelectorAuthoritySnapshot.from_dict(
                    _load_object(
                        authority_row["snapshot_json"],
                        "selector authority snapshot",
                    )
                )
                if authority_row is not None
                else None
            )
            if (
                authority is None
                or authority.generation != selector_attempt["selector_generation"]
                or authority.identity is None
                or _dump(authority.identity.to_dict())
                != selector_attempt["selector_identity_json"]
                or (
                    _dump(authority.native_sid_compatibility)
                    if authority.native_sid_compatibility is not None
                    else None
                )
                != selector_attempt["native_sid_compatibility_json"]
                or role_row is None
                or role_row["role"] != selector_attempt["role"]
            ):
                database.rollback()
                raise TransitionConflict(
                    "execution selector attempt lost its admitted authority"
                )
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
            if acceptance is not None:
                database.execute(
                    """
                    INSERT INTO selector_handoff_acceptances(
                        session_id, request_id, acceptance_json
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        command.session_id,
                        command.request_id,
                        _dump(acceptance),
                    ),
                )
            if selector_role is not None:
                database.execute(
                    """
                    INSERT INTO execution_selector_roles(
                        session_id, execution_turn_id, role
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        command.session_id,
                        command.execution_identity.execution_turn_id,
                        selector_role,
                    ),
                )
            if selector_attempt is not None:
                database.execute(
                    """
                    INSERT INTO execution_selector_attempts(
                        session_id, execution_turn_id, provider_run_id,
                        selector_generation, role, selector_identity_json,
                        native_sid_compatibility_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.session_id,
                        selector_attempt["execution_turn_id"],
                        selector_attempt["provider_run_id"],
                        selector_attempt["selector_generation"],
                        selector_attempt["role"],
                        selector_attempt["selector_identity_json"],
                        selector_attempt["native_sid_compatibility_json"],
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


def _consume_selector_handoff_at_commit(
    database: sqlite3.Connection,
    *,
    session_id: str,
    request_id: str,
    command: LifecycleCommand,
) -> None:
    row = database.execute(
        """
        SELECT acceptance_json
        FROM selector_handoff_acceptances
        WHERE session_id = ? AND request_id = ?
        """,
        (session_id, request_id),
    ).fetchone()
    if row is None:
        return
    acceptance = _validated_selector_handoff_acceptance(
        command,
        _load_object(row["acceptance_json"], "selector handoff acceptance"),
    )
    if acceptance is None:
        return
    effect_row = database.execute(
        """
        SELECT payload_json
        FROM effects
        WHERE session_id = ? AND request_id = ? AND ordinal = 0
        """,
        (session_id, request_id),
    ).fetchone()
    if effect_row is None or _load_object(
        effect_row["payload_json"],
        "effect payload",
    ).get("outcome") != "complete":
        return
    role_row = database.execute(
        """
        SELECT role
        FROM execution_selector_roles
        WHERE session_id = ? AND execution_turn_id = ?
        """,
        (session_id, command.execution_identity.execution_turn_id),
    ).fetchone()
    if role_row is None or role_row["role"] != acceptance["role"]:
        return
    authority_row = database.execute(
        """
        SELECT snapshot_json, pending_projection_json
        FROM selector_authorities
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if authority_row is None:
        return
    if authority_row["pending_projection_json"] is not None:
        raise SnapshotChanged("selector projection changed before handoff commit")
    snapshot = SelectorAuthoritySnapshot.from_dict(
        _load_object(authority_row["snapshot_json"], "selector authority snapshot")
    )
    target = SelectorIdentity.from_dict(acceptance["target"])
    handoff = snapshot.handoff
    if (
        snapshot.generation != acceptance["generation"]
        or snapshot.identity != target
        or handoff is None
        or handoff.target != target
    ):
        return
    role = acceptance["role"]
    if role == "supervisor":
        source_sid = handoff.supervisor_source_sid
        target_sid = snapshot.supervisor_native_sid
        target_compatibility = snapshot.supervisor_native_sid_compatibility
    else:
        source_sid = handoff.primary_source_sid
        target_sid = snapshot.primary_native_sid
        target_compatibility = snapshot.primary_native_sid_compatibility
    if (
        source_sid != acceptance["source_sid"]
        or target_sid is None
        or target_compatibility is None
        or target_compatibility != snapshot.native_sid_compatibility
    ):
        return
    primary_source = handoff.primary_source_sid if role == "supervisor" else None
    supervisor_source = handoff.supervisor_source_sid if role == "primary" else None
    remaining = (
        type(handoff)(
            primary_source_sid=primary_source,
            supervisor_source_sid=supervisor_source,
            target=handoff.target,
            primary_source_native_sid_compatibility=(
                handoff.primary_source_native_sid_compatibility
                if primary_source is not None
                else None
            ),
            supervisor_source_native_sid_compatibility=(
                handoff.supervisor_source_native_sid_compatibility
                if supervisor_source is not None
                else None
            ),
        )
        if primary_source is not None or supervisor_source is not None
        else None
    )
    updated = SelectorAuthoritySnapshot(
        generation=snapshot.generation,
        identity=snapshot.identity,
        native_sid_compatibility=snapshot.native_sid_compatibility,
        primary_native_sid=snapshot.primary_native_sid,
        supervisor_native_sid=snapshot.supervisor_native_sid,
        primary_native_sid_compatibility=snapshot.primary_native_sid_compatibility,
        supervisor_native_sid_compatibility=(
            snapshot.supervisor_native_sid_compatibility
        ),
        handoff=remaining,
    )
    projection = {
        "kind": "handoff_consumed",
        "generation": snapshot.generation,
        "role": role,
        "source_sid": source_sid,
        "target": target.to_dict(),
        "native_sid": target_sid,
        "provider_run_id": acceptance["provider_run_id"],
    }
    database.execute(
        """
        UPDATE selector_authorities
        SET snapshot_json = ?, pending_projection_json = ?
        WHERE session_id = ?
        """,
        (_dump(updated.to_dict()), _dump(projection), session_id),
    )


def _discard_selector_bind_intent(
    database: sqlite3.Connection,
    *,
    session_id: str,
    request_id: str,
    provider_run_id: str,
) -> None:
    database.execute(
        """
        DELETE FROM execution_selector_attempts
        WHERE session_id = ? AND provider_run_id = ?
        """,
        (session_id, provider_run_id),
    )
    database.execute(
        "DELETE FROM effects WHERE session_id = ? AND request_id = ?",
        (session_id, request_id),
    )
    database.execute(
        "DELETE FROM transitions WHERE session_id = ? AND request_id = ?",
        (session_id, request_id),
    )


def commit_transition(session_id: str, request_id: str) -> LifecycleSnapshot:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT command_json, source_revision, next_snapshot_json, status
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
        command = LifecycleCommand.from_dict(
            _load_object(row["command_json"], "command")
        )
        if row["status"] in {"committed", "notification_attempted"}:
            database.commit()
            return next_snapshot
        if row["status"] != "effects_applied":
            database.rollback()
            raise RuntimeError("cannot commit lifecycle transition before effects")
        if command.kind == "bind_execution_run":
            role_row = database.execute(
                """
                SELECT role FROM execution_selector_roles
                WHERE session_id = ? AND execution_turn_id = ?
                """,
                (
                    session_id,
                    command.execution_identity.execution_turn_id,
                ),
            ).fetchone()
            attempt_row = database.execute(
                """
                SELECT execution_turn_id, selector_generation, role,
                       selector_identity_json,
                       native_sid_compatibility_json
                FROM execution_selector_attempts
                WHERE session_id = ? AND provider_run_id = ?
                """,
                (session_id, command.provider_run_id),
            ).fetchone()
            if attempt_row is not None:
                authority_row = database.execute(
                    """
                    SELECT snapshot_json FROM selector_authorities
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                authority = (
                    SelectorAuthoritySnapshot.from_dict(
                        _load_object(
                            authority_row["snapshot_json"],
                            "selector authority snapshot",
                        )
                    )
                    if authority_row is not None
                    else None
                )
                if (
                    authority is None
                    or role_row is None
                    or attempt_row["execution_turn_id"]
                    != command.execution_identity.execution_turn_id
                    or attempt_row["role"] != role_row["role"]
                    or attempt_row["selector_generation"] != authority.generation
                    or authority.identity is None
                    or attempt_row["selector_identity_json"]
                    != _dump(authority.identity.to_dict())
                    or attempt_row["native_sid_compatibility_json"]
                    != (
                        _dump(authority.native_sid_compatibility)
                        if authority.native_sid_compatibility is not None
                        else None
                    )
                ):
                    _discard_selector_bind_intent(
                        database,
                        session_id=session_id,
                        request_id=request_id,
                        provider_run_id=command.provider_run_id,
                    )
                    database.commit()
                    raise TransitionConflict(
                        "execution selector attempt lost its admitted authority"
                    )
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
        _consume_selector_handoff_at_commit(
            database,
            session_id=session_id,
            request_id=request_id,
            command=command,
        )
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
