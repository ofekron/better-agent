"""Shared chat-tree lookup sidecar builder.

Extracted from `bff_chat_tree.py` so the durable REST read path
(`get_chat_tree`) and the ephemeral live-delta path
(`bff_current_turn_cache`/`bff_current_turn_feed`) build the lookup
sidecar identically — one implementation, not two copies that can
drift (the exact bug this project has already had to fix once for
rendering rules; the same guard applies to lookup construction).
"""
from __future__ import annotations

from typing import Any

_HEAVY_MESSAGE_FIELDS = frozenset({"events", "manager"})


def strip_heavy_message_fields(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if k not in _HEAVY_MESSAGE_FIELDS}


def referenced_ids(items: list[dict[str, Any]]) -> set[str]:
    referenced: set[str] = set()
    stack: list[dict[str, Any]] = list(items)
    while stack:
        item = stack.pop()
        kind = item.get("type")
        if kind == "ModelChange":
            referenced.add(item["id"])
            continue
        if kind in ("Turn", "NativeSubagentTurn", "WorkerTurn"):
            if kind == "Turn":
                referenced.add(item["prompt"])
            else:
                referenced.add(item["id"])
                referenced.update(item.get("children") or [])
            result = item.get("result")
            if result:
                referenced.update(result.get("part_ids") or [])
            stack.extend(item.get("body") or [])
            continue
        if kind == "Explanation":
            referenced.update(item.get("text_event_ids") or [])
            referenced.update(item.get("item_ids") or [])
            continue
        if kind == "SteeringMessage":
            referenced.add(item["id"])
    return referenced


def build_lookup(
    items: list[dict[str, Any]],
    adapted_messages: tuple[dict[str, Any], ...],
    adapted_events: tuple[dict[str, Any], ...],
    session: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Content sidecar for the ids the window references: the wire tree
    carries structure and ids; renderers look content up here
    (lookupForRender). Seqs come from the runtime session snapshot so
    live WS messages interleave correctly on the client."""
    referenced = referenced_ids(items)
    snapshot_seq: dict[str, Any] = {}
    run_meta: dict[str, Any] = {}
    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            snapshot_seq[message_id] = message.get("seq")
            if isinstance(message.get("run_meta"), dict):
                run_meta[message_id] = message["run_meta"]
    snapshot_by_id: dict[str, dict[str, Any]] = {}
    for message in session.get("messages") or []:
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            snapshot_by_id[message["id"]] = strip_heavy_message_fields(message)
    lookup: dict[str, dict[str, Any]] = {}
    for message in adapted_messages:
        if message["id"] not in referenced:
            continue
        lookup[message["id"]] = {
            "kind": "message",
            "role": message["role"],
            "text": message["content"],
            "seq": snapshot_seq.get(message["id"], message["seq"]),
            # Runtime snapshot passthrough (minus heavy payloads): liveness,
            # hydration pointers, run_meta — everything the client's
            # ChatMessage needs beyond tree structure.
            "snapshot": snapshot_by_id.get(message["id"]),
        }
    owning_messages: set[str] = set()
    for event in adapted_events:
        if event["event_id"] not in referenced:
            continue
        message_id = event.get("message_id")
        if isinstance(message_id, str) and message_id:
            owning_messages.add(message_id)
        lookup[event["event_id"]] = {
            "kind": "event",
            "type": event["type"],
            "data": event["data"],
            "message_id": message_id,
            "timestamp": event["timestamp"],
            "message_seq": snapshot_seq.get(message_id) if isinstance(message_id, str) else None,
            "run_meta": run_meta.get(message_id) if isinstance(message_id, str) else None,
        }
    # Messages the window's events belong to (assistant rows): the tree
    # references event ids, but the client's message shape needs the
    # owning message's snapshot too.
    for message in adapted_messages:
        if message["id"] in owning_messages and message["id"] not in lookup:
            lookup[message["id"]] = {
                "kind": "message",
                "role": message["role"],
                "text": message["content"],
                "seq": snapshot_seq.get(message["id"], message["seq"]),
                "snapshot": snapshot_by_id.get(message["id"]),
            }
    return lookup
