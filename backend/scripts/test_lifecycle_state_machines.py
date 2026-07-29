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
    await event_bus.publish(BusEvent(
        type="lifecycle.turn_start",
        root_id="session",
        sid="session",
        payload={},
        persist=False,
    ))
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={"turn_run_id": "turn-run", "execution_handle": handle_id},
    )
    await tree.publish(
        "lifecycle.admission_cancel_requested",
        root_id="session",
        session_id="session",
        payload={},
    )

    session = tree.session("session")
    assert session.prompts["message"].state == "queued"
    assert session.turn.state == "running"
    assert session.turn.admissions["provider-run"].state == "cancelled"
    assert execution.wait_for_admission() is False


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
        for event_type in ("lifecycle.turn_start", "lifecycle.turn_complete"):
            await event_bus.publish(BusEvent(
                type=event_type,
                root_id="session",
                sid="session",
                payload={},
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
    payload = {"turn_run_id": "turn-run", "execution_handle": handle_id}
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
        payload={},
    )
    assert handle_id not in tree._execution_handles
    await tree.close()
    await tree.bind()
    await tree.close()


async def test_deferred_commit_to_spawn_cancel_uses_real_facts() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    execution = _execution()
    handle_id = tree.register_execution_handle(execution)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={"turn_run_id": "turn-run", "execution_handle": handle_id},
    )
    await tree.publish(
        "lifecycle.admission_deferred",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={},
    )
    assert execution._try_commit_spawn()
    await tree.publish(
        "lifecycle.admission_admitted",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={},
    )
    await tree.publish(
        "lifecycle.admission_cancel_requested",
        root_id="session",
        session_id="session",
        payload={},
    )
    assert execution.cancel_after_admission_requested
    assert handle_id in tree._execution_handles
    execution._mark_spawn_completed()
    await tree.publish(
        "lifecycle.admission_spawned",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={},
    )
    assert handle_id not in tree._execution_handles


async def test_parent_terminal_and_close_cancel_pending_children() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    first = _execution()
    first_handle = tree.register_execution_handle(first)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session",
        session_id="session",
        run_id="provider-run",
        payload={"turn_run_id": "turn-run", "execution_handle": first_handle},
    )
    await event_bus.publish(BusEvent(
        type="lifecycle.turn_stopped",
        root_id="session",
        sid="session",
        payload={},
        persist=False,
    ))
    assert first.wait_for_admission() is False
    assert first_handle not in tree._execution_handles

    second = _execution()
    second_handle = tree.register_execution_handle(second)
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="session-2",
        session_id="session-2",
        run_id="provider-run-2",
        payload={"turn_run_id": "turn-run-2", "execution_handle": second_handle},
    )
    await tree.close()
    assert second.wait_for_admission() is False


async def test_steer_machine_stays_under_turn_and_fallback_transfers_owner() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    await tree.bind()
    await tree.publish(
        "lifecycle.turn_start",
        root_id="session",
        session_id="session",
        payload={},
    )
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
        payload={},
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
        payload={},
    )
    assert session.turn.steers == {}


async def test_persisted_projection_reloads_and_reconciles_from_reality() -> None:
    first_bus = EventBus()
    first = LifecycleStateTree(first_bus)
    await first.bind()
    await first.publish(
        "lifecycle.turn_start",
        root_id="restart-session",
        session_id="restart-session",
        payload={},
    )
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
    await tree.publish(
        "lifecycle.admission_registered",
        root_id="admission-only",
        session_id="admission-only",
        run_id="missing-run",
        payload={
            "turn_run_id": "turn-run",
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
