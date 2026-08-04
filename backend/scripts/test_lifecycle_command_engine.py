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

import pytest

pytestmark = pytest.mark.anyio


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
    ContinuationHandoff,
    ExecutionTurnIdentity,
    ExecutionTurnSnapshot,
    LifecycleCommand,
    LifecycleEffect,
    LifecycleSnapshot,
    SelectorAuthoritySnapshot,
    SelectorIdentity,
    UserTurnIdentity,
)
from lifecycle_command_states import (  # noqa: E402
    STATES,
    LifecycleCommandRejected,
    effect_id_for,
)
import lifecycle_command_store  # noqa: E402
import session_manager  # noqa: E402


def identity(suffix: str) -> UserTurnIdentity:
    return UserTurnIdentity(
        user_turn_id=f"user-{suffix}",
        lifecycle_message_id=f"message-{suffix}",
    )


def test_corrupt_serialized_snapshot_fails_closed() -> None:
    corrupt = {
        "phase": "running",
        "identity": identity("corrupt").to_dict(),
        "revision": 1,
        "execution": {
            "phase": "running",
            "identity": ExecutionTurnIdentity(
                "execution-corrupt",
                "assistant-corrupt",
                "native",
            ).to_dict(),
            "provider_run_id": None,
        },
        "execution_policy": "single",
        "completed_execution_count": 0,
    }
    try:
        LifecycleSnapshot.from_dict(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt serialized execution was accepted")


def test_selector_authority_transition_contract() -> None:
    source = SelectorIdentity("provider-a", "model-a", "runner-a")
    target_b = SelectorIdentity("provider-b", "model-b", "runner-b")
    target_c = SelectorIdentity("provider-c", "model-c", "runner-c")
    compatibility = {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": "/tmp/claude/projects",
        "claude_project_namespace": "-repo",
    }
    initial = SelectorAuthoritySnapshot(
        generation=0,
        identity=source,
        native_sid_compatibility=compatibility,
        primary_native_sid="native-a",
        supervisor_native_sid="supervisor-a",
        primary_native_sid_compatibility=compatibility,
        supervisor_native_sid_compatibility=compatibility,
    )
    first = initial.transition(target_b)
    assert first.generation == 1
    assert first.identity == target_b
    assert first.primary_native_sid is None
    assert first.supervisor_native_sid is None
    assert first.native_sid_compatibility is None
    assert first.handoff == ContinuationHandoff(
        primary_source_sid="native-a",
        supervisor_source_sid="supervisor-a",
        target=target_b,
        primary_source_native_sid_compatibility=compatibility,
        supervisor_source_native_sid_compatibility=compatibility,
    )

    flapped = first.transition(target_c)
    assert flapped.generation == 2
    assert flapped.handoff is not None
    assert flapped.handoff.primary_source_sid == "native-a"
    assert flapped.handoff.supervisor_source_sid == "supervisor-a"
    assert flapped.handoff.target == target_c

    repeated = flapped.transition(target_c)
    assert repeated == flapped
    assert SelectorAuthoritySnapshot.from_dict(repeated.to_dict()) == repeated

    legacy = SelectorAuthoritySnapshot()
    admitted, decision = legacy.admit_attempt(
        source,
        compatibility,
        primary_native_sid="native-a",
        supervisor_native_sid=None,
        primary_native_sid_compatibility=compatibility,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    assert admitted.identity == source
    assert admitted.primary_native_sid == "native-a"

    unresolved, decision = SelectorAuthoritySnapshot().admit_attempt(
        source,
        compatibility,
        primary_native_sid="native-a",
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "restart"
    assert unresolved.primary_native_sid is None
    assert unresolved.handoff is not None
    assert unresolved.handoff.primary_source_sid == "native-a"
    assert unresolved.native_sid_compatibility == compatibility

    admitted_after_handoff, decision = flapped.admit_attempt(
        target_c,
        compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    assert admitted_after_handoff.handoff == flapped.handoff
    assert admitted_after_handoff.native_sid_compatibility == compatibility

    stale_without_handoff, decision = initial.admit_attempt(
        target_b,
        compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "stale"
    assert stale_without_handoff is initial
    stale_with_handoff, decision = flapped.admit_attempt(
        target_b,
        compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "stale"
    assert stale_with_handoff is flapped

    for primary_proof, supervisor_proof in (
        (compatibility, None),
        (None, compatibility),
    ):
        asymmetric, decision = SelectorAuthoritySnapshot().admit_attempt(
            source,
            compatibility,
            primary_native_sid="native-a",
            supervisor_native_sid="supervisor-a",
            primary_native_sid_compatibility=primary_proof,
            supervisor_native_sid_compatibility=supervisor_proof,
        )
        assert decision == "restart"
        assert asymmetric.primary_native_sid is None
        assert asymmetric.supervisor_native_sid is None

    assert SelectorIdentity("provider", "m" * 512, "runner").model == "m" * 512
    with pytest.raises(ValueError):
        SelectorIdentity("provider", "m" * 513, "runner")


async def test_selector_native_sid_attachment_fences(monkeypatch) -> None:
    session_id = "selector-attachment-fences"
    projected: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        session_manager.manager,
        "get",
        lambda requested_id: (
            {"orchestration_mode": "native"}
            if requested_id == session_id
            else None
        ),
    )
    monkeypatch.setattr(
        session_manager.manager,
        "set_agent_sid",
        lambda requested_id, mode, native_sid, **_metadata: (
            projected.append((requested_id, mode, native_sid))
            or {"id": requested_id}
        ),
    )
    engine = LifecycleCommandEngine(EventBus())
    await engine.begin_turn(
        request_id=f"{session_id}:begin",
        session_id=session_id,
        identity=identity(session_id),
        execution_policy="single",
    )
    execution = ExecutionTurnIdentity(
        "selector-attachment-execution",
        "selector-attachment-assistant",
        "native",
    )
    await engine.start_execution(session_id, execution_identity=execution)
    await engine.bind_execution_run(
        session_id,
        execution_identity=execution,
        provider_run_id="selector-current-attempt",
    )
    compatibility = {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": "/tmp/claude/projects",
        "claude_project_namespace": "-repo",
    }
    authority, decision = lifecycle_command_store.persist_admitted_selector_attempt(
        session_id,
        target=SelectorIdentity("provider-a", "model-a", "runner-a"),
        native_sid_compatibility=compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    assert not await engine.attach_selector_native_sid(
        session_id,
        selector_generation=authority.generation,
        provider_run_id="selector-stale-attempt",
        role="primary",
        native_sid="native-stale-attempt",
    )
    assert not await engine.attach_selector_native_sid(
        session_id,
        selector_generation=authority.generation + 1,
        provider_run_id="selector-current-attempt",
        role="primary",
        native_sid="native-stale-generation",
    )
    assert await engine.attach_selector_native_sid(
        session_id,
        selector_generation=authority.generation,
        provider_run_id="selector-current-attempt",
        role="primary",
        native_sid="native-current",
    )
    attached = lifecycle_command_store.selector_authority_snapshot(session_id)
    assert attached is not None
    assert attached.primary_native_sid == "native-current"
    assert projected == [(session_id, "native", "native-current")]
    await engine.close()


async def test_native_sid_projection_replays_after_crash(monkeypatch) -> None:
    projected: list[tuple[str, str, str | None]] = []
    session_ids = {
        "primary": "selector-crash-primary",
        "supervisor": "selector-crash-supervisor",
    }
    monkeypatch.setattr(
        session_manager.manager,
        "get",
        lambda requested_id: (
            {"orchestration_mode": "native"}
            if requested_id in session_ids.values()
            else None
        ),
    )
    monkeypatch.setattr(
        session_manager.manager,
        "set_agent_sid",
        lambda requested_id, mode, native_sid, **_metadata: (
            projected.append((requested_id, mode, native_sid))
            or {"id": requested_id}
        ),
    )
    engine = LifecycleCommandEngine(EventBus())
    compatibility = {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": "/tmp/claude/projects",
        "claude_project_namespace": "-repo",
    }
    for role, session_id in session_ids.items():
        execution = ExecutionTurnIdentity(
            f"selector-crash-execution-{role}",
            f"selector-crash-assistant-{role}",
            "native",
        )
        await engine.begin_turn(
            request_id=f"{session_id}:begin",
            session_id=session_id,
            identity=identity(session_id),
            execution_policy="single",
        )
        await engine.start_execution(session_id, execution_identity=execution)
        await engine.bind_execution_run(
            session_id,
            execution_identity=execution,
            provider_run_id=f"selector-crash-run-{role}",
        )
        authority, decision = (
            lifecycle_command_store.persist_admitted_selector_attempt(
                session_id,
                target=SelectorIdentity("provider-a", "model-a", "runner-a"),
                native_sid_compatibility=compatibility,
                primary_native_sid=None,
                supervisor_native_sid=None,
                primary_native_sid_compatibility=None,
                supervisor_native_sid_compatibility=None,
            )
        )
        assert decision == "admitted"
        persisted = lifecycle_command_store.attach_selector_native_sid(
            session_id,
            expected_generation=authority.generation,
            role=role,
            native_sid=f"native-crash-{role}",
        )
        assert persisted is not None
    assert len(lifecycle_command_store.pending_selector_projections()) == 2
    await engine.close()

    recovered = LifecycleCommandEngine(EventBus())
    await recovered.bind()
    assert projected == [
        (session_ids["primary"], "native", "native-crash-primary"),
        (session_ids["supervisor"], "supervisor", "native-crash-supervisor"),
    ]
    assert lifecycle_command_store.pending_selector_projections() == ()
    await recovered.close()


async def test_late_prepared_selector_attempt_cannot_rewind_authority() -> None:
    compatibility = {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": "/tmp/claude/projects",
        "claude_project_namespace": "-repo",
    }
    selector_a = SelectorIdentity("provider-a", "model-a", "runner-a")
    selector_b = SelectorIdentity("provider-b", "model-b", "runner-b")
    selector_c = SelectorIdentity("provider-c", "model-c", "runner-c")
    engine = LifecycleCommandEngine(EventBus())
    for suffix, with_handoff in (("plain", False), ("handoff", True)):
        session_id = f"late-selector-{suffix}"
        await engine.begin_turn(
            request_id=f"{session_id}:begin",
            session_id=session_id,
            identity=identity(session_id),
            execution_policy="single",
        )
        admitted, decision = lifecycle_command_store.persist_admitted_selector_attempt(
            session_id,
            target=selector_a,
            native_sid_compatibility=compatibility,
            primary_native_sid="native-a" if with_handoff else None,
            supervisor_native_sid=None,
            primary_native_sid_compatibility=(
                compatibility if with_handoff else None
            ),
            supervisor_native_sid_compatibility=None,
        )
        assert decision == "admitted"
        assert admitted.identity == selector_a
        lifecycle_command_store.persist_selector_transition(
            session_id,
            target=selector_b,
            projection_updates=selector_b.to_dict(),
            primary_native_sid=None,
            supervisor_native_sid=None,
            primary_legacy_native_sid_compatibility=None,
            supervisor_legacy_native_sid_compatibility=None,
        )
        expected = lifecycle_command_store.persist_selector_transition(
            session_id,
            target=selector_c,
            projection_updates=selector_c.to_dict(),
            primary_native_sid=None,
            supervisor_native_sid=None,
            primary_legacy_native_sid_compatibility=None,
            supervisor_legacy_native_sid_compatibility=None,
        )
        actual, decision = lifecycle_command_store.persist_admitted_selector_attempt(
            session_id,
            target=selector_b,
            native_sid_compatibility=compatibility,
            primary_native_sid=None,
            supervisor_native_sid=None,
            primary_native_sid_compatibility=None,
            supervisor_native_sid_compatibility=None,
        )
        assert decision == "stale"
        assert actual == expected
        assert lifecycle_command_store.selector_authority_snapshot(session_id) == expected
        assert (expected.handoff is not None) is with_handoff
        assert lifecycle_command_store.acknowledge_selector_projection(
            session_id,
            selector_c,
        )
    await engine.close()


async def test_selector_transition_merges_missing_role_evidence_before_clear() -> None:
    compatibility = {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": "/tmp/claude/projects",
        "claude_project_namespace": "-repo",
    }
    selector_a = SelectorIdentity("provider-a", "model-a", "runner-a")
    selector_b = SelectorIdentity("provider-b", "model-b", "runner-b")
    selector_c = SelectorIdentity("provider-c", "model-c", "runner-c")
    session_id = "selector-role-evidence-merge"
    engine = LifecycleCommandEngine(EventBus())
    await engine.begin_turn(
        request_id=f"{session_id}:begin",
        session_id=session_id,
        identity=identity(session_id),
        execution_policy="single",
    )
    admitted, decision = lifecycle_command_store.persist_admitted_selector_attempt(
        session_id,
        target=selector_a,
        native_sid_compatibility=compatibility,
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    assert admitted.primary_native_sid is None

    first = lifecycle_command_store.persist_selector_transition(
        session_id,
        target=selector_b,
        projection_updates=selector_b.to_dict(),
        primary_native_sid="primary-a",
        supervisor_native_sid=None,
        primary_legacy_native_sid_compatibility=compatibility,
        supervisor_legacy_native_sid_compatibility=None,
    )
    assert first.handoff is not None
    assert first.handoff.primary_source_sid == "primary-a"
    assert first.handoff.primary_source_native_sid_compatibility == compatibility

    newest = lifecycle_command_store.persist_selector_transition(
        session_id,
        target=selector_c,
        projection_updates=selector_c.to_dict(),
        primary_native_sid=None,
        supervisor_native_sid="supervisor-a",
        primary_legacy_native_sid_compatibility=None,
        supervisor_legacy_native_sid_compatibility=compatibility,
    )
    assert newest.handoff is not None
    assert newest.handoff.primary_source_sid == "primary-a"
    assert newest.handoff.supervisor_source_sid == "supervisor-a"
    assert newest.handoff.primary_source_native_sid_compatibility == compatibility
    assert newest.handoff.supervisor_source_native_sid_compatibility == compatibility
    assert newest.handoff.target == selector_c
    assert lifecycle_command_store.acknowledge_selector_projection(
        session_id,
        selector_c,
    )
    await engine.close()


def test_reachable_snapshot_matrix() -> None:
    user_identity = identity("matrix")
    execution_identity = ExecutionTurnIdentity(
        "execution-matrix",
        "assistant-matrix",
        "native",
    )
    child_phases = (
        None, "starting", "running", "stopping", "detached",
        "detached_stopping", "complete", "stopped", "failed", "aborted",
    )

    def reachable(
        parent: str,
        child: str | None,
        has_provider: bool,
        policy: str,
        completed: int,
    ) -> bool:
        if child is None:
            return completed == 0
        if child in {"starting", "aborted"}:
            provider_valid = not has_provider if child == "aborted" else True
        else:
            provider_valid = has_provider
        parent_valid = {
            "starting": child in {"starting", "complete", "stopped", "failed", "aborted"},
            "running": child in {
                "starting", "running", "detached",
                "complete", "stopped", "failed", "aborted",
            },
            "stopping": child in {
                "starting", "stopping", "detached_stopping",
                "complete", "stopped", "failed", "aborted",
            },
        }[parent]
        terminal = child in {"complete", "stopped", "failed", "aborted"}
        count_valid = (
            completed > 0
            if terminal
            else completed == 0 or policy == "sequential"
        )
        policy_valid = policy == "sequential" or completed <= 1
        return provider_valid and parent_valid and count_valid and policy_valid

    for parent in ("starting", "running", "stopping"):
        for child in child_phases:
            for has_provider in (False, True):
                for policy in ("single", "sequential"):
                    for completed in (0, 1, 2):
                        execution = (
                            ExecutionTurnSnapshot(
                                child,
                                execution_identity,
                                "provider-matrix" if has_provider else None,
                            )
                            if child is not None
                            else None
                        )
                        expected = reachable(
                            parent, child, has_provider, policy, completed,
                        )
                        try:
                            LifecycleSnapshot(
                                phase=parent,
                                identity=user_identity,
                                revision=1,
                                execution=execution,
                                execution_policy=policy,
                                completed_execution_count=completed,
                            )
                        except ValueError:
                            accepted = False
                        else:
                            accepted = True
                        assert accepted == expected, (
                            parent, child, has_provider, policy, completed,
                        )

    for child_phase in ("running", "stopping", "detached"):
        try:
            LifecycleSnapshot(
                phase="starting",
                identity=user_identity,
                execution=ExecutionTurnSnapshot(
                    child_phase,
                    execution_identity,
                    "provider-hostile",
                ),
                execution_policy="sequential",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"starting parent accepted {child_phase} child"
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


async def test_retry_requires_fresh_durable_delivery_attempt() -> None:
    engine = LifecycleCommandEngine(EventBus())
    session_id = "durable-delivery-attempt"
    turn = identity("durable-delivery-attempt")
    await engine.begin_turn(
        request_id="user-turn:message-durable-delivery-attempt:0:begin",
        session_id=session_id,
        identity=turn,
    )
    await engine.finish_turn(
        request_id="user-turn:message-durable-delivery-attempt:0:finish",
        session_id=session_id,
        identity=turn,
        outcome="failed",
    )

    await engine.begin_turn(
        request_id="user-turn:message-durable-delivery-attempt:0:begin",
        session_id=session_id,
        identity=turn,
    )
    assert engine.snapshot(session_id).identity is None

    await engine.begin_turn(
        request_id="user-turn:message-durable-delivery-attempt:1:begin",
        session_id=session_id,
        identity=turn,
    )
    execution = ExecutionTurnIdentity(
        "execution-durable-delivery-attempt",
        "assistant-durable-delivery-attempt",
        "native",
    )
    await engine.start_execution(
        session_id,
        execution_identity=execution,
    )
    assert engine.snapshot(session_id).execution is not None
    await engine.abort_execution(
        session_id,
        execution_identity=execution,
        outcome="failed",
    )
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
            execution_policy="single",
        ),
        LifecycleCommand(
            request_id="store-cas-b",
            session_id=session_id,
            kind="begin_turn",
            identity=identity("store-cas-b"),
            execution_policy="single",
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


async def test_execution_subturn_state_matrix() -> None:
    engine = LifecycleCommandEngine(EventBus())
    sequential_turn = identity("execution-sequential")
    await engine.begin_turn(
        request_id="execution-sequential:begin",
        session_id="execution-sequential",
        identity=sequential_turn,
        execution_policy="sequential",
    )
    first = ExecutionTurnIdentity("execution-1", "assistant-1", "native")
    second = ExecutionTurnIdentity("execution-2", "assistant-2", "supervisor")
    await engine.start_execution(
        "execution-sequential",
        execution_identity=first,
    )
    overlap_revision = engine.snapshot("execution-sequential").revision
    try:
        await engine.start_execution(
            "execution-sequential",
            execution_identity=second,
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("overlapping execution was accepted")
    assert engine.snapshot("execution-sequential").revision == overlap_revision

    await engine.elect_execution_attempt(
        "execution-sequential",
        execution_identity=first,
        provider_run_id="provider-1",
    )
    await engine.elect_execution_attempt(
        "execution-sequential",
        execution_identity=first,
        provider_run_id="provider-1b",
    )
    stale_attempt_revision = engine.snapshot("execution-sequential").revision
    try:
        await engine.finish_execution(
            "execution-sequential",
            execution_identity=first,
            provider_run_id="provider-1",
            outcome="failed",
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("stale provider attempt terminal was accepted")
    assert engine.snapshot("execution-sequential").revision == stale_attempt_revision
    await engine.admit_execution_attempt(
        "execution-sequential",
        execution_identity=first,
        provider_run_id="provider-1b",
    )
    await engine.detach_execution(
        "execution-sequential",
        execution_identity=first,
    )
    detached_revision = engine.snapshot("execution-sequential").revision
    try:
        await engine.adopt_execution(
            "execution-sequential",
            execution_identity=first,
            provider_run_id="stale-provider",
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("stale provider attempt adopted execution")
    assert engine.snapshot("execution-sequential").revision == detached_revision
    await engine.adopt_execution(
        "execution-sequential",
        execution_identity=first,
        provider_run_id="provider-1b",
    )
    retried = engine.snapshot("execution-sequential")
    assert retried.identity == sequential_turn
    assert retried.execution.identity == first
    await engine.finish_execution(
        "execution-sequential",
        execution_identity=first,
        provider_run_id="provider-1b",
        outcome="complete",
    )
    await engine.start_execution(
        "execution-sequential",
        execution_identity=second,
    )
    stale_revision = engine.snapshot("execution-sequential").revision
    try:
        await engine.finish_execution(
            "execution-sequential",
            execution_identity=first,
            provider_run_id="provider-1b",
            outcome="failed",
        )
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("stale terminal finished newer execution")
    assert engine.snapshot("execution-sequential").revision == stale_revision
    await engine.bind_execution_run(
        "execution-sequential",
        execution_identity=second,
        provider_run_id="provider-2",
    )
    await engine.finish_execution_and_turn(
        "execution-sequential",
        execution_identity=second,
        provider_run_id="provider-2",
        outcome="complete",
    )
    assert engine.snapshot("execution-sequential").phase == "idle"

    single_turn = identity("execution-single")
    await engine.begin_turn(
        request_id="execution-single:begin",
        session_id="execution-single",
        identity=single_turn,
        execution_policy="single",
    )
    await engine.start_execution("execution-single", execution_identity=first)
    await engine.bind_execution_run(
        "execution-single",
        execution_identity=first,
        provider_run_id="provider-single",
    )
    await engine.finish_execution_and_turn(
        "execution-single",
        execution_identity=first,
        provider_run_id="provider-single",
        outcome="complete",
    )
    assert engine.snapshot("execution-single").phase == "idle"
    try:
        await engine.start_execution("execution-single", execution_identity=second)
    except (LifecycleCommandRejected, RuntimeError):
        pass
    else:
        raise AssertionError("single policy accepted a second execution")

    stopping_turn = identity("execution-stopping")
    await engine.begin_turn(
        request_id="execution-stopping:begin",
        session_id="execution-stopping",
        identity=stopping_turn,
        execution_policy="sequential",
    )
    await engine.start_execution("execution-stopping", execution_identity=first)
    await engine.bind_execution_run(
        "execution-stopping",
        execution_identity=first,
        provider_run_id="provider-stop",
    )
    await engine.confirm_execution_started(
        "execution-stopping",
        execution_identity=first,
        provider_run_id="provider-stop",
    )
    await engine.request_active_stop("execution-stopping")
    await engine.detach_execution("execution-stopping", execution_identity=first)
    try:
        await engine.start_execution("execution-stopping", execution_identity=second)
    except LifecycleCommandRejected:
        pass
    else:
        raise AssertionError("stopping turn accepted another execution")
    await engine.adopt_execution(
        "execution-stopping",
        execution_identity=first,
        provider_run_id="provider-stop",
    )
    await engine.finish_execution(
        "execution-stopping",
        execution_identity=first,
        provider_run_id="provider-stop",
        outcome="stopped",
    )
    await engine.finish_turn(
        request_id="execution-stopping:finish",
        session_id="execution-stopping",
        identity=stopping_turn,
        outcome="stopped",
    )

    recovery_turn = identity("execution-recovery")
    recovery_execution = ExecutionTurnIdentity(
        "execution-recovery",
        "assistant-recovery",
        "native",
    )
    await engine.begin_turn(
        request_id="execution-recovery:begin",
        session_id="execution-recovery",
        identity=recovery_turn,
    )
    await engine.start_execution(
        "execution-recovery",
        execution_identity=recovery_execution,
    )
    await engine.bind_execution_run(
        "execution-recovery",
        execution_identity=recovery_execution,
        provider_run_id="provider-recovery",
    )
    expected_recovery = engine.snapshot("execution-recovery")
    resolver_calls = 0

    async def resolve_failed() -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return "failed"

    stale_recovery = LifecycleSnapshot(
        phase=expected_recovery.phase,
        identity=expected_recovery.identity,
        revision=expected_recovery.revision - 1,
        execution=expected_recovery.execution,
        execution_policy=expected_recovery.execution_policy,
        completed_execution_count=expected_recovery.completed_execution_count,
    )
    assert not await engine.recover_missing_bound_execution(
        "execution-recovery",
        expected_snapshot=stale_recovery,
        resolve_outcome=resolve_failed,
    )
    assert resolver_calls == 0
    assert await engine.recover_missing_bound_execution(
        "execution-recovery",
        expected_snapshot=expected_recovery,
        resolve_outcome=resolve_failed,
    )
    assert resolver_calls == 1
    recovered = engine.snapshot("execution-recovery")
    assert recovered.phase == "idle"
    assert recovered.execution is None

    async def seed_recovery_boundary(
        suffix: str,
        phase: str,
    ) -> tuple[str, LifecycleSnapshot]:
        session_id = f"execution-recovery-{suffix}"
        turn = identity(session_id)
        execution = ExecutionTurnIdentity(
            f"execution-{suffix}",
            f"assistant-{suffix}",
            "native",
        )
        await engine.begin_turn(
            request_id=f"{session_id}:begin",
            session_id=session_id,
            identity=turn,
            execution_policy="sequential",
        )
        await engine.start_execution(
            session_id,
            execution_identity=execution,
        )
        if phase != "unbound":
            await engine.bind_execution_run(
                session_id,
                execution_identity=execution,
                provider_run_id=f"provider-{suffix}",
            )
        if phase in {"running", "detached", "detached_stopping", "stopping"}:
            await engine.confirm_execution_started(
                session_id,
                execution_identity=execution,
                provider_run_id=f"provider-{suffix}",
            )
        if phase in {"stopping", "detached_stopping"}:
            await engine.request_active_stop(session_id)
        if phase in {"detached", "detached_stopping"}:
            await engine.detach_execution(
                session_id,
                execution_identity=execution,
            )
        if phase == "terminal":
            await engine.finish_execution(
                session_id,
                execution_identity=execution,
                provider_run_id=f"provider-{suffix}",
                outcome="complete",
            )
        return session_id, engine.snapshot(session_id)

    for boundary in (
        "unbound",
        "running",
        "stopping",
        "detached",
        "detached_stopping",
        "terminal",
    ):
        session_id, boundary_snapshot = await seed_recovery_boundary(
            boundary,
            boundary,
        )
        before_calls = resolver_calls
        assert not await engine.recover_missing_bound_execution(
            session_id,
            expected_snapshot=boundary_snapshot,
            resolve_outcome=resolve_failed,
        )
        assert resolver_calls == before_calls
        assert engine.snapshot(session_id) == boundary_snapshot

    none_session, none_snapshot = await seed_recovery_boundary(
        "none",
        "starting",
    )

    async def resolve_none() -> None:
        return None

    assert not await engine.recover_missing_bound_execution(
        none_session,
        expected_snapshot=none_snapshot,
        resolve_outcome=resolve_none,
    )
    assert engine.snapshot(none_session) == none_snapshot

    await engine.close()


def test_v1_migration_and_atomic_rollback() -> None:
    migrated = sqlite3.connect(":memory:", isolation_level=None)
    migrated.row_factory = sqlite3.Row
    _create_v1_schema(migrated)
    _insert_v1_projection(migrated, session_id="migration-valid")
    lifecycle_command_store._migrate(migrated)

    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 6
    session_columns = {
        row[1]
        for row in migrated.execute("PRAGMA table_info(sessions)").fetchall()
    }
    assert "owner_incarnation" in session_columns
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
        execution_policy="single",
    )
    assert effect_id_for(command, 0) != effect_id_for(command, 1)
    other_session = LifecycleCommand(
        request_id=command.request_id,
        session_id="effect-identity-other-session",
        kind=command.kind,
        identity=identity("effect-identity-other"),
        execution_policy="single",
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
        assert type(version) is int and version == 6


async def test_terminal_render_reconciliation_inbox() -> None:
    engine = LifecycleCommandEngine(EventBus())
    await engine.bind()
    turn = identity("terminal-render")
    execution = ExecutionTurnIdentity(
        "execution-terminal-render",
        "assistant-terminal-render",
        "native",
    )
    await engine.begin_turn(
        request_id="terminal-render-begin",
        session_id="terminal-render-session",
        identity=turn,
        execution_policy="single",
    )
    await engine.start_execution(
        "terminal-render-session",
        execution_identity=execution,
    )
    await engine.bind_execution_run(
        "terminal-render-session",
        execution_identity=execution,
        provider_run_id="provider-terminal-render",
    )
    await engine.finish_execution_and_turn(
        "terminal-render-session",
        execution_identity=execution,
        provider_run_id="provider-terminal-render",
        outcome="failed",
    )
    pending = [
        item
        for item in lifecycle_command_store.pending_terminal_renders()
        if item["session_id"] == "terminal-render-session"
    ]
    assert len(pending) == 1
    first_request_id = pending[0]["request_id"]
    assert first_request_id
    assert pending[0] == {
        "session_id": "terminal-render-session",
        "request_id": first_request_id,
        "lifecycle_message_id": turn.lifecycle_message_id,
        "execution_turn_id": execution.execution_turn_id,
        "assistant_message_id": execution.assistant_message_id,
        "outcome": "failed",
    }
    replacement_turn = identity("terminal-render-replacement")
    replacement_execution = ExecutionTurnIdentity(
        "execution-terminal-render-replacement",
        "assistant-terminal-render-replacement",
        "native",
    )
    await engine.begin_turn(
        request_id="terminal-render-replacement-begin",
        session_id="terminal-render-session",
        identity=replacement_turn,
        execution_policy="single",
    )
    await engine.start_execution(
        "terminal-render-session",
        execution_identity=replacement_execution,
    )
    await engine.bind_execution_run(
        "terminal-render-session",
        execution_identity=replacement_execution,
        provider_run_id="provider-terminal-render-replacement",
    )
    replacement_result = await engine.finish_execution_and_turn(
        "terminal-render-session",
        execution_identity=replacement_execution,
        provider_run_id="provider-terminal-render-replacement",
        outcome="complete",
    )
    assert not lifecycle_command_store.acknowledge_terminal_render(
        "terminal-render-session",
        first_request_id,
    )
    pending = [
        item
        for item in lifecycle_command_store.pending_terminal_renders()
        if item["session_id"] == "terminal-render-session"
    ]
    assert len(pending) == 1
    assert pending[0]["request_id"] == replacement_result.request_id
    assert await engine.acknowledge_terminal_render(
        "terminal-render-session",
        replacement_result.request_id,
    )
    assert all(
        item["session_id"] != "terminal-render-session"
        for item in lifecycle_command_store.pending_terminal_renders()
    )
    await engine.close()


async def main() -> None:
    test_corrupt_serialized_snapshot_fails_closed()
    test_selector_authority_transition_contract()
    test_reachable_snapshot_matrix()
    await test_state_contract_idempotency_and_reload()
    await test_concurrent_bind_waits_for_one_recovery()
    await test_single_engine_authority()
    await test_authority_rejects_another_thread_and_event_loop()
    await test_cross_process_lease_crash_takeover()
    await test_at_least_once_effect_and_best_effort_notification()
    await test_identity_retirement_and_waiter_cleanup()
    await test_retry_requires_fresh_durable_delivery_attempt()
    await test_store_initialization_and_atomic_compare_insert()
    await test_execution_subturn_state_matrix()
    await test_terminal_render_reconciliation_inbox()
    test_v1_migration_and_atomic_rollback()
    test_handler_validation_and_sqlite_scaling_contract()
    print("lifecycle command engine integration: ok")


if __name__ == "__main__":
    asyncio.run(main())
