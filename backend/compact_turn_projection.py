from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable, Optional


_TEXT_EVENT_TYPES = {
    "assistant_text",
    "output_text",
    "text",
    "text_delta",
    "text_group",
}
_TEXT_KEYS = ("text", "content", "message")


class ProjectionRejected(ValueError):
    pass


def _message_seq(message: dict[str, Any], index: int) -> int:
    seq = message.get("seq")
    return seq if isinstance(seq, int) else index


def _stable_turn_id(messages: Iterable[dict[str, Any]]) -> str:
    materialized = list(messages)
    anchor = next(
        (message for message in materialized if message.get("role") == "user"),
        materialized[0] if materialized else {},
    )
    identity = {"id": anchor.get("id"), "seq": anchor.get("seq")}
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return f"turn-{digest[:24]}"


def _visible_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_visible_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("type") in {"text", "output_text"}:
            return _visible_text(value.get("text"))
    return ""


def _event_text(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    data = event.get("data")
    if event_type == "agent_message" and isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, dict):
            return _visible_text(message.get("content"))
        return _visible_text(message)
    if event_type not in _TEXT_EVENT_TYPES:
        return ""
    if isinstance(data, dict):
        for key in _TEXT_KEYS:
            text = _visible_text(data.get(key))
            if text:
                return text
    return _visible_text(data)


def _content_revision(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def historical_root_revision(message: dict[str, Any]) -> str:
    stub = message.get("stub")
    event_count = (
        int(stub.get("event_count") or 0)
        if isinstance(stub, dict)
        else len(message.get("events") or [])
    )
    return _content_revision({
        "message_id": message.get("id"),
        "seq": message.get("seq"),
        "event_count": event_count,
        "worker_count": len(message.get("workers") or []),
    })


def _canonical_event_uuid(event: dict[str, Any]) -> Optional[str]:
    for owner in (event, event.get("data")):
        if not isinstance(owner, dict):
            continue
        for key in ("uuid", "id", "event_id"):
            value = owner.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _canonical_parent_uuid(event: dict[str, Any]) -> Optional[str]:
    for owner in (event, event.get("data")):
        if not isinstance(owner, dict):
            continue
        for key in ("parentUuid", "parent_uuid", "parent_id"):
            value = owner.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _historical_nodes(message: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    message_id = str(message.get("id") or _stable_turn_id([message]))
    root_id = f"message-{message_id}"
    nodes: dict[str, dict[str, Any]] = {}
    uuid_to_id: dict[str, str] = {}

    def add_event(event: dict[str, Any], index_key: str, default_parent: str) -> None:
        canonical_uuid = _canonical_event_uuid(event)
        stable_source = canonical_uuid or f"{message_id}:{index_key}"
        node_id = f"event-{hashlib.sha256(stable_source.encode()).hexdigest()[:24]}"
        if canonical_uuid:
            uuid_to_id[canonical_uuid] = node_id
        nodes[node_id] = {
            "id": node_id,
            "canonical_uuid": canonical_uuid,
            "canonical_parent_uuid": _canonical_parent_uuid(event),
            "default_parent": default_parent,
            "type": str(event.get("type") or "event"),
            "revision": _content_revision(event),
            "display_summary": (_event_text(event) or str(event.get("type") or "event"))[:160],
            "render_payload": deepcopy(event),
        }

    for index, event in enumerate(message.get("events") or []):
        if isinstance(event, dict):
            add_event(event, f"event:{index}", root_id)
    for worker_index, worker in enumerate(message.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        worker_source = str(
            worker.get("delegation_id") or worker.get("id") or f"{message_id}:worker:{worker_index}"
        )
        worker_id = f"worker-{hashlib.sha256(worker_source.encode()).hexdigest()[:24]}"
        nodes[worker_id] = {
            "id": worker_id,
            "canonical_uuid": worker.get("delegation_id") or worker.get("id"),
            "canonical_parent_uuid": None,
            "default_parent": root_id,
            "type": "worker",
            "revision": _content_revision(worker),
            "display_summary": str(worker.get("name") or worker.get("label") or "worker")[:160],
            "render_payload": {
                **deepcopy(worker),
                "events": [],
            },
        }
        for event_index, event in enumerate(worker.get("events") or []):
            if isinstance(event, dict):
                add_event(event, f"worker:{worker_index}:event:{event_index}", worker_id)

    for node in nodes.values():
        parent_uuid = node.pop("canonical_parent_uuid")
        node["parent_id"] = uuid_to_id.get(parent_uuid, node.pop("default_parent"))
        node.pop("canonical_uuid", None)
    return root_id, nodes


def _historical_manifest(node: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    node_id = node["id"]
    return {
        "id": node_id,
        "type": node["type"],
        "revision": node["revision"],
        "direct_child_count": sum(1 for child in nodes.values() if child["parent_id"] == node_id),
        "display_summary": node["display_summary"],
    }


def _historical_child(node: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    manifest = _historical_manifest(node, nodes)
    child_manifests = [
        _historical_manifest(child, nodes)
        for child in nodes.values()
        if child["parent_id"] == node["id"]
    ]
    return {
        **manifest,
        "render_payload": deepcopy(node["render_payload"]),
        "child_manifests": child_manifests,
    }


def historical_root_manifest(message: dict[str, Any]) -> dict[str, Any]:
    root_id, nodes = _historical_nodes(message)
    root = {
        "id": root_id,
        "type": "turn_root",
        "revision": historical_root_revision(message),
        "display_summary": _visible_text(message.get("content"))[:160],
    }
    return _historical_manifest(root, nodes)


def project_historical_children(
    message: dict[str, Any],
    *,
    parent_id: str,
    expected_revision: str,
) -> dict[str, Any]:
    root_id, nodes = _historical_nodes(message)
    if parent_id == root_id:
        parent = {
            "id": root_id,
            "type": "turn_root",
            "revision": historical_root_revision(message),
            "display_summary": _visible_text(message.get("content"))[:160],
        }
    else:
        parent = nodes.get(parent_id)
    if parent is None or parent["revision"] != expected_revision:
        raise ProjectionRejected("unknown parent or revision mismatch")
    children = [
        _historical_child(node, nodes)
        for node in nodes.values()
        if node["parent_id"] == parent_id
    ]
    return {"parent": _historical_manifest(parent, nodes), "children": children}


def _running_text_groups(message: dict[str, Any], revision: str) -> list[dict[str, Any]]:
    groups = []
    for index, event in enumerate(message.get("events") or []):
        if not isinstance(event, dict):
            continue
        text = _event_text(event)
        if not text:
            continue
        event_id = event.get("uuid")
        if not isinstance(event_id, str):
            data = event.get("data")
            event_id = data.get("uuid") if isinstance(data, dict) else None
        stable_source = event_id or f"{message.get('id')}:{index}"
        group_id = hashlib.sha256(str(stable_source).encode()).hexdigest()[:24]
        groups.append({
            "id": f"text-{group_id}",
            "type": "text_group",
            "revision": revision,
            "direct_child_count": 0,
            "display_summary": text[:160],
            "text": text,
        })
    return groups


def _turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "user":
            if current:
                turns.append(current)
            current = [message]
            continue
        if current and current[0].get("role") == "user":
            current.append(message)
            continue
        if current:
            turns.append(current)
        current = [message]
    if current:
        turns.append(current)
    return turns


def _project_turn(
    source: list[dict[str, Any]],
    *,
    running_message_id: Optional[str],
    revision: str,
) -> dict[str, Any]:
    user = next((message for message in source if message.get("role") == "user"), None)
    assistants = [message for message in source if message.get("role") == "assistant"]
    assistant = assistants[-1] if assistants else None
    running = bool(
        assistant
        and (
            assistant.get("id") == running_message_id
            or assistant.get("isStreaming") is True
        )
    )
    assistant_text = _visible_text(assistant.get("content")) if assistant else ""
    manifests = _running_text_groups(assistant, revision) if running and assistant else []
    actionable_cards = []
    if assistant and isinstance(assistant.get("ask_result"), dict):
        actionable_cards.append({
            "type": "propose_sessions",
            "status": "resolved" if assistant.get("chosen_session_id") else "pending",
            "ask_result": deepcopy(assistant["ask_result"]),
            "chosen_session_id": assistant.get("chosen_session_id"),
        })
    root_manifest = None
    if assistant:
        if running:
            events = assistant.get("events")
            direct_child_count = len(events) if isinstance(events, list) else 0
            root_manifest = {
                "id": f"message-{assistant.get('id') or _stable_turn_id([assistant])}",
                "type": "turn_root",
                "revision": historical_root_revision(assistant),
                "direct_child_count": direct_child_count,
                "display_summary": assistant_text[:160],
            }
        else:
            root_manifest = historical_root_manifest(assistant)
            stub = assistant.get("stub")
            if isinstance(stub, dict):
                root_manifest["direct_child_count"] = int(
                    stub.get("event_count") or 0
                )
    seqs = [message.get("seq") for message in source if isinstance(message.get("seq"), int)]
    return {
        "id": _stable_turn_id(source),
        "start_seq": min(seqs) if seqs else None,
        "end_seq": max(seqs) if seqs else None,
        "prompt": {
            "id": user.get("id") if user else None,
            "content": _visible_text(user.get("content")) if user else "",
        },
        "assistant": {
            "id": assistant.get("id") if assistant else None,
            "final_visible_text": assistant_text,
            "running": running,
            "hydration_root": root_manifest,
            "visible_text_groups": manifests,
            "actionable_cards": actionable_cards,
        },
    }


def build_compact_turn_page(
    messages: list[dict[str, Any]],
    *,
    turn_limit: int,
    before_seq: Optional[int] = None,
    running_message_id: Optional[str] = None,
    revision: str = "",
) -> dict[str, Any]:
    if turn_limit < 1:
        raise ValueError("turn_limit must be positive")
    ordered = sorted(
        enumerate(messages),
        key=lambda item: (_message_seq(item[1], item[0]), item[0]),
    )
    eligible = [
        message for index, message in ordered
        if before_seq is None or _message_seq(message, index) < before_seq
    ]
    all_turns = _turns(eligible)
    selected = all_turns[-turn_limit:]
    projected = [
        _project_turn(
            turn,
            running_message_id=running_message_id,
            revision=revision,
        )
        for turn in selected
    ]
    oldest_seq = projected[0]["start_seq"] if projected else None
    has_older = len(all_turns) > len(selected)
    return {
        "turns": projected,
        "page_cursor": {
            "before_seq": oldest_seq,
            "has_older": has_older,
            "revision": revision,
        },
    }


def compact_session_metadata(session: dict[str, Any]) -> dict[str, Any]:
    projected = {
        key: deepcopy(value)
        for key, value in session.items()
        if key not in {"messages", "root_events", "forks", "max_seq_by_sid"}
    }
    projected["messages"] = []
    projected["forks"] = [
        compact_session_metadata(fork)
        for fork in session.get("forks") or []
        if isinstance(fork, dict)
    ]
    return projected


def project_compact_turn_for_message(
    messages: list[dict[str, Any]],
    *,
    message_id: str,
    revision: str = "",
) -> dict[str, Any] | None:
    ordered = [
        message for _index, message in sorted(
            enumerate(messages),
            key=lambda item: (_message_seq(item[1], item[0]), item[0]),
        )
    ]
    for turn in _turns(ordered):
        if any(message.get("id") == message_id for message in turn):
            return _project_turn(
                turn,
                running_message_id=None,
                revision=revision,
            )
    return None
