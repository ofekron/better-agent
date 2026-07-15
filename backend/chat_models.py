from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias


ImmutableJson: TypeAlias = str | int | float | bool | None | tuple["ImmutableJson", ...] | Mapping[str, "ImmutableJson"]


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

    def __post_init__(self) -> None:
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
