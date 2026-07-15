from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from chat_models import (
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
) -> Chat:
    canonical = _canonical_events(events)
    prompts = _prompts(messages)
    message_turns = {
        str(message["id"]): str(message["turn_id"])
        for message in messages
        if message.get("id") is not None and message.get("turn_id") is not None
    }
    ownership = _ownership(canonical)
    top_level = defaultdict(list)
    changes = defaultdict(list)
    for event in canonical:
        if event.type == "model_change":
            turn_id = _effective_turn_id(event, message_turns, ownership)
            if turn_id:
                changes[turn_id].append(event)
            continue
        if event.metadata_only:
            continue
        turn_id = _effective_turn_id(event, message_turns, ownership)
        if turn_id in prompts and (
            event.parent_event_id is None
            or event.provider_final
            or event.event_id in ownership
        ):
            top_level[turn_id].append(event)

    items = []
    for turn_id, prompt in prompts.items():
        for change in _ordered(changes[turn_id]):
            items.append(ModelChange(change.event_id, turn_id, change.provider))
        items.append(_derive_turn(turn_id, prompt, top_level[turn_id], canonical))
    return Chat(tuple(items))


def model_marker_targets(
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
    visibility_plans: Iterable[VisibilityPlan] | None = None,
) -> tuple[ModelMarkerTarget, ...]:
    canonical = _canonical_events(events)
    plans = {plan.scope: set(plan.visible_event_ids) for plan in visibility_plans or ()}
    by_scope = defaultdict(list)
    for event in canonical:
        if event.metadata_only or event.type == "model_change" or event.provider is None:
            continue
        if plans and event.context_id not in plans:
            continue
        if event.context_id in plans and event.event_id not in plans[event.context_id]:
            continue
        by_scope[event.context_id].append(event)

    targets = []
    for scope, scoped_events in by_scope.items():
        run = []
        for event in _ordered(scoped_events):
            if run and event.provider != run[-1].provider:
                targets.append(ModelMarkerTarget(scope, run[-1].provider, run[-1].event_id))
                run = []
            run.append(event)
        if run:
            targets.append(ModelMarkerTarget(scope, run[-1].provider, run[-1].event_id))
    event_by_id = {event.event_id: event for event in canonical}
    return tuple(sorted(
        targets,
        key=lambda target: (
            event_by_id[target.target_event_id].timestamp,
            event_by_id[target.target_event_id].sequence,
        ),
    ))


def canonical_quick_reply_text(
    chat: Chat,
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
) -> str:
    event_by_id = {event.event_id: event for event in _canonical_events(events)}
    for item in reversed(chat.items):
        if isinstance(item, Turn) and item.result is not None:
            return "".join(
                _event_text(event_by_id[event_id])
                for event_id in item.result.part_ids
                if event_id in event_by_id and event_by_id[event_id].type in _TEXT_TYPES
            )
    return ""


def _derive_turn(
    turn_id: str,
    prompt: TypedPrompt,
    events: Sequence[CanonicalEvent],
    all_events: Sequence[CanonicalEvent],
) -> Turn:
    ordered = _ordered(events)
    result, result_ids = _resolve_result(ordered)
    body = _derive_body(
        [event for event in ordered if event.event_id not in result_ids], all_events,
    )
    return Turn(turn_id, prompt, body, result)


def _resolve_result(events: Sequence[CanonicalEvent]) -> tuple[Result | None, set[str]]:
    marked = [
        event for event in events
        if event.provider_final
        and event.type not in _SCOPED_TYPES
        and event.type != "steering_message"
    ]
    if marked:
        ids = tuple(event.event_id for event in marked)
        return Result("ProviderResult", ids, _text_for(marked)), set(ids)
    trailing = []
    for event in reversed(events):
        if event.type not in _TEXT_TYPES:
            break
        trailing.append(event)
    if trailing:
        trailing.reverse()
        ids = tuple(event.event_id for event in trailing)
        return Result("DerivedResult", ids, _text_for(trailing)), set(ids)
    if not events:
        return None, set()
    final = events[-1]
    if final.type in _SCOPED_TYPES or final.type == "steering_message":
        return None, set()
    return Result("DerivedResult", (final.event_id,), _event_text(final)), {final.event_id}


def _derive_body(
    events: Sequence[CanonicalEvent],
    all_events: Sequence[CanonicalEvent],
) -> tuple[BodyItem, ...]:
    body = []
    partition = []
    explanation_number = 0

    def flush() -> None:
        nonlocal explanation_number
        if not partition:
            return
        explanation_number += 1
        text_events = []
        item_events = []
        leading = True
        for event in partition:
            if leading and event.type in _TEXT_TYPES:
                text_events.append(event)
            else:
                leading = False
                item_events.append(event)
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
            body.append(_scoped_turn(event, all_events))
        elif event.type == "steering_message":
            flush()
            body.append(SteeringMessage(event.event_id, _event_text(event)))
        elif (
            event.type in _TEXT_TYPES
            and partition
            and any(item.type not in _TEXT_TYPES for item in partition)
        ):
            flush()
            partition.append(event)
        else:
            partition.append(event)
    flush()
    return tuple(body)


def _scoped_turn(event: CanonicalEvent, all_events: Sequence[CanonicalEvent]) -> ScopedTurn:
    children = _ordered(item for item in all_events if item.parent_event_id == event.event_id)
    prompt = TypedPrompt(f"prompt-{event.event_id}", str(event.data.get("prompt") or ""))
    nested_result = None
    embedded = event.data.get("result") or event.data.get("text")
    if embedded:
        kind = "ProviderResult" if event.provider_final else "DerivedResult"
        nested_result = Result(kind, (event.event_id,), str(embedded))
    nested_body = tuple(_scoped_turn(child, all_events) for child in children if child.type in _SCOPED_TYPES)
    return ScopedTurn(
        _SCOPED_TYPES[event.type], event.event_id, prompt, nested_body, nested_result,
        tuple(child.event_id for child in children),
    )


def _canonical_events(
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
) -> tuple[CanonicalEvent, ...]:
    latest = {}
    positions = {}
    for raw in events:
        event = raw if isinstance(raw, CanonicalEvent) else _event_from_mapping(raw)
        position = positions.get(event.event_id)
        candidate_position = (event.timestamp, event.sequence)
        positions[event.event_id] = min(position, candidate_position) if position else candidate_position
        current = latest.get(event.event_id)
        if current is None or event.sequence > current.sequence:
            latest[event.event_id] = event
    materialized = [
        CanonicalEvent(
            event.event_id, positions[event.event_id][0], positions[event.event_id][1],
            event.context_id, event.turn_id, event.message_id, event.parent_event_id,
            event.type, event.data, event.provider, event.provider_final, event.metadata_only,
        )
        for event in latest.values()
    ]
    return tuple(_ordered(materialized))


def _event_from_mapping(raw: Mapping[str, Any]) -> CanonicalEvent:
    provider = raw.get("provider")
    identity = None
    if isinstance(provider, Mapping):
        identity = ProviderIdentity(
            str(provider.get("id") or ""), str(provider.get("model") or ""),
            str(provider.get("effort") or ""),
        )
    return CanonicalEvent(
        str(raw["event_id"]), str(raw.get("timestamp") or ""),
        int(raw.get("journal_seq") or raw.get("sequence") or 0),
        str(raw.get("context_id") or ""),
        _optional_str(raw.get("turn_id")), _optional_str(raw.get("message_id")),
        _optional_str(raw.get("parent_event_id")), str(raw.get("type") or ""),
        dict(raw.get("data") or {}), identity, bool(raw.get("provider_final")),
        bool(raw.get("metadata_only")),
    )


def _prompts(messages: Sequence[Mapping[str, Any]]) -> dict[str, TypedPrompt]:
    ordered = sorted(messages, key=lambda message: int(message.get("seq") or 0))
    return {
        str(message["turn_id"]): TypedPrompt(str(message["id"]), str(message.get("content") or ""))
        for message in ordered
        if message.get("role") == "user" and message.get("turn_id") is not None
    }


def _ownership(events: Sequence[CanonicalEvent]) -> dict[str, tuple[str | None, str | None]]:
    result = {}
    for event in events:
        if event.type != "message_ownership_declared":
            continue
        for event_id in event.data.get("owns_event_ids") or ():
            result[str(event_id)] = (event.turn_id, event.message_id)
    return result


def _effective_turn_id(
    event: CanonicalEvent,
    message_turns: Mapping[str, str],
    ownership: Mapping[str, tuple[str | None, str | None]],
) -> str | None:
    owned_turn, owned_message = ownership.get(event.event_id, (None, None))
    message_id = owned_message or event.message_id
    return owned_turn or (message_turns.get(message_id) if message_id else None) or event.turn_id


def _ordered(events: Iterable[CanonicalEvent]) -> list[CanonicalEvent]:
    return sorted(events, key=lambda event: (event.timestamp, event.sequence))


def _event_text(event: CanonicalEvent) -> str:
    for key in ("text", "content", "message"):
        value = event.data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _text_for(events: Iterable[CanonicalEvent]) -> str:
    return "".join(_event_text(event) for event in events if event.type in _TEXT_TYPES)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
