from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import ask_status_store
from event_ingester import event_ingester
from event_journal import event_journal_writer
import inbox_store


logger = logging.getLogger(__name__)

_WAITING = "waiting"
_PENDING = "pending"
_RECEIVED = "received"
_INBOXED = "inboxed"


def prepare(ask_id: str, caller_session_id: str) -> None:
    if not ask_id:
        return
    ask_status_store.write_status(ask_id, sender_session_id=caller_session_id)


def set_result(
    ask_id: str,
    result: dict[str, Any],
    fallback_sender_session_id: str,
) -> dict[str, Any]:
    response = {**result, "ask_id": ask_id}
    ask_status_store.write_status(
        ask_id,
        target_session_id=fallback_sender_session_id,
        result=response,
    )
    return response


def _receipt_id_from_value(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith("Error: "):
            value = value.removeprefix("Error: ")
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return ""
    if isinstance(value, dict):
        ask_id = value.get("ask_id")
        return str(ask_id or "").strip()
    return ""


def receipt_ids_from_event(data: Any) -> set[str]:
    receipt_ids: set[str] = set()

    def _visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "tool_result":
                content = value.get("content")
                receipt_id = _receipt_id_from_value(content)
                if ask_status_store.is_valid_ask_id(receipt_id):
                    receipt_ids.add(receipt_id)
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            receipt_id = _receipt_id_from_value(item.get("text"))
                            if ask_status_store.is_valid_ask_id(receipt_id):
                                receipt_ids.add(receipt_id)
                return
            for child in value.values():
                _visit(child)
        elif isinstance(value, list):
            for child in value:
                _visit(child)

    _visit(data)
    return receipt_ids


def mark_received(ask_id: str, caller_session_id: str) -> None:
    status = ask_status_store.read_status(ask_id)
    delivery = dict((status or {}).get("delivery") or {})
    if delivery.get("caller_session_id") != caller_session_id:
        return

    def _update(status: dict[str, Any]) -> dict[str, Any]:
        delivery = dict(status.get("delivery") or {})
        if delivery.get("caller_session_id") != caller_session_id:
            return status
        if delivery.get("state") != _INBOXED:
            delivery["state"] = _RECEIVED
            status["delivery"] = delivery
        return status

    updated = ask_status_store.update_status(ask_id, _update)
    if dict(updated.get("delivery") or {}).get("state") == _RECEIVED:
        ask_status_store.delete_status(ask_id)


def _journal_has_receipt(ask_id: str, delivery: dict[str, Any]) -> bool:
    root_id = str(delivery.get("caller_root_id") or "")
    caller_session_id = str(delivery.get("caller_session_id") or "")
    if not root_id or not caller_session_id:
        return False
    rows, _total, _has_more = event_ingester.read_events(
        root_id,
        after_seq=int(delivery.get("journal_after_seq") or 0),
        limit=999_999,
        sid_filter=caller_session_id,
    )
    return any(ask_id in receipt_ids_from_event(row.get("data")) for row in rows)


async def deliver_if_needed(ask_id: str) -> None:
    status = await asyncio.to_thread(ask_status_store.read_status, ask_id)
    delivery = dict((status or {}).get("delivery") or {})
    if delivery.get("state") not in {_PENDING, _WAITING}:
        return
    if not delivery.get("caller_terminal") or not delivery.get("fallback_message"):
        return
    root_id = str(delivery.get("caller_root_id") or "")
    if root_id:
        await event_journal_writer.barrier(root_id)
    if await asyncio.to_thread(_journal_has_receipt, ask_id, delivery):
        await asyncio.to_thread(
            mark_received,
            ask_id,
            str(delivery.get("caller_session_id") or ""),
        )
        return
    await asyncio.to_thread(
        inbox_store.send,
        sender_session_id=str(delivery.get("fallback_sender_session_id") or ""),
        recipient_session_id=str(delivery.get("caller_session_id") or ""),
        message=str(delivery.get("fallback_message") or ""),
        delivery_id=f"ask:{ask_id}",
    )

    def _mark_inboxed(current: dict[str, Any]) -> dict[str, Any]:
        current_delivery = dict(current.get("delivery") or {})
        current_delivery["state"] = _INBOXED
        current["delivery"] = current_delivery
        return current

    await asyncio.to_thread(ask_status_store.update_status, ask_id, _mark_inboxed)
    await asyncio.to_thread(ask_status_store.delete_status, ask_id)


async def complete(
    ask_id: str,
    result: dict[str, Any],
    fallback_sender_session_id: str,
    *,
    caller_session_id: str,
    caller_active: bool,
) -> dict[str, Any]:
    response = await asyncio.to_thread(
        set_result,
        ask_id,
        result,
        fallback_sender_session_id,
    )
    if not caller_active:
        await mark_caller_terminal(caller_session_id)
    return response


async def mark_caller_terminal(caller_session_id: str) -> None:
    if not caller_session_id:
        return
    statuses = await asyncio.to_thread(ask_status_store.list_statuses)
    matching: list[str] = []
    for ask_id, status in statuses:
        delivery = dict(status.get("delivery") or {})
        if delivery.get("caller_session_id") != caller_session_id:
            continue
        if delivery.get("state") in {_RECEIVED, _INBOXED}:
            continue

        def _mark_terminal(current: dict[str, Any]) -> dict[str, Any]:
            current_delivery = dict(current.get("delivery") or {})
            current_delivery["caller_terminal"] = True
            current["delivery"] = current_delivery
            return current

        await asyncio.to_thread(ask_status_store.update_status, ask_id, _mark_terminal)
        matching.append(ask_id)
    for ask_id in matching:
        try:
            await deliver_if_needed(ask_id)
        except Exception:
            logger.exception("ask fallback delivery failed ask_id=%s", ask_id)


async def on_journal_written(event: Any) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    for ask_id in receipt_ids_from_event(payload.get("data")):
        await asyncio.to_thread(mark_received, ask_id, event.sid)


async def on_caller_terminal(event: Any) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if payload.get("reason") == "worker_inner":
        return
    await mark_caller_terminal(event.sid)


async def on_target_turn_terminal(event: Any) -> None:
    """Write the ask result the moment the TARGET session's turn ends,
    independent of whether the caller's own `_ask_team_message_wait`
    coroutine is still alive to observe it live. Mirrors the fork/
    delegate_task path (`_delegation.py`'s `_with_ask_delivery`), where the
    callee side always writes the result — the plain-`ask` flow previously
    had no equivalent, so a caller whose own turn completed while its ask
    was still outstanding (`ask_delivery.mark_caller_terminal`) could never
    get a `fallback_message` to deliver and hung until the 24h timeout."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    lifecycle_msg_id = str(payload.get("lifecycle_msg_id") or event.msg_id or "").strip()
    if not lifecycle_msg_id:
        return
    ask_id = await asyncio.to_thread(ask_status_store.find_ask_id_by_lifecycle, lifecycle_msg_id)
    if not ask_id:
        return
    status = await asyncio.to_thread(ask_status_store.read_status, ask_id)
    if not status or status.get("result") is not None:
        return
    if str(status.get("target_session_id") or "") != str(event.sid or ""):
        return
    from orchestrator import get_active_coordinator

    coordinator = get_active_coordinator()
    if coordinator is None:
        return
    target_session_id = str(event.sid or "")
    if event.type == "user_message_done":
        # Match the live path exactly (orchestrator.py's wait_callback):
        # `user_message_done` is success regardless of `payload["success"]`,
        # which can be False for a sub-turn-recorded failure that didn't
        # raise the overall turn. Branching on that field here would make
        # this writer disagree with the live path on the identical event.
        result: dict[str, Any] = {"success": True}
        response = await asyncio.to_thread(
            coordinator._team_message_turn_response,
            target_session_id=target_session_id,
            lifecycle_msg_id=lifecycle_msg_id,
        )
    else:
        result = {
            "success": False,
            "error": payload.get("error") or payload.get("reason") or "target turn failed",
        }
        response = {}
    full = {
        **result,
        "target_session_id": target_session_id,
        "queued_id": str(status.get("queue_item_id") or ""),
        **response,
    }
    await ask_status_store.write_status_async(ask_id, result=full)
