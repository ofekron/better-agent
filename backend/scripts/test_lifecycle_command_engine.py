from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = Path(_test_home.isolate("ba-test-lifecycle-command-engine-"))

from event_bus import BusEvent, EventBus  # noqa: E402
from lifecycle_command_engine import (  # noqa: E402
    IdentityRetired,
    LifecycleAuthorityBound,
    LifecycleCommandEngine,
)
from lifecycle_command_model import (  # noqa: E402
    LifecycleCommand,
    LifecycleEffect,
    UserTurnIdentity,
)
from lifecycle_command_states import (  # noqa: E402
    STATES,
    LifecycleCommandRejected,
    effect_id_for,
)
import lifecycle_command_store  # noqa: E402


def identity(suffix: str) -> UserTurnIdentity:
    return UserTurnIdentity(
        user_turn_id=f"user-{suffix}",
        lifecycle_message_id=f"message-{suffix}",
    )


def _create_v1_schema(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            phase TEXT NOT NULL,
            identity_json TEXT,
            revision INTEGER NOT NULL CHECK (revision >= 0)
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
            status TEXT NOT NULL,
            PRIMARY KEY (session_id, request_id)
        ) STRICT;
        CREATE TABLE effects (
            session_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            effect_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            result_json TEXT,
            PRIMARY KEY (session_id, request_id, ordinal),
            UNIQUE (effect_id)
        ) STRICT;
        CREATE UNIQUE INDEX one_unfinished_transition_per_session
            ON transitions(session_id)
            WHERE status IN ('planned', 'effects_applied', 'committed');
        CREATE INDEX transitions_by_status
            ON transitions(status, session_id, request_id);
        PRAGMA user_version = 1;
        """
    )


def _insert_v1_projection(
    database: sqlite3.Connection,
    *,
    session_id: str,
    valid: bool = True,
) -> dict[str, Any]:
    old_identity = {
        "user_turn_id": f"user-{session_id}",
        "lifecycle_message_id": f"message-{session_id}",
        "execution_turn_id": f"execution-{session_id}",
        "assistant_message_id": f"assistant-{session_id}",
    }
    if not valid:
        old_identity.pop("lifecycle_message_id")
    command = {
        "request_id": f"request-{session_id}",
        "session_id": session_id,
        "kind": "begin_turn",
        "identity": old_identity,
        "outcome": None,
    }
    snapshot = {
        "phase": "starting",
        "identity": old_identity,
        "revision": 1,
    }
    notification = {
        "request_id": command["request_id"],
        "command": "begin_turn",
        "source_phase": "idle",
        "next_phase": "starting",
        "identity": old_identity,
    }
    effect = dict(notification)
    def encode(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
    database.execute(
        "INSERT INTO sessions VALUES (?, 'starting', ?, 1)",
        (session_id, encode(old_identity)),
    )
    database.execute(
        """
        INSERT INTO transitions VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (
            session_id,
            command["request_id"],
            encode(command),
            encode(command),
            encode(snapshot),
            "lifecycle_command_completed",
            encode(notification),
            "notification_attempted",
        ),
    )
    database.execute(
        "INSERT INTO effects VALUES (?, ?, 0, ?, ?, ?, ?)",
        (
            session_id,
            command["request_id"],
            f"effect-{session_id}",
            "observe_turn_begin",
            encode(effect),
            encode({"observed": "observe_turn_begin"}),
        ),
    )
    return {
        "identity": old_identity,
        "command": command,
        "snapshot": snapshot,
        "notification": notification,
        "effect": effect,
    }


class IdempotentRecorder:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.consequences: dict[str, dict[str, Any]] = {}
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail_after_consequence = False

    async def execute_idempotently(
        self,
        effect: LifecycleEffect,
    ) -> Mapping[str, Any]:
        self.attempts.append(effect.effect_id)
        result = self.consequences.setdefault(
            effect.effect_id,
            {"observed": effect.kind},
        )
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.fail_after_consequence:
            self.fail_after_consequence = False
            raise RuntimeError("injected crash after durable consequence")
        return result


class UnsafeCallable:
    async def __call__(self, effect: LifecycleEffect) -> Mapping[str, Any]:
        return {"unsafe": effect.kind}


class FailingEffectHandler:
    async def execute_idempotently(
        self,
        effect: LifecycleEffect,
    ) -> Mapping[str, Any]:
        raise RuntimeError(f"cannot recover {effect.effect_id}")


class LosingNotificationBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def publish(self, event: BusEvent, *, is_replay: bool = False) -> None:
        self.attempts += 1
        raise RuntimeError("notification transport unavailable")


async def test_state_contract_idempotency_and_reload() -> None:
    recorder = IdempotentRecorder()
    bus = EventBus()
    notifications: list[str] = []

    async def capture(event: BusEvent) -> None:
        notifications.append(event.payload["request_id"])

    bus.subscribe(
        "lifecycle_command_completed",
        capture,
        name="lifecycle-command-test",
    )
    engine = LifecycleCommandEngine(bus, effect_handler=recorder)
    turn = identity("legal")
    first, duplicate = await asyncio.gather(
        engine.begin_turn(
            request_id="legal-begin",
            session_id="legal-session",
            identity=turn,
        ),
        engine.begin_turn(
            request_id="legal-begin",
            session_id="legal-session",
            identity=turn,
        ),
    )
    assert first == duplicate
    assert first.snapshot.phase == "starting"
    assert len(recorder.attempts) == 1
    assert len(recorder.consequences) == 1
    assert notifications == ["legal-begin"]

    try:
        await engine.begin_turn(
            request_id="legal-begin",
            session_id="legal-session",
            identity=identity("different"),
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("same request id accepted different command")

    try:
        await engine.confirm_started(
            request_id="legal-stale",
            session_id="legal-session",
            identity=identity("stale"),
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("stale identity was accepted")

    await engine.confirm_started(
        request_id="legal-started",
        session_id="legal-session",
        identity=turn,
    )
    await engine.request_stop(
        request_id="legal-stop",
        session_id="legal-session",
        identity=turn,
    )
    await engine.finish_turn(
        request_id="legal-finish",
        session_id="legal-session",
        identity=turn,
        outcome="stopped",
    )
    assert engine.snapshot("legal-session").phase == "idle"
    await engine.close()

    restored = LifecycleCommandEngine(EventBus(), effect_handler=recorder)
    await restored.bind()
    historical = await restored.begin_turn(
        request_id="legal-begin",
        session_id="legal-session",
        identity=turn,
    )
    assert historical.snapshot.phase == "starting"
    assert restored.snapshot("legal-session").phase == "idle"
    assert len(recorder.attempts) == 4
    await restored.close()


async def test_concurrent_bind_waits_for_one_recovery() -> None:
    crashed_handler = IdempotentRecorder()
    crashed_handler.fail_after_consequence = True
    crashed = LifecycleCommandEngine(
        EventBus(),
        effect_handler=crashed_handler,
    )
    turn = identity("bind")
    try:
        await crashed.begin_turn(
            request_id="bind-pending",
            session_id="bind-session",
            identity=turn,
        )
    except RuntimeError as exc:
        assert "durable consequence" in str(exc)
    else:
        raise AssertionError("effect crash was not surfaced")
    await crashed.close()

    failed_recovery = LifecycleCommandEngine(
        EventBus(),
        effect_handler=FailingEffectHandler(),
    )
    try:
        await failed_recovery.bind()
    except RuntimeError as exc:
        assert "cannot recover" in str(exc)
    else:
        raise AssertionError("recovery failure was not surfaced")

    recovery_handler = IdempotentRecorder()
    recovery_handler.block = True
    recovering = LifecycleCommandEngine(
        EventBus(),
        effect_handler=recovery_handler,
    )
    first_bind = asyncio.create_task(recovering.bind())
    await recovery_handler.entered.wait()
    second_entered = asyncio.Event()

    async def bind_second_caller() -> None:
        second_entered.set()
        await recovering.bind()

    second_bind = asyncio.create_task(bind_second_caller())
    await second_entered.wait()
    assert recovering._bind_task is not None
    recovery_task = recovering._bind_task
    recovery_handler.release.set()
    await asyncio.gather(first_bind, second_bind)
    assert recovering._bind_task is recovery_task
    assert len(recovery_handler.attempts) == 1
    assert recovering.snapshot("bind-session").phase == "starting"
    await recovering.close()


async def test_single_engine_authority() -> None:
    first_engine = LifecycleCommandEngine(EventBus())
    second_engine = LifecycleCommandEngine(EventBus())
    await first_engine.bind()
    try:
        await second_engine.bind()
    except LifecycleAuthorityBound:
        pass
    else:
        raise AssertionError("second live lifecycle authority was accepted")
    await first_engine.close()
    await second_engine.bind()
    await second_engine.close()


async def test_authority_rejects_another_thread_and_event_loop() -> None:
    bound = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def hold_authority() -> None:
        async def run() -> None:
            engine = LifecycleCommandEngine(EventBus())
            await engine.bind()
            bound.set()
            await asyncio.to_thread(release.wait)
            await engine.close()

        try:
            asyncio.run(run())
        except BaseException as exc:
            failures.append(exc)
            bound.set()
        finally:
            finished.set()

    owner_thread = threading.Thread(
        target=hold_authority,
        name="lifecycle-authority-owner",
    )
    owner_thread.start()
    await asyncio.to_thread(bound.wait)
    if failures:
        release.set()
        await asyncio.to_thread(finished.wait)
        owner_thread.join()
        raise AssertionError("authority owner thread failed") from failures[0]
    competing = LifecycleCommandEngine(EventBus())
    try:
        await competing.bind()
    except LifecycleAuthorityBound:
        pass
    else:
        raise AssertionError("cross-loop lifecycle authority was accepted")
    finally:
        release.set()
        await asyncio.to_thread(finished.wait)
        owner_thread.join()
    assert not failures


async def test_cross_process_lease_crash_takeover() -> None:
    child_code = """
import asyncio
import sys
from event_bus import EventBus
from lifecycle_command_engine import LifecycleCommandEngine

async def main():
    engine = LifecycleCommandEngine(EventBus())
    await engine.bind()
    print("READY", flush=True)
    await asyncio.to_thread(sys.stdin.read)

asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        ready = await child.stdout.readline()
        if ready != b"READY\n":
            assert child.stderr is not None
            error = (await child.stderr.read()).decode("utf-8", errors="replace")
            raise AssertionError(f"lease child failed readiness: {error}")
        competing = LifecycleCommandEngine(EventBus())
        try:
            await competing.bind()
        except lifecycle_command_store.AuthorityLeaseHeld:
            pass
        else:
            raise AssertionError("live child lifecycle lease was stolen")
        child.kill()
        await child.wait()
        successor = LifecycleCommandEngine(EventBus())
        await successor.bind()
        owner = lifecycle_command_store.authority_owner()
        assert owner is not None and owner["pid"] == os.getpid()
        await successor.close()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_at_least_once_effect_and_best_effort_notification() -> None:
    recorder = IdempotentRecorder()
    recorder.fail_after_consequence = True
    crashed = LifecycleCommandEngine(EventBus(), effect_handler=recorder)
    try:
        await crashed.begin_turn(
            request_id="effect-retry",
            session_id="effect-session",
            identity=identity("effect"),
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("effect crash was not surfaced")
    await crashed.close()

    resumed = LifecycleCommandEngine(EventBus(), effect_handler=recorder)
    await resumed.bind()
    assert len(recorder.attempts) == 2
    assert len(recorder.consequences) == 1
    assert resumed.snapshot("effect-session").phase == "starting"
    await resumed.close()

    losing_bus = LosingNotificationBus()
    notification_engine = LifecycleCommandEngine(losing_bus)
    result = await notification_engine.begin_turn(
        request_id="lost-notification",
        session_id="notification-session",
        identity=identity("notification"),
    )
    assert result.snapshot.phase == "starting"
    transition = lifecycle_command_store.required_transition(
        "notification-session",
        "lost-notification",
    )
    assert transition["status"] == "notification_attempted"
    assert losing_bus.attempts == 1
    await notification_engine.close()

    replay_bus = LosingNotificationBus()
    restored = LifecycleCommandEngine(replay_bus)
    await restored.bind()
    assert replay_bus.attempts == 0
    assert restored.snapshot("notification-session").phase == "starting"
    await restored.close()


async def test_identity_retirement_and_waiter_cleanup() -> None:
    engine = LifecycleCommandEngine(EventBus())
    turn = identity("wait")
    await engine.begin_turn(
        request_id="wait-begin",
        session_id="wait-session",
        identity=turn,
    )
    waiter = asyncio.create_task(engine.wait_for_phase(
        "wait-session",
        {"running"},
        identity=turn,
        min_revision=999,
    ))
    await engine.finish_turn(
        request_id="wait-finish",
        session_id="wait-session",
        identity=turn,
        outcome="complete",
    )
    try:
        await waiter
    except IdentityRetired:
        pass
    else:
        raise AssertionError("retired identity left waiter pending")
    assert engine.waiter_count("wait-session") == 0

    cancelled = asyncio.create_task(engine.wait_for_phase(
        "wait-session",
        {"running"},
    ))
    await engine.wait_until_waiter_registered("wait-session")
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    assert engine.waiter_count("wait-session") == 0
    await engine.close()


async def test_store_initialization_and_atomic_compare_insert() -> None:
    session_id = "store-cas-session"
    snapshot = lifecycle_command_store.session_snapshot(session_id)
    commands = (
        LifecycleCommand(
            request_id="store-cas-a",
            session_id=session_id,
            kind="begin_turn",
            identity=identity("store-cas-a"),
        ),
        LifecycleCommand(
            request_id="store-cas-b",
            session_id=session_id,
            kind="begin_turn",
            identity=identity("store-cas-b"),
        ),
    )
    outcomes = await asyncio.gather(
        *(
            asyncio.to_thread(
                lifecycle_command_store.persist_plan,
                command,
                snapshot,
                STATES["idle"].decide(snapshot, command),
            )
            for command in commands
        ),
        return_exceptions=True,
    )
    assert sum(outcome == "inserted" for outcome in outcomes) == 1
    assert sum(
        isinstance(outcome, lifecycle_command_store.TransitionConflict)
        for outcome in outcomes
    ) == 1

    alternate_home = Path(tempfile.mkdtemp(prefix="ba-lifecycle-init-"))
    original_home = os.environ["BETTER_AGENT_HOME"]
    original_migrate = lifecycle_command_store._migrate
    migrate_calls = 0

    def counted_migrate(connection: sqlite3.Connection) -> None:
        nonlocal migrate_calls
        migrate_calls += 1
        original_migrate(connection)

    try:
        os.environ["BETTER_AGENT_HOME"] = str(alternate_home)
        lifecycle_command_store._migrate = counted_migrate
        await asyncio.gather(
            *(asyncio.to_thread(lifecycle_command_store.initialize) for _ in range(4))
        )
        assert migrate_calls == 1
        lifecycle_command_store._migrate = lambda connection: (_ for _ in ()).throw(
            AssertionError("ordinary read attempted schema migration")
        )
        assert lifecycle_command_store.session_snapshot("unused").phase == "idle"
        assert lifecycle_command_store.open_connection_count() == 0
        database = alternate_home / "lifecycle-command-state.sqlite3"
        moved = alternate_home / "moved.sqlite3"
        os.replace(database, moved)
        os.replace(moved, database)
    finally:
        lifecycle_command_store._migrate = original_migrate
        os.environ["BETTER_AGENT_HOME"] = original_home
        shutil.rmtree(alternate_home)


def test_v1_migration_and_atomic_rollback() -> None:
    migrated = sqlite3.connect(":memory:", isolation_level=None)
    migrated.row_factory = sqlite3.Row
    _create_v1_schema(migrated)
    _insert_v1_projection(migrated, session_id="migration-valid")
    lifecycle_command_store._migrate(migrated)

    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrated.execute(
        "SELECT COUNT(*) FROM authority_owner"
    ).fetchone()[0] == 0
    session_identity = json.loads(migrated.execute(
        "SELECT identity_json FROM sessions WHERE session_id = ?",
        ("migration-valid",),
    ).fetchone()[0])
    transition = migrated.execute(
        """
        SELECT fingerprint, command_json, next_snapshot_json,
               notification_payload_json
        FROM transitions WHERE session_id = ?
        """,
        ("migration-valid",),
    ).fetchone()
    command = json.loads(transition["command_json"])
    snapshot = json.loads(transition["next_snapshot_json"])
    notification = json.loads(transition["notification_payload_json"])
    effect = json.loads(migrated.execute(
        "SELECT payload_json FROM effects WHERE session_id = ?",
        ("migration-valid",),
    ).fetchone()[0])
    logical_keys = {"user_turn_id", "lifecycle_message_id"}
    assert set(session_identity) == logical_keys
    assert set(command["identity"]) == logical_keys
    assert set(snapshot["identity"]) == logical_keys
    assert set(notification["identity"]) == logical_keys
    assert set(effect["identity"]) == logical_keys
    assert transition["fingerprint"] == LifecycleCommand.from_dict(
        command,
    ).fingerprint()
    migrated.close()

    rolled_back = sqlite3.connect(":memory:", isolation_level=None)
    rolled_back.row_factory = sqlite3.Row
    _create_v1_schema(rolled_back)
    original = _insert_v1_projection(
        rolled_back,
        session_id="rollback-valid",
    )
    _insert_v1_projection(
        rolled_back,
        session_id="rollback-invalid",
        valid=False,
    )
    try:
        lifecycle_command_store._migrate(rolled_back)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid v1 identity did not fail closed")
    assert rolled_back.execute("PRAGMA user_version").fetchone()[0] == 1
    assert rolled_back.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type = 'table' AND name = 'authority_owner'
        """
    ).fetchone()[0] == 0
    persisted = json.loads(rolled_back.execute(
        "SELECT identity_json FROM sessions WHERE session_id = ?",
        ("rollback-valid",),
    ).fetchone()[0])
    assert persisted == original["identity"]
    rolled_back.close()


def test_handler_validation_and_sqlite_scaling_contract() -> None:
    try:
        LifecycleCommandEngine(
            EventBus(),
            effect_handler=UnsafeCallable(),  # type: ignore[arg-type]
        )
    except TypeError as exc:
        assert "execute_idempotently" in str(exc)
    else:
        raise AssertionError("unsafe plain callable effect handler was accepted")

    effect = LifecycleEffect(
        "immutable:0",
        "observe_turn_begin",
        {"nested": {"value": 1}},
    )
    try:
        effect.payload["new"] = "value"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("effect payload remained mutable")

    for non_finite in (float("nan"), float("inf"), float("-inf")):
        try:
            LifecycleEffect(
                "non-finite:0",
                "observe_turn_begin",
                {"value": non_finite},
            )
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite JSON number was accepted")

    command = LifecycleCommand(
        request_id="effect-identity",
        session_id="effect-identity-session",
        kind="begin_turn",
        identity=identity("effect-identity"),
    )
    assert effect_id_for(command, 0) != effect_id_for(command, 1)
    other_session = LifecycleCommand(
        request_id=command.request_id,
        session_id="effect-identity-other-session",
        kind=command.kind,
        identity=identity("effect-identity-other"),
    )
    assert effect_id_for(command, 0) != effect_id_for(other_session, 0)

    counts = lifecycle_command_store.table_counts()
    assert counts["transitions"] >= 10
    database = TEST_HOME / "lifecycle-command-state.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT *
            FROM transitions
            WHERE session_id = ? AND request_id = ?
            """,
            ("legal-session", "legal-begin"),
        ).fetchall()
        detail = " ".join(str(row[3]) for row in plan)
        assert "INDEX" in detail and "session_id" in detail
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        assert type(version) is int and version == 2


async def main() -> None:
    await test_state_contract_idempotency_and_reload()
    await test_concurrent_bind_waits_for_one_recovery()
    await test_single_engine_authority()
    await test_authority_rejects_another_thread_and_event_loop()
    await test_cross_process_lease_crash_takeover()
    await test_at_least_once_effect_and_best_effort_notification()
    await test_identity_retirement_and_waiter_cleanup()
    await test_store_initialization_and_atomic_compare_insert()
    test_v1_migration_and_atomic_rollback()
    test_handler_validation_and_sqlite_scaling_contract()
    print("lifecycle command engine integration: ok")


if __name__ == "__main__":
    asyncio.run(main())
