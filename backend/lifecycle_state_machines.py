from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from event_bus import BusEvent, EventBus
from execution_template import PreparedExecution
import lifecycle_state_store


logger = logging.getLogger(__name__)


_PROMPT_STATES = {
    "user_message_requested": "requested",
    "user_message_queued": "queued",
    "user_message_sent": "sent",
    "user_message_received": "received",
    "user_message_done": "done",
    "user_message_failed": "failed",
}

_TURN_STATES = {
    "lifecycle.turn_start": "running",
    "lifecycle.turn_complete": "complete",
    "lifecycle.turn_stopped": "stopped",
}

_ADMISSION_STATES = {
    "lifecycle.admission_registered": "registered",
    "lifecycle.admission_starting": "starting",
    "lifecycle.admission_admitted": "admitted",
    "lifecycle.admission_spawned": "spawned",
    "lifecycle.admission_deferred": "deferred",
    "lifecycle.admission_cancelled": "cancelled",
    "lifecycle.admission_failed": "failed",
}

_STEER_STATES = {
    "lifecycle.steer_requested": "requested",
    "lifecycle.steer_accepted": "accepted",
    "lifecycle.steer_persisted": "persisted",
    "lifecycle.steer_failed": "failed",
}

_STEER_TRANSITIONS = {
    "requested": {"accepted", "persisted", "failed"},
    "accepted": {"persisted", "failed"},
    "persisted": {"persisted"},
    "failed": {"failed"},
}


@dataclass
class PromptLifecycleMachine:
    message_id: str
    state: str = "requested"


@dataclass
class AdmissionLifecycleMachine:
    provider_run_id: str
    execution_turn_id: str
    user_turn_id: str | None
    lifecycle_message_id: str | None
    assistant_message_id: str | None
    execution: PreparedExecution | None
    handle_id: str | None
    state: str = "registered"

    def cancel(self) -> None:
        if self.state in {"cancelled", "failed"}:
            return
        if self.execution is None:
            self.state = "cancelled"
            return
        # Record the cancel on the single authoritative signal in every
        # branch. A cancel that races with admission resolution on another
        # thread must still leave the Event set so the provider's pre-spawn
        # gates (and the post-spawn teardown safety net) honour it instead
        # of spawning an untracked runner.
        self.execution._request_cancel_after_admission()
        if self.execution.admission_pending:
            self.execution._mark_cancelled()
            self.state = "cancelled"
            return
        self.state = "cancellation_requested"


@dataclass
class SteerLifecycleMachine:
    message_id: str
    provider_run_id: str | None
    state: str = "requested"
    failure_reason: str | None = None


@dataclass
class ExecutionTurnLifecycleMachine:
    execution_turn_id: str
    assistant_message_id: str | None
    state: str = "running"


@dataclass
class TurnLifecycleMachine:
    state: str = "idle"
    user_turn_id: str | None = None
    lifecycle_message_id: str | None = None
    execution_turn_id: str | None = None
    assistant_message_id: str | None = None
    executions: dict[str, ExecutionTurnLifecycleMachine] = field(default_factory=dict)
    admissions: dict[str, AdmissionLifecycleMachine] = field(default_factory=dict)
    steers: dict[str, SteerLifecycleMachine] = field(default_factory=dict)


@dataclass
class SessionLifecycleMachine:
    session_id: str
    prompts: dict[str, PromptLifecycleMachine] = field(default_factory=dict)
    turn: TurnLifecycleMachine = field(default_factory=TurnLifecycleMachine)


class LifecycleStateTree:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, SessionLifecycleMachine] = {}
        self._execution_handles: dict[str, PreparedExecution] = {}
        self._subscriber_name = f"lifecycle_state_tree_{id(self)}"
        self._persist_task: asyncio.Task | None = None
        self._pending_session_projections: dict[str, dict[str, Any] | None] = {}
        self._persist_error: Exception | None = None

    async def bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._loop is not None:
            raise RuntimeError("lifecycle state tree cannot move between event loops")
        self._loop = loop
        projection = await asyncio.to_thread(lifecycle_state_store.load)
        self._restore(projection)
        self._bus.subscribe(
            "user_message_*",
            self._apply,
            priority=5,
            name=self._subscriber_name,
        )
        self._bus.subscribe(
            "lifecycle.*",
            self._apply,
            priority=5,
            name=self._subscriber_name,
        )

    async def close(self) -> None:
        self._assert_owner()
        await self.flush()
        self._bus.unsubscribe(self._subscriber_name)
        for session in self._sessions.values():
            for admission in session.turn.admissions.values():
                admission.cancel()
        self._sessions.clear()
        self._execution_handles.clear()
        self._loop = None

    def register_execution_handle(self, execution: PreparedExecution) -> str:
        self._assert_owner()
        handle_id = str(uuid.uuid4())
        self._execution_handles[handle_id] = execution
        return handle_id

    async def publish(
        self,
        event_type: str,
        *,
        root_id: str,
        session_id: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        await self.bind()
        await self._bus.publish(BusEvent(
            type=event_type,
            root_id=root_id,
            sid=session_id,
            payload=payload,
            run_id=run_id,
            msg_id=message_id,
            persist=False,
        ))

    def session(self, session_id: str) -> SessionLifecycleMachine:
        self._assert_owner()
        return self._sessions.setdefault(
            session_id,
            SessionLifecycleMachine(session_id),
        )

    def has_active_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return bool(
            session.prompts
            or session.turn.admissions
            or session.turn.steers
            or session.turn.state in {"running", "legacy_reconciling"}
        )

    def active_turn_identity(self, session_id: str) -> dict[str, str] | None:
        self._assert_owner()
        session = self._sessions.get(session_id)
        if session is None or session.turn.state != "running":
            return None
        return self._identity_from_turn(session.turn)

    async def _apply(self, event: BusEvent) -> None:
        self._assert_owner()
        session = self.session(event.sid)
        prompt_state = _PROMPT_STATES.get(event.type)
        if prompt_state and event.msg_id:
            if prompt_state in {"done", "failed"}:
                session.prompts.pop(event.msg_id, None)
                self._retire_session_if_idle(event.sid)
                self._schedule_persist(event.sid)
                return
            prompt = session.prompts.setdefault(
                event.msg_id,
                PromptLifecycleMachine(event.msg_id),
            )
            prompt.state = prompt_state
            self._schedule_persist(event.sid)
            return
        turn_state = _TURN_STATES.get(event.type)
        if turn_state:
            identity = self._required_identity(event.payload)
            if turn_state == "running":
                self._start_execution_turn(session.turn, identity)
                self._schedule_persist(event.sid)
                return
            if not self._terminal_matches(session.turn, identity):
                logger.warning(
                    "ignoring stale lifecycle terminal sid=%s current=%s incoming=%s",
                    event.sid,
                    session.turn.execution_turn_id,
                    identity["execution_turn_id"],
                )
                return
            session.turn.state = turn_state
            execution_turn_id = session.turn.execution_turn_id
            if execution_turn_id:
                execution = session.turn.executions.get(execution_turn_id)
                if execution is not None:
                    execution.state = turn_state
            if turn_state in {"complete", "stopped"}:
                for admission in session.turn.admissions.values():
                    admission.cancel()
                    self._execution_handles.pop(admission.handle_id, None)
                session.turn.admissions.clear()
                session.turn.steers.clear()
                self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)
            return
        steer_state = _STEER_STATES.get(event.type)
        if steer_state and event.msg_id:
            steer = session.turn.steers.get(event.msg_id)
            if steer is None:
                steer = SteerLifecycleMachine(event.msg_id, event.run_id)
                session.turn.steers[event.msg_id] = steer
            elif steer_state == "requested":
                return
            elif steer_state not in _STEER_TRANSITIONS.get(steer.state, set()):
                raise ValueError(
                    f"invalid steer transition {steer.state} -> {steer_state}"
                )
            steer.state = steer_state
            if steer.provider_run_id is None and event.run_id is not None:
                steer.provider_run_id = event.run_id
            if steer_state == "failed":
                reason = event.payload.get("reason")
                steer.failure_reason = reason if isinstance(reason, str) else None
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.steer_fallback_queued" and event.msg_id:
            session.turn.steers.pop(event.msg_id, None)
            session.prompts[event.msg_id] = PromptLifecycleMachine(
                event.msg_id,
                "queued",
            )
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.reconcile_turn_missing":
            if session.turn.state == "legacy_reconciling":
                if any(event.payload.values()):
                    return
            elif not self._terminal_matches(
                session.turn,
                self._required_identity(event.payload),
            ):
                return
            session.turn.state = "stopped"
            execution_turn_id = session.turn.execution_turn_id
            if execution_turn_id:
                execution = session.turn.executions.get(execution_turn_id)
                if execution is not None:
                    execution.state = "stopped"
            session.turn.admissions.clear()
            session.turn.steers.clear()
            self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.reconcile_prompt_completed" and event.msg_id:
            session.prompts.pop(event.msg_id, None)
            self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.reconcile_prompt_missing" and event.msg_id:
            session.prompts.pop(event.msg_id, None)
            self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.reconcile_admission_missing" and event.run_id:
            admission = session.turn.admissions.get(event.run_id)
            if admission is None:
                return
            if session.turn.state == "legacy_reconciling":
                if event.payload != {"legacy_reconciling": True}:
                    return
            elif not self._admission_identity_matches(admission, event.payload):
                return
            session.turn.admissions.pop(event.run_id)
            self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.reconcile_steer_parent_gone" and event.msg_id:
            session.turn.steers.pop(event.msg_id, None)
            self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)
            return
        if event.type == "lifecycle.admission_cancel_requested":
            identity = self._required_identity(event.payload)
            for admission in session.turn.admissions.values():
                if not self._admission_identity_matches(admission, identity):
                    continue
                admission.cancel()
            self._schedule_persist(event.sid)
            return
        admission_state = _ADMISSION_STATES.get(event.type)
        if admission_state is None or event.run_id is None:
            return
        if admission_state == "registered":
            handle_id = event.payload.get("execution_handle")
            if not isinstance(handle_id, str):
                raise TypeError("admission registration requires execution handle")
            execution = self._execution_handles.get(handle_id)
            if execution is None:
                raise KeyError("admission execution handle is unavailable")
            identity = self._required_identity(event.payload)
            if (
                session.turn.state != "running"
                or not self._terminal_matches(session.turn, identity)
            ):
                self._execution_handles.pop(handle_id, None)
                raise ValueError("admission registration parent mismatch")
            session.turn.admissions[event.run_id] = AdmissionLifecycleMachine(
                provider_run_id=event.run_id,
                execution_turn_id=identity["execution_turn_id"],
                user_turn_id=identity["user_turn_id"],
                lifecycle_message_id=identity["lifecycle_message_id"],
                assistant_message_id=identity["assistant_message_id"],
                execution=execution,
                handle_id=handle_id,
            )
            self._schedule_persist(event.sid)
            return
        admission = session.turn.admissions.get(event.run_id)
        if admission is not None:
            if not self._admission_identity_matches(admission, event.payload):
                return
            admission.state = admission_state
            if admission_state in {"spawned", "cancelled", "failed"}:
                self._execution_handles.pop(admission.handle_id, None)
            if admission_state in {"spawned", "cancelled", "failed"}:
                session.turn.admissions.pop(event.run_id, None)
                self._retire_session_if_idle(event.sid)
            self._schedule_persist(event.sid)

    async def reconcile(
        self,
        session_id: str,
        *,
        live_run_ids: set[str],
        queued_message_ids: set[str],
        completed_message_ids: set[str],
    ) -> None:
        self._assert_owner()
        session = self._sessions.get(session_id)
        if session is None:
            return
        root_id = session_id
        facts: list[tuple[str, str | None, str | None]] = []
        turn_missing = (
            session.turn.state in {"running", "legacy_reconciling"}
            and not live_run_ids
        )
        if turn_missing:
            facts.append(("lifecycle.reconcile_turn_missing", None, None))
        for message_id in session.prompts:
            if message_id in queued_message_ids:
                continue
            if message_id in completed_message_ids:
                facts.append(("lifecycle.reconcile_prompt_completed", message_id, None))
                continue
            facts.append(("lifecycle.reconcile_prompt_missing", message_id, None))
        if not turn_missing:
            for run_id in session.turn.admissions:
                if run_id not in live_run_ids:
                    facts.append((
                        "lifecycle.reconcile_admission_missing",
                        None,
                        run_id,
                    ))
        if not turn_missing:
            for message_id, steer in session.turn.steers.items():
                if steer.provider_run_id in live_run_ids:
                    continue
                facts.append((
                    "lifecycle.reconcile_steer_parent_gone",
                    message_id,
                    steer.provider_run_id,
                ))
        for event_type, message_id, run_id in facts:
            payload: dict[str, Any] = {}
            if event_type == "lifecycle.reconcile_turn_missing":
                if session.turn.state != "legacy_reconciling":
                    payload = self._identity_from_turn(session.turn)
            elif event_type == "lifecycle.reconcile_admission_missing" and run_id:
                if session.turn.state == "legacy_reconciling":
                    payload = {"legacy_reconciling": True}
                else:
                    admission = session.turn.admissions[run_id]
                    payload = self._identity_from_admission(admission)
            await self.publish(
                event_type,
                root_id=root_id,
                session_id=session_id,
                run_id=run_id,
                message_id=message_id,
                payload=payload,
            )

    def reconciliation_requirements(self) -> dict[str, dict[str, Any]]:
        self._assert_owner()
        requirements: dict[str, dict[str, Any]] = {}
        for session_id, session in self._sessions.items():
            prompt_message_ids = {
                message_id
                for message_id, prompt in session.prompts.items()
                if prompt.state in {"queued", "sent", "received"}
            }
            run_ids = set(session.turn.admissions)
            run_ids.update(
                steer.provider_run_id
                for steer in session.turn.steers.values()
                if steer.provider_run_id
            )
            if (
                not prompt_message_ids
                and session.turn.state not in {"running", "legacy_reconciling"}
                and not run_ids
            ):
                continue
            requirements[session_id] = {
                "prompt_message_ids": prompt_message_ids,
                "needs_live_runs": (
                    session.turn.state in {"running", "legacy_reconciling"}
                    or bool(run_ids)
                ),
            }
        return requirements

    async def flush(self) -> None:
        self._assert_owner()
        if self._pending_session_projections and (
            self._persist_task is None or self._persist_task.done()
        ):
            self._persist_task = asyncio.create_task(self._persist_loop())
        if self._persist_task is not None:
            await self._persist_task
        if self._pending_session_projections:
            error = self._persist_error or RuntimeError("lifecycle state persist incomplete")
            raise RuntimeError("lifecycle state persist failed") from error

    def _schedule_persist(self, session_id: str) -> None:
        self._pending_session_projections[session_id] = self._snapshot_session(session_id)
        if self._persist_task is None or self._persist_task.done():
            self._persist_task = asyncio.create_task(self._persist_loop())

    async def _persist_loop(self) -> None:
        while self._pending_session_projections:
            changes = self._pending_session_projections
            self._pending_session_projections = {}
            try:
                await asyncio.to_thread(lifecycle_state_store.merge_sessions, changes)
                self._persist_error = None
            except Exception as exc:
                newer_changes_pending = bool(self._pending_session_projections)
                self._persist_error = exc
                for session_id, projection in changes.items():
                    self._pending_session_projections.setdefault(session_id, projection)
                logger.exception("lifecycle state projection persist failed")
                if newer_changes_pending:
                    continue
                return

    def _snapshot_session(self, session_id: str) -> dict[str, Any] | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return {
            "prompts": {
                message_id: {"state": prompt.state}
                for message_id, prompt in session.prompts.items()
            },
            "turn": {
                "state": session.turn.state,
                "user_turn_id": session.turn.user_turn_id,
                "lifecycle_message_id": session.turn.lifecycle_message_id,
                "execution_turn_id": session.turn.execution_turn_id,
                "assistant_message_id": session.turn.assistant_message_id,
                "executions": {
                    execution_turn_id: {
                        "state": execution.state,
                        "assistant_message_id": execution.assistant_message_id,
                    }
                    for execution_turn_id, execution in session.turn.executions.items()
                },
                "admissions": {
                    run_id: {
                        "execution_turn_id": admission.execution_turn_id,
                        "user_turn_id": admission.user_turn_id,
                        "lifecycle_message_id": admission.lifecycle_message_id,
                        "assistant_message_id": admission.assistant_message_id,
                        "state": admission.state,
                    }
                    for run_id, admission in session.turn.admissions.items()
                },
                "steers": {
                    message_id: {
                        "provider_run_id": steer.provider_run_id,
                        "state": steer.state,
                        "failure_reason": steer.failure_reason,
                    }
                    for message_id, steer in session.turn.steers.items()
                },
            },
        }

    def _restore(self, projection: dict[str, Any]) -> None:
        self._sessions.clear()
        for session_id, raw_session in projection.get("sessions", {}).items():
            if not isinstance(session_id, str) or not isinstance(raw_session, dict):
                continue
            session = SessionLifecycleMachine(session_id)
            for message_id, raw_prompt in (raw_session.get("prompts") or {}).items():
                if isinstance(message_id, str) and isinstance(raw_prompt, dict):
                    session.prompts[message_id] = PromptLifecycleMachine(
                        message_id,
                        str(raw_prompt.get("state") or "queued"),
                    )
            raw_turn = raw_session.get("turn") or {}
            session.turn.state = str(raw_turn.get("state") or "idle")
            session.turn.user_turn_id = self._optional_identity(
                raw_turn,
                "user_turn_id",
            )
            session.turn.lifecycle_message_id = self._optional_identity(
                raw_turn,
                "lifecycle_message_id",
            )
            session.turn.execution_turn_id = self._optional_identity(
                raw_turn,
                "execution_turn_id",
            )
            session.turn.assistant_message_id = self._optional_identity(
                raw_turn,
                "assistant_message_id",
            )
            for execution_turn_id, raw_execution in (
                raw_turn.get("executions") or {}
            ).items():
                if not isinstance(execution_turn_id, str) or not isinstance(
                    raw_execution,
                    dict,
                ):
                    continue
                session.turn.executions[execution_turn_id] = (
                    ExecutionTurnLifecycleMachine(
                        execution_turn_id,
                        self._optional_identity(
                            raw_execution,
                            "assistant_message_id",
                        ),
                        str(raw_execution.get("state") or "running"),
                    )
                )
            for run_id, raw_admission in (raw_turn.get("admissions") or {}).items():
                if not isinstance(run_id, str) or not isinstance(raw_admission, dict):
                    continue
                execution_turn_id = raw_admission.get("execution_turn_id")
                if not isinstance(execution_turn_id, str):
                    continue
                session.turn.admissions[run_id] = AdmissionLifecycleMachine(
                    run_id,
                    execution_turn_id,
                    self._optional_identity(raw_admission, "user_turn_id"),
                    self._optional_identity(
                        raw_admission,
                        "lifecycle_message_id",
                    ),
                    self._optional_identity(
                        raw_admission,
                        "assistant_message_id",
                    ),
                    None,
                    None,
                    str(raw_admission.get("state") or "registered"),
                )
            for message_id, raw_steer in (raw_turn.get("steers") or {}).items():
                if not isinstance(message_id, str) or not isinstance(raw_steer, dict):
                    continue
                provider_run_id = raw_steer.get("provider_run_id")
                session.turn.steers[message_id] = SteerLifecycleMachine(
                    message_id,
                    provider_run_id if isinstance(provider_run_id, str) else None,
                    str(raw_steer.get("state") or "requested"),
                    raw_steer.get("failure_reason"),
                )
            self._sessions[session_id] = session

    @staticmethod
    def _optional_identity(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _required_identity(payload: dict[str, Any]) -> dict[str, str]:
        identity: dict[str, str] = {}
        for key in (
            "user_turn_id",
            "lifecycle_message_id",
            "execution_turn_id",
            "assistant_message_id",
        ):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"lifecycle fact requires {key}")
            identity[key] = value
        return identity

    @staticmethod
    def _identity_from_turn(turn: TurnLifecycleMachine) -> dict[str, str]:
        return LifecycleStateTree._required_identity({
            "user_turn_id": turn.user_turn_id,
            "lifecycle_message_id": turn.lifecycle_message_id,
            "execution_turn_id": turn.execution_turn_id,
            "assistant_message_id": turn.assistant_message_id,
        })

    @staticmethod
    def _identity_from_admission(
        admission: AdmissionLifecycleMachine,
    ) -> dict[str, str]:
        return LifecycleStateTree._required_identity({
            "user_turn_id": admission.user_turn_id,
            "lifecycle_message_id": admission.lifecycle_message_id,
            "execution_turn_id": admission.execution_turn_id,
            "assistant_message_id": admission.assistant_message_id,
        })

    @staticmethod
    def _admission_identity_matches(
        admission: AdmissionLifecycleMachine,
        payload: dict[str, Any],
    ) -> bool:
        try:
            return (
                LifecycleStateTree._identity_from_admission(admission)
                == LifecycleStateTree._required_identity(payload)
            )
        except ValueError:
            return False

    @staticmethod
    def _start_execution_turn(
        turn: TurnLifecycleMachine,
        identity: dict[str, str],
    ) -> None:
        incoming_execution_id = identity["execution_turn_id"]
        if (
            turn.state == "running"
            and turn.execution_turn_id
            and incoming_execution_id != turn.execution_turn_id
        ):
            raise ValueError("cannot replace a running execution turn")
        incoming_user_turn_id = identity["user_turn_id"]
        if (
            turn.user_turn_id
            and turn.user_turn_id != incoming_user_turn_id
        ):
            if turn.state == "running":
                raise ValueError("cannot replace an active logical user turn")
            if turn.state == "legacy_reconciling":
                raise ValueError("legacy logical turn requires reconciliation")
            if turn.admissions:
                raise ValueError("terminal logical turn retained admissions")
            turn.user_turn_id = None
            turn.lifecycle_message_id = None
            turn.execution_turn_id = None
            turn.assistant_message_id = None
            turn.executions.clear()
            turn.steers.clear()
        turn.state = "running"
        turn.user_turn_id = incoming_user_turn_id
        turn.lifecycle_message_id = identity["lifecycle_message_id"]
        turn.execution_turn_id = incoming_execution_id
        turn.assistant_message_id = identity["assistant_message_id"]
        if incoming_execution_id:
            turn.executions[incoming_execution_id] = ExecutionTurnLifecycleMachine(
                incoming_execution_id,
                identity["assistant_message_id"],
            )

    @staticmethod
    def _terminal_matches(
        turn: TurnLifecycleMachine,
        identity: dict[str, str],
    ) -> bool:
        for field_name in (
            "user_turn_id",
            "lifecycle_message_id",
            "execution_turn_id",
            "assistant_message_id",
        ):
            current = getattr(turn, field_name)
            incoming = identity[field_name]
            if current and incoming != current:
                return False
        return True

    def _retire_session_if_idle(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.prompts or session.turn.admissions or session.turn.steers:
            return
        if session.turn.state in {"running", "legacy_reconciling"}:
            return
        self._sessions.pop(session_id, None)

    def _assert_owner(self) -> None:
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("lifecycle state mutation must run on its owning loop")
