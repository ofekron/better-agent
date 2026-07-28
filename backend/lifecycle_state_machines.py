from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from event_bus import BusEvent, EventBus
from execution_template import PreparedExecution


_PROMPT_STATES = {
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


@dataclass
class PromptLifecycleMachine:
    message_id: str
    state: str = "queued"


@dataclass
class AdmissionLifecycleMachine:
    provider_run_id: str
    turn_run_id: str
    execution: PreparedExecution
    handle_id: str
    state: str = "registered"

    def cancel(self) -> None:
        if self.state in {"cancelled", "failed"}:
            return
        if self.execution.admission_pending:
            self.execution._mark_cancelled()
            self.state = "cancelled"
            return
        self.execution._request_cancel_after_admission()
        self.state = "cancellation_requested"


@dataclass
class TurnLifecycleMachine:
    state: str = "idle"
    admissions: dict[str, AdmissionLifecycleMachine] = field(default_factory=dict)


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

    def bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._loop is not None:
            raise RuntimeError("lifecycle state tree cannot move between event loops")
        self._loop = loop
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

    def close(self) -> None:
        self._assert_owner()
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
        self.bind()
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

    async def _apply(self, event: BusEvent) -> None:
        self._assert_owner()
        session = self.session(event.sid)
        prompt_state = _PROMPT_STATES.get(event.type)
        if prompt_state and event.msg_id:
            if prompt_state in {"done", "failed"}:
                session.prompts.pop(event.msg_id, None)
                self._retire_session_if_idle(event.sid)
                return
            prompt = session.prompts.setdefault(
                event.msg_id,
                PromptLifecycleMachine(event.msg_id),
            )
            prompt.state = prompt_state
            return
        turn_state = _TURN_STATES.get(event.type)
        if turn_state:
            session.turn.state = turn_state
            if turn_state in {"complete", "stopped"}:
                for admission in session.turn.admissions.values():
                    admission.cancel()
                    self._execution_handles.pop(admission.handle_id, None)
                session.turn.admissions.clear()
                self._retire_session_if_idle(event.sid)
            return
        if event.type == "lifecycle.admission_cancel_requested":
            for admission in session.turn.admissions.values():
                admission.cancel()
            return
        admission_state = _ADMISSION_STATES.get(event.type)
        if admission_state is None or event.run_id is None:
            return
        if admission_state == "registered":
            handle_id = event.payload.get("execution_handle")
            turn_run_id = event.payload.get("turn_run_id")
            if not isinstance(handle_id, str):
                raise TypeError("admission registration requires execution handle")
            execution = self._execution_handles.get(handle_id)
            if execution is None:
                raise KeyError("admission execution handle is unavailable")
            if not isinstance(turn_run_id, str) or not turn_run_id:
                raise ValueError("admission registration requires turn_run_id")
            session.turn.admissions[event.run_id] = AdmissionLifecycleMachine(
                provider_run_id=event.run_id,
                turn_run_id=turn_run_id,
                execution=execution,
                handle_id=handle_id,
            )
            return
        admission = session.turn.admissions.get(event.run_id)
        if admission is not None:
            admission.state = admission_state
            if admission_state in {"spawned", "cancelled", "failed"}:
                self._execution_handles.pop(admission.handle_id, None)
            if admission_state in {"spawned", "cancelled", "failed"}:
                session.turn.admissions.pop(event.run_id, None)
                self._retire_session_if_idle(event.sid)

    def _retire_session_if_idle(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.prompts or session.turn.admissions:
            return
        if session.turn.state in {"running"}:
            return
        self._sessions.pop(session_id, None)

    def _assert_owner(self) -> None:
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("lifecycle state mutation must run on its owning loop")
