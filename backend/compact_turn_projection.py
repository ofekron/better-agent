from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable, Iterable, Optional


_TEXT_EVENT_TYPES = {
    "assistant_text",
    "output_text",
    "text",
    "text_delta",
    "text_group",
}
_TEXT_KEYS = ("text", "content", "message")


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


def event_display_summary(event: dict[str, Any]) -> str:
    return _event_text(event)[:160]


def assistant_display_summary(message: dict[str, Any]) -> str:
    return _visible_text(message.get("content"))[:160]


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
    historical_manifest_loader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
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
    boundary_events = [
        deepcopy(event)
        for event in ((assistant or {}).get("events") or [])
        if isinstance(event, dict) and event.get("type") == "model_switched"
    ]
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
            if historical_manifest_loader is not None:
                root_manifest = historical_manifest_loader(assistant)
            if root_manifest is None:
                stub = assistant.get("stub")
                stub = stub if isinstance(stub, dict) else {}
                direct_child_count = stub.get("direct_child_count")
                if not isinstance(direct_child_count, int) or direct_child_count < 0:
                    direct_child_count = (
                        len(assistant.get("events") or []) + len(assistant.get("workers") or [])
                    )
                historical_revision = stub.get("historical_revision")
                if not isinstance(historical_revision, str) or not historical_revision:
                    historical_revision = historical_root_revision(assistant)
                root_manifest = {
                    "id": f"message-{assistant.get('id') or _stable_turn_id([assistant])}",
                    "type": "turn_root",
                    "revision": historical_revision,
                    "direct_child_count": direct_child_count,
                    "display_summary": assistant_text[:160],
                }
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
            "boundary_events": boundary_events,
        },
    }


def build_compact_turn_page(
    messages: list[dict[str, Any]],
    *,
    turn_limit: int,
    before_seq: Optional[int] = None,
    running_message_id: Optional[str] = None,
    revision: str = "",
    historical_manifest_loader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    selected_turns: list[list[dict[str, Any]]] | None = None,
    selected_has_older: bool | None = None,
) -> dict[str, Any]:
    if turn_limit < 1:
        raise ValueError("turn_limit must be positive")
    if selected_turns is None:
        selected, has_older = select_compact_turns(
            messages, turn_limit=turn_limit, before_seq=before_seq,
        )
    else:
        selected = selected_turns
        has_older = bool(selected_has_older)
    projected = [
        _project_turn(
            turn,
            running_message_id=running_message_id,
            revision=revision,
            historical_manifest_loader=historical_manifest_loader,
        )
        for turn in selected
    ]
    oldest_seq = projected[0]["start_seq"] if projected else None
    return {
        "turns": projected,
        "page_cursor": {
            "before_seq": oldest_seq,
            "has_older": has_older,
            "revision": revision,
        },
    }


def select_compact_turns(
    messages: list[dict[str, Any]], *, turn_limit: int,
    before_seq: Optional[int] = None,
) -> tuple[list[list[dict[str, Any]]], bool]:
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
    return selected, len(all_turns) > len(selected)


def compact_session_metadata(session: dict[str, Any]) -> dict[str, Any]:
    projected = {
        key: deepcopy(value)
        for key, value in session.items()
        if key not in {"messages", "root_events", "forks", "max_seq_by_sid"}
    }
    projected["messages"] = []
    projected["forks"] = []
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
