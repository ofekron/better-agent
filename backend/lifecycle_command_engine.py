from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from event_bus import BusEvent, EventBus
from lifecycle_command_model import (
    CommandResult,
    LifecycleCommand,
    LifecycleEffect,
    LifecycleSnapshot,
    TurnIdentity,
    freeze_json,
    materialize_json,
    validate_identifier,
)
from lifecycle_command_states import STATES, LifecycleCommandRejected
import lifecycle_command_store


logger = logging.getLogger(__name__)


class IdentityRetired(RuntimeError):
    pass


@runtime_checkable
class IdempotentEffectHandler(Protocol):
    """At-least-once sink deduped by effect_id and provider run identity."""

    async def execute_idempotently(
        self,
        effect: LifecycleEffect,
    ) -> Mapping[str, Any]:
        ...


class ObservationEffectHandler:
    async def execute_idempotently(
        self,
        effect: LifecycleEffect,
    ) -> Mapping[str, Any]:
        return {"observed": effect.kind}


@dataclass(frozen=True)
class _PhaseWait:
    future: asyncio.Future[LifecycleSnapshot]
    phases: frozenset[str]
    identity: TurnIdentity | None
    min_revision: int


class LifecycleCommandEngine:
    def __init__(
        self,
        event_bus: EventBus,
        *,
        effect_handler: IdempotentEffectHandler | None = None,
    ) -> None:
        handler = effect_handler or ObservationEffectHandler()
        if not isinstance(handler, IdempotentEffectHandler):
            raise TypeError(
                "effect_handler must implement execute_idempotently(effect); "
                "provider effects require effect/run identity deduplication"
            )
        self._bus = event_bus
        self._effect_handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bind_task: asyncio.Task[None] | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, set[_PhaseWait]] = {}

    async def bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("lifecycle command engine cannot move between event loops")
        if self._bind_task is None:
            self._bind_task = loop.create_task(self._recover())
        await asyncio.shield(self._bind_task)

    async def _recover(self) -> None:
        await asyncio.to_thread(lifecycle_command_store.initialize)
        pending = await asyncio.to_thread(
            lifecycle_command_store.unfinished_transitions
        )
        for session_id, request_id in pending:
            async with self._lock_for(session_id):
                await self._resume_transition(session_id, request_id)

    async def close(self) -> None:
        self._assert_owner()
        try:
            if self._bind_task is not None:
                await asyncio.shield(self._bind_task)
        finally:
            for waiters in self._waiters.values():
                for waiter in waiters:
                    if not waiter.future.done():
                        waiter.future.cancel()
            self._waiters.clear()
            self._session_locks.clear()
            self._bind_task = None
            self._loop = None

    def snapshot(self, session_id: str) -> LifecycleSnapshot:
        self._assert_ready()
        validate_identifier(session_id, "session_id")
        return lifecycle_command_store.session_snapshot(session_id)

    async def begin_turn(
        self,
        *,
        request_id: str,
        session_id: str,
        identity: TurnIdentity,
    ) -> CommandResult:
        return await self.execute(LifecycleCommand(
            request_id=request_id,
            session_id=session_id,
            kind="begin_turn",
            identity=identity,
        ))

    async def confirm_started(
        self,
        *,
        request_id: str,
        session_id: str,
        identity: TurnIdentity,
    ) -> CommandResult:
        return await self.execute(LifecycleCommand(
            request_id=request_id,
            session_id=session_id,
            kind="confirm_started",
            identity=identity,
        ))

    async def request_stop(
        self,
        *,
        request_id: str,
        session_id: str,
        identity: TurnIdentity,
    ) -> CommandResult:
        return await self.execute(LifecycleCommand(
            request_id=request_id,
            session_id=session_id,
            kind="request_stop",
            identity=identity,
        ))

    async def finish_turn(
        self,
        *,
        request_id: str,
        session_id: str,
        identity: TurnIdentity,
        outcome: str,
    ) -> CommandResult:
        return await self.execute(LifecycleCommand(
            request_id=request_id,
            session_id=session_id,
            kind="finish_turn",
            identity=identity,
            outcome=outcome,
        ))

    async def execute(self, command: LifecycleCommand) -> CommandResult:
        await self.bind()
        async with self._lock_for(command.session_id):
            existing = await asyncio.to_thread(
                lifecycle_command_store.transition_for,
                command.session_id,
                command.request_id,
            )
            if existing is not None:
                if existing["fingerprint"] != command.fingerprint():
                    raise LifecycleCommandRejected(
                        "request_id is already bound to another command"
                    )
                return await self._resume_transition(
                    command.session_id,
                    command.request_id,
                )
            snapshot = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                command.session_id,
            )
            plan = STATES[snapshot.phase].decide(snapshot, command)
            try:
                disposition = await asyncio.to_thread(
                    lifecycle_command_store.persist_plan,
                    command,
                    snapshot,
                    plan,
                )
            except lifecycle_command_store.TransitionConflict as exc:
                raise LifecycleCommandRejected(str(exc)) from exc
            if disposition not in {"inserted", "existing"}:
                raise RuntimeError("unknown lifecycle plan disposition")
            return await self._resume_transition(
                command.session_id,
                command.request_id,
            )

    async def wait_for_phase(
        self,
        session_id: str,
        phases: Collection[str],
        *,
        identity: TurnIdentity | None = None,
        min_revision: int = 0,
    ) -> LifecycleSnapshot:
        await self.bind()
        validate_identifier(session_id, "session_id")
        expected = frozenset(phases)
        if not expected or not expected.issubset(STATES):
            raise ValueError("wait phases must be known lifecycle phases")
        if type(min_revision) is not int or min_revision < 0:
            raise ValueError("min_revision must be a non-negative integer")
        snapshot = await asyncio.to_thread(
            lifecycle_command_store.session_snapshot,
            session_id,
        )
        if self._wait_matches(snapshot, expected, identity, min_revision):
            return snapshot
        if identity is not None and snapshot.identity != identity:
            raise IdentityRetired("requested turn identity is no longer active")
        waiter = _PhaseWait(
            asyncio.get_running_loop().create_future(),
            expected,
            identity,
            min_revision,
        )
        self._waiters.setdefault(session_id, set()).add(waiter)
        try:
            return await waiter.future
        finally:
            session_waiters = self._waiters.get(session_id)
            if session_waiters is not None:
                session_waiters.discard(waiter)
                if not session_waiters:
                    self._waiters.pop(session_id, None)

    def waiter_count(self, session_id: str) -> int:
        self._assert_ready()
        return len(self._waiters.get(session_id, ()))

    async def _resume_transition(
        self,
        session_id: str,
        request_id: str,
    ) -> CommandResult:
        transition = await asyncio.to_thread(
            lifecycle_command_store.required_transition,
            session_id,
            request_id,
        )
        effects = tuple(
            LifecycleEffect.from_dict(value)
            for value in transition["effects"]
        )
        results = list(transition["effect_results"])
        while len(results) < len(effects):
            ordinal = len(results)
            result = await self._effect_handler.execute_idempotently(
                effects[ordinal]
            )
            if not isinstance(result, Mapping):
                raise TypeError("lifecycle effect result must be a mapping")
            materialized = materialize_json(freeze_json(dict(result)))
            durable_result = await asyncio.to_thread(
                lifecycle_command_store.record_effect_result,
                session_id,
                request_id,
                ordinal,
                materialized,
            )
            results.append(durable_result)
        next_snapshot = await asyncio.to_thread(
            lifecycle_command_store.commit_transition,
            session_id,
            request_id,
        )
        self._notify_waiters(session_id, next_snapshot)
        should_notify = await asyncio.to_thread(
            lifecycle_command_store.mark_notification_attempted,
            session_id,
            request_id,
        )
        if should_notify:
            transition = await asyncio.to_thread(
                lifecycle_command_store.required_transition,
                session_id,
                request_id,
            )
            try:
                await self._bus.publish(BusEvent(
                    type=transition["notification_type"],
                    root_id=session_id,
                    sid=session_id,
                    payload=copy.deepcopy(
                        transition["notification_payload"]
                    ),
                    persist=False,
                ))
            except Exception:
                logger.warning(
                    "best-effort lifecycle notification failed session=%s request=%s",
                    session_id,
                    request_id,
                )
        return CommandResult(request_id, next_snapshot, tuple(results))

    def _notify_waiters(
        self,
        session_id: str,
        snapshot: LifecycleSnapshot,
    ) -> None:
        for waiter in tuple(self._waiters.get(session_id, ())):
            if waiter.future.done():
                continue
            if self._wait_matches(
                snapshot,
                waiter.phases,
                waiter.identity,
                waiter.min_revision,
            ):
                waiter.future.set_result(snapshot)
                continue
            if (
                waiter.identity is not None
                and snapshot.revision >= waiter.min_revision
                and snapshot.identity != waiter.identity
            ):
                waiter.future.set_exception(IdentityRetired(
                    "requested turn identity was retired"
                ))

    @staticmethod
    def _wait_matches(
        snapshot: LifecycleSnapshot,
        phases: Collection[str],
        identity: TurnIdentity | None,
        min_revision: int,
    ) -> bool:
        if snapshot.phase not in phases or snapshot.revision < min_revision:
            return False
        return identity is None or snapshot.identity == identity

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def _assert_owner(self) -> None:
        if self._loop is None or self._loop is not asyncio.get_running_loop():
            raise RuntimeError("lifecycle command engine is not bound to this loop")

    def _assert_ready(self) -> None:
        self._assert_owner()
        if self._bind_task is None or not self._bind_task.done():
            raise RuntimeError("lifecycle command engine recovery is incomplete")
        if self._bind_task.exception() is not None:
            raise RuntimeError("lifecycle command engine recovery failed")
