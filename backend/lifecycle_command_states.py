from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from lifecycle_command_model import (
    LifecycleCommand,
    LifecycleEffect,
    LifecycleSnapshot,
    TransitionPlan,
)


class LifecycleCommandRejected(ValueError):
    pass


class LifecycleState(ABC):
    phase: str
    accepted_commands: frozenset[str]

    @abstractmethod
    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        raise NotImplementedError

    def _require_accepted(self, command: LifecycleCommand) -> None:
        if command.kind not in self.accepted_commands:
            raise LifecycleCommandRejected(
                f"{command.kind} is invalid while lifecycle is {self.phase}"
            )

    def _require_identity(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> None:
        if snapshot.identity != command.identity:
            raise LifecycleCommandRejected("stale or mismatched turn identity")

    def _plan(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
        *,
        next_phase: str,
        effect_kind: str,
        outcome: str | None = None,
    ) -> TransitionPlan:
        next_identity = None if next_phase == "idle" else command.identity
        next_snapshot = LifecycleSnapshot(
            phase=next_phase,
            identity=next_identity,
            revision=snapshot.revision + 1,
        )
        payload = {
            "request_id": command.request_id,
            "command": command.kind,
            "source_phase": snapshot.phase,
            "next_phase": next_phase,
            "identity": command.identity.to_dict(),
        }
        if outcome is not None:
            payload["outcome"] = outcome
        effect = LifecycleEffect(
            effect_id=effect_id_for(command, 0),
            kind=effect_kind,
            payload=payload,
        )
        return TransitionPlan(
            next_snapshot=next_snapshot,
            effects=(effect,),
            fact_type="lifecycle_command_completed",
            fact_payload=payload,
        )

class IdleState(LifecycleState):
    phase = "idle"
    accepted_commands = frozenset({"begin_turn"})

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        return self._plan(
            snapshot,
            command,
            next_phase="starting",
            effect_kind="observe_turn_begin",
        )


class StartingState(LifecycleState):
    phase = "starting"
    accepted_commands = frozenset({
        "confirm_started",
        "request_stop",
        "finish_turn",
    })

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        self._require_identity(snapshot, command)
        if command.kind == "confirm_started":
            return self._plan(
                snapshot,
                command,
                next_phase="running",
                effect_kind="observe_turn_started",
            )
        if command.kind == "request_stop":
            return self._plan(
                snapshot,
                command,
                next_phase="stopping",
                effect_kind="observe_stop_requested",
            )
        return self._plan(
            snapshot,
            command,
            next_phase="idle",
            effect_kind="observe_turn_finished",
            outcome=command.outcome,
        )


class RunningState(LifecycleState):
    phase = "running"
    accepted_commands = frozenset({"request_stop", "finish_turn"})

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        self._require_identity(snapshot, command)
        if command.kind == "request_stop":
            return self._plan(
                snapshot,
                command,
                next_phase="stopping",
                effect_kind="observe_stop_requested",
            )
        return self._plan(
            snapshot,
            command,
            next_phase="idle",
            effect_kind="observe_turn_finished",
            outcome=command.outcome,
        )


class StoppingState(LifecycleState):
    phase = "stopping"
    accepted_commands = frozenset({"finish_turn"})

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        self._require_identity(snapshot, command)
        return self._plan(
            snapshot,
            command,
            next_phase="idle",
            effect_kind="observe_turn_finished",
            outcome=command.outcome,
        )


STATES: dict[str, LifecycleState] = {
    state.phase: state
    for state in (IdleState(), StartingState(), RunningState(), StoppingState())
}


def effect_id_for(command: LifecycleCommand, ordinal: int) -> str:
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("effect ordinal must be a non-negative integer")
    material = (
        f"{command.session_id}\0{command.request_id}\0"
        f"{command.kind}\0{ordinal}"
    ).encode("utf-8")
    return f"lifecycle:{hashlib.sha256(material).hexdigest()}"
