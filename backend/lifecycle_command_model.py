from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


PHASES = frozenset({"idle", "starting", "running", "stopping"})
COMMANDS = frozenset({
    "begin_turn",
    "confirm_started",
    "request_stop",
    "finish_turn",
})
OUTCOMES = frozenset({"complete", "stopped", "failed"})
EFFECT_KINDS = frozenset({
    "observe_turn_begin",
    "observe_turn_started",
    "observe_stop_requested",
    "observe_turn_finished",
})
MAX_IDENTIFIER_LENGTH = 256


def freeze_json(value: Any) -> Any:
    if value is None or type(value) in {str, int, float, bool}:
        if type(value) is float and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({
            key: freeze_json(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError("value is not JSON-compatible")


def materialize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: materialize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [materialize_json(item) for item in value]
    return value


def validate_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{name} must be a non-empty bounded string")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} cannot contain control characters")
    return value


@dataclass(frozen=True)
class UserTurnIdentity:
    user_turn_id: str
    lifecycle_message_id: str

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            validate_identifier(value, name)

    def to_dict(self) -> dict[str, str]:
        return {
            "user_turn_id": self.user_turn_id,
            "lifecycle_message_id": self.lifecycle_message_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> UserTurnIdentity:
        if set(value) != {"user_turn_id", "lifecycle_message_id"}:
            raise ValueError("turn identity has unexpected fields")
        return cls(**dict(value))


@dataclass(frozen=True)
class LifecycleSnapshot:
    phase: str = "idle"
    identity: UserTurnIdentity | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError("invalid lifecycle phase")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("invalid lifecycle revision")
        if (self.phase == "idle") != (self.identity is None):
            raise ValueError("only idle lifecycle snapshots omit identity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "identity": self.identity.to_dict() if self.identity else None,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleSnapshot:
        if set(value) != {"phase", "identity", "revision"}:
            raise ValueError("lifecycle snapshot has unexpected fields")
        identity = value["identity"]
        return cls(
            phase=value["phase"],
            identity=(
                UserTurnIdentity.from_dict(identity)
                if identity is not None
                else None
            ),
            revision=value["revision"],
        )


@dataclass(frozen=True)
class LifecycleCommand:
    request_id: str
    session_id: str
    kind: str
    identity: UserTurnIdentity
    outcome: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.request_id, "request_id")
        validate_identifier(self.session_id, "session_id")
        if self.kind not in COMMANDS:
            raise ValueError("invalid lifecycle command")
        if self.kind == "finish_turn":
            if self.outcome not in OUTCOMES:
                raise ValueError("finish_turn requires a valid outcome")
        elif self.outcome is not None:
            raise ValueError("only finish_turn accepts an outcome")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "identity": self.identity.to_dict(),
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleCommand:
        if set(value) != {"request_id", "session_id", "kind", "identity", "outcome"}:
            raise ValueError("lifecycle command has unexpected fields")
        return cls(
            request_id=value["request_id"],
            session_id=value["session_id"],
            kind=value["kind"],
            identity=UserTurnIdentity.from_dict(value["identity"]),
            outcome=value["outcome"],
        )

    def fingerprint(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class LifecycleEffect:
    effect_id: str
    kind: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_identifier(self.effect_id, "effect_id")
        if self.kind not in EFFECT_KINDS:
            raise ValueError("unknown lifecycle effect kind")
        if not isinstance(self.payload, Mapping):
            raise ValueError("lifecycle effect payload must be a mapping")
        frozen = freeze_json(dict(self.payload))
        json.dumps(materialize_json(frozen), allow_nan=False)
        object.__setattr__(self, "payload", frozen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "kind": self.kind,
            "payload": materialize_json(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleEffect:
        if set(value) != {"effect_id", "kind", "payload"}:
            raise ValueError("lifecycle effect has unexpected fields")
        payload = value["payload"]
        if not isinstance(payload, dict):
            raise ValueError("lifecycle effect payload must be an object")
        return cls(value["effect_id"], value["kind"], payload)


@dataclass(frozen=True)
class TransitionPlan:
    next_snapshot: LifecycleSnapshot
    effects: tuple[LifecycleEffect, ...]
    fact_type: str
    fact_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_identifier(self.fact_type, "fact_type")
        frozen = freeze_json(dict(self.fact_payload))
        json.dumps(materialize_json(frozen), allow_nan=False)
        object.__setattr__(self, "fact_payload", frozen)


@dataclass(frozen=True)
class CommandResult:
    request_id: str
    snapshot: LifecycleSnapshot
    effect_results: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effect_results",
            tuple(freeze_json(dict(result)) for result in self.effect_results),
        )
