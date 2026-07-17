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

from canonical_event_adapter import walk_session_nodes

_HEAVY_MESSAGE_FIELDS = frozenset({"events", "manager"})
_HYDRATION_ROOT_FIELDS = {
    "id": str, "type": str, "revision": str,
    "direct_child_count": int, "display_summary": str,
}


def strip_heavy_message_fields(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if k not in _HEAVY_MESSAGE_FIELDS}


def historical_hydration_root_of(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Frontend-shaped expansion manifest from a runtime message snapshot.

    Fail closed: anything that is not exactly the
    {id, type, revision, direct_child_count, display_summary} object the
    frontend gate consumes maps to None rather than a partial shape."""
    if not isinstance(snapshot, dict):
        return None
    manifest = snapshot.get("historical_hydration_root")
    if not isinstance(manifest, dict):
        return None
    for field, kind in _HYDRATION_ROOT_FIELDS.items():
        value = manifest.get(field)
        if not isinstance(value, kind) or isinstance(value, bool):
            return None
        if kind is int and value < 0:
            return None
    return {field: manifest[field] for field in _HYDRATION_ROOT_FIELDS}


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
    snapshot_by_id: dict[str, dict[str, Any]] = {}
    # Root-first walk: fork panes carry their own tail messages; copied
    # prefix ids resolve to the root's snapshot (first write wins).
    for node in walk_session_nodes(dict(session)):
        for message in node.get("messages") or []:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id:
                continue
            if message_id in snapshot_by_id:
                continue
            snapshot_seq[message_id] = message.get("seq")
            if isinstance(message.get("run_meta"), dict):
                run_meta[message_id] = message["run_meta"]
            snapshot_by_id[message_id] = strip_heavy_message_fields(message)
    lookup: dict[str, dict[str, Any]] = {}
    for message in adapted_messages:
        if message["id"] not in referenced:
            continue
        snapshot = snapshot_by_id.get(message["id"])
        lookup[message["id"]] = {
            "kind": "message",
            "role": message["role"],
            "text": message["content"],
            "seq": snapshot_seq.get(message["id"], message["seq"]),
            # Runtime snapshot passthrough (minus heavy payloads): liveness,
            # hydration pointers, run_meta — everything the client's
            # ChatMessage needs beyond tree structure.
            "snapshot": snapshot,
            # Canonical carrier for the three-dot expansion gate; the
            # frontend maps this onto ChatMessage.historical_hydration_root.
            "historical_hydration_root": historical_hydration_root_of(snapshot),
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
            snapshot = snapshot_by_id.get(message["id"])
            lookup[message["id"]] = {
                "kind": "message",
                "role": message["role"],
                "text": message["content"],
                "seq": snapshot_seq.get(message["id"], message["seq"]),
                "snapshot": snapshot,
                "historical_hydration_root": historical_hydration_root_of(snapshot),
            }
    return lookup
