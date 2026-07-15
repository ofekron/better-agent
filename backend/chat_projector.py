from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
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
_EVENT_FIELDS = {
    "event_id", "timestamp", "journal_seq", "content_version", "context_id",
    "turn_id", "message_id", "parent_event_id", "type", "data", "provider",
    "provider_final", "metadata_only", "source",
}
_PROVIDER_FIELDS = {"id", "model", "effort"}
_MESSAGE_FIELDS = {"id", "turn_id", "seq", "role", "content"}
_MODEL_IDENTITY_FIELDS = {"provider", "model", "effort"}
MAX_CANONICAL_ROWS = 200_000
MAX_CANONICAL_JSON_BYTES = 128 * 1024 * 1024
MAX_MESSAGES = 200_000
MAX_MESSAGE_JSON_BYTES = 128 * 1024 * 1024
MAX_STRING_LENGTH = 4 * 1024 * 1024
MAX_LIST_ITEMS = 100_000
MAX_OPTIONS = 10_000
MAX_SESSIONS = 10_000
MAX_PAYLOAD_DEPTH = 64
CHAT_PROJECTION_INPUT_ERROR_CODES = frozenset({
    "unsupported_schema", "invalid_event_model", "event_schema_mismatch",
    "duplicate_journal_seq", "missing_event_fields", "unexpected_fields",
    "invalid_provider", "invalid_event_data", "invalid_flag", "invalid_scalar",
    "too_many_messages", "message_bytes_exceeded", "missing_message_fields",
    "invalid_message_role", "invalid_message_content", "duplicate_message_id",
    "duplicate_message_seq", "duplicate_prompt", "invalid_ownership",
    "ownership_boundary", "ownership_unknown_event", "ownership_conflict",
    "ownership_scoped_conflict", "unknown_parent", "parent_cycle",
    "parent_message_boundary", "parent_context_boundary", "invalid_payload",
    "missing_payload_fields", "model_provider_mismatch", "version_identity_changed",
    "version_not_incremented", "too_many_rows", "canonical_bytes_exceeded",
    "string_too_long", "list_too_large", "too_many_options", "too_many_sessions",
    "payload_depth_exceeded", "payload_not_tree",
})


class ChatProjectionInputError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        if code not in CHAT_PROJECTION_INPUT_ERROR_CODES:
            raise RuntimeError(f"unregistered chat projection input error code: {code}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def project_chat(
    messages: Sequence[Mapping[str, Any]],
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
    *,
    schema_version: int,
) -> Chat:
    canonical = _canonical_events(events, schema_version)
    event_by_id = {event.event_id: event for event in canonical}
    _validate_parent_graph(canonical, event_by_id)
    prompts, message_turns = _validated_messages(messages)
    ownership = _ownership(canonical, message_turns)
    _validate_boundaries(canonical, event_by_id, ownership, message_turns)
    associations = _build_associated_text_index(
        canonical, event_by_id, ownership, message_turns,
    )
    scoped_turns = _project_scoped_turns(canonical, event_by_id, associations)

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
            *_derive_content(
                top_level[turn_id], canonical, event_by_id, scoped_turns,
                associations.get(("turn", turn_id), ()),
            ),
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
    scoped_turns: Mapping[str, ScopedTurn],
    associated_text: Sequence[CanonicalEvent],
) -> tuple[tuple[BodyItem, ...], Result | None]:
    ordered = _ordered(event for event in events if not event.metadata_only)
    result, result_ids = _resolve_result(ordered, associated_text)
    body = _derive_body(
        [event for event in ordered if event.event_id not in result_ids],
        all_events,
        event_by_id,
        scoped_turns,
    )
    return body, result


def _resolve_result(
    events: Sequence[CanonicalEvent],
    associated_text: Sequence[CanonicalEvent],
) -> tuple[Result | None, set[str]]:
    marked = [event for event in events if event.provider_final and event.type not in _SCOPED_TYPES]
    if marked:
        result_events = _ordered({
            event.event_id: event for event in (*marked, *associated_text)
        }.values())
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
    scoped_turns: Mapping[str, ScopedTurn],
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
            body.append(scoped_turns[event.event_id])
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


def _build_associated_text_index(
    events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
    ownership: Mapping[str, tuple[str, str]],
    message_turns: Mapping[str, str],
    observe: Any = None,
) -> dict[tuple[str, str], tuple[CanonicalEvent, ...]]:
    children: dict[str, list[CanonicalEvent]] = defaultdict(list)
    queue = deque()
    for event in events:
        if event.parent_event_id is None:
            queue.append(event)
        else:
            children[event.parent_event_id].append(event)
    scope_by_id: dict[str, tuple[str, str]] = {}
    has_final_by_id: dict[str, bool] = {}
    associated: dict[tuple[str, str], list[CanonicalEvent]] = defaultdict(list)
    while queue:
        event = queue.popleft()
        parent = event_by_id.get(event.parent_event_id or "")
        if parent is None:
            turn_id = _effective_turn_id(event, message_turns, ownership) or ""
            scope = ("turn", turn_id)
            inherited_final = False
        elif parent.type in _SCOPED_TYPES:
            scope = ("scoped", parent.event_id)
            inherited_final = False
        else:
            scope = scope_by_id[parent.event_id]
            inherited_final = has_final_by_id[parent.event_id]
        has_final = inherited_final or (
            event.provider_final and event.type not in _SCOPED_TYPES
        )
        scope_by_id[event.event_id] = scope
        has_final_by_id[event.event_id] = has_final
        if has_final and not event.metadata_only and event.type in _TEXT_TYPES:
            associated[scope].append(event)
        if observe is not None:
            observe(event.event_id)
        queue.extend(children[event.event_id])
    return {scope: tuple(_ordered(items)) for scope, items in associated.items()}


def _project_scoped_turns(
    all_events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
    associations: Mapping[tuple[str, str], Sequence[CanonicalEvent]],
) -> dict[str, ScopedTurn]:
    children_by_parent: dict[str, list[CanonicalEvent]] = defaultdict(list)
    scoped = [event for event in all_events if event.type in _SCOPED_TYPES]
    for child in all_events:
        if child.parent_event_id is not None and not child.metadata_only:
            children_by_parent[child.parent_event_id].append(child)
    projected: dict[str, ScopedTurn] = {}
    roots = [
        event for event in scoped
        if event.parent_event_id is None
        or event_by_id[event.parent_event_id].type not in _SCOPED_TYPES
    ]
    stack = [(event, False) for event in reversed(_ordered(roots))]
    while stack:
        event, expanded = stack.pop()
        children = _ordered(children_by_parent[event.event_id])
        if not expanded:
            stack.append((event, True))
            for child in reversed(children):
                if child.type in _SCOPED_TYPES:
                    stack.append((child, False))
            continue
        body, result = _derive_content(
            children, all_events, event_by_id, projected,
            associations.get(("scoped", event.event_id), ()),
        )
        embedded = event.data.get("result") or event.data.get("text")
        if result is None and isinstance(embedded, str) and embedded:
            kind = "ProviderResult" if event.provider_final else "DerivedResult"
            result = Result(kind, (event.event_id,), embedded)
        projected[event.event_id] = ScopedTurn(
            _SCOPED_TYPES[event.type],
            event.event_id,
            TypedPrompt(f"prompt-{event.event_id}", event.data["prompt"]),
            body,
            result,
            tuple(child.event_id for child in children),
        )
    return projected


def _canonical_events(
    events: Sequence[CanonicalEvent | Mapping[str, Any]], schema_version: int,
) -> tuple[CanonicalEvent, ...]:
    _validate_schema_version(schema_version)
    _admit_canonical_rows(events)
    latest: dict[str, CanonicalEvent] = {}
    positions: dict[str, tuple[str, int]] = {}
    sequences: dict[int, str] = {}
    for raw in events:
        if not isinstance(raw, (CanonicalEvent, Mapping)):
            raise ChatProjectionInputError("invalid_event_data", "event row must be an object")
        event = raw if isinstance(raw, CanonicalEvent) else _event_from_mapping(raw, schema_version)
        if event.schema_version != schema_version:
            raise ChatProjectionInputError(
                "event_schema_mismatch", "event schema version does not match request",
            )
        _validate_nested_data(event.type, event.data)
        _validate_model_change_provider(event)
        if event.sequence in sequences:
            raise ChatProjectionInputError(
                "duplicate_journal_seq",
                f"journal_seq {event.sequence} is already owned by {sequences[event.sequence]}",
            )
        sequences[event.sequence] = event.event_id
        position = (event.timestamp, event.sequence)
        current_position = positions.get(event.event_id)
        if current_position is None or _position_key(position) < _position_key(current_position):
            positions[event.event_id] = position
        current = latest.get(event.event_id)
        if current is not None:
            _validate_version_update(current, event)
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
        raise ChatProjectionInputError(
            "missing_event_fields", f"missing canonical event fields: {', '.join(missing)}",
        )
    _reject_extra_keys(raw, _EVENT_FIELDS, "event")
    provider = raw["provider"]
    if not isinstance(provider, Mapping):
        raise ChatProjectionInputError("invalid_provider", "event provider must be an object")
    if _PROVIDER_FIELDS - provider.keys():
        raise ChatProjectionInputError("invalid_provider", "provider identity fields are required")
    _reject_extra_keys(provider, _PROVIDER_FIELDS, "provider")
    if not isinstance(raw["data"], Mapping):
        raise ChatProjectionInputError("invalid_event_data", "event data must be an object")
    for flag in ("provider_final", "metadata_only"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise ChatProjectionInputError("invalid_flag", f"{flag} must be a boolean")
    if "source" in raw:
        _required_str(raw["source"], "source")
    _validate_nested_data(_required_str(raw["type"], "type"), raw["data"])
    try:
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
    except ChatProjectionInputError:
        raise
    except (TypeError, ValueError) as exc:
        raise ChatProjectionInputError("invalid_event_model", str(exc)) from exc


def _validated_messages(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, TypedPrompt], dict[str, str]]:
    _admit_messages(messages)
    prompts: dict[str, TypedPrompt] = {}
    message_turns: dict[str, str] = {}
    message_ids = set()
    message_sequences = set()
    for message in messages:
        if not isinstance(message, Mapping):
            raise ChatProjectionInputError("invalid_message_content", "message row must be an object")
    for message in sorted(messages, key=lambda row: _positive_int(row.get("seq"), "message.seq")):
        required = {"id", "turn_id", "seq", "role", "content"}
        missing = sorted(required - message.keys())
        if missing:
            raise ChatProjectionInputError(
                "missing_message_fields", f"missing message fields: {', '.join(missing)}",
            )
        _reject_extra_keys(message, _MESSAGE_FIELDS, "message")
        message_id = _required_str(message["id"], "message.id")
        turn_id = _required_str(message["turn_id"], "message.turn_id")
        role = _required_str(message["role"], "message.role")
        if role not in {"user", "assistant"}:
            raise ChatProjectionInputError("invalid_message_role", f"unsupported role: {role}")
        if not isinstance(message["content"], str):
            raise ChatProjectionInputError("invalid_message_content", "content must be a string")
        if message_id in message_ids:
            raise ChatProjectionInputError("duplicate_message_id", f"duplicate id: {message_id}")
        message_ids.add(message_id)
        sequence = _positive_int(message["seq"], "message.seq")
        if sequence in message_sequences:
            raise ChatProjectionInputError(
                "duplicate_message_seq", f"duplicate message seq: {sequence}",
            )
        message_sequences.add(sequence)
        message_turns[message_id] = turn_id
        if role == "user":
            if turn_id in prompts:
                raise ChatProjectionInputError("duplicate_prompt", f"duplicate prompt: {turn_id}")
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
            raise ChatProjectionInputError("invalid_ownership", "ownership pointers are required")
        if message_turns.get(event.message_id) != event.turn_id:
            raise ChatProjectionInputError("ownership_boundary", "ownership crosses boundary")
        owned_ids = event.data.get("owns_event_ids")
        if not isinstance(owned_ids, tuple):
            raise ChatProjectionInputError("invalid_ownership", "owned ids must be a sequence")
        for owned_id in owned_ids:
            if not isinstance(owned_id, str) or owned_id not in event_ids:
                raise ChatProjectionInputError("ownership_unknown_event", "unknown owned event")
            owner = (event.turn_id, event.message_id)
            if owned_id in result and result[owned_id] != owner:
                raise ChatProjectionInputError("ownership_conflict", "conflicting ownership")
            result[owned_id] = owner
    event_by_id = {event.event_id: event for event in events}
    for owned_id in result:
        if _has_scoped_ancestor(event_by_id[owned_id], event_by_id):
            raise ChatProjectionInputError(
                "ownership_scoped_conflict", "ownership conflicts with scoped structure",
            )
    return result


def _validate_parent_graph(
    events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
    observe: Any = None,
) -> None:
    colors: dict[str, int] = {}
    for event in events:
        if event.parent_event_id is not None and event.parent_event_id not in event_by_id:
            raise ChatProjectionInputError(
                "unknown_parent", f"unknown parent event: {event.parent_event_id}",
            )
    for event in events:
        if colors.get(event.event_id) == 2:
            continue
        path = []
        current_id: str | None = event.event_id
        while current_id is not None and colors.get(current_id, 0) == 0:
            colors[current_id] = 1
            path.append(current_id)
            if observe is not None:
                observe(current_id)
            current_id = event_by_id[current_id].parent_event_id
        if current_id is not None and colors.get(current_id) == 1:
            raise ChatProjectionInputError("parent_cycle", f"cycle includes event: {current_id}")
        for visited_id in path:
            colors[visited_id] = 2


def _validate_boundaries(
    events: Sequence[CanonicalEvent],
    event_by_id: Mapping[str, CanonicalEvent],
    ownership: Mapping[str, tuple[str, str]],
    message_turns: Mapping[str, str],
) -> None:
    for child in events:
        if child.parent_event_id is None:
            continue
        parent = event_by_id[child.parent_event_id]
        child_turn, child_message = _boundary(child, ownership, message_turns)
        parent_turn, parent_message = _boundary(parent, ownership, message_turns)
        if child_message != parent_message:
            raise ChatProjectionInputError("parent_message_boundary", "parent crosses message")
        if parent.type not in _SCOPED_TYPES:
            if child_turn != parent_turn or child.context_id != parent.context_id:
                raise ChatProjectionInputError(
                    "parent_context_boundary", "parent crosses context or turn",
                )


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


def _has_scoped_ancestor(
    event: CanonicalEvent, event_by_id: Mapping[str, CanonicalEvent],
) -> bool:
    parent_id = event.parent_event_id
    seen = set()
    while parent_id is not None:
        if parent_id in seen:
            raise ChatProjectionInputError("parent_cycle", f"cycle includes event: {parent_id}")
        seen.add(parent_id)
        parent = event_by_id.get(parent_id)
        if parent is None:
            raise ChatProjectionInputError("unknown_parent", f"unknown parent event: {parent_id}")
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


def _sort_key(event: CanonicalEvent) -> tuple[datetime, int]:
    return datetime.fromisoformat(event.timestamp[:-1] + "+00:00"), event.sequence


def _position_key(position: tuple[str, int]) -> tuple[datetime, int]:
    return datetime.fromisoformat(position[0][:-1] + "+00:00"), position[1]


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
        raise ChatProjectionInputError("unsupported_schema", "unsupported chat schema version")


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ChatProjectionInputError("invalid_scalar", f"{field} must be a positive integer")
    return value


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChatProjectionInputError("invalid_scalar", f"{field} is required")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ChatProjectionInputError("invalid_scalar", f"{field} must be a string or null")
    return value


def _reject_extra_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unexpected = sorted(value.keys() - allowed, key=str)
    if unexpected:
        raise ChatProjectionInputError(
            "unexpected_fields", f"unexpected {name} fields: {', '.join(map(str, unexpected))}",
        )


def _validate_nested_data(event_type: str, data: Mapping[str, Any]) -> None:
    schemas = {
        "ai_title": ({"title"}, {"title"}),
        "assistant_text": ({"text"}, {"text", "source_timestamp"}),
        "text": ({"text"}, {"text", "source_timestamp"}),
        "output_text": ({"text"}, {"text", "source_timestamp"}),
        "file_history_snapshot": ({"snapshot_id"}, {"snapshot_id"}),
        "native_subagent_turn": ({"prompt"}, {"prompt", "status", "text", "result"}),
        "other_typed_work": ({"kind", "label"}, {"kind", "label", "source_timestamp"}),
        "steering_message": ({"text"}, {"text"}),
        "thinking": ({"text", "status"}, {"text", "status"}),
        "tool_interaction": (
            {"tool_name", "tool_use_id", "status"},
            {
                "tool_name", "tool_use_id", "status", "assistant_text", "selected_session_id",
                "session_ids", "sessions", "options", "question", "summary",
            },
        ),
        "turn_completed": (set(), set()),
        "turn_started": (set(), set()),
        "worker_turn": ({"prompt"}, {"prompt", "status", "text", "result"}),
    }
    if event_type == "model_change":
        _reject_extra_keys(data, {"from", "to"}, "model_change data")
        if "to" not in data or data["to"] is None:
            raise ChatProjectionInputError("missing_payload_fields", "model_change data.to required")
        for field in ("from", "to"):
            identity = data.get(field)
            if identity is None:
                continue
            if not isinstance(identity, Mapping):
                raise ChatProjectionInputError("invalid_payload", f"model_change {field} invalid")
            if set(identity) != _MODEL_IDENTITY_FIELDS:
                raise ChatProjectionInputError("invalid_payload", f"model_change {field} fields invalid")
            for key in _MODEL_IDENTITY_FIELDS:
                _required_str(identity[key], f"model_change data.{field}.{key}")
        return
    if event_type == "message_ownership_declared":
        _reject_extra_keys(
            data,
            {"owns_event_ids", "boundary_seq", "source_timestamp"},
            "ownership data",
        )
        owned = data.get("owns_event_ids")
        if not isinstance(owned, (list, tuple)):
            raise ChatProjectionInputError("invalid_payload", "ownership ids must be a sequence")
        if any(not isinstance(event_id, str) or not event_id for event_id in owned):
            raise ChatProjectionInputError("invalid_payload", "ownership ids invalid")
        if "boundary_seq" in data:
            _positive_int(data["boundary_seq"], "ownership data.boundary_seq")
        if "source_timestamp" in data:
            _required_str(data["source_timestamp"], "ownership data.source_timestamp")
        return
    if event_type not in schemas:
        raise ChatProjectionInputError("invalid_payload", f"unknown event type: {event_type}")
    required, allowed = schemas[event_type]
    missing = required - data.keys()
    if missing:
        raise ChatProjectionInputError(
            "missing_payload_fields", f"missing {event_type} fields: {', '.join(sorted(missing))}",
        )
    _reject_extra_keys(data, allowed, f"{event_type} data")
    for field in required:
        _required_str(data[field], f"{event_type} data.{field}")
    string_fields = {
        "text", "source_timestamp", "snapshot_id", "prompt", "status", "result", "kind",
        "label", "tool_name", "tool_use_id", "assistant_text", "selected_session_id",
        "question", "summary", "title",
    }
    for field in string_fields & data.keys():
        _required_str(data[field], f"{event_type} data.{field}")
    for field in {"session_ids", "sessions", "options"} & data.keys():
        if not isinstance(data[field], (list, tuple)):
            raise ChatProjectionInputError("invalid_payload", f"{event_type}.{field} must be a sequence")
    if len(data.get("options", ())) > MAX_OPTIONS:
        raise ChatProjectionInputError(
            "too_many_options", f"options exceed {MAX_OPTIONS}",
        )
    if len(data.get("sessions", ())) > MAX_SESSIONS:
        raise ChatProjectionInputError(
            "too_many_sessions", f"sessions exceed {MAX_SESSIONS}",
        )
    for field in {"session_ids", "options"} & data.keys():
        if any(not isinstance(value, str) or not value for value in data[field]):
            raise ChatProjectionInputError("invalid_payload", f"{event_type}.{field} strings required")
    for session in data.get("sessions", ()):
        if not isinstance(session, Mapping):
            raise ChatProjectionInputError("invalid_payload", "sessions must contain objects")
        if set(session) != {"id", "title"}:
            raise ChatProjectionInputError("invalid_payload", "session fields invalid")
        _required_str(session["id"], f"{event_type} data.sessions.id")
        _required_str(session["title"], f"{event_type} data.sessions.title")


def _validate_model_change_provider(event: CanonicalEvent) -> None:
    if event.type != "model_change":
        return
    target = event.data["to"]
    expected = ProviderIdentity(target["provider"], target["model"], target["effort"])
    if event.provider != expected:
        raise ChatProjectionInputError("model_provider_mismatch", "provider must match data.to")


def _admit_canonical_rows(
    events: Sequence[CanonicalEvent | Mapping[str, Any]],
) -> None:
    if len(events) > MAX_CANONICAL_ROWS:
        raise ChatProjectionInputError(
            "too_many_rows", f"canonical rows exceed {MAX_CANONICAL_ROWS}",
        )
    total_bytes = 0
    for raw in events:
        value: Any
        if isinstance(raw, CanonicalEvent):
            value = {
                "event_id": raw.event_id, "timestamp": raw.timestamp,
                "context_id": raw.context_id, "turn_id": raw.turn_id,
                "message_id": raw.message_id, "parent_event_id": raw.parent_event_id,
                "type": raw.type, "data": raw.data,
                "provider": {
                    "id": raw.provider.id if raw.provider else "",
                    "model": raw.provider.model if raw.provider else "",
                    "effort": raw.provider.effort if raw.provider else "",
                },
            }
        else:
            value = raw
        total_bytes += _measure_json(value)
        if total_bytes > MAX_CANONICAL_JSON_BYTES:
            raise ChatProjectionInputError(
                "canonical_bytes_exceeded",
                f"canonical JSON bytes exceed {MAX_CANONICAL_JSON_BYTES}",
            )


def _admit_messages(messages: Sequence[Mapping[str, Any]]) -> None:
    if len(messages) > MAX_MESSAGES:
        raise ChatProjectionInputError(
            "too_many_messages", f"messages exceed {MAX_MESSAGES}",
        )
    total_bytes = 0
    for message in messages:
        total_bytes += _measure_json(message)
        if total_bytes > MAX_MESSAGE_JSON_BYTES:
            raise ChatProjectionInputError(
                "message_bytes_exceeded",
                f"message JSON bytes exceed {MAX_MESSAGE_JSON_BYTES}",
            )


def _measure_json(value: Any) -> int:
    total_bytes = 0
    stack = [(value, 0)]
    containers = set()
    while stack:
        item, depth = stack.pop()
        if depth > MAX_PAYLOAD_DEPTH:
            raise ChatProjectionInputError(
                "payload_depth_exceeded", f"payload depth exceeds {MAX_PAYLOAD_DEPTH}",
            )
        if isinstance(item, str):
            length = len(item)
            if length > MAX_STRING_LENGTH:
                raise ChatProjectionInputError(
                    "string_too_long", f"string length exceeds {MAX_STRING_LENGTH}",
                )
            try:
                total_bytes += len(item.encode("utf-8"))
            except UnicodeError as exc:
                raise ChatProjectionInputError("invalid_scalar", "string must be valid UTF-8") from exc
            continue
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in containers:
                raise ChatProjectionInputError(
                    "payload_not_tree", "payload contains a cyclic or aliased object",
                )
            containers.add(identity)
            if len(item) > MAX_LIST_ITEMS:
                raise ChatProjectionInputError(
                    "list_too_large", f"object size exceeds {MAX_LIST_ITEMS}",
                )
            for key, child in item.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in containers:
                raise ChatProjectionInputError(
                    "payload_not_tree", "payload contains a cyclic or aliased list",
                )
            containers.add(identity)
            if len(item) > MAX_LIST_ITEMS:
                raise ChatProjectionInputError(
                    "list_too_large", f"list size exceeds {MAX_LIST_ITEMS}",
                )
            stack.extend((child, depth + 1) for child in item)
            continue
        total_bytes += 8
    return total_bytes


def _validate_version_update(current: CanonicalEvent, candidate: CanonicalEvent) -> None:
    identity = (
        "context_id", "turn_id", "message_id", "parent_event_id", "type", "provider",
    )
    if any(getattr(current, field) != getattr(candidate, field) for field in identity):
        raise ChatProjectionInputError("version_identity_changed", "event identity changed")
    render_changed = (
        current.data != candidate.data
        or current.provider_final != candidate.provider_final
        or current.metadata_only != candidate.metadata_only
    )
    if render_changed and current.content_version == candidate.content_version:
        raise ChatProjectionInputError("version_not_incremented", "content_version must increase")
