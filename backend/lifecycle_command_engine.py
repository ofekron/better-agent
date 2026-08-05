from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from event_bus import BusEvent, EventBus
from lifecycle_command_model import (
    CommandResult,
    LifecycleCommand,
    LifecycleEffect,
    LifecycleSnapshot,
    ExecutionTurnIdentity,
    SelectorAttemptDecision,
    SelectorIdentity,
    TransitionPlan,
    UserTurnIdentity,
    freeze_json,
    materialize_json,
    validate_identifier,
)
from lifecycle_command_states import STATES, LifecycleCommandRejected
import lifecycle_command_store
from process_identity import ProcessIdentity, capture_process_identity


logger = logging.getLogger(__name__)
_AUTHORITY_LOCK = threading.Lock()
_BOUND_AUTHORITIES: dict[str, LifecycleCommandEngine] = {}

class IdentityRetired(RuntimeError):
    pass


class LifecycleAuthorityBound(RuntimeError):
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
    identity: UserTurnIdentity | None
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
        self._registration_watchers: dict[
            str,
            set[asyncio.Future[None]],
        ] = {}
        self._authority_key: str | None = None
        self._owner_id = str(uuid.uuid4())
        self._subscriber_name = f"lifecycle_command_engine_{self._owner_id}"
        self._process_identity: ProcessIdentity | None = None
        self._lease_acquired = False

    async def bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            authority_key = str(lifecycle_command_store.store_path())
            with _AUTHORITY_LOCK:
                existing = _BOUND_AUTHORITIES.get(authority_key)
                if existing is not None and existing is not self:
                    raise LifecycleAuthorityBound(
                        "a lifecycle command authority is already bound "
                        "to this store and event loop"
                    )
                _BOUND_AUTHORITIES[authority_key] = self
            self._authority_key = authority_key
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("lifecycle command engine cannot move between event loops")
        if self._bind_task is None:
            self._bind_task = loop.create_task(self._recover())
            self._bind_task.add_done_callback(self._bind_finished)
        task = self._bind_task
        await asyncio.shield(task)

    def _bind_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self._release_authority()
            self._bind_task = None
            self._loop = None

    async def _recover(self) -> None:
        await asyncio.to_thread(lifecycle_command_store.initialize)
        process_identity = await asyncio.to_thread(
            capture_process_identity,
            os.getpid(),
        )
        if process_identity is None:
            raise RuntimeError("cannot prove lifecycle authority process identity")
        self._process_identity = process_identity
        await asyncio.to_thread(
            lifecycle_command_store.acquire_authority,
            self._owner_id,
            process_identity,
        )
        self._lease_acquired = True
        self._bus.subscribe(
            "session.deleted",
            self._retire_deleted_sessions,
            priority=5,
            name=self._subscriber_name,
            loop=self._loop,
        )
        try:
            pending = await asyncio.to_thread(
                lifecycle_command_store.unfinished_transitions
            )
            for session_id, request_id in pending:
                async with self._lock_for(session_id):
                    await self._resume_transition(session_id, request_id)
            for session_id, projection in await asyncio.to_thread(
                lifecycle_command_store.pending_selector_projections,
            ):
                async with self._lock_for(session_id):
                    await self._apply_selector_projection(session_id, projection)
        except BaseException:
            self._bus.unsubscribe(self._subscriber_name)
            await self._release_lease()
            raise

    async def _retire_deleted_sessions(self, event: BusEvent) -> None:
        deleted_sids = event.payload.get("deleted_sids")
        deleted_incarnations = event.payload.get("deleted_incarnations")
        if not isinstance(deleted_sids, list) or not isinstance(
            deleted_incarnations,
            dict,
        ):
            return
        for session_id in deleted_sids:
            if not isinstance(session_id, str):
                continue
            owner_incarnation = deleted_incarnations.get(session_id)
            if not isinstance(owner_incarnation, str) or not owner_incarnation:
                continue
            async with self._lock_for(session_id):
                import session_manager

                current_owner = await asyncio.to_thread(
                    session_manager.manager.claim_owner,
                    session_id,
                )
                if current_owner is not None:
                    if current_owner.incarnation != owner_incarnation:
                        continue
                await asyncio.to_thread(
                    lifecycle_command_store.retire_session,
                    session_id,
                    expected_owner_incarnation=owner_incarnation,
                    allow_unbound_owner=True,
                )

    async def _align_session_owner(self, session_id: str) -> None:
        import session_manager

        owner = await asyncio.to_thread(
            session_manager.manager.claim_owner,
            session_id,
        )
        if owner is None or not owner.incarnation:
            return
        await asyncio.to_thread(
            lifecycle_command_store.align_session_owner,
            session_id,
            owner.incarnation,
        )

    async def close(self) -> None:
        self._assert_owner()
        try:
            if self._bind_task is not None:
                await asyncio.shield(self._bind_task)
        finally:
            self._bus.unsubscribe(self._subscriber_name)
            await self._release_lease()
            for waiters in self._waiters.values():
                for waiter in waiters:
                    if not waiter.future.done():
                        waiter.future.cancel()
            self._waiters.clear()
            for watchers in self._registration_watchers.values():
                for watcher in watchers:
                    if not watcher.done():
                        watcher.cancel()
            self._registration_watchers.clear()
            self._session_locks.clear()
            self._bind_task = None
            self._loop = None
            self._release_authority()

    async def _release_lease(self) -> None:
        process_identity = self._process_identity
        if not self._lease_acquired or process_identity is None:
            return
        try:
            await asyncio.to_thread(
                lifecycle_command_store.release_authority,
                self._owner_id,
                process_identity,
            )
        finally:
            self._lease_acquired = False
            self._process_identity = None

    def snapshot(self, session_id: str) -> LifecycleSnapshot:
        self._assert_ready()
        validate_identifier(session_id, "session_id")
        return lifecycle_command_store.session_snapshot(session_id)

    async def begin_turn(
        self,
        *,
        request_id: str,
        session_id: str,
        identity: UserTurnIdentity,
        execution_policy: str = "single",
    ) -> CommandResult:
        return await self.execute(LifecycleCommand(
            request_id=request_id,
            session_id=session_id,
            kind="begin_turn",
            identity=identity,
            execution_policy=execution_policy,
        ))

    async def confirm_started(
        self,
        *,
        request_id: str,
        session_id: str,
        identity: UserTurnIdentity,
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
        identity: UserTurnIdentity,
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
        identity: UserTurnIdentity,
        outcome: str,
    ) -> CommandResult:
        return await self.execute(LifecycleCommand(
            request_id=request_id,
            session_id=session_id,
            kind="finish_turn",
            identity=identity,
            outcome=outcome,
        ))

    async def confirm_active_started(
        self,
        session_id: str,
        *,
        lifecycle_message_id: str,
    ) -> CommandResult | None:
        return await self._transition_active(
            session_id,
            kind="confirm_started",
            lifecycle_message_id=lifecycle_message_id,
        )

    async def request_active_stop(
        self,
        session_id: str,
    ) -> CommandResult | None:
        return await self._transition_active(session_id, kind="request_stop")

    async def finish_active(
        self,
        session_id: str,
        *,
        lifecycle_message_id: str,
        outcome: str,
    ) -> CommandResult | None:
        return await self._transition_active(
            session_id,
            kind="finish_turn",
            lifecycle_message_id=lifecycle_message_id,
            outcome=outcome,
        )

    async def start_execution(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        selector_role: str | None = None,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "start_execution",
            execution_identity,
            execution_selector_role=selector_role,
        )

    async def transition_selectors(
        self,
        session_id: str,
        *,
        updates: Mapping[str, Any],
        client_id: str | None = None,
    ) -> dict[str, Any] | None:
        allowed = {
            "model", "runner", "reasoning_effort", "permission", "cwd",
            "provider_id", "runtime_profile_id", "harness_profile_id",
        }
        if type(updates) is not dict or set(updates) - allowed:
            raise ValueError("selector updates have unexpected fields")
        await self.bind()
        async with self._lock_for(session_id):
            await self._align_session_owner(session_id)
            import session_manager

            current = await asyncio.to_thread(session_manager.manager.get, session_id)
            if current is None:
                return None
            apply_updates = dict(updates)
            apply_updates["client_id"] = client_id
            validated = await asyncio.to_thread(
                session_manager.manager.set_selectors,
                session_id,
                _validate_only=True,
                **apply_updates,
            )
            if validated is None:
                return None
            target = SelectorIdentity(
                provider_id=str(
                    updates.get("provider_id", current.get("provider_id")) or ""
                ),
                model=str(updates.get("model", current.get("model")) or ""),
                runner=str(updates.get("runner", current.get("runner")) or ""),
            )
            current_identity = SelectorIdentity(
                provider_id=str(current.get("provider_id") or ""),
                model=str(current.get("model") or ""),
                runner=str(current.get("runner") or ""),
            )
            if target == current_identity:
                return await asyncio.to_thread(
                    session_manager.manager.set_selectors,
                    session_id,
                    **apply_updates,
                )
            primary_sid = current.get("agent_session_id")
            supervisor_sid = current.get("supervisor_agent_session_id")
            primary_legacy_compatibility = None
            if isinstance(primary_sid, str) and primary_sid:
                primary_legacy_compatibility = await asyncio.to_thread(
                    self._legacy_native_compatibility,
                    session_id,
                    current,
                    primary_sid,
                )
            supervisor_legacy_compatibility = None
            if isinstance(supervisor_sid, str) and supervisor_sid:
                supervisor_legacy_compatibility = await asyncio.to_thread(
                    self._legacy_native_compatibility,
                    session_id,
                    current,
                    supervisor_sid,
                )
            projection_updates = {
                **updates,
                "provider_id": target.provider_id,
                "model": target.model,
                "runner": target.runner,
            }
            authority = await asyncio.to_thread(
                lifecycle_command_store.persist_selector_transition,
                session_id,
                target=target,
                projection_updates=projection_updates,
                primary_native_sid=(
                    primary_sid if isinstance(primary_sid, str) and primary_sid else None
                ),
                supervisor_native_sid=(
                    supervisor_sid
                    if isinstance(supervisor_sid, str) and supervisor_sid
                    else None
                ),
                primary_legacy_native_sid_compatibility=(
                    primary_legacy_compatibility.to_dict()
                    if primary_legacy_compatibility
                    else None
                ),
                supervisor_legacy_native_sid_compatibility=(
                    supervisor_legacy_compatibility.to_dict()
                    if supervisor_legacy_compatibility
                    else None
                ),
            )
            projected = await asyncio.to_thread(
                session_manager.manager.set_selectors,
                session_id,
                client_id=client_id,
                _authoritative_clear_native_sids=True,
                **projection_updates,
            )
            if projected is None:
                return None
            await asyncio.to_thread(
                lifecycle_command_store.acknowledge_selector_projection,
                session_id,
                authority.identity,
            )
            return projected

    async def selector_handoff_source(
        self,
        session_id: str,
        *,
        role: str,
    ) -> tuple[str, SelectorIdentity] | None:
        await self.bind()
        snapshot = await asyncio.to_thread(
            lifecycle_command_store.selector_authority_snapshot,
            session_id,
        )
        if snapshot is None or snapshot.handoff is None:
            return None
        source_sid = (
            snapshot.handoff.supervisor_source_sid
            if role == "supervisor"
            else snapshot.handoff.primary_source_sid
        )
        if source_sid is None:
            return None
        return source_sid, snapshot.handoff.target

    async def admit_prepared_selector_attempt(
        self,
        session_id: str,
        *,
        selector: SelectorIdentity,
        native_sid_compatibility: Mapping[str, Any] | None,
        role: str,
        current_native_sid: str | None,
    ) -> tuple[SelectorAttemptDecision, int]:
        if role not in {"primary", "supervisor"}:
            raise ValueError("selector attempt role is invalid")
        await self.bind()
        async with self._lock_for(session_id):
            await self._align_session_owner(session_id)
            import session_manager

            session = await asyncio.to_thread(session_manager.manager.get, session_id)
            if session is None:
                raise LifecycleCommandRejected("selector attempt session is missing")
            legacy_compatibility = None
            if current_native_sid:
                legacy_compatibility = await asyncio.to_thread(
                    self._legacy_native_compatibility,
                    session_id,
                    session,
                    current_native_sid,
                )
            authority, decision = await asyncio.to_thread(
                lifecycle_command_store.persist_admitted_selector_attempt,
                session_id,
                target=selector,
                native_sid_compatibility=(
                    dict(native_sid_compatibility)
                    if native_sid_compatibility is not None
                    else None
                ),
                primary_native_sid=(
                    current_native_sid if role == "primary" else None
                ),
                supervisor_native_sid=(
                    current_native_sid if role == "supervisor" else None
                ),
                primary_native_sid_compatibility=(
                    legacy_compatibility.to_dict()
                    if role == "primary" and legacy_compatibility
                    else None
                ),
                supervisor_native_sid_compatibility=(
                    legacy_compatibility.to_dict()
                    if role == "supervisor" and legacy_compatibility
                    else None
                ),
            )
            return decision, authority.generation

    async def attach_selector_native_sid(
        self,
        session_id: str,
        *,
        selector_generation: int,
        provider_run_id: str,
        role: str,
        native_sid: str,
    ) -> bool:
        validate_identifier(native_sid, "native_sid")
        return await self._persist_native_sid_projection(
            session_id,
            selector_generation=selector_generation,
            provider_run_id=provider_run_id,
            role=role,
            native_sid=native_sid,
            expected_native_sid=None,
        )

    async def attach_recovered_selector_native_sid(
        self,
        session_id: str,
        *,
        provider_run_id: str,
        native_sid: str,
        native_sid_compatibility: Mapping[str, Any],
        selector: SelectorIdentity,
    ) -> bool:
        from native_sid_compatibility import NativeSidCompatibility

        validate_identifier(native_sid, "native_sid")
        compatibility = NativeSidCompatibility.from_dict(
            native_sid_compatibility,
        ).to_dict()
        validate_identifier(provider_run_id, "provider_run_id")
        await self.bind()
        async with self._lock_for(session_id):
            attempt = await self._recover_execution_selector_attempt_locked(
                session_id,
                provider_run_id=provider_run_id,
                selector=selector,
                compatibility=compatibility,
            )
            if attempt is None:
                return False
            persisted = await asyncio.to_thread(
                lifecycle_command_store.attach_selector_native_sid,
                session_id,
                expected_generation=attempt["selector_generation"],
                role=attempt["role"],
                native_sid=native_sid,
            )
            if persisted is None:
                return False
            _authority, projection = persisted
            return await self._apply_selector_projection(session_id, projection)

    async def recover_execution_selector_attempt(
        self,
        session_id: str,
        *,
        provider_run_id: str,
        native_sid_compatibility: Mapping[str, Any],
        selector: SelectorIdentity,
    ) -> dict[str, Any] | None:
        from native_sid_compatibility import NativeSidCompatibility

        validate_identifier(provider_run_id, "provider_run_id")
        compatibility = NativeSidCompatibility.from_dict(
            native_sid_compatibility,
        ).to_dict()
        await self.bind()
        async with self._lock_for(session_id):
            return await self._recover_execution_selector_attempt_locked(
                session_id,
                provider_run_id=provider_run_id,
                selector=selector,
                compatibility=compatibility,
            )

    async def _recover_execution_selector_attempt_locked(
        self,
        session_id: str,
        *,
        provider_run_id: str,
        selector: SelectorIdentity,
        compatibility: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        lifecycle = await asyncio.to_thread(
            lifecycle_command_store.session_snapshot,
            session_id,
        )
        execution = lifecycle.execution
        if execution is None or execution.provider_run_id != provider_run_id:
            return None
        authority = await asyncio.to_thread(
            lifecycle_command_store.selector_authority_snapshot,
            session_id,
        )
        if (
            authority is None
            or authority.identity != selector
            or authority.native_sid_compatibility != compatibility
        ):
            return None
        attempt = await asyncio.to_thread(
            lifecycle_command_store.execution_selector_attempt,
            session_id,
            provider_run_id,
        )
        if attempt is None:
            role = await asyncio.to_thread(
                lifecycle_command_store.execution_selector_role,
                session_id,
                execution.identity.execution_turn_id,
            )
            if role is None:
                return None
            await asyncio.to_thread(
                lifecycle_command_store.record_execution_selector_attempt,
                session_id,
                execution_turn_id=execution.identity.execution_turn_id,
                provider_run_id=provider_run_id,
                selector_generation=authority.generation,
                role=role,
                selector=selector,
                native_sid_compatibility=dict(compatibility),
            )
            attempt = await asyncio.to_thread(
                lifecycle_command_store.execution_selector_attempt,
                session_id,
                provider_run_id,
            )
        if (
            attempt is None
            or attempt["execution_turn_id"]
            != execution.identity.execution_turn_id
            or attempt["selector_generation"] != authority.generation
            or attempt["selector"] != selector
            or attempt["native_sid_compatibility"] != compatibility
        ):
            return None
        return attempt

    async def clear_selector_native_sid(
        self,
        session_id: str,
        *,
        selector_generation: int,
        provider_run_id: str,
        role: str,
        expected_native_sid: str,
    ) -> bool:
        validate_identifier(expected_native_sid, "expected_native_sid")
        return await self._persist_native_sid_projection(
            session_id,
            selector_generation=selector_generation,
            provider_run_id=provider_run_id,
            role=role,
            native_sid=None,
            expected_native_sid=expected_native_sid,
        )

    async def _persist_native_sid_projection(
        self,
        session_id: str,
        *,
        selector_generation: int,
        provider_run_id: str,
        role: str,
        native_sid: str | None,
        expected_native_sid: str | None,
    ) -> bool:
        validate_identifier(provider_run_id, "provider_run_id")
        await self.bind()
        async with self._lock_for(session_id):
            lifecycle = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                session_id,
            )
            execution = lifecycle.execution
            if execution is None or execution.provider_run_id != provider_run_id:
                return False
            attempt = await asyncio.to_thread(
                lifecycle_command_store.execution_selector_attempt,
                session_id,
                provider_run_id,
            )
            authority = await asyncio.to_thread(
                lifecycle_command_store.selector_authority_snapshot,
                session_id,
            )
            if (
                attempt is None
                or authority is None
                or attempt["execution_turn_id"]
                != execution.identity.execution_turn_id
                or attempt["selector_generation"] != selector_generation
                or attempt["role"] != role
                or attempt["selector"] != authority.identity
                or attempt["native_sid_compatibility"]
                != authority.native_sid_compatibility
            ):
                return False
            if native_sid is None:
                persisted = await asyncio.to_thread(
                    lifecycle_command_store.clear_selector_native_sid,
                    session_id,
                    expected_generation=selector_generation,
                    role=role,
                    expected_native_sid=expected_native_sid,
                )
            else:
                persisted = await asyncio.to_thread(
                    lifecycle_command_store.attach_selector_native_sid,
                    session_id,
                    expected_generation=selector_generation,
                    role=role,
                    native_sid=native_sid,
                )
            if persisted is None:
                return False
            _authority, projection = persisted
            return await self._apply_selector_projection(session_id, projection)

    async def _apply_selector_projection(
        self,
        session_id: str,
        projection: Mapping[str, Any],
    ) -> bool:
        if projection.get("kind") == "handoff_consumed":
            if set(projection) != {
                "kind",
                "generation",
                "role",
                "source_sid",
                "target",
                "native_sid",
                "provider_run_id",
            }:
                raise RuntimeError("persisted handoff projection has unexpected fields")
            generation = projection["generation"]
            role = projection["role"]
            if type(generation) is not int or generation < 0:
                raise RuntimeError("persisted handoff projection generation is invalid")
            if role not in {"primary", "supervisor"}:
                raise RuntimeError("persisted handoff projection role is invalid")
            for field in ("source_sid", "native_sid", "provider_run_id"):
                try:
                    validate_identifier(projection[field], field)
                except ValueError as exc:
                    raise RuntimeError(
                        f"persisted handoff projection {field} is invalid"
                    ) from exc
            target = SelectorIdentity.from_dict(projection["target"])
            projected = await self._project_native_sid(
                session_id,
                role=role,
                identity=target,
                native_sid=projection["native_sid"],
            )
            if not projected:
                return False
            return await asyncio.to_thread(
                lifecycle_command_store.acknowledge_handoff_projection,
                session_id,
                dict(projection),
            )
        if projection.get("kind") == "native_sid":
            if set(projection) != {
                "kind", "generation", "identity", "role", "native_sid",
            }:
                raise RuntimeError("persisted native SID projection has unexpected fields")
            identity = SelectorIdentity.from_dict(projection["identity"])
            generation = projection["generation"]
            role = projection["role"]
            native_sid = projection["native_sid"]
            if type(generation) is not int or generation < 0:
                raise RuntimeError("persisted native SID projection generation is invalid")
            if role not in {"primary", "supervisor"}:
                raise RuntimeError("persisted native SID projection role is invalid")
            if native_sid is not None and (
                not isinstance(native_sid, str) or not native_sid
            ):
                raise RuntimeError("persisted native SID projection value is invalid")
            projected = await self._project_native_sid(
                session_id,
                role=role,
                identity=identity,
                native_sid=native_sid,
            )
            if not projected:
                return False
            return await asyncio.to_thread(
                lifecycle_command_store.acknowledge_native_sid_projection,
                session_id,
                dict(projection),
            )
        if set(projection) != {"updates", "target", "clear_native_sids"}:
            raise RuntimeError("persisted selector projection has unexpected fields")
        target = SelectorIdentity.from_dict(projection["target"])
        updates = projection["updates"]
        if type(updates) is not dict:
            raise RuntimeError("persisted selector projection updates are invalid")
        import session_manager

        projected = await asyncio.to_thread(
            session_manager.manager.set_selectors,
            session_id,
            _authoritative_clear_native_sids=bool(
                projection["clear_native_sids"]
            ),
            **updates,
        )
        if projected is None:
            return False
        return await asyncio.to_thread(
            lifecycle_command_store.acknowledge_selector_projection,
            session_id,
            target,
        )

    async def _project_native_sid(
        self,
        session_id: str,
        *,
        role: str,
        identity: SelectorIdentity,
        native_sid: str | None,
    ) -> bool:
        import session_manager

        orchestration_mode = await asyncio.to_thread(
            session_manager.manager.get_field,
            session_id,
            "orchestration_mode",
        )
        if not isinstance(orchestration_mode, str) or not orchestration_mode:
            return False
        projected = await asyncio.to_thread(
            session_manager.manager.set_agent_sid,
            session_id,
            "supervisor" if role == "supervisor" else orchestration_mode,
            native_sid,
            provider_id=identity.provider_id,
            model=identity.model,
            runner=identity.runner,
        )
        return projected is not None

    @staticmethod
    def _legacy_native_compatibility(
        session_id: str,
        session: Mapping[str, Any],
        native_sid: str,
    ) -> Any:
        import session_store
        from native_sid_compatibility import (
            resolve_legacy_native_sid_compatibility_projection,
        )

        root_id = str(session.get("root_session_id") or session_id)
        path = (
            Path(session_store.root_session_file_path(root_id)).parent
            / root_id
            / "native_paths"
        )
        artifact_paths: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if type(row) is not dict or row.get("agent_sid") != native_sid:
                continue
            artifact_path = row.get("jsonl_path")
            if isinstance(artifact_path, str) and artifact_path:
                artifact_paths.append(artifact_path)
        return resolve_legacy_native_sid_compatibility_projection(
            node_id=str(session.get("node_id") or "primary"),
            native_sid=native_sid,
            artifact_paths=artifact_paths,
        )

    async def bind_execution_run(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "bind_execution_run",
            execution_identity,
            provider_run_id=provider_run_id,
        )

    async def elect_execution_attempt(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
        selector_generation: int | None = None,
        native_sid_compatibility: Mapping[str, Any] | None = None,
        selector: SelectorIdentity | None = None,
    ) -> CommandResult:
        if selector_generation is None:
            if native_sid_compatibility is not None or selector is not None:
                raise ValueError("selector evidence requires a generation")
            return await self.bind_execution_run(
                session_id,
                execution_identity=execution_identity,
                provider_run_id=provider_run_id,
            )
        if selector is None:
            raise ValueError("selector identity is required")
        if native_sid_compatibility is None:
            compatibility = None
        else:
            from native_sid_compatibility import NativeSidCompatibility

            compatibility = NativeSidCompatibility.from_dict(
                native_sid_compatibility,
            ).to_dict()
        await self.bind()
        async with self._lock_for(session_id):
            role = await asyncio.to_thread(
                lifecycle_command_store.execution_selector_role,
                session_id,
                execution_identity.execution_turn_id,
            )
            if role is None:
                raise LifecycleCommandRejected(
                    "execution selector role is unavailable"
                )
            return await self._transition_execution_locked(
                session_id,
                "bind_execution_run",
                execution_identity,
                provider_run_id=provider_run_id,
                execution_selector_attempt={
                    "selector_generation": selector_generation,
                    "role": role,
                    "selector": selector.to_dict(),
                    "native_sid_compatibility": compatibility,
                },
            )

    async def restore_execution_run(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        expected_failed_run_id: str,
        replacement_provider_run_id: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "restore_execution_run",
            execution_identity,
            provider_run_id=expected_failed_run_id,
            replacement_provider_run_id=replacement_provider_run_id,
        )

    async def confirm_execution_started(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "confirm_execution_started",
            execution_identity,
            provider_run_id=provider_run_id,
        )

    async def admit_execution_attempt(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
    ) -> CommandResult:
        return await self.confirm_execution_started(
            session_id,
            execution_identity=execution_identity,
            provider_run_id=provider_run_id,
        )

    async def detach_execution(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "detach_execution",
            execution_identity,
        )

    async def adopt_execution(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "adopt_execution",
            execution_identity,
            provider_run_id=provider_run_id,
        )

    async def finish_execution(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
        outcome: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "finish_execution",
            execution_identity,
            provider_run_id=provider_run_id,
            outcome=outcome,
        )

    async def finish_execution_and_turn(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        provider_run_id: str,
        outcome: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "finish_execution_and_turn",
            execution_identity,
            provider_run_id=provider_run_id,
            outcome=outcome,
        )

    async def abort_execution(
        self,
        session_id: str,
        *,
        execution_identity: ExecutionTurnIdentity,
        outcome: str,
    ) -> CommandResult:
        return await self._transition_execution(
            session_id,
            "abort_execution",
            execution_identity,
            outcome=outcome,
        )

    async def active_snapshots(self) -> list[tuple[str, LifecycleSnapshot]]:
        await self.bind()
        return await asyncio.to_thread(
            lifecycle_command_store.active_session_snapshots,
        )

    async def acknowledge_terminal_render(
        self,
        session_id: str,
        request_id: str,
    ) -> bool:
        self._assert_ready()
        return await asyncio.to_thread(
            lifecycle_command_store.acknowledge_terminal_render,
            session_id,
            request_id,
        )

    async def recover_unbound_execution(
        self,
        session_id: str,
        *,
        expected_snapshot: LifecycleSnapshot,
        resolve_outcome: Callable[[], Awaitable[str | None]],
    ) -> bool:
        await self.bind()
        async with self._lock_for(session_id):
            snapshot = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                session_id,
            )
            if snapshot != expected_snapshot:
                return False
            execution = snapshot.execution
            if (
                snapshot.phase != "starting"
                or execution is None
                or execution.phase not in {"starting", "aborted"}
                or execution.provider_run_id is not None
            ):
                return False
            outcome = await resolve_outcome()
            if outcome is None:
                return False
            result_snapshot = snapshot
            if execution.phase == "starting":
                result = await self._execute_bound(LifecycleCommand(
                    request_id=f"recovery:{snapshot.revision}:abort_execution",
                    session_id=session_id,
                    kind="abort_execution",
                    identity=snapshot.identity,
                    execution_identity=execution.identity,
                    outcome=outcome,
                ))
                result_snapshot = result.snapshot
            if result_snapshot.phase != "idle":
                await self._execute_bound(LifecycleCommand(
                    request_id=(
                        f"recovery:{result_snapshot.revision}:finish_turn"
                    ),
                    session_id=session_id,
                    kind="finish_turn",
                    identity=result_snapshot.identity,
                    outcome=outcome,
                ))
            return True

    async def recover_missing_bound_execution(
        self,
        session_id: str,
        *,
        expected_snapshot: LifecycleSnapshot,
        resolve_outcome: Callable[[], Awaitable[str | None]],
    ) -> bool:
        await self.bind()
        async with self._lock_for(session_id):
            snapshot = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                session_id,
            )
            if snapshot != expected_snapshot:
                return False
            execution = snapshot.execution
            if (
                snapshot.phase != "starting"
                or snapshot.identity is None
                or execution is None
                or execution.phase != "starting"
                or execution.provider_run_id is None
            ):
                return False
            outcome = await resolve_outcome()
            if outcome is None:
                return False
            await self._execute_bound(LifecycleCommand(
                request_id=(
                    f"recovery:{snapshot.revision}:"
                    "finish_execution_and_turn"
                ),
                session_id=session_id,
                kind="finish_execution_and_turn",
                identity=snapshot.identity,
                execution_identity=execution.identity,
                provider_run_id=execution.provider_run_id,
                outcome=outcome,
            ))
            return True

    async def _transition_execution(
        self,
        session_id: str,
        kind: str,
        execution_identity: ExecutionTurnIdentity,
        *,
        provider_run_id: str | None = None,
        replacement_provider_run_id: str | None = None,
        outcome: str | None = None,
        execution_selector_role: str | None = None,
        execution_selector_attempt: dict[str, Any] | None = None,
    ) -> CommandResult:
        if execution_selector_role is not None and execution_selector_role not in {
            "primary",
            "supervisor",
        }:
            raise ValueError("selector role is invalid")
        if execution_selector_role is not None and kind != "start_execution":
            raise ValueError("selector role is only valid when starting execution")
        await self.bind()
        async with self._lock_for(session_id):
            return await self._transition_execution_locked(
                session_id,
                kind,
                execution_identity,
                provider_run_id=provider_run_id,
                replacement_provider_run_id=replacement_provider_run_id,
                outcome=outcome,
                execution_selector_role=execution_selector_role,
                execution_selector_attempt=execution_selector_attempt,
            )

    async def _transition_execution_locked(
        self,
        session_id: str,
        kind: str,
        execution_identity: ExecutionTurnIdentity,
        *,
        provider_run_id: str | None = None,
        replacement_provider_run_id: str | None = None,
        outcome: str | None = None,
        execution_selector_role: str | None = None,
        execution_selector_attempt: dict[str, Any] | None = None,
    ) -> CommandResult:
        snapshot = await asyncio.to_thread(
            lifecycle_command_store.session_snapshot,
            session_id,
        )
        if snapshot.identity is None:
            raise LifecycleCommandRejected(
                "execution requires an active user turn"
            )
        command = LifecycleCommand(
            request_id=f"execution:{snapshot.revision}:{kind}",
            session_id=session_id,
            kind=kind,
            identity=snapshot.identity,
            execution_identity=execution_identity,
            provider_run_id=provider_run_id,
            replacement_provider_run_id=replacement_provider_run_id,
            outcome=outcome,
        )
        return await self._execute_bound(
            command,
            execution_selector_role=execution_selector_role,
            execution_selector_attempt=execution_selector_attempt,
        )

    async def _transition_active(
        self,
        session_id: str,
        *,
        kind: str,
        lifecycle_message_id: str | None = None,
        outcome: str | None = None,
    ) -> CommandResult | None:
        await self.bind()
        async with self._lock_for(session_id):
            snapshot = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                session_id,
            )
            identity = snapshot.identity
            if identity is None:
                return None
            if (
                lifecycle_message_id is not None
                and identity.lifecycle_message_id != lifecycle_message_id
            ):
                return None
            if kind == "confirm_started" and snapshot.phase != "starting":
                return None
            if kind == "request_stop" and snapshot.phase not in {"starting", "running"}:
                return None
            if kind == "finish_turn" and snapshot.phase == "idle":
                return None
            return await self._execute_bound(LifecycleCommand(
                request_id=f"active:{snapshot.revision}:{kind}",
                session_id=session_id,
                kind=kind,
                identity=identity,
                outcome=outcome,
            ))

    async def execute(self, command: LifecycleCommand) -> CommandResult:
        await self.bind()
        async with self._lock_for(command.session_id):
            return await self._execute_bound(command)

    async def _selector_handoff_acceptance(
        self,
        command: LifecycleCommand,
        snapshot: LifecycleSnapshot,
        plan: TransitionPlan,
    ) -> dict[str, Any] | None:
        if (
            command.kind not in {"finish_execution", "finish_execution_and_turn"}
            or plan.fact_payload.get("outcome") != "complete"
            or snapshot.execution is None
            or snapshot.execution.identity != command.execution_identity
            or snapshot.execution.provider_run_id != command.provider_run_id
        ):
            return None
        role = await asyncio.to_thread(
            lifecycle_command_store.execution_selector_role,
            command.session_id,
            snapshot.execution.identity.execution_turn_id,
        )
        if role is None:
            return None
        authority = await asyncio.to_thread(
            lifecycle_command_store.selector_authority_snapshot,
            command.session_id,
        )
        if (
            authority is None
            or authority.handoff is None
            or authority.identity is None
        ):
            return None
        source_sid = (
            authority.handoff.supervisor_source_sid
            if role == "supervisor"
            else authority.handoff.primary_source_sid
        )
        if source_sid is None:
            return None
        return {
            "generation": authority.generation,
            "role": role,
            "source_sid": source_sid,
            "target": authority.identity.to_dict(),
            "provider_run_id": command.provider_run_id,
        }

    async def _execute_bound(
        self,
        command: LifecycleCommand,
        *,
        execution_selector_role: str | None = None,
        execution_selector_attempt: dict[str, Any] | None = None,
    ) -> CommandResult:
        await self._align_session_owner(command.session_id)
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
            if command.kind == "start_execution":
                persisted_role = await asyncio.to_thread(
                    lifecycle_command_store.execution_selector_role,
                    command.session_id,
                    command.execution_identity.execution_turn_id,
                )
                if persisted_role != execution_selector_role:
                    raise LifecycleCommandRejected(
                        "request_id is already bound to another selector role"
                    )
            if command.kind == "bind_execution_run":
                persisted_attempt = await asyncio.to_thread(
                    lifecycle_command_store.execution_selector_attempt,
                    command.session_id,
                    command.provider_run_id,
                )
                expected_attempt = (
                    None
                    if execution_selector_attempt is None
                    else {
                        "execution_turn_id": (
                            command.execution_identity.execution_turn_id
                        ),
                        "provider_run_id": command.provider_run_id,
                        "selector_generation": execution_selector_attempt[
                            "selector_generation"
                        ],
                        "role": execution_selector_attempt["role"],
                        "selector": SelectorIdentity.from_dict(
                            execution_selector_attempt["selector"]
                        ),
                        "native_sid_compatibility": execution_selector_attempt[
                            "native_sid_compatibility"
                        ],
                    }
                )
                if persisted_attempt != expected_attempt:
                    raise LifecycleCommandRejected(
                        "request_id is already bound to another selector attempt"
                    )
            if existing["status"] in {"planned", "effects_applied"}:
                snapshot = await asyncio.to_thread(
                    lifecycle_command_store.session_snapshot,
                    command.session_id,
                )
                plan = STATES[snapshot.phase].decide(snapshot, command)
                acceptance = await self._selector_handoff_acceptance(
                    command,
                    snapshot,
                    plan,
                )
                if existing["selector_handoff_acceptance"] != acceptance:
                    raise LifecycleCommandRejected(
                        "request_id selector acceptance changed before resume"
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
        selector_handoff_acceptance = await self._selector_handoff_acceptance(
            command,
            snapshot,
            plan,
        )
        try:
            disposition = await asyncio.to_thread(
                lifecycle_command_store.persist_plan,
                command,
                snapshot,
                plan,
                selector_handoff_acceptance=selector_handoff_acceptance,
                execution_selector_role=execution_selector_role,
                execution_selector_attempt=execution_selector_attempt,
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
        identity: UserTurnIdentity | None = None,
        min_revision: int = 0,
    ) -> LifecycleSnapshot:
        await self.bind()
        validate_identifier(session_id, "session_id")
        expected = frozenset(phases)
        if not expected or not expected.issubset(STATES):
            raise ValueError("wait phases must be known lifecycle phases")
        if type(min_revision) is not int or min_revision < 0:
            raise ValueError("min_revision must be a non-negative integer")
        waiter = _PhaseWait(
            asyncio.get_running_loop().create_future(),
            expected,
            identity,
            min_revision,
        )
        async with self._lock_for(session_id):
            snapshot = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                session_id,
            )
            if self._wait_matches(snapshot, expected, identity, min_revision):
                return snapshot
            if identity is not None and snapshot.identity != identity:
                raise IdentityRetired(
                    "requested turn identity is no longer active"
                )
            self._waiters.setdefault(session_id, set()).add(waiter)
            current = await asyncio.to_thread(
                lifecycle_command_store.session_snapshot,
                session_id,
            )
            self._settle_waiter(waiter, current)
            self._announce_waiter_registration(session_id)
        try:
            return await waiter.future
        finally:
            session_waiters = self._waiters.get(session_id)
            if session_waiters is not None:
                session_waiters.discard(waiter)
                if not session_waiters:
                    self._waiters.pop(session_id, None)

    async def wait_until_waiter_registered(self, session_id: str) -> None:
        await self.bind()
        validate_identifier(session_id, "session_id")
        if self.waiter_count(session_id):
            return
        watcher = asyncio.get_running_loop().create_future()
        watchers = self._registration_watchers.setdefault(session_id, set())
        watchers.add(watcher)
        if self.waiter_count(session_id):
            watcher.set_result(None)
        try:
            await watcher
        finally:
            watchers.discard(watcher)
            if not watchers:
                self._registration_watchers.pop(session_id, None)

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
        pending_projection = await asyncio.to_thread(
            lifecycle_command_store.pending_selector_projection,
            session_id,
        )
        if pending_projection is not None:
            await self._apply_selector_projection(session_id, pending_projection)
        try:
            next_snapshot = await asyncio.to_thread(
                lifecycle_command_store.commit_transition,
                session_id,
                request_id,
            )
        except lifecycle_command_store.TransitionConflict as exc:
            raise LifecycleCommandRejected(str(exc)) from exc
        pending_projection = await asyncio.to_thread(
            lifecycle_command_store.pending_selector_projection,
            session_id,
        )
        if pending_projection is not None:
            await self._apply_selector_projection(session_id, pending_projection)
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
            self._settle_waiter(waiter, snapshot)

    def _settle_waiter(
        self,
        waiter: _PhaseWait,
        snapshot: LifecycleSnapshot,
    ) -> None:
        if self._wait_matches(
            snapshot,
            waiter.phases,
            waiter.identity,
            waiter.min_revision,
        ):
            waiter.future.set_result(snapshot)
            return
        if waiter.identity is not None and snapshot.identity != waiter.identity:
            waiter.future.set_exception(IdentityRetired(
                "requested turn identity was retired"
            ))

    def _announce_waiter_registration(self, session_id: str) -> None:
        for watcher in self._registration_watchers.get(session_id, ()):
            if not watcher.done():
                watcher.set_result(None)

    @staticmethod
    def _wait_matches(
        snapshot: LifecycleSnapshot,
        phases: Collection[str],
        identity: UserTurnIdentity | None,
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

    def _release_authority(self) -> None:
        authority_key = self._authority_key
        if authority_key is None:
            return
        with _AUTHORITY_LOCK:
            if _BOUND_AUTHORITIES.get(authority_key) is self:
                _BOUND_AUTHORITIES.pop(authority_key)
        self._authority_key = None
