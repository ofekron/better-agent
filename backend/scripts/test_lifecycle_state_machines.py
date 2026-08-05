from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_test_home.isolate("ba-test-lifecycle-state-machines-")

from event_bus import BusEvent, EventBus  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from json_store import read_json, write_json  # noqa: E402
import lifecycle_state_store  # noqa: E402
from lifecycle_state_machines import (  # noqa: E402
    AdmissionLifecycleMachine,
    LifecycleStateTree,
    TurnLifecycleMachine,
)


def _execution():
    return prepare_execution(
        {
            "id": "provider",
            "kind": "codex",
            "generation": "0a1f0f6c-f19f-4b9d-93d1-45d2db3af620",
            "revision": 1,
            "execution_revision": 1,
        },
        run_id="provider-run",
        turn_run_id="turn-run",
        prompt="test",
        cwd="/workspace",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        session_id=None,
        mode="native",
        app_session_id="session",
    )


def _turn_identity(execution_turn_id: str, assistant_message_id: str) -> dict:
    return {
        "user_turn_id": "user-turn",
        "lifecycle_message_id": "user-turn",
        "execution_turn_id": execution_turn_id,
        "assistant_message_id": assistant_message_id,
    }


async def _start_turn(
    tree: LifecycleStateTree,
    session_id: str,
    *,
    execution_turn_id: str = "turn-run",
    assistant_message_id: str = "assistant-message",
) -> dict:
    identity = _turn_identity(execution_turn_id, assistant_message_id)
    await tree.publish(
        "lifecycle.turn_start",
        root_id=session_id,
        session_id=session_id,
        payload=identity,
    )
    return identity


def _seed_v1_legacy_store() -> None:
    """Write a v1 store with a running 'legacy' session to migrate from.

    Every test that binds a LifecycleStateTree expecting the persisted
    'legacy' session must seed the store itself; module-level temp-home
    state is shared across tests but no test may rely on another test's
    on-disk side effects.
    """
    write_json(lifecycle_state_store._path(), {
        "version": 1,
        "sessions": {
            "legacy": {
                "prompts": {"prompt": {"state": "queued"}},
                "turn": {
                    "state": "running",
                    "admissions": {
                        "live-run": {
                            "turn_run_id": "live-execution",
                            "state": "deferred",
                        },
                        "missing-run": {
                            "turn_run_id": "missing-execution",
                            "state": "deferred",
                        },
                    },
                    "steers": {},
                },
            },
        },
    })


def test_schema_v1_migrates_contiguously_to_v2() -> None:
    _seed_v1_legacy_store()
    migrated = lifecycle_state_store.load()
    assert migrated["version"] == 2
    turn = migrated["sessions"]["legacy"]["turn"]
    assert turn["state"] == "legacy_reconciling"
    assert turn["execution_turn_id"] is None
    assert turn["executions"] == {}
    admission = turn["admissions"]["live-run"]
    assert admission["execution_turn_id"] == "live-execution"
    assert "turn_run_id" not in admission
    assert read_json(lifecycle_state_store._path(), {}) == migrated
    assert lifecycle_state_store.load() == migrated


async def test_migrated_running_state_binds_and_reconciles_without_matching_live_turn() -> None:
    _seed_v1_legacy_store()
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    assert tree.session("legacy").turn.state == "legacy_reconciling"
    await tree.reconcile(
        "legacy",
        live_run_ids={"live-run"},
        queued_message_ids={"prompt"},
        completed_message_ids=set(),
    )
    turn = tree.session("legacy").turn
    assert turn.state == "legacy_reconciling"
    assert set(turn.admissions) == {"live-run"}
    await tree.reconcile(
        "legacy",
        live_run_ids=set(),
        queued_message_ids=set(),
        completed_message_ids={"prompt"},
    )
    assert tree.session("legacy").turn.state == "idle"
    await tree.close()


def test_schema_rejects_future_and_malformed_v2() -> None:
    for projection in (
        {"version": 3, "sessions": {}},
        {
            "version": 2,
            "sessions": {
                "bad": {
                    "prompts": {},
                    "turn": {
                        "state": "running",
                        "user_turn_id": None,
                        "lifecycle_message_id": None,
                        "execution_turn_id": None,
                        "assistant_message_id": None,
                        "executions": [],
                        "admissions": {},
                        "steers": {},
                    },
                },
            },
        },
    ):
        write_json(lifecycle_state_store._path(), projection)
        try:
            lifecycle_state_store.load()
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid lifecycle schema was accepted")
    lifecycle_state_store.save({"version": 2, "sessions": {}})


async def test_sequential_executions_share_user_turn_and_ignore_stale_terminal() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    await tree.publish(
        "user_message_queued",
        root_id="sequential",
        session_id="sequential",
        message_id="user-turn",
        payload={},
    )
    first = _turn_identity("execution-1", "assistant-1")
    second = _turn_identity("execution-2", "assistant-2")
    await tree.publish(
        "lifecycle.turn_start",
        root_id="sequential",
        session_id="sequential",
        payload=first,
    )
    await tree.publish(
        "lifecycle.turn_complete",
        root_id="sequential",
        session_id="sequential",
        payload=first,
    )
    await tree.publish(
        "lifecycle.turn_start",
        root_id="sequential",
        session_id="sequential",
        payload=second,
    )
    await tree.publish(
        "lifecycle.turn_stopped",
        root_id="sequential",
        session_id="sequential",
        payload=first,
    )

    turn = tree.session("sequential").turn
    assert turn.state == "running"
    assert turn.user_turn_id == "user-turn"
    assert turn.lifecycle_message_id == "user-turn"
    assert turn.execution_turn_id == "execution-2"
    assert turn.assistant_message_id == "assistant-2"
    assert turn.executions["execution-1"].state == "complete"
    assert turn.executions["execution-2"].state == "running"

    await tree.publish(
        "lifecycle.turn_complete",
        root_id="sequential",
        session_id="sequential",
        payload=second,
    )
    assert turn.executions["execution-2"].state == "complete"
    await tree.close()


async def test_queued_successor_starts_with_clean_logical_turn_scope() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    for message_id in ("user-turn-a", "user-turn-b"):
        await tree.publish(
            "user_message_queued",
            root_id="queued-successor",
            session_id="queued-successor",
            message_id=message_id,
            payload={},
        )
    first = {
        "user_turn_id": "user-turn-a",
        "lifecycle_message_id": "user-turn-a",
        "execution_turn_id": "execution-a",
        "assistant_message_id": "assistant-a",
    }
    second = {
        "user_turn_id": "user-turn-b",
        "lifecycle_message_id": "user-turn-b",
        "execution_turn_id": "execution-b",
        "assistant_message_id": "assistant-b",
    }
    await tree.publish(
        "lifecycle.turn_start",
        root_id="queued-successor",
        session_id="queued-successor",
        payload=first,
    )
    await tree.publish(
        "lifecycle.turn_complete",
        root_id="queued-successor",
        session_id="queued-successor",
        payload=first,
    )
    await tree.publish(
        "user_message_done",
        root_id="queued-successor",
        session_id="queued-successor",
        message_id="user-turn-a",
        payload={},
    )
    await tree.publish(
        "lifecycle.turn_start",
        root_id="queued-successor",
        session_id="queued-successor",
        payload=second,
    )
    await tree.publish(
        "lifecycle.turn_stopped",
        root_id="queued-successor",
        session_id="queued-successor",
        payload=first,
    )

    session = tree.session("queued-successor")
    assert set(session.prompts) == {"user-turn-b"}
    assert session.turn.state == "running"
    assert session.turn.user_turn_id == "user-turn-b"
    assert session.turn.execution_turn_id == "execution-b"
    assert set(session.turn.executions) == {"execution-b"}
    assert session.turn.admissions == {}
    assert session.turn.steers == {}
    await tree.close()


async def test_missing_identity_and_worker_terminal_cannot_mutate_active_turn() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    identity = await _start_turn(tree, "identity-guard")
    await tree.publish(
        "lifecycle.turn_complete",
        root_id="identity-guard",
        session_id="identity-guard",
        payload={},
    )
    await tree.publish(
        "lifecycle.worker_turn_complete",
        root_id="identity-guard",
        session_id="identity-guard",
        run_id="worker-provider-run",
        payload={
            "delegation_id": "delegation",
            "execution_turn_id": "worker-turn",
            "assistant_message_id": "assistant-message",
        },
    )
    turn = tree.session("identity-guard").turn
    assert turn.state == "running"
    assert turn.execution_turn_id == identity["execution_turn_id"]
    await tree.close()


async def test_admission_rejects_stale_parent_and_ignores_stale_transition() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    current = await _start_turn(tree, "admission-identity")
    stale = _turn_identity("stale-execution", "stale-assistant")

    rejected_execution = _execution()
    rejected_handle = tree.register_execution_handle(rejected_execution)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="admission-identity",
        session_id="admission-identity",
        run_id="stale-registration",
        payload={**stale, "execution_handle": rejected_handle},
    )
    assert rejected_handle not in tree._execution_handles
    assert "stale-registration" not in tree.session(
        "admission-identity"
    ).turn.admissions

    execution = _execution()
    handle = tree.register_execution_handle(execution)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="admission-identity",
        session_id="admission-identity",
        run_id="provider-run",
        payload={**current, "execution_handle": handle},
    )
    await tree.publish(
        "lifecycle.admission_failed",
        root_id="admission-identity",
        session_id="admission-identity",
        run_id="provider-run",
        payload=stale,
    )
    admission = tree.session("admission-identity").turn.admissions["provider-run"]
    assert admission.state == "registered"
    assert handle in tree._execution_handles
    await tree.close()


async def test_fact_driven_hierarchy_and_pre_spawn_cancel() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    execution = _execution()
    handle_id = tree.register_execution_handle(execution)

    await event_bus.publish(BusEvent(
        type="user_message_requested",
        root_id="session",
        sid="session",
        msg_id="message",
        payload={},
        persist=False,
    ))
    assert tree.has_active_session("session")
    assert tree.session("session").prompts["message"].state == "requested"
    await event_bus.publish(BusEvent(
        type="user_message_queued",
        root_id="session",
        sid="session",
        msg_id="message",
        payload={},
        persist=False,
    ))
    identity = await _start_turn(tree, "session")
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={**identity, "execution_handle": handle_id},
    )
    await tree.publish(
        "lifecycle.admission_cancel_requested",
        root_id="session",
        session_id="session",
        payload=identity,
    )

    session = tree.session("session")
    assert session.prompts["message"].state == "queued"
    assert session.turn.state == "running"
    assert session.turn.admissions["provider-run"].state == "cancelled"
    assert execution.wait_for_admission() is False
    await tree.publish(
        "lifecycle.admission_cancelled",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload=identity,
    )
    await tree.publish(
        "lifecycle.turn_stopped",
        root_id="session",
        session_id="session",
        payload=identity,
    )
    await tree.publish(
        "user_message_done",
        root_id="session",
        session_id="session",
        message_id="message",
        payload={},
    )
    await tree.close()


async def test_same_facts_converge_to_same_tree() -> None:
    async def project() -> tuple[str, str]:
        event_bus = EventBus()
        tree = LifecycleStateTree(event_bus)
        await tree.bind()
        for event_type in (
            "user_message_queued",
            "user_message_sent",
            "user_message_received",
            "user_message_done",
        ):
            await event_bus.publish(BusEvent(
                type=event_type,
                root_id="session",
                sid="session",
                msg_id="message",
                payload={},
                persist=False,
            ))
        identity = _turn_identity("execution", "assistant")
        for event_type in ("lifecycle.turn_start", "lifecycle.turn_complete"):
            await event_bus.publish(BusEvent(
                type=event_type,
                root_id="session",
                sid="session",
                payload=identity,
                persist=False,
            ))
        session = tree.session("session")
        return (
            "retired" if "message" not in session.prompts else session.prompts["message"].state,
            session.turn.state,
        )

    assert await project() == await project() == ("retired", "idle")


async def test_bus_facts_are_serializable_and_tree_rebinds_after_close() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    execution = _execution()
    handle_id = tree.register_execution_handle(execution)
    identity = await _start_turn(tree, "session")
    payload = {**identity, "execution_handle": handle_id}
    json.dumps(payload)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload=payload,
    )
    await tree.publish(
        "lifecycle.admission_failed",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload=identity,
    )
    assert handle_id not in tree._execution_handles
    await tree.publish(
        "lifecycle.turn_stopped",
        root_id="session",
        session_id="session",
        payload=identity,
    )
    await tree.close()
    await tree.bind()
    await tree.close()


async def test_deferred_commit_to_spawn_cancel_uses_real_facts() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    execution = _execution()
    handle_id = tree.register_execution_handle(execution)
    identity = await _start_turn(tree, "session")
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={**identity, "execution_handle": handle_id},
    )
    await tree.publish(
        "lifecycle.admission_deferred",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload=identity,
    )
    assert execution._try_commit_spawn()
    await tree.publish(
        "lifecycle.admission_admitted",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload=identity,
    )
    await tree.publish(
        "lifecycle.admission_cancel_requested",
        root_id="session",
        session_id="session",
        payload=identity,
    )
    assert execution.cancel_after_admission_requested
    assert handle_id in tree._execution_handles
    execution._mark_spawn_completed()
    await tree.publish(
        "lifecycle.admission_spawned",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload=identity,
    )
    assert handle_id not in tree._execution_handles
    await tree.publish(
        "lifecycle.turn_complete",
        root_id="session",
        session_id="session",
        payload=identity,
    )
    await tree.close()


async def test_parent_terminal_and_close_cancel_pending_children() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    first = _execution()
    first_handle = tree.register_execution_handle(first)
    first_identity = await _start_turn(tree, "session")
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={**first_identity, "execution_handle": first_handle},
    )
    await event_bus.publish(BusEvent(
        type="lifecycle.turn_stopped",
        root_id="session",
        sid="session",
        payload=first_identity,
        persist=False,
    ))
    assert first.wait_for_admission() is False
    assert first_handle not in tree._execution_handles

    second = _execution()
    second_handle = tree.register_execution_handle(second)
    second_identity = await _start_turn(
        tree,
        "session-2",
        execution_turn_id="turn-run-2",
        assistant_message_id="assistant-2",
    )
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session-2",
        session_id="session-2",
        run_id="provider-run-2",
        payload={**second_identity, "execution_handle": second_handle},
    )
    await tree.close()
    assert second.wait_for_admission() is False


async def test_steer_machine_stays_under_turn_and_fallback_transfers_owner() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    identity = await _start_turn(tree, "session")
    for event_type in (
        "lifecycle.steer_requested",
        "lifecycle.steer_accepted",
        "lifecycle.steer_persisted",
    ):
        await tree.publish(
            event_type,
            root_id="session",
            session_id="session",
            run_id="provider-run",
            message_id="steer-1",
            payload={},
        )
    session = tree.session("session")
    assert session.turn.steers["steer-1"].state == "persisted"
    assert session.turn.steers["steer-1"].provider_run_id == "provider-run"
    assert "steer-1" not in session.prompts

    await tree.publish(
        "lifecycle.steer_requested",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        message_id="steer-2",
        payload=identity,
    )
    await tree.publish(
        "lifecycle.steer_failed",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        message_id="steer-2",
        payload={"reason": "runner_not_ready"},
    )
    assert session.turn.steers["steer-2"].failure_reason == "runner_not_ready"
    await tree.publish(
        "lifecycle.steer_fallback_queued",
        root_id="session",
        session_id="session",
        message_id="steer-2",
        payload={"reason": "steer_rejected"},
    )
    assert "steer-2" not in session.turn.steers
    assert session.prompts["steer-2"].state == "queued"

    await tree.publish(
        "lifecycle.turn_complete",
        root_id="session",
        session_id="session",
        payload=identity,
    )
    assert session.turn.steers == {}


async def test_persisted_projection_reloads_and_reconciles_from_reality() -> None:
    first_bus = EventBus()
    first = LifecycleStateTree(first_bus)
    await first.bind()
    await _start_turn(first, "restart-session")
    await first.publish(
        "lifecycle.steer_requested",
        root_id="restart-session",
        session_id="restart-session",
        run_id="gone-run",
        message_id="gone-steer",
        payload={},
    )
    await first.publish(
        "user_message_queued",
        root_id="restart-session",
        session_id="restart-session",
        message_id="persisted-prompt",
        payload={},
    )
    await first.flush()
    await first.close()

    second_bus = EventBus()
    second = LifecycleStateTree(second_bus)
    await second.bind()
    restored = second.session("restart-session")
    assert restored.turn.state == "running"
    assert restored.turn.steers["gone-steer"].provider_run_id == "gone-run"
    assert restored.prompts["persisted-prompt"].state == "queued"
    requirements = second.reconciliation_requirements()
    assert requirements.get("restart-session") == {
        "prompt_message_ids": {"persisted-prompt"},
        "needs_live_runs": True,
    }, requirements

    await second.reconcile(
        "restart-session",
        live_run_ids=set(),
        queued_message_ids=set(),
        completed_message_ids={"persisted-prompt"},
    )
    reconciled = second.session("restart-session")
    assert reconciled.turn.state == "idle"
    assert reconciled.turn.steers == {}
    assert reconciled.prompts == {}
    await second.close()


async def test_reconcile_queries_and_repairs_only_state_declared_evidence() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    await tree.publish(
        "user_message_queued",
        root_id="queued-only",
        session_id="queued-only",
        message_id="missing-prompt",
        payload={},
    )
    execution = _execution()
    handle_id = tree.register_execution_handle(execution)
    identity = await _start_turn(tree, "admission-only")
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="admission-only",
        session_id="admission-only",
        run_id="missing-run",
        payload={
            **identity,
            "execution_handle": handle_id,
        },
    )

    requirements = tree.reconciliation_requirements()
    assert requirements["queued-only"] == {
        "prompt_message_ids": {"missing-prompt"},
        "needs_live_runs": False,
    }
    assert requirements["admission-only"] == {
        "prompt_message_ids": set(),
        "needs_live_runs": True,
    }

    await tree.reconcile(
        "queued-only",
        live_run_ids=set(),
        queued_message_ids=set(),
        completed_message_ids=set(),
    )
    await tree.reconcile(
        "admission-only",
        live_run_ids=set(),
        queued_message_ids=set(),
        completed_message_ids=set(),
    )
    assert tree.session("queued-only").prompts == {}
    assert tree.session("admission-only").turn.admissions == {}
    await tree.close()


async def test_persist_snapshots_only_changed_session_without_blocking_loop() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    for index in range(10_000):
        tree.session(f"bulk-{index}")

    started = threading.Event()
    release = threading.Event()
    original_merge = lifecycle_state_store.merge_sessions

    def blocked_merge(changes):
        assert set(changes) == {"target"}
        started.set()
        assert release.wait(timeout=2.0)
        original_merge(changes)

    lifecycle_state_store.merge_sessions = blocked_merge
    try:
        await tree.publish(
            "user_message_queued",
            root_id="target",
            session_id="target",
            message_id="message",
            payload={},
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        loop_advanced = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_advanced.set)
        await asyncio.wait_for(loop_advanced.wait(), timeout=0.2)
        release.set()
        await tree.flush()
    finally:
        release.set()
        lifecycle_state_store.merge_sessions = original_merge
    await tree.close()


async def test_persist_merges_ordered_sessions_tombstones_and_retries() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    await tree.publish(
        "user_message_queued",
        root_id="a",
        session_id="a",
        message_id="a-message",
        payload={},
    )
    await tree.publish(
        "user_message_queued",
        root_id="b",
        session_id="b",
        message_id="b-message",
        payload={},
    )
    await tree.publish(
        "user_message_sent",
        root_id="a",
        session_id="a",
        message_id="a-message",
        payload={},
    )
    await tree.publish(
        "user_message_done",
        root_id="b",
        session_id="b",
        message_id="b-message",
        payload={},
    )
    await tree.publish(
        "user_message_received",
        root_id="a",
        session_id="a",
        message_id="a-message",
        payload={},
    )
    await tree.flush()
    projection = lifecycle_state_store.load()
    assert projection["sessions"]["a"]["prompts"]["a-message"]["state"] == "received"
    assert "b" not in projection["sessions"]

    original_merge = lifecycle_state_store.merge_sessions
    lifecycle_state_store.merge_sessions = lambda _changes: (
        (_ for _ in ()).throw(OSError("persist unavailable"))
    )
    try:
        await tree.publish(
            "user_message_queued",
            root_id="retry",
            session_id="retry",
            message_id="retry-message",
            payload={},
        )
        try:
            await tree.flush()
        except RuntimeError as exc:
            assert isinstance(exc.__cause__, OSError)
        else:
            raise AssertionError("failed lifecycle save was reported as successful")
        assert "retry" in tree._pending_session_projections
    finally:
        lifecycle_state_store.merge_sessions = original_merge

    tree._schedule_persist("retry")
    await tree.flush()
    restored = lifecycle_state_store.load()
    assert restored["sessions"]["retry"]["prompts"]["retry-message"]["state"] == "queued"

    first_merge_started = threading.Event()
    release_first_merge = threading.Event()
    merge_calls = 0

    def fail_once_while_newer_event_arrives(changes):
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls == 1:
            first_merge_started.set()
            assert release_first_merge.wait(timeout=2.0)
            raise OSError("first persist unavailable")
        original_merge(changes)

    lifecycle_state_store.merge_sessions = fail_once_while_newer_event_arrives
    try:
        await tree.publish(
            "user_message_queued",
            root_id="concurrent",
            session_id="concurrent",
            message_id="first",
            payload={},
        )
        assert await asyncio.to_thread(first_merge_started.wait, 1.0)
        await tree.publish(
            "user_message_queued",
            root_id="concurrent",
            session_id="concurrent",
            message_id="second",
            payload={},
        )
        release_first_merge.set()
        await tree.flush()
    finally:
        release_first_merge.set()
        lifecycle_state_store.merge_sessions = original_merge
    concurrent = lifecycle_state_store.load()["sessions"]["concurrent"]["prompts"]
    assert set(concurrent) == {"first", "second"}
    assert merge_calls == 2
    await tree.close()


# ---------------------------------------------------------------------------
# Edge cases, error guards, and reconciliation no-op paths. Each test asserts
# a real behaviour (a raise, a state change, or a deliberate no-op) rather than
# merely executing a line. They extend lifecycle_state_machines.py coverage
# past the core happy-path flows above.
# ---------------------------------------------------------------------------


def _identity(
    *,
    user_turn_id: str = "user-turn",
    lifecycle_message_id: str = "user-turn",
    execution_turn_id: str = "turn-run",
    assistant_message_id: str = "assistant-message",
) -> dict:
    return {
        "user_turn_id": user_turn_id,
        "lifecycle_message_id": lifecycle_message_id,
        "execution_turn_id": execution_turn_id,
        "assistant_message_id": assistant_message_id,
    }


async def _register_admission(
    tree: LifecycleStateTree,
    session_id: str,
    *,
    run_id: str,
    execution,
    identity: dict | None = None,
) -> str:
    identity = identity or _identity()
    handle_id = tree.register_execution_handle(execution)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id=session_id,
        session_id=session_id,
        run_id=run_id,
        payload={"execution_handle": handle_id, **identity},
    )
    return handle_id


def _reset_store() -> None:
    """Reset the shared temp-home store to empty.

    The module-level _test_home.isolate() shares one store across every test
    in this file; each test that assumes an empty slate must reset it first.
    """
    write_json(lifecycle_state_store._path(), {"version": 2, "sessions": {}})


def _turn_projection(execution_turn_id: str) -> dict:
    """A valid v2 running turn with full identity and no execution record.

    The store validator permits a running turn whose `executions` map does not
    contain the active execution_turn_id, so this shape loads normally.
    """
    return {
        "state": "running",
        "user_turn_id": "u",
        "lifecycle_message_id": "u",
        "execution_turn_id": execution_turn_id,
        "assistant_message_id": "a",
        "executions": {},
        "admissions": {},
        "steers": {},
    }


def _event(
    type_: str,
    session_id: str,
    *,
    payload: dict | None = None,
    run_id: str | None = None,
    message_id: str | None = None,
) -> BusEvent:
    return BusEvent(
        type=type_,
        root_id=session_id,
        sid=session_id,
        payload=payload or {},
        run_id=run_id,
        msg_id=message_id,
    )


def test_assert_owner_rejects_unbound_access() -> None:
    tree = LifecycleStateTree(EventBus())
    with pytest.raises(RuntimeError, match="owning loop"):
        tree.session("x")


def test_cancel_is_noop_for_already_terminal_admission() -> None:
    cancelled = AdmissionLifecycleMachine(
        "run", "turn-run", None, None, None, None, None, state="cancelled"
    )
    cancelled.cancel()
    assert cancelled.state == "cancelled"
    failed = AdmissionLifecycleMachine(
        "run", "turn-run", None, None, None, None, None, state="failed"
    )
    failed.cancel()
    assert failed.state == "failed"


async def test_bind_rejects_a_foreign_event_loop() -> None:
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    other = asyncio.new_event_loop()
    tree._loop = other
    try:
        with pytest.raises(RuntimeError, match="cannot move between event loops"):
            await tree.bind()
    finally:
        other.close()


async def test_has_active_session_and_active_turn_identity() -> None:
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    assert tree.has_active_session("unknown") is False
    assert tree.active_turn_identity("unknown") is None
    tree.session("idle")
    assert tree.has_active_session("idle") is False
    assert tree.active_turn_identity("idle") is None
    await _start_turn(tree, "active")
    assert tree.has_active_session("active") is True
    assert tree.active_turn_identity("active") == _identity()
    await tree.close()


async def test_retire_and_flush_noop_for_idle_state() -> None:
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    tree._retire_session_if_idle("unknown")  # unknown session -> no crash
    await tree.flush()  # nothing pending, no task -> returns cleanly
    assert tree._pending_session_projections == {}
    await tree.close()


async def test_steer_rerequest_is_ignored_and_invalid_transition_raises() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    await tree.publish(
        "lifecycle.steer_requested",
        root_id="s",
        session_id="s",
        message_id="steer-1",
        run_id=None,
        payload={},
    )
    # A steer created without a run_id backfills it from a later event.
    await tree.publish(
        "lifecycle.steer_accepted",
        root_id="s",
        session_id="s",
        message_id="steer-1",
        run_id="run-9",
        payload={},
    )
    assert tree.session("s").turn.steers["steer-1"].provider_run_id == "run-9"
    assert tree.session("s").turn.steers["steer-1"].state == "accepted"
    # A second "requested" for an existing steer is ignored (no reset).
    await tree.publish(
        "lifecycle.steer_requested",
        root_id="s",
        session_id="s",
        message_id="steer-1",
        payload={},
    )
    assert tree.session("s").turn.steers["steer-1"].state == "accepted"
    await tree.publish(
        "lifecycle.steer_persisted",
        root_id="s",
        session_id="s",
        message_id="steer-1",
        payload={},
    )
    # An illegal transition raises inside the handler. EventBus.publish swallows
    # subscriber exceptions, so exercise the handler directly.
    with pytest.raises(ValueError, match="invalid steer transition"):
        await tree._apply(_event("lifecycle.steer_accepted", "s", message_id="steer-1"))
    await tree.close()


async def test_reconcile_turn_missing_noop_paths() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s", execution_turn_id="exec-1")
    identity = _identity(execution_turn_id="exec-1")
    # A queued prompt keeps the session alive across the stop transition so the
    # post-state is observable (an idle session would be retired on the spot).
    await tree.publish(
        "user_message_queued",
        root_id="s",
        session_id="s",
        message_id="p",
        payload={},
    )
    # Non-legacy running turn, mismatched identity -> ignored, stays running.
    await tree.publish(
        "lifecycle.reconcile_turn_missing",
        root_id="s",
        session_id="s",
        payload=_identity(execution_turn_id="other", user_turn_id="other"),
    )
    assert tree.session("s").turn.state == "running"
    # Matching identity -> turn stopped.
    await tree.publish(
        "lifecycle.reconcile_turn_missing",
        root_id="s",
        session_id="s",
        payload=identity,
    )
    assert tree.session("s").turn.state == "stopped"
    await tree.close()


async def test_reconcile_admission_missing_identity_gated() -> None:
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    await _register_admission(tree, "s", run_id="run-1", execution=_execution())
    # Unknown run -> no-op.
    await tree.publish(
        "lifecycle.reconcile_admission_missing",
        root_id="s",
        session_id="s",
        run_id="run-missing",
        payload=_identity(),
    )
    assert "run-1" in tree.session("s").turn.admissions
    # Mismatched identity -> no-op.
    await tree.publish(
        "lifecycle.reconcile_admission_missing",
        root_id="s",
        session_id="s",
        run_id="run-1",
        payload=_identity(execution_turn_id="other", user_turn_id="other"),
    )
    assert "run-1" in tree.session("s").turn.admissions
    # Matching identity -> admission dropped.
    await tree.publish(
        "lifecycle.reconcile_admission_missing",
        root_id="s",
        session_id="s",
        run_id="run-1",
        payload=_identity(),
    )
    assert "run-1" not in tree.session("s").turn.admissions
    await tree.close()


async def test_legacy_reconcile_admission_missing_requires_legacy_payload() -> None:
    _seed_v1_legacy_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    # Legacy-reconciling turn: a non-legacy payload is rejected (no-op).
    await tree.publish(
        "lifecycle.reconcile_admission_missing",
        root_id="legacy",
        session_id="legacy",
        run_id="live-run",
        payload=_identity(),
    )
    assert "live-run" in tree.session("legacy").turn.admissions
    # The legacy sentinel payload drops the admission.
    await tree.publish(
        "lifecycle.reconcile_admission_missing",
        root_id="legacy",
        session_id="legacy",
        run_id="live-run",
        payload={"legacy_reconciling": True},
    )
    assert "live-run" not in tree.session("legacy").turn.admissions
    await tree.close()


async def test_legacy_reconcile_turn_missing_with_payload_is_noop() -> None:
    _seed_v1_legacy_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    assert tree.session("legacy").turn.state == "legacy_reconciling"
    await tree.publish(
        "lifecycle.reconcile_turn_missing",
        root_id="legacy",
        session_id="legacy",
        payload={"legacy_reconciling": True},
    )
    assert tree.session("legacy").turn.state == "legacy_reconciling"
    await tree.close()


async def test_reconcile_steer_parent_gone_removes_steer() -> None:
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    await tree.publish(
        "lifecycle.steer_requested",
        root_id="s",
        session_id="s",
        message_id="steer-1",
        run_id="run-9",
        payload={},
    )
    await tree.publish(
        "lifecycle.reconcile_steer_parent_gone",
        root_id="s",
        session_id="s",
        message_id="steer-1",
        run_id="run-9",
        payload={},
    )
    assert "steer-1" not in tree.session("s").turn.steers
    await tree.close()


async def test_admission_cancel_requested_skips_non_matching() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    await _register_admission(tree, "s", run_id="run-a", execution=_execution())
    # Mismatched identity -> no admission cancelled (the `continue` path).
    await tree.publish(
        "lifecycle.admission_cancel_requested",
        root_id="s",
        session_id="s",
        payload=_identity(
            execution_turn_id="other", assistant_message_id="x", user_turn_id="other"
        ),
    )
    assert tree.session("s").turn.admissions["run-a"].state == "registered"
    await tree.close()


async def test_admission_identity_matches_tolerates_malformed_payload() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    await _register_admission(tree, "s", run_id="run-a", execution=_execution())
    # A malformed payload reaching _admission_identity_matches (via the
    # reconcile_admission_missing handler, which does not pre-validate identity)
    # is treated as a non-match rather than crashing.
    await tree.publish(
        "lifecycle.reconcile_admission_missing",
        root_id="s",
        session_id="s",
        run_id="run-a",
        payload={"incomplete": True},
    )
    assert "run-a" in tree.session("s").turn.admissions
    assert tree.session("s").turn.admissions["run-a"].state == "registered"
    await tree.close()


async def test_admission_registered_rejects_bad_handle() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    # EventBus.publish swallows subscriber exceptions, so exercise the handler
    # directly to observe the guard raises.
    with pytest.raises(TypeError, match="execution handle"):
        await tree._apply(
            _event(
                "lifecycle.admission_registered",
                "s",
                run_id="run-1",
                payload={"execution_handle": 123, **_identity()},
            )
        )
    with pytest.raises(KeyError, match="execution handle is unavailable"):
        await tree._apply(
            _event(
                "lifecycle.admission_registered",
                "s",
                run_id="run-1",
                payload={"execution_handle": "no-such-handle", **_identity()},
            )
        )
    await tree.close()


async def test_admission_state_event_for_unknown_run_is_noop() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    await tree.publish(
        "lifecycle.admission_admitted",
        root_id="s",
        session_id="s",
        run_id="never-registered",
        payload=_identity(),
    )
    assert tree.session("s").turn.admissions == {}
    await tree.close()


async def test_reconcile_unknown_session_and_requirements_skip_idle() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    # Reconciling a session the tree never heard of is a no-op.
    await tree.reconcile(
        "unknown",
        live_run_ids=set(),
        queued_message_ids=set(),
        completed_message_ids=set(),
    )
    # An idle session contributes nothing to reconciliation requirements.
    tree.session("idle")
    assert tree.reconciliation_requirements() == {}
    await tree.close()


async def test_reconcile_drops_missing_admission_and_orphaned_steer() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s")
    # A live admission keeps the turn non-missing, so the admission/steer fact
    # loops run (turn_missing is False whenever live_run_ids is non-empty).
    await _register_admission(tree, "s", run_id="run-alive", execution=_execution())
    await _register_admission(tree, "s", run_id="run-dead", execution=_execution())
    await tree.publish(
        "lifecycle.steer_requested",
        root_id="s",
        session_id="s",
        message_id="steer-dead",
        run_id="run-dead",
        payload={},
    )
    # A steer whose parent is still live is retained (exercises the `continue`).
    await tree.publish(
        "lifecycle.steer_requested",
        root_id="s",
        session_id="s",
        message_id="steer-alive",
        run_id="run-alive",
        payload={},
    )
    await tree.reconcile(
        "s",
        live_run_ids={"run-alive"},
        queued_message_ids=set(),
        completed_message_ids=set(),
    )
    assert "run-alive" in tree.session("s").turn.admissions
    assert "run-dead" not in tree.session("s").turn.admissions
    assert "steer-dead" not in tree.session("s").turn.steers
    assert "steer-alive" in tree.session("s").turn.steers
    await tree.close()


async def test_start_execution_turn_rejects_running_conflicts() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await _start_turn(tree, "s", execution_turn_id="exec-1")
    with pytest.raises(ValueError, match="cannot replace a running execution turn"):
        await tree._apply(
            _event(
                "lifecycle.turn_start",
                "s",
                payload=_identity(execution_turn_id="exec-2"),
            )
        )
    with pytest.raises(ValueError, match="cannot replace an active logical user turn"):
        await tree._apply(
            _event(
                "lifecycle.turn_start",
                "s",
                payload=_identity(user_turn_id="other-user", execution_turn_id="exec-1"),
            )
        )
    await tree.close()


async def test_handlers_skip_missing_execution_record() -> None:
    # A valid v2 projection may carry a running turn whose execution_turn_id has
    # no matching entry in `executions`. Terminal and reconcile-turn-missing
    # handlers must skip the missing execution rather than KeyError.
    write_json(lifecycle_state_store._path(), {
        "version": 2,
        "sessions": {
            "to-complete": {
                "prompts": {"p": {"state": "queued"}},
                "turn": _turn_projection("exec-1"),
            },
            "to-stop": {
                "prompts": {"p": {"state": "queued"}},
                "turn": _turn_projection("exec-2"),
            },
        },
    })
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    await tree.publish(
        "lifecycle.turn_complete",
        root_id="to-complete",
        session_id="to-complete",
        payload=_identity(
            user_turn_id="u",
            lifecycle_message_id="u",
            execution_turn_id="exec-1",
            assistant_message_id="a",
        ),
    )
    assert tree.session("to-complete").turn.state == "complete"
    await tree.publish(
        "lifecycle.reconcile_turn_missing",
        root_id="to-stop",
        session_id="to-stop",
        payload=_identity(
            user_turn_id="u",
            lifecycle_message_id="u",
            execution_turn_id="exec-2",
            assistant_message_id="a",
        ),
    )
    assert tree.session("to-stop").turn.state == "stopped"
    await tree.close()


async def test_terminal_event_on_turn_without_execution_id() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    # A terminal event for a turn with no execution_turn_id (an idle turn that
    # never received turn_start) must resolve cleanly rather than KeyError.
    await tree.publish(
        "user_message_queued",
        root_id="s",
        session_id="s",
        message_id="p",
        payload={},
    )
    await tree.publish(
        "lifecycle.turn_complete",
        root_id="s",
        session_id="s",
        payload=_identity(),
    )
    assert tree.session("s").turn.state == "complete"
    await tree.close()


def test_restore_skips_malformed_projection_entries() -> None:
    # _restore is the tree's own defensive parser for a persisted projection.
    # Every malformed entry (non-str key or non-dict value) is skipped rather
    # than crashing; only well-formed entries materialize in the sessions map.
    tree = LifecycleStateTree(EventBus())
    tree._restore(
        {
            "sessions": {
                5: "not-a-session",  # non-str key -> skip
                "ok": {
                    "prompts": {
                        "p": {"state": "queued"},
                        7: "not-a-prompt",  # non-str key -> skip
                    },
                    "turn": {
                        "state": "running",
                        "executions": {
                            9: "not-an-exec",  # non-str key -> skip
                            "exec-1": {"state": "running"},
                        },
                        "admissions": {
                            "bad-adm": "not-a-dict",  # non-dict value -> skip
                            # non-str execution_turn_id -> skip
                            "bad-exec": {"execution_turn_id": 5},
                            "good-adm": {
                                "execution_turn_id": "exec-1",
                                "state": "registered",
                            },
                        },
                        "steers": {11: "not-a-steer"},  # non-str key -> skip
                    },
                },
            }
        }
    )
    session = tree._sessions["ok"]
    assert set(session.prompts) == {"p"}
    assert set(session.turn.executions) == {"exec-1"}
    assert set(session.turn.admissions) == {"good-adm"}
    assert session.turn.steers == {}
    # The non-str-keyed session never materialized.
    assert 5 not in tree._sessions


def test_start_execution_turn_rejects_legacy_and_terminal_user_mismatch() -> None:
    legacy = TurnLifecycleMachine(state="legacy_reconciling", user_turn_id="old-user")
    with pytest.raises(ValueError, match="legacy logical turn requires reconciliation"):
        LifecycleStateTree._start_execution_turn(legacy, _identity(user_turn_id="new"))

    terminal = TurnLifecycleMachine(state="complete", user_turn_id="old-user")
    terminal.admissions["run-1"] = AdmissionLifecycleMachine(
        "run-1", "exec-1", "old-user", None, None, None, None
    )
    with pytest.raises(ValueError, match="terminal logical turn retained admissions"):
        LifecycleStateTree._start_execution_turn(terminal, _identity(user_turn_id="new"))


async def test_flush_spawns_persist_task_when_none_live() -> None:
    _reset_store()
    tree = LifecycleStateTree(EventBus())
    await tree.bind()
    # A pending projection with no live persist task forces flush() to spawn
    # the persist loop itself (the _schedule_persist fast path already creates
    # one, so this is the only way to reach that branch in flush()).
    tree._pending_session_projections = {
        "s": {
            "prompts": {},
            "turn": {
                "state": "idle",
                "executions": {},
                "admissions": {},
                "steers": {},
            },
        }
    }
    tree._persist_task = None
    await tree.flush()
    assert tree._pending_session_projections == {}
    await tree.close()


def main() -> None:
    test_schema_v1_migrates_contiguously_to_v2()
    asyncio.run(
        test_migrated_running_state_binds_and_reconciles_without_matching_live_turn()
    )
    test_schema_rejects_future_and_malformed_v2()
    asyncio.run(
        test_sequential_executions_share_user_turn_and_ignore_stale_terminal()
    )
    asyncio.run(test_queued_successor_starts_with_clean_logical_turn_scope())
    asyncio.run(
        test_missing_identity_and_worker_terminal_cannot_mutate_active_turn()
    )
    asyncio.run(
        test_admission_rejects_stale_parent_and_ignores_stale_transition()
    )
    asyncio.run(test_fact_driven_hierarchy_and_pre_spawn_cancel())
    asyncio.run(test_same_facts_converge_to_same_tree())
    asyncio.run(test_bus_facts_are_serializable_and_tree_rebinds_after_close())
    asyncio.run(test_deferred_commit_to_spawn_cancel_uses_real_facts())
    asyncio.run(test_parent_terminal_and_close_cancel_pending_children())
    asyncio.run(test_steer_machine_stays_under_turn_and_fallback_transfers_owner())
    asyncio.run(test_persisted_projection_reloads_and_reconciles_from_reality())
    asyncio.run(test_reconcile_queries_and_repairs_only_state_declared_evidence())
    asyncio.run(test_persist_snapshots_only_changed_session_without_blocking_loop())
    asyncio.run(test_persist_merges_ordered_sessions_tombstones_and_retries())
    print("PASS lifecycle state machines")


if __name__ == "__main__":
    main()
