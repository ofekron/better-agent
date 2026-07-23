"""Regression test for the target-turn-terminal ask completion writer.

Locks the fix for the bug where a caller's own turn could reach
`lifecycle.turn_complete` while its outstanding `ask` was still parked in
`_ask_team_message_wait`, permanently orphaning the wait: `ask_delivery` had
no writer of `result` from the target side (unlike the fork/delegate_task
path's `_with_ask_delivery`, which always writes from the callee side), so
`deliver_if_needed` could never populate `fallback_message` and the caller
hung until the 24h timeout. `ask_delivery.on_target_turn_terminal` now
writes the result the moment the target's turn ends, regardless of caller
liveness — this must converge whichever of the two triggers (target-done,
caller-terminal) fires first.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ask-target-terminal-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_delivery
import ask_status_store
import inbox_store
from event_bus import BusEvent
from orchestrator import Coordinator
from session_manager import manager as session_manager

_coordinator = Coordinator()


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _seed_ask(
    ask_id: str,
    *,
    sender_session_id: str,
    target_session_id: str,
    lifecycle_msg_id: str,
    queue_item_id: str,
) -> None:
    ask_status_store.claim_route(
        ask_id,
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
    )
    ask_status_store.write_status(
        ask_id,
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id=queue_item_id,
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
    )


def _complete_target_turn(target_id: str, lifecycle_msg_id: str, content: str = "CONVERGED") -> None:
    session_manager.append_user_msg(target_id, {
        "id": f"user-{lifecycle_msg_id}",
        "role": "user",
        "content": "review this",
        "events": [],
        "timestamp": "2026-07-23T10:00:00",
        "lifecycle_msg_id": lifecycle_msg_id,
    })
    session_manager.append_assistant_msg(target_id, {
        "id": f"assistant-{lifecycle_msg_id}",
        "role": "assistant",
        "content": content,
        "events": [],
        "timestamp": "2026-07-23T10:00:01",
        "completed_at": "2026-07-23T10:00:01",
    })


def _done_event(target_id: str, lifecycle_msg_id: str) -> BusEvent:
    root_id = session_manager._root_id_for(target_id)
    return BusEvent(
        type="user_message_done",
        root_id=root_id,
        sid=target_id,
        msg_id=lifecycle_msg_id,
        payload={"lifecycle_msg_id": lifecycle_msg_id, "success": True},
    )


def test_lifecycle_index_roundtrip():
    ask_status_store.write_status(
        "ask_idx1", lifecycle_msg_id="life-idx1", target_session_id="t",
    )
    assert ask_status_store.find_ask_id_by_lifecycle("life-idx1") == "ask_idx1"
    ask_status_store.delete_status("ask_idx1")
    assert ask_status_store.find_ask_id_by_lifecycle("life-idx1") is None


def test_on_target_turn_terminal_ignores_unknown_lifecycle():
    """No ask ever claimed this lifecycle_msg_id (fresh uuid4, no match) —
    must be a clean no-op, not create a stray ask-status file."""
    event = _done_event("nonexistent-session", "life-unclaimed")
    asyncio.run(ask_delivery.on_target_turn_terminal(event))
    assert ask_status_store.find_ask_id_by_lifecycle("life-unclaimed") is None


def test_target_done_then_caller_terminal_converges():
    """Order A: target finishes first, caller's own turn completes second —
    this is the ordering that already worked before the fix."""
    sender = session_manager.create(name="caller A", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target A", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-order-a"
    ask_id = "ask_order_a"
    _seed_ask(
        ask_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="q-order-a",
    )
    _complete_target_turn(target["id"], lifecycle_msg_id)

    asyncio.run(ask_delivery.on_target_turn_terminal(_done_event(target["id"], lifecycle_msg_id)))
    status = ask_status_store.read_status(ask_id)
    assert status["result"]["success"] is True
    assert status["delivery"]["fallback_message"]
    assert status["delivery"]["caller_terminal"] is False

    asyncio.run(ask_delivery.mark_caller_terminal(sender["id"]))

    assert ask_status_store.read_status(ask_id) is None, "must be delivered+deleted, not left waiting"
    inbox = inbox_store.read_new(recipient_session_id=sender["id"])
    assert inbox["count"] == 1
    assert inbox["new_messages"][0]["delivery_id"] == f"ask:{ask_id}"


def test_caller_terminal_then_target_done_converges():
    """Order B (the bug): caller's own turn completes FIRST, orphaning the
    live wait, target finishes second. Before the fix this hop stayed in
    `waiting` state forever with no result and no inbox delivery."""
    sender = session_manager.create(name="caller B", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target B", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-order-b"
    ask_id = "ask_order_b"
    _seed_ask(
        ask_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="q-order-b",
    )

    asyncio.run(ask_delivery.mark_caller_terminal(sender["id"]))
    status = ask_status_store.read_status(ask_id)
    assert status is not None
    assert status["delivery"]["caller_terminal"] is True
    assert status.get("result") is None, "no result yet — this is the pre-fix hang condition"

    _complete_target_turn(target["id"], lifecycle_msg_id)
    asyncio.run(ask_delivery.on_target_turn_terminal(_done_event(target["id"], lifecycle_msg_id)))

    assert ask_status_store.read_status(ask_id) is None, "must self-heal via write_status_async's tail check"
    inbox = inbox_store.read_new(recipient_session_id=sender["id"])
    assert inbox["count"] == 1
    assert inbox["new_messages"][0]["delivery_id"] == f"ask:{ask_id}"


def test_on_target_turn_terminal_reports_failure():
    sender = session_manager.create(name="caller C", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target C", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-order-c"
    ask_id = "ask_order_c"
    _seed_ask(
        ask_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="q-order-c",
    )
    root_id = session_manager._root_id_for(target["id"])
    failed_event = BusEvent(
        type="user_message_failed",
        root_id=root_id,
        sid=target["id"],
        msg_id=lifecycle_msg_id,
        payload={"lifecycle_msg_id": lifecycle_msg_id, "reason": "boom"},
    )
    asyncio.run(ask_delivery.on_target_turn_terminal(failed_event))
    status = ask_status_store.read_status(ask_id)
    assert status["result"]["success"] is False
    assert status["result"]["error"] == "boom"
    ask_status_store.delete_status(ask_id)


def test_on_target_turn_terminal_matches_live_path_success_semantics():
    """`user_message_done` is success regardless of `payload["success"]` —
    matching orchestrator.py's live wait_callback (`event.get("type") ==
    "user_message_done"` alone, no `payload["success"]` check), since
    `emit_done` can legitimately carry `success=False` for a recorded
    sub-turn failure without failing the overall turn. The target-side
    writer must agree with the live path on the identical event, or the
    two "single sources of truth" for the same ask would diverge."""
    sender = session_manager.create(name="caller D", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target D", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-order-d"
    ask_id = "ask_order_d"
    _seed_ask(
        ask_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="q-order-d",
    )
    _complete_target_turn(target["id"], lifecycle_msg_id)
    root_id = session_manager._root_id_for(target["id"])
    done_event_with_false_success = BusEvent(
        type="user_message_done",
        root_id=root_id,
        sid=target["id"],
        msg_id=lifecycle_msg_id,
        payload={"lifecycle_msg_id": lifecycle_msg_id, "success": False},
    )
    asyncio.run(ask_delivery.on_target_turn_terminal(done_event_with_false_success))
    status = ask_status_store.read_status(ask_id)
    assert status["result"]["success"] is True
    ask_status_store.delete_status(ask_id)


def test_bus_publish_end_to_end_wires_the_subscriber():
    """Proves the actual `bind_ask_delivery()` wiring, not just the handler
    function in isolation — a typo in the subscribed event name or a broken
    `bus.subscribe` call would be invisible to the direct-call tests above."""
    from event_bus import bus
    from event_bus_subscribers import bind_ask_delivery

    bind_ask_delivery()

    sender = session_manager.create(name="caller E", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target E", cwd="/repo", orchestration_mode="native")
    lifecycle_msg_id = "life-order-e"
    ask_id = "ask_order_e"
    _seed_ask(
        ask_id,
        sender_session_id=sender["id"],
        target_session_id=target["id"],
        lifecycle_msg_id=lifecycle_msg_id,
        queue_item_id="q-order-e",
    )
    asyncio.run(ask_delivery.mark_caller_terminal(sender["id"]))
    _complete_target_turn(target["id"], lifecycle_msg_id)
    root_id = session_manager._root_id_for(target["id"])

    async def _publish():
        await bus.publish(BusEvent(
            type="user_message_done",
            root_id=root_id,
            sid=target["id"],
            msg_id=lifecycle_msg_id,
            payload={"lifecycle_msg_id": lifecycle_msg_id, "success": True},
        ))

    asyncio.run(_publish())

    assert ask_status_store.read_status(ask_id) is None, "wiring must self-heal via the real bus, not just the handler"
    inbox = inbox_store.read_new(recipient_session_id=sender["id"])
    assert inbox["count"] == 1
    assert inbox["new_messages"][0]["delivery_id"] == f"ask:{ask_id}"
