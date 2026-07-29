from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_test_home.isolate("ba-test-lifecycle-state-machines-")

from event_bus import BusEvent, EventBus  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from json_store import read_json, write_json  # noqa: E402
import lifecycle_state_store  # noqa: E402
from lifecycle_state_machines import LifecycleStateTree  # noqa: E402


def _execution():
    return prepare_execution(
        {
            "id": "provider",
            "kind": "codex",
            "generation": "0a1f0f6c-f19f-4b9d-93d1-45d2db3af620",
            "revision": 1,
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


def test_schema_v1_migrates_contiguously_to_v2() -> None:
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
