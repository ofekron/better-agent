from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from chat_models import (
    CHAT_SCHEMA_VERSION,
    BodyItem,
    CanonicalEvent,
    Chat,
    Explanation,
    ModelChange,
    ModelMarkerTarget,
    ProviderIdentity,
    Result,
    ScopedTurn,
    SteeringMessage,
    Turn,
    TypedPrompt,
    VisibilityPlan,
)


_TEXT_TYPES = {"assistant_text", "text", "output_text"}
_SCOPED_TYPES = {
    "native_subagent_turn": "NativeSubagentTurn",
    "worker_turn": "WorkerTurn",
}


def project_chat(
    messages: Sequence[Mapping[str, Any]],
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
    *,
    schema_version: int,
) -> Chat:
    canonical = _canonical_events(events, schema_version)
    prompts, message_turns = _validated_messages(messages)
    ownership = _ownership(canonical, message_turns)
    event_by_id = {event.event_id: event for event in canonical}
    _validate_edges(canonical, event_by_id, ownership, message_turns)

    changes = defaultdict(list)
    top_level = defaultdict(list)
    for event in canonical:
        if event.metadata_only:
            continue
        turn_id = _effective_turn_id(event, message_turns, ownership)
        if event.type == "model_change":
            if turn_id in prompts:
                changes[turn_id].append(event)
            continue
        if turn_id not in prompts:
            continue
        if _is_top_level(event, ownership, event_by_id):
            top_level[turn_id].append(event)

    items = []
    for turn_id, prompt in prompts.items():
        items.extend(
            ModelChange(change.event_id, turn_id, change.provider)
            for change in _ordered(changes[turn_id])
        )
        items.append(Turn(
            turn_id,
            prompt,
            *_derive_content(top_level[turn_id], canonical, event_by_id),
        ))
    return Chat(tuple(items))


def model_marker_targets(
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
    visibility_plans: Iterable[VisibilityPlan] | None = None,
    *,
    schema_version: int,
) -> tuple[ModelMarkerTarget, ...]:
    canonical = _canonical_events(events, schema_version)
    visible = {plan.scope: set(plan.visible_event_ids) for plan in visibility_plans or ()}
    by_scope = defaultdict(list)
    for event in canonical:
        if not event.metadata_only and event.type != "model_change":
            by_scope[event.context_id].append(event)

    targets = []
    for scope, events_in_scope in by_scope.items():
        allowed = visible.get(scope)
        if visible and allowed is None:
            continue
        for run in _provider_runs(events_in_scope):
            visible_run = [event for event in run if allowed is None or event.event_id in allowed]
            if visible_run:
                event = visible_run[-1]
                targets.append(ModelMarkerTarget(scope, event.provider, event.event_id))
    event_by_id = {event.event_id: event for event in canonical}
    return tuple(sorted(targets, key=lambda target: _sort_key(event_by_id[target.target_event_id])))


def canonical_quick_reply_text(
    chat: Chat,
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
    *,
    schema_version: int,
) -> str:
    event_by_id = {
        event.event_id: event for event in _canonical_events(events, schema_version)
    }
    for item in reversed(chat.items):
        if not isinstance(item, Turn) or item.result is None:
            continue
        text = item.result.text or "".join(
            _event_text(event_by_id[event_id])
            for event_id in item.result.part_ids
            if event_id in event_by_id and event_by_id[event_id].type in _TEXT_TYPES
        )
        if text:
            return text
    return ""


def _derive_content(
    events: Sequence[CanonicalEvent],
    all_events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
) -> tuple[tuple[BodyItem, ...], Result | None]:
    ordered = _ordered(event for event in events if not event.metadata_only)
    result, result_ids = _resolve_result(ordered, all_events, event_by_id)
    body = _derive_body(
        [event for event in ordered if event.event_id not in result_ids],
        all_events,
        event_by_id,
    )
    return body, result


def _resolve_result(
    events: Sequence[CanonicalEvent],
    all_events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
) -> tuple[Result | None, set[str]]:
    marked = [event for event in events if event.provider_final and event.type not in _SCOPED_TYPES]
    if marked:
        marked_ids = {event.event_id for event in marked}
        associated = [
            event for event in all_events
            if event.type in _TEXT_TYPES
            and (event.event_id in marked_ids or _has_ancestor(event, marked_ids, event_by_id))
        ]
        result_events = _ordered({event.event_id: event for event in marked + associated}.values())
        ids = tuple(event.event_id for event in result_events)
        return Result("ProviderResult", ids, _text_for(result_events)), set(ids)

    trailing = []
    for event in reversed(events):
        if event.type not in _TEXT_TYPES:
            break
        trailing.append(event)
    if trailing:
        trailing.reverse()
        ids = tuple(event.event_id for event in trailing)
        return Result("DerivedResult", ids, _text_for(trailing)), set(ids)
    if not events or events[-1].type in _SCOPED_TYPES or events[-1].type == "steering_message":
        return None, set()
    final = events[-1]
    return Result("DerivedResult", (final.event_id,), _event_text(final)), {final.event_id}


def _derive_body(
    events: Sequence[CanonicalEvent],
    all_events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
) -> tuple[BodyItem, ...]:
    body: list[BodyItem] = []
    partition: list[CanonicalEvent] = []
    explanation_number = 0

    def flush() -> None:
        nonlocal explanation_number
        if not partition:
            return
        explanation_number += 1
        split = next(
            (index for index, event in enumerate(partition) if event.type not in _TEXT_TYPES),
            len(partition),
        )
        text_events, item_events = partition[:split], partition[split:]
        owner = partition[0].turn_id or partition[0].message_id or "root"
        body.append(Explanation(
            f"explanation-{owner}-{explanation_number}",
            _text_for(text_events),
            tuple(event.event_id for event in text_events),
            tuple(event.event_id for event in item_events),
        ))
        partition.clear()

    for event in events:
        if event.type in _SCOPED_TYPES:
            flush()
            body.append(_scoped_turn(event, all_events, event_by_id))
        elif event.type == "steering_message":
            flush()
            body.append(SteeringMessage(event.event_id, _event_text(event)))
        elif event.type in _TEXT_TYPES and any(item.type not in _TEXT_TYPES for item in partition):
            flush()
            partition.append(event)
        else:
            partition.append(event)
    flush()
    return tuple(body)


def _scoped_turn(
    event: CanonicalEvent,
    all_events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
) -> ScopedTurn:
    children = _ordered(
        child for child in all_events
        if child.parent_event_id == event.event_id and not child.metadata_only
    )
    body, result = _derive_content(children, all_events, event_by_id)
    embedded = event.data.get("result") or event.data.get("text")
    if result is None and isinstance(embedded, str) and embedded:
        kind = "ProviderResult" if event.provider_final else "DerivedResult"
        result = Result(kind, (event.event_id,), embedded)
    return ScopedTurn(
        _SCOPED_TYPES[event.type],
        event.event_id,
        TypedPrompt(f"prompt-{event.event_id}", str(event.data.get("prompt") or "")),
        body,
        result,
        tuple(child.event_id for child in children),
    )


def _canonical_events(
    events: Sequence[CanonicalEvent | Mapping[str, Any]], schema_version: int,
) -> tuple[CanonicalEvent, ...]:
    _validate_schema_version(schema_version)
    latest: dict[str, CanonicalEvent] = {}
    positions: dict[str, tuple[str, int]] = {}
    for raw in events:
        event = raw if isinstance(raw, CanonicalEvent) else _event_from_mapping(raw, schema_version)
        if event.schema_version != schema_version:
            raise ValueError("event schema version does not match request")
        position = (event.timestamp, event.sequence)
        positions[event.event_id] = min(positions.get(event.event_id, position), position)
        current = latest.get(event.event_id)
        if current is None or (event.content_version, event.sequence) > (current.content_version, current.sequence):
            latest[event.event_id] = event
    return tuple(_ordered(
        CanonicalEvent(
            event.event_id, positions[event.event_id][0], positions[event.event_id][1],
            event.context_id, event.turn_id, event.message_id, event.parent_event_id,
            event.type, event.data, event.provider, event.provider_final, event.metadata_only,
            event.schema_version, event.content_version,
        )
        for event in latest.values()
    ))


def _event_from_mapping(raw: Mapping[str, Any], schema_version: int) -> CanonicalEvent:
    required = {"event_id", "timestamp", "journal_seq", "context_id", "type", "provider", "data", "content_version"}
    missing = sorted(required - raw.keys())
    if missing:
        raise ValueError(f"missing canonical event fields: {', '.join(missing)}")
    provider = raw["provider"]
    if not isinstance(provider, Mapping):
        raise ValueError("event provider must be an object")
    provider_fields = {"id", "model", "effort"}
    if provider_fields - provider.keys():
        raise ValueError("provider id, model, and effort are required")
    if not isinstance(raw["data"], Mapping):
        raise ValueError("event data must be an object")
    for flag in ("provider_final", "metadata_only"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise ValueError(f"{flag} must be a boolean")
    return CanonicalEvent(
        _required_str(raw["event_id"], "event_id"),
        _required_str(raw["timestamp"], "timestamp"),
        _positive_int(raw["journal_seq"], "journal_seq"),
        _required_str(raw["context_id"], "context_id"),
        _optional_str(raw.get("turn_id"), "turn_id"),
        _optional_str(raw.get("message_id"), "message_id"),
        _optional_str(raw.get("parent_event_id"), "parent_event_id"),
        _required_str(raw["type"], "type"),
        raw["data"],
        ProviderIdentity(
            _required_str(provider["id"], "provider.id"),
            _required_str(provider["model"], "provider.model"),
            _required_str(provider["effort"], "provider.effort"),
        ),
        raw.get("provider_final", False),
        raw.get("metadata_only", False),
        schema_version,
        _positive_int(raw["content_version"], "content_version"),
    )


def _validated_messages(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, TypedPrompt], dict[str, str]]:
    prompts: dict[str, TypedPrompt] = {}
    message_turns: dict[str, str] = {}
    message_ids = set()
    for message in sorted(messages, key=lambda row: _positive_int(row.get("seq"), "message.seq")):
        required = {"id", "turn_id", "seq", "role", "content"}
        missing = sorted(required - message.keys())
        if missing:
            raise ValueError(f"missing message fields: {', '.join(missing)}")
        message_id = _required_str(message["id"], "message.id")
        turn_id = _required_str(message["turn_id"], "message.turn_id")
        role = _required_str(message["role"], "message.role")
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported message role: {role}")
        if not isinstance(message["content"], str):
            raise ValueError("message.content must be a string")
        if message_id in message_ids:
            raise ValueError(f"duplicate message id: {message_id}")
        message_ids.add(message_id)
        message_turns[message_id] = turn_id
        if role == "user":
            if turn_id in prompts:
                raise ValueError(f"duplicate prompt for turn: {turn_id}")
            prompts[turn_id] = TypedPrompt(message_id, message["content"])
    return prompts, message_turns


def _ownership(
    events: Sequence[CanonicalEvent], message_turns: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    result = {}
    event_ids = {event.event_id for event in events}
    for event in events:
        if event.type != "message_ownership_declared":
            continue
        if not event.metadata_only or event.turn_id is None or event.message_id is None:
            raise ValueError("ownership declarations require metadata turn and message pointers")
        if message_turns.get(event.message_id) != event.turn_id:
            raise ValueError("ownership declaration crosses message/turn boundary")
        owned_ids = event.data.get("owns_event_ids")
        if not isinstance(owned_ids, tuple):
            raise ValueError("ownership declaration owns_event_ids must be a sequence")
        for owned_id in owned_ids:
            if not isinstance(owned_id, str) or owned_id not in event_ids:
                raise ValueError("ownership declaration references an unknown event")
            owner = (event.turn_id, event.message_id)
            if owned_id in result and result[owned_id] != owner:
                raise ValueError("event has conflicting ownership declarations")
            result[owned_id] = owner
    return result


def _validate_edges(
    events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
    ownership: Mapping[str, tuple[str, str]],
    message_turns: Mapping[str, str],
) -> None:
    for child in events:
        if child.parent_event_id is None:
            continue
        parent = event_by_id.get(child.parent_event_id)
        if parent is None:
            raise ValueError(f"unknown parent event: {child.parent_event_id}")
        child_turn, child_message = _boundary(child, ownership, message_turns)
        parent_turn, parent_message = _boundary(parent, ownership, message_turns)
        if child_message != parent_message:
            raise ValueError("parent edge crosses message boundary")
        if parent.type not in _SCOPED_TYPES:
            if child_turn != parent_turn or child.context_id != parent.context_id:
                raise ValueError("parent edge crosses context or turn boundary")
        _validate_acyclic_parent_chain(child, event_by_id)


def _validate_acyclic_parent_chain(
    event: CanonicalEvent, event_by_id: Mapping[str, CanonicalEvent],
) -> None:
    seen = {event.event_id}
    parent_id = event.parent_event_id
    while parent_id is not None:
        if parent_id in seen:
            raise ValueError("event parent cycle")
        seen.add(parent_id)
        parent_id = event_by_id[parent_id].parent_event_id


def _boundary(
    event: CanonicalEvent,
    ownership: Mapping[str, tuple[str, str]],
    message_turns: Mapping[str, str],
) -> tuple[str | None, str | None]:
    owned = ownership.get(event.event_id)
    message_id = owned[1] if owned else event.message_id
    turn_id = owned[0] if owned else event.turn_id
    return turn_id or (message_turns.get(message_id) if message_id else None), message_id


def _is_top_level(
    event: CanonicalEvent,
    ownership: Mapping[str, tuple[str, str]],
    event_by_id: Mapping[str, CanonicalEvent],
) -> bool:
    if event.event_id in ownership or event.parent_event_id is None:
        return True
    if event.provider_final:
        return not _has_scoped_ancestor(event, event_by_id)
    return False


def _effective_turn_id(
    event: CanonicalEvent,
    message_turns: Mapping[str, str],
    ownership: Mapping[str, tuple[str, str]],
) -> str | None:
    owned = ownership.get(event.event_id)
    if owned:
        return owned[0]
    return message_turns.get(event.message_id or "") or event.turn_id


def _has_ancestor(
    event: CanonicalEvent,
    ancestor_ids: set[str],
    event_by_id: Mapping[str, CanonicalEvent],
) -> bool:
    parent_id = event.parent_event_id
    seen = set()
    while parent_id is not None:
        if parent_id in seen:
            raise ValueError("event parent cycle")
        seen.add(parent_id)
        if parent_id in ancestor_ids:
            return True
        parent = event_by_id.get(parent_id)
        parent_id = parent.parent_event_id if parent else None
    return False


def _has_scoped_ancestor(
    event: CanonicalEvent, event_by_id: Mapping[str, CanonicalEvent],
) -> bool:
    parent_id = event.parent_event_id
    while parent_id is not None:
        parent = event_by_id[parent_id]
        if parent.type in _SCOPED_TYPES:
            return True
        parent_id = parent.parent_event_id
    return False


def _provider_runs(events: Sequence[CanonicalEvent]) -> tuple[tuple[CanonicalEvent, ...], ...]:
    runs: list[list[CanonicalEvent]] = []
    for event in _ordered(events):
        if not runs or event.provider != runs[-1][-1].provider:
            runs.append([])
        runs[-1].append(event)
    return tuple(tuple(run) for run in runs)


def _ordered(events: Iterable[CanonicalEvent]) -> list[CanonicalEvent]:
    return sorted(events, key=_sort_key)


def _sort_key(event: CanonicalEvent) -> tuple[str, int]:
    return event.timestamp, event.sequence


def _event_text(event: CanonicalEvent) -> str:
    for key in ("text", "content", "message"):
        value = event.data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _text_for(events: Iterable[CanonicalEvent]) -> str:
    return "".join(_event_text(event) for event in events if event.type in _TEXT_TYPES)


def _validate_schema_version(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value != CHAT_SCHEMA_VERSION:
        raise ValueError("unsupported chat schema version")


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string or null")
    return value
