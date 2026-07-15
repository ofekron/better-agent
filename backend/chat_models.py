from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias


ImmutableJson: TypeAlias = str | int | float | bool | None | tuple["ImmutableJson", ...] | Mapping[str, "ImmutableJson"]
CHAT_SCHEMA_VERSION = 1
KNOWN_EVENT_TYPES = frozenset({
    "ai_title", "assistant_text", "file_history_snapshot", "message_ownership_declared",
    "model_change", "native_subagent_turn", "other_typed_work", "output_text",
    "steering_message", "text", "thinking", "tool_interaction", "turn_completed",
    "turn_started", "worker_turn",
})


def freeze_json(value: Any) -> ImmutableJson:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


@dataclass(frozen=True)
class ProviderIdentity:
    id: str
    model: str
    effort: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.id, self.model, self.effort)):
            raise ValueError("provider id, model, and effort are required")


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    timestamp: str
    sequence: int
    context_id: str
    turn_id: str | None
    message_id: str | None
    parent_event_id: str | None
    type: str
    data: Mapping[str, ImmutableJson] = field(compare=False, hash=False, repr=False)
    provider: ProviderIdentity | None = None
    provider_final: bool = False
    metadata_only: bool = False
    schema_version: int = CHAT_SCHEMA_VERSION
    content_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != CHAT_SCHEMA_VERSION:
            raise ValueError("unsupported chat schema version")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id is required")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("event sequence must be a positive integer")
        if not isinstance(self.content_version, int) or isinstance(self.content_version, bool) or self.content_version < 1:
            raise ValueError("content_version must be a positive integer")
        if not isinstance(self.timestamp, str) or not self.timestamp:
            raise ValueError("event timestamp is required")
        try:
            datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("event timestamp must be ISO-8601") from exc
        if not isinstance(self.context_id, str) or not self.context_id:
            raise ValueError("event context_id is required")
        if self.type not in KNOWN_EVENT_TYPES:
            raise ValueError(f"unknown canonical event type: {self.type}")
        if self.provider is None:
            raise ValueError("event provider identity is required")
        if not isinstance(self.provider_final, bool) or not isinstance(self.metadata_only, bool):
            raise ValueError("event flags must be booleans")
        object.__setattr__(self, "data", freeze_json(self.data))


@dataclass(frozen=True)
class TypedPrompt:
    id: str
    text: str


@dataclass(frozen=True)
class Result:
    type: Literal["ProviderResult", "DerivedResult"]
    part_ids: tuple[str, ...]
    text: str = ""


@dataclass(frozen=True)
class Explanation:
    id: str
    text: str
    text_event_ids: tuple[str, ...]
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class SteeringMessage:
    id: str
    text: str


@dataclass(frozen=True)
class ScopedTurn:
    type: Literal["NativeSubagentTurn", "WorkerTurn"]
    id: str
    prompt: TypedPrompt
    body: tuple[BodyItem, ...]
    result: Result | None
    children: tuple[str, ...] = ()


BodyItem: TypeAlias = Explanation | SteeringMessage | ScopedTurn


@dataclass(frozen=True)
class Turn:
    id: str
    prompt: TypedPrompt
    body: tuple[BodyItem, ...]
    result: Result | None


@dataclass(frozen=True)
class ModelChange:
    id: str
    before_turn: str
    provider: ProviderIdentity | None


ChatItem: TypeAlias = ModelChange | Turn


@dataclass(frozen=True)
class Chat:
    items: tuple[ChatItem, ...]


@dataclass(frozen=True)
class VisibilityPlan:
    scope: str
    visible_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class ModelMarkerTarget:
    scope: str
    provider: ProviderIdentity
    target_event_id: str
