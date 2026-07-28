from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_test_home.isolate("ba-test-lifecycle-state-machines-")

from event_bus import BusEvent, EventBus  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
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
    tree.bind()
    execution = _execution()
    handle_id = tree.register_execution_handle(execution)

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
        tree.bind()
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
    tree.bind()
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
    tree.close()
    tree.bind()
    tree.close()


async def test_deferred_commit_to_spawn_cancel_uses_real_facts() -> None:
    event_bus = EventBus()
    tree = LifecycleStateTree(event_bus)
    tree.bind()
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
    tree.bind()
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
    tree.close()
    assert second.wait_for_admission() is False


def main() -> None:
    asyncio.run(test_fact_driven_hierarchy_and_pre_spawn_cancel())
    asyncio.run(test_same_facts_converge_to_same_tree())
    asyncio.run(test_bus_facts_are_serializable_and_tree_rebinds_after_close())
    asyncio.run(test_deferred_commit_to_spawn_cancel_uses_real_facts())
    asyncio.run(test_parent_terminal_and_close_cancel_pending_children())
    print("PASS lifecycle state machines")


if __name__ == "__main__":
    main()
