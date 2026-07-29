from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = Path(_test_home.isolate("ba-test-lifecycle-command-engine-"))

from event_bus import BusEvent, EventBus  # noqa: E402
from lifecycle_command_engine import (  # noqa: E402
    IdentityRetired,
    LifecycleCommandEngine,
)
from lifecycle_command_model import LifecycleEffect, TurnIdentity  # noqa: E402
from lifecycle_command_states import LifecycleCommandRejected  # noqa: E402
import lifecycle_command_store  # noqa: E402


def identity(suffix: str) -> TurnIdentity:
    return TurnIdentity(
        user_turn_id=f"user-{suffix}",
        lifecycle_message_id=f"message-{suffix}",
        execution_turn_id=f"execution-{suffix}",
        assistant_message_id=f"assistant-{suffix}",
    )


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


class LosingNotificationBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def publish(self, event: BusEvent, *, is_replay: bool = False) -> None:
        self.attempts += 1
        raise RuntimeError("notification transport unavailable")


async def wait_until_registered(
    engine: LifecycleCommandEngine,
    session_id: str,
) -> None:
    for _ in range(20):
        if engine.waiter_count(session_id) == 1:
            return
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    raise AssertionError("waiter did not register")


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

    recovery_handler = IdempotentRecorder()
    recovery_handler.block = True
    recovering = LifecycleCommandEngine(
        EventBus(),
        effect_handler=recovery_handler,
    )
    first_bind = asyncio.create_task(recovering.bind())
    await recovery_handler.entered.wait()
    second_bind = asyncio.create_task(recovering.bind())
    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    assert not first_bind.done()
    assert not second_bind.done()
    assert recovering._bind_task is not None
    recovery_task = recovering._bind_task
    recovery_handler.release.set()
    await asyncio.gather(first_bind, second_bind)
    assert recovering._bind_task is recovery_task
    assert len(recovery_handler.attempts) == 1
    assert recovering.snapshot("bind-session").phase == "starting"
    await recovering.close()


async def test_two_engines_admit_only_one_turn_atomically() -> None:
    handler = IdempotentRecorder()
    handler.block = True
    first_engine = LifecycleCommandEngine(EventBus(), effect_handler=handler)
    second_engine = LifecycleCommandEngine(EventBus(), effect_handler=handler)
    await asyncio.gather(first_engine.bind(), second_engine.bind())
    first = asyncio.create_task(first_engine.begin_turn(
        request_id="atomic-first",
        session_id="atomic-session",
        identity=identity("atomic-first"),
    ))
    await handler.entered.wait()
    try:
        await second_engine.begin_turn(
            request_id="atomic-second",
            session_id="atomic-session",
            identity=identity("atomic-second"),
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("two engines admitted competing turns")
    handler.release.set()
    assert (await first).snapshot.identity == identity("atomic-first")
    assert lifecycle_command_store.session_snapshot(
        "atomic-session"
    ).identity == identity("atomic-first")

    handler.block = False
    existing_effect_ids = set(handler.consequences)
    shared_request_results = await asyncio.gather(
        first_engine.begin_turn(
            request_id="shared-request",
            session_id="shared-session-a",
            identity=identity("shared-a"),
        ),
        second_engine.begin_turn(
            request_id="shared-request",
            session_id="shared-session-b",
            identity=identity("shared-b"),
        ),
    )
    assert all(result.snapshot.phase == "starting" for result in shared_request_results)
    shared_effect_ids = set(handler.consequences) - existing_effect_ids
    assert len(shared_effect_ids) == 2
    await asyncio.gather(first_engine.close(), second_engine.close())


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
    ))
    await wait_until_registered(engine, "wait-session")
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
    await wait_until_registered(engine, "wait-session")
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    assert engine.waiter_count("wait-session") == 0
    await engine.close()


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

    counts = lifecycle_command_store.table_counts()
    assert counts["transitions"] >= 10
    database = TEST_HOME / "lifecycle-command-state.sqlite3"
    with sqlite3.connect(database) as connection:
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
        assert type(version) is int and version == 1


async def main() -> None:
    await test_state_contract_idempotency_and_reload()
    await test_concurrent_bind_waits_for_one_recovery()
    await test_two_engines_admit_only_one_turn_atomically()
    await test_at_least_once_effect_and_best_effort_notification()
    await test_identity_retirement_and_waiter_cleanup()
    test_handler_validation_and_sqlite_scaling_contract()
    print("lifecycle command engine integration: ok")


if __name__ == "__main__":
    asyncio.run(main())
