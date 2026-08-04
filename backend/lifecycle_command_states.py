from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from lifecycle_command_model import (
    LifecycleCommand,
    LifecycleEffect,
    LifecycleSnapshot,
    ExecutionTurnSnapshot,
    TransitionPlan,
    materialize_json,
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
        execution = snapshot.execution
        policy = snapshot.execution_policy
        if command.kind == "begin_turn":
            policy = command.execution_policy
        completed = snapshot.completed_execution_count
        if next_phase == "idle":
            execution = None
            policy = None
            completed = 0
        next_snapshot = LifecycleSnapshot(
            phase=next_phase,
            identity=next_identity,
            revision=snapshot.revision + 1,
            execution=execution,
            execution_policy=policy,
            completed_execution_count=completed,
        )
        payload = {
            "request_id": command.request_id,
            "command": command.kind,
            "source_phase": snapshot.phase,
            "next_phase": next_phase,
            "identity": command.identity.to_dict(),
        }
        if snapshot.execution is not None:
            payload["execution_identity"] = snapshot.execution.identity.to_dict()
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

    def _execution_plan(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_identity(snapshot, command)
        execution_identity = command.execution_identity
        if execution_identity is None:
            raise LifecycleCommandRejected("execution identity is required")
        execution = snapshot.execution
        policy = snapshot.execution_policy
        completed = snapshot.completed_execution_count
        effect_kind: str
        if command.kind == "start_execution":
            if execution is not None and execution.phase not in {
                "complete", "stopped", "failed", "aborted",
            }:
                raise LifecycleCommandRejected("execution overlap is forbidden")
            if policy is None:
                raise LifecycleCommandRejected("execution policy is not frozen")
            if policy == "single" and completed:
                raise LifecycleCommandRejected(
                    "single-execution user turn already ran"
                )
            execution = ExecutionTurnSnapshot(
                phase="starting",
                identity=execution_identity,
            )
            effect_kind = "observe_execution_started"
        else:
            if execution is None or execution.identity != execution_identity:
                raise LifecycleCommandRejected(
                    "stale or mismatched execution identity"
                )
            if command.kind == "bind_execution_run":
                if execution.phase not in {
                    "starting", "running", "stopping",
                    "detached", "detached_stopping",
                }:
                    raise LifecycleCommandRejected(
                        "provider run cannot bind in this execution phase"
                    )
                execution = ExecutionTurnSnapshot(
                    execution.phase,
                    execution.identity,
                    command.provider_run_id,
                )
                effect_kind = "observe_execution_run_bound"
            elif command.kind == "restore_execution_run":
                if execution.provider_run_id != command.provider_run_id:
                    raise LifecycleCommandRejected(
                        "provider run election changed"
                    )
                execution = ExecutionTurnSnapshot(
                    execution.phase,
                    execution.identity,
                    command.replacement_provider_run_id,
                )
                effect_kind = "observe_execution_run_bound"
            elif command.kind == "confirm_execution_started":
                if execution.phase not in {"starting", "running"}:
                    raise LifecycleCommandRejected(
                        "execution admission is not awaiting confirmation"
                    )
                if execution.provider_run_id != command.provider_run_id:
                    raise LifecycleCommandRejected("provider run identity mismatch")
                execution = ExecutionTurnSnapshot(
                    "running",
                    execution.identity,
                    execution.provider_run_id,
                )
                effect_kind = "observe_execution_admitted"
            elif command.kind == "detach_execution":
                if execution.phase not in {"running", "stopping"}:
                    raise LifecycleCommandRejected(
                        "only an admitted execution can detach"
                    )
                execution = ExecutionTurnSnapshot(
                    (
                        "detached_stopping"
                        if execution.phase == "stopping"
                        else "detached"
                    ),
                    execution.identity,
                    execution.provider_run_id,
                )
                effect_kind = "observe_execution_detached"
            elif command.kind == "adopt_execution":
                if execution.phase not in {"detached", "detached_stopping"}:
                    raise LifecycleCommandRejected(
                        "only a detached execution can be adopted"
                    )
                if execution.provider_run_id != command.provider_run_id:
                    raise LifecycleCommandRejected("provider run identity mismatch")
                execution = ExecutionTurnSnapshot(
                    (
                        "stopping"
                        if execution.phase == "detached_stopping"
                        else "running"
                    ),
                    execution.identity,
                    execution.provider_run_id,
                )
                effect_kind = "observe_execution_adopted"
            elif command.kind in {
                "finish_execution",
                "finish_execution_and_turn",
            }:
                if execution.provider_run_id != command.provider_run_id:
                    raise LifecycleCommandRejected("provider run identity mismatch")
                if execution.phase in {"complete", "stopped", "failed", "aborted"}:
                    if command.kind != "finish_execution_and_turn":
                        raise LifecycleCommandRejected(
                            "execution is already terminal"
                        )
                else:
                    execution = ExecutionTurnSnapshot(
                        command.outcome,
                        execution.identity,
                        execution.provider_run_id,
                    )
                    completed += 1
                effect_kind = "observe_execution_finished"
            elif command.kind == "abort_execution":
                if execution.phase != "starting" or execution.provider_run_id is not None:
                    raise LifecycleCommandRejected(
                        "only an unbound execution can abort"
                    )
                execution = ExecutionTurnSnapshot(
                    "aborted",
                    execution.identity,
                )
                completed += 1
                effect_kind = "observe_execution_aborted"
            else:
                raise LifecycleCommandRejected(
                    f"{command.kind} is invalid while lifecycle is {self.phase}"
                )
        next_user_phase = snapshot.phase
        if (
            command.kind == "confirm_execution_started"
            and snapshot.phase == "starting"
        ):
            next_user_phase = "running"
        finish_user_turn = (
            command.kind == "finish_execution_and_turn"
            or (
                command.kind == "abort_execution"
                and policy == "single"
            )
        )
        if finish_user_turn:
            next_user_phase = "idle"
        next_snapshot = LifecycleSnapshot(
            phase=(
                next_user_phase
            ),
            identity=(
                None
                if finish_user_turn
                else snapshot.identity
            ),
            revision=snapshot.revision + 1,
            execution=None if finish_user_turn else execution,
            execution_policy=None if finish_user_turn else policy,
            completed_execution_count=(
                0 if finish_user_turn else completed
            ),
        )
        payload = {
            "request_id": command.request_id,
            "command": command.kind,
            "source_phase": snapshot.phase,
            "next_phase": snapshot.phase,
            "identity": command.identity.to_dict(),
            "execution_identity": execution_identity.to_dict(),
            "provider_run_id": command.provider_run_id,
        }
        if command.outcome is not None:
            payload["outcome"] = (
                execution.phase
                if command.kind in {
                    "finish_execution",
                    "finish_execution_and_turn",
                }
                else command.outcome
            )
        return TransitionPlan(
            next_snapshot=next_snapshot,
            effects=(LifecycleEffect(
                effect_id=effect_id_for(command, 0),
                kind=effect_kind,
                payload=payload,
            ),),
            fact_type="lifecycle_command_completed",
            fact_payload=payload,
        )

    def _custom_plan(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
        next_snapshot: LifecycleSnapshot,
        effect_kind: str,
    ) -> TransitionPlan:
        payload = {
            "request_id": command.request_id,
            "command": command.kind,
            "source_phase": snapshot.phase,
            "next_phase": next_snapshot.phase,
            "identity": command.identity.to_dict(),
        }
        return TransitionPlan(
            next_snapshot=next_snapshot,
            effects=(LifecycleEffect(
                effect_id=effect_id_for(command, 0),
                kind=effect_kind,
                payload=payload,
            ),),
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
        if command.execution_policy is None:
            raise LifecycleCommandRejected("execution policy is required")
        plan = self._plan(
            snapshot,
            command,
            next_phase="starting",
            effect_kind="observe_turn_begin",
        )
        next_snapshot = LifecycleSnapshot(
            phase=plan.next_snapshot.phase,
            identity=plan.next_snapshot.identity,
            revision=plan.next_snapshot.revision,
            execution_policy=command.execution_policy,
        )
        return TransitionPlan(
            next_snapshot=next_snapshot,
            effects=plan.effects,
            fact_type=plan.fact_type,
            fact_payload=materialize_json(plan.fact_payload),
        )


class StartingState(LifecycleState):
    phase = "starting"
    accepted_commands = frozenset({
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

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        self._require_identity(snapshot, command)
        if command.execution_identity is not None:
            return self._execution_plan(snapshot, command)
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
        if (
            snapshot.execution is not None
            and snapshot.execution.phase not in {"complete", "stopped", "failed", "aborted"}
        ):
            raise LifecycleCommandRejected(
                "user turn cannot finish with an active execution"
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
    accepted_commands = frozenset({
        "request_stop", "finish_turn", "start_execution",
        "bind_execution_run", "confirm_execution_started",
        "restore_execution_run",
        "detach_execution", "adopt_execution", "finish_execution",
        "finish_execution_and_turn",
        "abort_execution",
    })

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        self._require_identity(snapshot, command)
        if command.execution_identity is not None:
            return self._execution_plan(snapshot, command)
        if command.kind == "request_stop":
            execution = snapshot.execution
            if execution is not None and execution.phase not in {
                "complete", "stopped", "failed", "aborted",
            }:
                execution = ExecutionTurnSnapshot(
                    (
                        "detached_stopping"
                        if execution.phase == "detached"
                        else "stopping"
                    ),
                    execution.identity,
                    execution.provider_run_id,
                )
                next_snapshot = LifecycleSnapshot(
                    phase="stopping",
                    identity=snapshot.identity,
                    revision=snapshot.revision + 1,
                    execution=execution,
                    execution_policy=snapshot.execution_policy,
                    completed_execution_count=snapshot.completed_execution_count,
                )
                return self._custom_plan(
                    snapshot, command, next_snapshot,
                    "observe_stop_requested",
                )
            return self._plan(
                snapshot,
                command,
                next_phase="stopping",
                effect_kind="observe_stop_requested",
            )
        if (
            snapshot.execution is not None
            and snapshot.execution.phase not in {"complete", "stopped", "failed", "aborted"}
        ):
            raise LifecycleCommandRejected(
                "user turn cannot finish with an active execution"
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
    accepted_commands = frozenset({
        "finish_turn", "bind_execution_run", "detach_execution",
        "restore_execution_run",
        "adopt_execution", "finish_execution",
        "finish_execution_and_turn",
        "abort_execution",
    })

    def decide(
        self,
        snapshot: LifecycleSnapshot,
        command: LifecycleCommand,
    ) -> TransitionPlan:
        self._require_accepted(command)
        self._require_identity(snapshot, command)
        if command.execution_identity is not None:
            return self._execution_plan(snapshot, command)
        if (
            snapshot.execution is not None
            and snapshot.execution.phase not in {"complete", "stopped", "failed", "aborted"}
        ):
            raise LifecycleCommandRejected(
                "user turn cannot finish with an active execution"
            )
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
