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
    "start_execution",
    "bind_execution_run",
    "restore_execution_run",
    "confirm_execution_started",
    "detach_execution",
    "adopt_execution",
    "finish_execution",
    "finish_execution_and_turn",
    "abort_execution",
})
OUTCOMES = frozenset({"complete", "stopped", "failed"})
EFFECT_KINDS = frozenset({
    "observe_turn_begin",
    "observe_turn_started",
    "observe_stop_requested",
    "observe_turn_finished",
    "observe_execution_started",
    "observe_execution_run_bound",
    "observe_execution_admitted",
    "observe_execution_detached",
    "observe_execution_adopted",
    "observe_execution_finished",
    "observe_execution_aborted",
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
class ExecutionTurnIdentity:
    execution_turn_id: str
    assistant_message_id: str
    role: str

    def __post_init__(self) -> None:
        validate_identifier(self.execution_turn_id, "execution_turn_id")
        validate_identifier(self.assistant_message_id, "assistant_message_id")
        if self.role not in {"native", "manager", "supervisor"}:
            raise ValueError("invalid execution role")

    def to_dict(self) -> dict[str, str]:
        return {
            "execution_turn_id": self.execution_turn_id,
            "assistant_message_id": self.assistant_message_id,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionTurnIdentity:
        if set(value) != {"execution_turn_id", "assistant_message_id", "role"}:
            raise ValueError("execution identity has unexpected fields")
        return cls(**dict(value))


@dataclass(frozen=True)
class ExecutionTurnSnapshot:
    phase: str
    identity: ExecutionTurnIdentity
    provider_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in {
            "starting",
            "running",
            "stopping",
            "detached",
            "detached_stopping",
            "complete",
            "stopped",
            "failed",
            "aborted",
        }:
            raise ValueError("invalid execution phase")
        if self.provider_run_id is not None:
            validate_identifier(self.provider_run_id, "provider_run_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "identity": self.identity.to_dict(),
            "provider_run_id": self.provider_run_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionTurnSnapshot:
        if set(value) != {"phase", "identity", "provider_run_id"}:
            raise ValueError("execution snapshot has unexpected fields")
        return cls(
            phase=value["phase"],
            identity=ExecutionTurnIdentity.from_dict(value["identity"]),
            provider_run_id=value["provider_run_id"],
        )


@dataclass(frozen=True)
class LifecycleSnapshot:
    phase: str = "idle"
    identity: UserTurnIdentity | None = None
    revision: int = 0
    execution: ExecutionTurnSnapshot | None = None
    execution_policy: str | None = None
    completed_execution_count: int = 0

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError("invalid lifecycle phase")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("invalid lifecycle revision")
        if (self.phase == "idle") != (self.identity is None):
            raise ValueError("only idle lifecycle snapshots omit identity")
        if self.execution_policy not in {None, "single", "sequential"}:
            raise ValueError("invalid execution policy")
        if type(self.completed_execution_count) is not int or self.completed_execution_count < 0:
            raise ValueError("invalid completed execution count")
        if self.phase == "idle" and (
            self.execution is not None
            or self.execution_policy is not None
            or self.completed_execution_count
        ):
            raise ValueError("idle lifecycle cannot retain execution state")
        if self.execution is not None and self.execution_policy is None:
            raise ValueError("active execution requires a policy")
        if self.execution is not None:
            execution_phase = self.execution.phase
            provider_run_id = self.execution.provider_run_id
            if execution_phase in {"starting", "aborted"}:
                if execution_phase == "aborted" and provider_run_id is not None:
                    raise ValueError("aborted execution cannot have provider_run_id")
            elif provider_run_id is None:
                raise ValueError(
                    "admitted, detached, and terminal execution requires provider_run_id"
                )
            if self.phase == "stopping" and execution_phase == "running":
                raise ValueError("stopping user turn cannot contain running execution")
            if (
                self.phase == "starting"
                and execution_phase
                in {"running", "stopping", "detached", "detached_stopping"}
            ):
                raise ValueError(
                    "starting user turn cannot contain admitted execution"
                )
            if execution_phase == "stopping" and self.phase != "stopping":
                raise ValueError("stopping execution requires stopping user turn")
            if execution_phase == "detached" and self.phase != "running":
                raise ValueError("detached execution requires running user turn")
            if execution_phase == "detached_stopping" and self.phase != "stopping":
                raise ValueError("detached_stopping requires stopping user turn")
            terminal = execution_phase in {
                "complete", "stopped", "failed", "aborted",
            }
            if terminal and self.completed_execution_count == 0:
                raise ValueError(
                    "terminal execution requires completed count"
                )
            if (
                not terminal
                and self.execution_policy == "single"
                and self.completed_execution_count
            ):
                raise ValueError(
                    "single policy cannot start after completed execution"
                )
        elif self.completed_execution_count:
            raise ValueError("completed execution identity must remain projected")
        elif self.phase != "idle" and self.execution_policy is None:
            raise ValueError("active user turn requires an execution policy")
        if (
            self.execution_policy == "single"
            and self.completed_execution_count > 1
        ):
            raise ValueError("single policy cannot complete multiple executions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "identity": self.identity.to_dict() if self.identity else None,
            "revision": self.revision,
            "execution": self.execution.to_dict() if self.execution else None,
            "execution_policy": self.execution_policy,
            "completed_execution_count": self.completed_execution_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleSnapshot:
        if set(value) != {
            "phase",
            "identity",
            "revision",
            "execution",
            "execution_policy",
            "completed_execution_count",
        }:
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
            execution=(
                ExecutionTurnSnapshot.from_dict(value["execution"])
                if value["execution"] is not None
                else None
            ),
            execution_policy=value["execution_policy"],
            completed_execution_count=value["completed_execution_count"],
        )


@dataclass(frozen=True)
class LifecycleCommand:
    request_id: str
    session_id: str
    kind: str
    identity: UserTurnIdentity
    outcome: str | None = None
    execution_identity: ExecutionTurnIdentity | None = None
    provider_run_id: str | None = None
    replacement_provider_run_id: str | None = None
    execution_policy: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.request_id, "request_id")
        validate_identifier(self.session_id, "session_id")
        if self.kind not in COMMANDS:
            raise ValueError("invalid lifecycle command")
        execution_commands = {
            "start_execution",
            "bind_execution_run",
            "restore_execution_run",
            "confirm_execution_started",
            "detach_execution",
            "adopt_execution",
            "finish_execution",
            "finish_execution_and_turn",
            "abort_execution",
        }
        if (self.kind in execution_commands) != (self.execution_identity is not None):
            raise ValueError("execution command identity mismatch")
        if self.provider_run_id is not None:
            validate_identifier(self.provider_run_id, "provider_run_id")
        if self.replacement_provider_run_id is not None:
            validate_identifier(
                self.replacement_provider_run_id,
                "replacement_provider_run_id",
            )
        if self.kind in {
            "bind_execution_run", "confirm_execution_started", "adopt_execution",
            "finish_execution", "finish_execution_and_turn",
            "restore_execution_run",
        }:
            if self.provider_run_id is None:
                raise ValueError(f"{self.kind} requires provider_run_id")
        elif self.provider_run_id is not None:
            raise ValueError("provider_run_id is invalid for this command")
        if self.kind == "restore_execution_run":
            if self.replacement_provider_run_id is None:
                raise ValueError(
                    "restore_execution_run requires replacement_provider_run_id"
                )
        elif self.replacement_provider_run_id is not None:
            raise ValueError(
                "replacement_provider_run_id is invalid for this command"
            )
        if self.execution_policy not in {None, "single", "sequential"}:
            raise ValueError("invalid command execution policy")
        if (self.kind == "begin_turn") != (self.execution_policy is not None):
            raise ValueError("begin_turn requires execution policy")
        if self.kind in {
            "finish_turn", "finish_execution", "finish_execution_and_turn",
            "abort_execution",
        }:
            if self.outcome not in OUTCOMES:
                raise ValueError("terminal command requires a valid outcome")
        elif self.outcome is not None:
            raise ValueError("outcome is invalid for this command")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "identity": self.identity.to_dict(),
            "outcome": self.outcome,
            "execution_identity": (
                self.execution_identity.to_dict()
                if self.execution_identity
                else None
            ),
            "provider_run_id": self.provider_run_id,
            "replacement_provider_run_id": self.replacement_provider_run_id,
            "execution_policy": self.execution_policy,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LifecycleCommand:
        expected = {
            "request_id", "session_id", "kind", "identity", "outcome",
            "execution_identity", "provider_run_id",
            "replacement_provider_run_id",
            "execution_policy",
        }
        if set(value) not in {
            frozenset(expected),
            frozenset(expected - {"replacement_provider_run_id"}),
        }:
            raise ValueError("lifecycle command has unexpected fields")
        return cls(
            request_id=value["request_id"],
            session_id=value["session_id"],
            kind=value["kind"],
            identity=UserTurnIdentity.from_dict(value["identity"]),
            outcome=value["outcome"],
            execution_identity=(
                ExecutionTurnIdentity.from_dict(value["execution_identity"])
                if value["execution_identity"] is not None
                else None
            ),
            provider_run_id=value["provider_run_id"],
            replacement_provider_run_id=value.get("replacement_provider_run_id"),
            execution_policy=value["execution_policy"],
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
