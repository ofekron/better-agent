from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home


_TMP_HOME = _test_home.isolate("bc-test-ask-delivery-")

import ask_delivery  # noqa: E402
import ask_status_store  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
import inbox_store  # noqa: E402
from event_bus import BusEvent  # noqa: E402
from orchs.manager._delegation import run_delegation  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _session(name: str) -> str:
    return session_manager.create(
        name=name,
        cwd="/tmp",
        orchestration_mode="native",
    )["id"]


def _result(text: str) -> dict:
    return {"success": True, "assistant_content": text}


def _persist_receipt(caller: str, ask_id: str, *, error_prefix: bool = False) -> None:
    payload = {"success": True, "assistant_content": "answer", "ask_id": ask_id}
    content = ("Error: " if error_prefix else "") + json.dumps(payload)
    event_ingester.ingest(
        caller,
        caller,
        "agent_message",
        {
            "uuid": str(uuid.uuid4()),
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": content}],
            },
        },
        source="test",
    )


async def _run() -> None:
    caller = _session("caller")
    target = _session("target")

    received_id = "ask_received"
    ask_status_store.write_status(
        received_id,
        sender_session_id=caller,
        target_session_id=target,
    )
    received = _result("inline")
    ask_status_store.write_status(received_id, result=received)
    assert received["ask_id"] == received_id
    _persist_receipt(caller, received_id)
    await ask_delivery.mark_caller_terminal(caller)
    assert inbox_store.read_new(recipient_session_id=caller)["count"] == 0

    worker_inner_id = "ask_worker_inner"
    ask_delivery.prepare(worker_inner_id, caller)
    await ask_delivery.on_caller_terminal(BusEvent(
        type="lifecycle.turn_complete",
        root_id=caller,
        sid=caller,
        payload={"reason": "worker_inner"},
        persist=False,
    ))
    worker_inner_status = ask_status_store.read_status(worker_inner_id)
    assert worker_inner_status["delivery"]["caller_terminal"] is False
    ask_status_store.delete_status(worker_inner_id)
    assert ask_status_store.read_status(received_id) is None

    result_first_id = "ask_result_first"
    ask_delivery.prepare(result_first_id, caller)
    ask_delivery.set_result(result_first_id, _result("fallback one"), target)
    await ask_delivery.mark_caller_terminal(caller)
    await ask_delivery.mark_caller_terminal(caller)
    result_first = inbox_store.read_new(recipient_session_id=caller)
    assert result_first["count"] == 1
    assert result_first["new_messages"][0]["text"] == "fallback one"

    terminal_first_id = "ask_terminal_first"
    ask_delivery.prepare(terminal_first_id, caller)
    await ask_delivery.mark_caller_terminal(caller)
    terminal_first = await ask_delivery.complete(
        terminal_first_id,
        _result("fallback two"),
        target,
        caller_session_id=caller,
        caller_active=False,
    )
    assert terminal_first["ask_id"] == terminal_first_id
    terminal_messages = inbox_store.read_new(recipient_session_id=caller)
    assert terminal_messages["count"] == 1
    assert terminal_messages["new_messages"][0]["text"] == "fallback two"

    direct_terminal_first_id = "ask_direct_terminal_first"
    await ask_status_store.write_status_async(
        direct_terminal_first_id,
        sender_session_id=caller,
        target_session_id=target,
    )
    await ask_delivery.mark_caller_terminal(caller)
    await ask_status_store.write_status_async(
        direct_terminal_first_id,
        result=_result("fallback three"),
    )
    direct_terminal_messages = inbox_store.read_new(recipient_session_id=caller)
    assert direct_terminal_messages["count"] == 1
    assert direct_terminal_messages["new_messages"][0]["text"] == "fallback three"

    prefixed_id = "ask_prefixed_receipt"
    ask_delivery.prepare(prefixed_id, caller)
    ask_delivery.set_result(prefixed_id, _result("error receipt"), target)
    _persist_receipt(caller, prefixed_id, error_prefix=True)
    await ask_delivery.mark_caller_terminal(caller)
    assert inbox_store.read_new(recipient_session_id=caller)["count"] == 0

    unknown = {
        "type": "tool_result",
        "content": '{"ask_id":"../../not-a-status"}',
    }
    assert ask_delivery.receipt_ids_from_event(unknown) == set()
    await ask_delivery.on_journal_written(type("Event", (), {
        "payload": {"data": unknown},
        "sid": caller,
    })())
    assert not (ask_status_store.status_path("ask_never_created").exists())

    class _TurnManager:
        @staticmethod
        def has_active_turn(_session_id: str) -> bool:
            return True

    class _Coordinator:
        turn_manager = _TurnManager()

    fork_error_id = "del_fork_early_error"
    fork_error = await run_delegation(
        _Coordinator(),
        caller,
        "invalid run mode",
        target,
        "target",
        "",
        "/tmp",
        client_delegation_id=fork_error_id,
        run_mode="invalid",
        ask_mode="wait_and_grab_last_assistant_mssg_in_turn",
    )
    assert fork_error["success"] is False
    assert fork_error["ask_id"] == fork_error_id
    ask_status_store.delete_status(fork_error_id)


def main() -> None:
    try:
        asyncio.run(_run())
        print("PASS: ask delivery fallback")
    finally:
        event_ingester.close_all()


if __name__ == "__main__":
    main()
