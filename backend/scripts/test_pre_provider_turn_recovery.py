"""Integration regression for restart before provider-run ownership."""

from __future__ import annotations

import asyncio
import json
import inspect
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-pre-provider-recovery-")
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import EventBus  # noqa: E402
from event_journal import publish_event  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from ingestion_versions import marker_matches_current, write_marker  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from lifecycle_command_model import ExecutionTurnIdentity, UserTurnIdentity  # noqa: E402
from run_recovery import (  # noqa: E402
    reconcile_pre_provider_orphans,
    reconcile_unbound_lifecycle_orphans,
)
from runs_dir import runs_root  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import provider  # noqa: E402
import run_recovery  # noqa: E402
import runs_dir  # noqa: E402
import user_msg_lifecycle  # noqa: E402


class _TurnManager:
    def __init__(self) -> None:
        self.states: dict[str, list[dict]] = {}
        self.emitted: list[str] = []

    def get_run_state(self, session_id: str) -> list[dict]:
        return list(self.states.get(session_id, []))

    async def emit_run_state(self, session_id: str) -> None:
        self.emitted.append(session_id)
        await publish_event(
            session_id=session_id,
            context_id=session_id,
            event_type="run_state",
            data={"app_session_id": session_id, "runs": []},
            source="test.run_state",
        )


def _coordinator(
    lifecycle_commands: LifecycleCommandEngine | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        turn_manager=_TurnManager(),
        lifecycle_commands=lifecycle_commands,
    )


def _seed_tail(
    *,
    terminal: str | None = None,
    assistant_fields: dict | None = None,
) -> tuple[str, str, str, str]:
    session = session_manager.create(
        name="pre-provider recovery",
        model="test-model",
        cwd=str(_TMP_HOME),
        orchestration_mode="native",
    )
    session_id = session["id"]
    user_id = str(uuid.uuid4())
    assistant_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    session_manager.append_user_msg(session_id, {
        "id": user_id,
        "role": "user",
        "content": "test prompt",
        "events": [],
    })
    assistant = {
        "id": assistant_id,
        "role": "assistant",
        "content": "",
        "events": [],
        **(assistant_fields or {}),
    }
    if terminal == "completed":
        assistant["completed_at"] = "2026-07-28T00:00:00+00:00"
    session_manager.append_assistant_msg(session_id, assistant)
    session_manager.flush_pending_persists()
    return session_id, user_id, assistant_id, turn_id


async def _publish_started(
    session_id: str,
    assistant_id: str,
    turn_id: str,
) -> None:
    await publish_event(
        session_id=session_id,
        context_id=session_id,
        event_type="turn_started",
        data={
            "message_id": assistant_id,
            "turn_id": turn_id,
            "source_ts": "2026-07-28T00:00:00+00:00",
        },
        source="test.turn",
        message_id=assistant_id,
        turn_id=turn_id,
        run_id=turn_id,
    )


def _message(session_id: str, message_id: str) -> dict | None:
    session = session_manager.get(session_id) or {}
    return next(
        (
            message for message in session.get("messages") or []
            if message.get("id") == message_id
        ),
        None,
    )


async def _test_unmatched_orphan_finalizes_once() -> None:
    session_id, user_id, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    coordinator = _coordinator()

    assert await reconcile_pre_provider_orphans(coordinator, []) == 1
    assert _message(session_id, assistant_id) is None
    user = _message(session_id, user_id)
    assert user is not None and user.get("status") == "error"
    assert user.get("errorText")
    assert (session_manager.get(session_id) or {}).get("unseen_error")
    assert coordinator.turn_manager.emitted == [session_id]
    session_manager.flush_pending_persists()
    session_manager.reload_root_from_disk(session_id)
    assert _message(session_id, assistant_id) is None
    rows, _, _ = event_ingester.read_events(session_id, limit=10_000)
    assert sum(
        row.get("type") == "turn_stopped"
        and (row.get("data") or {}).get("message_id") == assistant_id
        for row in rows
    ) == 1
    assert [
        row for row in rows
        if row.get("type") == "run_state"
        and (row.get("data") or {}).get("runs") == []
    ]

    assert await reconcile_pre_provider_orphans(coordinator, []) == 0
    assert coordinator.turn_manager.emitted == [session_id]
    rows, _, _ = event_ingester.read_events(session_id, limit=10_000)
    assert sum(
        row.get("type") == "turn_stopped"
        and (row.get("data") or {}).get("message_id") == assistant_id
        for row in rows
    ) == 1


async def _seed_unbound_lifecycle(
    engine: LifecycleCommandEngine,
    *,
    policy: str = "single",
    publish_started: bool = True,
) -> tuple[str, UserTurnIdentity, ExecutionTurnIdentity]:
    session_id, user_id, assistant_id, turn_id = _seed_tail()
    identity = UserTurnIdentity(
        user_turn_id=(lifecycle_id := str(uuid.uuid4())),
        lifecycle_message_id=lifecycle_id,
    )
    with session_manager.batch(session_id):
        session = session_manager.get_ref(session_id)
        assert session is not None
        user = next(
            message
            for message in session["messages"]
            if message.get("id") == user_id
        )
        assert user is not None
        user["lifecycle_msg_id"] = lifecycle_id
    execution_identity = ExecutionTurnIdentity(
        execution_turn_id=turn_id,
        assistant_message_id=assistant_id,
        role="native",
    )
    await engine.begin_turn(
        request_id=f"begin-{session_id}",
        session_id=session_id,
        identity=identity,
        execution_policy=policy,
    )
    await engine.start_execution(
        session_id,
        execution_identity=execution_identity,
    )
    if publish_started:
        await _publish_started(session_id, assistant_id, turn_id)
    return session_id, identity, execution_identity


async def _test_unbound_lifecycle_recovery() -> None:
    engine = LifecycleCommandEngine(EventBus())
    try:
        session_id, identity, _ = await _seed_unbound_lifecycle(engine)
        coordinator = _coordinator(engine)
        async def durable_failed(**kwargs) -> None:
            await publish_event(
                session_id=kwargs["app_session_id"],
                context_id=kwargs["app_session_id"],
                event_type="user_message_failed",
                data={
                    "lifecycle_msg_id": kwargs["lifecycle_msg_id"],
                    "reason": kwargs["reason"],
                    "error": kwargs["error"],
                },
                source="test.lifecycle",
                message_id=kwargs["lifecycle_msg_id"],
            )

        with patch.object(
            user_msg_lifecycle,
            "emit_failed",
            durable_failed,
        ):
            assert await reconcile_unbound_lifecycle_orphans(coordinator, []) == 1
        assert engine.snapshot(session_id).phase == "idle"
        terminal = await user_msg_lifecycle.terminal_event_for_lifecycle_async(
            session_id,
            identity.lifecycle_message_id,
        )
        assert terminal is not None
        assert terminal["type"] == "user_message_failed"
        assert await reconcile_unbound_lifecycle_orphans(coordinator, []) == 0

        legacy_sid, legacy_identity, legacy_execution = (
            await _seed_unbound_lifecycle(engine)
        )
        legacy_session = session_manager.get(legacy_sid) or {}
        legacy_user = next(
            message
            for message in legacy_session["messages"]
            if message.get("lifecycle_msg_id")
            == legacy_identity.lifecycle_message_id
        )
        session_manager.mark_user_error(
            legacy_sid,
            legacy_user["id"],
            "legacy recovery failure",
        )
        session_manager.remove_assistant_msg(
            legacy_sid,
            legacy_execution.assistant_message_id,
        )
        await publish_event(
            session_id=legacy_sid,
            context_id=legacy_sid,
            event_type="turn_stopped",
            data={
                "app_session_id": legacy_sid,
                "message_id": legacy_execution.assistant_message_id,
                "turn_id": legacy_execution.execution_turn_id,
                "reason": "orphaned_before_provider",
            },
            source="run_recovery.pre_provider",
            message_id=legacy_execution.assistant_message_id,
            turn_id=legacy_execution.execution_turn_id,
            run_id=legacy_execution.execution_turn_id,
        )
        with patch.object(
            user_msg_lifecycle,
            "emit_failed",
            durable_failed,
        ):
            assert await reconcile_unbound_lifecycle_orphans(
                coordinator,
                [],
            ) == 1
        assert engine.snapshot(legacy_sid).phase == "idle"

        no_rows_sid, no_rows_identity, no_rows_execution = (
            await _seed_unbound_lifecycle(engine, publish_started=False)
        )
        no_rows_session = session_manager.get(no_rows_sid) or {}
        no_rows_user = next(
            message
            for message in no_rows_session["messages"]
            if message.get("lifecycle_msg_id")
            == no_rows_identity.lifecycle_message_id
        )
        session_manager.mark_user_error(
            no_rows_sid,
            no_rows_user["id"],
            "legacy recovery failure",
        )
        session_manager.remove_assistant_msg(
            no_rows_sid,
            no_rows_execution.assistant_message_id,
        )
        with patch.object(
            user_msg_lifecycle,
            "emit_failed",
            durable_failed,
        ):
            assert await reconcile_unbound_lifecycle_orphans(
                coordinator,
                [],
            ) == 1
        assert engine.snapshot(no_rows_sid).phase == "idle"

        for event_type, source, reason, turn_suffix in (
            ("agent_message", "provider_stream", None, ""),
            (
                "turn_stopped",
                "run_recovery.pre_provider",
                "different_reason",
                "",
            ),
            ("turn_complete", "run_recovery.pre_provider", None, ""),
        ):
            rejected_sid, rejected_identity, rejected_execution = (
                await _seed_unbound_lifecycle(engine)
            )
            rejected_session = session_manager.get(rejected_sid) or {}
            rejected_user = next(
                message
                for message in rejected_session["messages"]
                if message.get("lifecycle_msg_id")
                == rejected_identity.lifecycle_message_id
            )
            session_manager.mark_user_error(
                rejected_sid,
                rejected_user["id"],
                "legacy recovery failure",
            )
            session_manager.remove_assistant_msg(
                rejected_sid,
                rejected_execution.assistant_message_id,
            )
            await publish_event(
                session_id=rejected_sid,
                context_id=rejected_sid,
                event_type=event_type,
                data={
                    "app_session_id": rejected_sid,
                    "message_id": rejected_execution.assistant_message_id,
                    "turn_id": (
                        rejected_execution.execution_turn_id + turn_suffix
                    ),
                    **({"reason": reason} if reason else {}),
                },
                source=source,
                message_id=rejected_execution.assistant_message_id,
                turn_id=rejected_execution.execution_turn_id,
                run_id=rejected_execution.execution_turn_id,
            )
            assert await reconcile_unbound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
            rejected_snapshot = engine.snapshot(rejected_sid)
            assert await engine.recover_unbound_execution(
                rejected_sid,
                expected_snapshot=rejected_snapshot,
                resolve_outcome=lambda: asyncio.sleep(0, result="failed"),
            )

        done_sid, done_identity, done_execution = await _seed_unbound_lifecycle(
            engine,
            policy="sequential",
        )
        await publish_event(
            session_id=done_sid,
            context_id=done_sid,
            event_type="user_message_done",
            data={
                "lifecycle_msg_id": done_identity.lifecycle_message_id,
                "success": True,
            },
            source="test.lifecycle",
            message_id=done_identity.lifecycle_message_id,
        )
        assert await reconcile_unbound_lifecycle_orphans(coordinator, []) == 1
        assert engine.snapshot(done_sid).phase == "idle"
        await reconcile_pre_provider_orphans(coordinator, [])
        completed = _message(
            done_sid,
            done_execution.assistant_message_id,
        )
        assert completed is not None and completed.get("completed_at")

        stopped_sid, stopped_identity, stopped_execution = (
            await _seed_unbound_lifecycle(engine)
        )
        await publish_event(
            session_id=stopped_sid,
            context_id=stopped_sid,
            event_type="user_message_done",
            data={
                "lifecycle_msg_id": stopped_identity.lifecycle_message_id,
                "success": False,
                "cancelled": True,
            },
            source="test.lifecycle",
            message_id=stopped_identity.lifecycle_message_id,
        )
        assert await reconcile_unbound_lifecycle_orphans(
            coordinator,
            [],
        ) == 1
        assert engine.snapshot(stopped_sid).phase == "idle"
        await reconcile_pre_provider_orphans(coordinator, [])
        stopped = _message(
            stopped_sid,
            stopped_execution.assistant_message_id,
        )
        assert stopped is not None and stopped.get("stopped_at")

        failed_sid, failed_identity, failed_execution = (
            await _seed_unbound_lifecycle(engine)
        )
        await durable_failed(
            app_session_id=failed_sid,
            lifecycle_msg_id=failed_identity.lifecycle_message_id,
            reason="existing_failure",
            error="existing failure",
        )
        assert await reconcile_unbound_lifecycle_orphans(coordinator, []) == 1
        assert engine.snapshot(failed_sid).phase == "idle"
        await reconcile_pre_provider_orphans(coordinator, [])
        assert _message(
            failed_sid,
            failed_execution.assistant_message_id,
        ) is None
        failed_rows, _, _ = event_ingester.read_events(
            failed_sid,
            limit=10_000,
        )
        assert sum(
            row.get("type") == "user_message_failed"
            and (row.get("data") or {}).get("lifecycle_msg_id")
            == failed_identity.lifecycle_message_id
            for row in failed_rows
        ) == 1

        aborted_sid, aborted_identity, aborted_execution = (
            await _seed_unbound_lifecycle(engine, policy="sequential")
        )
        await durable_failed(
            app_session_id=aborted_sid,
            lifecycle_msg_id=aborted_identity.lifecycle_message_id,
            reason="existing_failure",
            error="existing failure",
        )
        await engine.abort_execution(
            aborted_sid,
            execution_identity=aborted_execution,
            outcome="failed",
        )
        assert engine.snapshot(aborted_sid).execution.phase == "aborted"
        assert await reconcile_unbound_lifecycle_orphans(
            coordinator,
            [],
        ) == 1
        assert engine.snapshot(aborted_sid).phase == "idle"

        bound_sid, _, bound_execution = await _seed_unbound_lifecycle(engine)
        await engine.bind_execution_run(
            bound_sid,
            execution_identity=bound_execution,
            provider_run_id=str(uuid.uuid4()),
        )
        assert await reconcile_unbound_lifecycle_orphans(coordinator, []) == 0
        assert engine.snapshot(bound_sid).phase == "starting"

        protected_sid, _, protected_execution = (
            await _seed_unbound_lifecycle(engine)
        )
        recovered = [{
            "app_session_id": protected_sid,
            "target_message_id": protected_execution.assistant_message_id,
        }]
        assert await reconcile_unbound_lifecycle_orphans(
            coordinator,
            recovered,
        ) == 0
        assert engine.snapshot(protected_sid).phase == "starting"
        protected_snapshot = engine.snapshot(protected_sid)
        assert await engine.recover_unbound_execution(
            protected_sid,
            expected_snapshot=protected_snapshot,
            resolve_outcome=lambda: asyncio.sleep(0, result="failed"),
        )

        nonempty_sid, _, nonempty_execution = (
            await _seed_unbound_lifecycle(engine)
        )
        with session_manager.batch(nonempty_sid):
            message = _message(
                nonempty_sid,
                nonempty_execution.assistant_message_id,
            )
            assert message is not None
            message["content"] = "provider output"
        assert await reconcile_unbound_lifecycle_orphans(
            coordinator,
            [],
        ) == 0
        assert engine.snapshot(nonempty_sid).phase == "starting"
        nonempty_snapshot = engine.snapshot(nonempty_sid)
        assert await engine.recover_unbound_execution(
            nonempty_sid,
            expected_snapshot=nonempty_snapshot,
            resolve_outcome=lambda: asyncio.sleep(0, result="failed"),
        )

        changed_sid, _, _ = await _seed_unbound_lifecycle(engine)
        stale = engine.snapshot(changed_sid)
        await engine.request_active_stop(changed_sid)
        assert not await engine.recover_unbound_execution(
            changed_sid,
            expected_snapshot=stale,
            resolve_outcome=lambda: asyncio.sleep(0, result="failed"),
        )
        assert engine.snapshot(changed_sid).phase == "stopping"

        raced_sid, _, _ = await _seed_unbound_lifecycle(engine)
        original_recover = engine.recover_unbound_execution

        async def race_recover(*args, **kwargs) -> bool:
            await engine.request_active_stop(raced_sid)
            return await original_recover(*args, **kwargs)

        with (
            patch.object(
                user_msg_lifecycle,
                "emit_failed",
                durable_failed,
            ),
            patch.object(
                engine,
                "recover_unbound_execution",
                race_recover,
            ),
        ):
            assert await reconcile_unbound_lifecycle_orphans(coordinator, []) == 0
        assert engine.snapshot(raced_sid).phase == "stopping"
    finally:
        await engine.close()


async def _test_descriptor_target_is_protected() -> None:
    session_id, _, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    recovered = [{
        "app_session_id": session_id,
        "target_message_id": assistant_id,
        "run_id": str(uuid.uuid4()),
        "alive": True,
    }]

    assert await reconcile_pre_provider_orphans(_coordinator(), recovered) == 0
    assert _message(session_id, assistant_id) is not None
    session_manager.set_completed_at(
        session_id,
        assistant_id,
        "2026-07-28T00:00:01+00:00",
    )
    session_manager.flush_pending_persists()


async def _test_run_documents_protect_targets() -> None:
    protected: list[tuple[str, str]] = []
    ownership_documents: list[dict] = []
    for document_name in ("input.json", "backend_state.json"):
        session_id, _, assistant_id, turn_id = _seed_tail()
        await _publish_started(session_id, assistant_id, turn_id)
        run_dir = runs_root() / str(uuid.uuid4())
        run_dir.mkdir(parents=True)
        (run_dir / document_name).write_text(json.dumps({
            "app_session_id": session_id,
            "target_message_id": assistant_id,
        }))
        ownership_documents.append({
            "persist_to": session_id,
            "target_message_id": assistant_id,
            "run_id": run_dir.name,
        })
        protected.append((session_id, assistant_id))

    worker_sid, _, worker_assistant_id, worker_turn_id = _seed_tail()
    await _publish_started(worker_sid, worker_assistant_id, worker_turn_id)
    worker_run_dir = runs_root() / str(uuid.uuid4())
    worker_run_dir.mkdir(parents=True)
    (worker_run_dir / "input.json").write_text(json.dumps({
        "app_session_id": str(uuid.uuid4()),
        "worker_agent_session_id": worker_sid,
        "target_message_id": worker_assistant_id,
    }))
    ownership_documents.append({
        "persist_to": worker_sid,
        "target_message_id": worker_assistant_id,
        "run_id": worker_run_dir.name,
    })
    protected.append((worker_sid, worker_assistant_id))

    assert await reconcile_pre_provider_orphans(
        _coordinator(),
        [],
        ownership_documents=ownership_documents,
    ) == 0
    assert all(
        _message(session_id, assistant_id) is not None
        for session_id, assistant_id in protected
    )
    for session_id, assistant_id in protected:
        session_manager.set_completed_at(
            session_id,
            assistant_id,
            "2026-07-28T00:00:01+00:00",
        )
    session_manager.flush_pending_persists()


async def _test_current_run_state_is_protected() -> None:
    session_id, _, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    coordinator = _coordinator()
    coordinator.turn_manager.states[session_id] = [{
        "run_id": turn_id,
        "target_message_id": assistant_id,
    }]

    assert await reconcile_pre_provider_orphans(coordinator, []) == 0
    assert _message(session_id, assistant_id) is not None
    session_manager.set_completed_at(
        session_id,
        assistant_id,
        "2026-07-28T00:00:02+00:00",
    )
    session_manager.flush_pending_persists()


async def _test_partial_assistants_and_render_facts_are_untouched() -> None:
    for field, value in (
        ("content", "partial"),
        ("events", [{"type": "agent_message"}]),
        ("workers", [{"delegation_id": "worker-1"}]),
    ):
        session_id, _, assistant_id, turn_id = _seed_tail(
            assistant_fields={field: value},
        )
        await _publish_started(session_id, assistant_id, turn_id)
        assert await reconcile_pre_provider_orphans(_coordinator(), []) == 0
        assert _message(session_id, assistant_id) is not None
        session_manager.set_completed_at(
            session_id,
            assistant_id,
            "2026-07-28T00:00:03+00:00",
        )
        session_manager.flush_pending_persists()

    session_id, _, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    await publish_event(
        session_id=session_id,
        context_id=session_id,
        event_type="agent_message",
        data={"uuid": str(uuid.uuid4()), "message_id": assistant_id},
        source="test.provider",
        message_id=assistant_id,
        turn_id=turn_id,
    )
    assert await reconcile_pre_provider_orphans(_coordinator(), []) == 0
    assert _message(session_id, assistant_id) is not None
    session_manager.set_completed_at(
        session_id,
        assistant_id,
        "2026-07-28T00:00:04+00:00",
    )
    session_manager.flush_pending_persists()


async def _test_provider_fact_order_and_ownership_resolution_are_untouched() -> None:
    before_sid, _, before_assistant_id, before_turn_id = _seed_tail()
    await publish_event(
        session_id=before_sid,
        context_id=before_sid,
        event_type="agent_message",
        data={"message_id": before_assistant_id, "uuid": str(uuid.uuid4())},
        source="test.provider",
        message_id=before_assistant_id,
        turn_id=before_turn_id,
    )
    await _publish_started(before_sid, before_assistant_id, before_turn_id)

    resolved_sid, _, resolved_assistant_id, resolved_turn_id = _seed_tail()
    await _publish_started(resolved_sid, resolved_assistant_id, resolved_turn_id)
    await publish_event(
        session_id=resolved_sid,
        context_id=resolved_sid,
        event_type="event_ownership_resolved",
        data={
            "message_id": resolved_assistant_id,
            "event_seq": 1,
        },
        source="test.ownership",
        message_id=resolved_assistant_id,
        turn_id=resolved_turn_id,
    )

    assert await reconcile_pre_provider_orphans(_coordinator(), []) == 0
    assert _message(before_sid, before_assistant_id) is not None
    assert _message(resolved_sid, resolved_assistant_id) is not None
    for session_id, assistant_id in (
        (before_sid, before_assistant_id),
        (resolved_sid, resolved_assistant_id),
    ):
        session_manager.set_completed_at(
            session_id,
            assistant_id,
            "2026-07-28T00:00:05+00:00",
        )
    session_manager.flush_pending_persists()


async def _test_current_reconciled_marker_does_not_protect() -> None:
    session_id, _, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    run_dir = runs_root() / str(uuid.uuid4())
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text(json.dumps({
        "app_session_id": session_id,
    }))
    write_marker(run_dir / "reconciled.marker", "claude")

    assert await reconcile_pre_provider_orphans(_coordinator(), []) == 1
    assert _message(session_id, assistant_id) is None


async def _test_malformed_run_document_aborts_sweep() -> None:
    session_id, _, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    run_dir = runs_root() / str(uuid.uuid4())
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text("{not-json")

    assert await reconcile_pre_provider_orphans(
        _coordinator(),
        [],
        ownership_safe=False,
    ) == 0
    assert _message(session_id, assistant_id) is not None


async def _test_symlink_marker_is_not_read_and_aborts_sweep() -> None:
    session_id, _, assistant_id, turn_id = _seed_tail()
    await _publish_started(session_id, assistant_id, turn_id)
    run_dir = runs_root() / str(uuid.uuid4())
    run_dir.mkdir(parents=True)
    outside_marker = Path(_TMP_HOME) / "outside-reconciled.marker"
    write_marker(outside_marker, "claude")
    marker = run_dir / "reconciled.marker"
    marker.symlink_to(outside_marker)

    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == marker or path == outside_marker:
            raise AssertionError("symlink marker target was read")
        return original_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", guarded_read_text):
        assert marker_matches_current(marker, "claude") is False

    assert await reconcile_pre_provider_orphans(
        _coordinator(),
        [],
        ownership_safe=False,
    ) == 0
    assert _message(session_id, assistant_id) is not None


def _test_ownership_scan_is_single_pass_and_marker_first() -> None:
    reconcile_source = inspect.getsource(
        run_recovery.reconcile_pre_provider_orphans,
    )
    assert "iter_run_dirs" not in reconcile_source
    assert "_run_document_ownership" not in reconcile_source

    discovery_source = inspect.getsource(provider._recover_all_in_flight_owned)
    indexed_skip = discovery_source.index("indexed_marker =")
    symlink_guard = discovery_source.index("if marker_path.is_symlink()")
    input_read = discovery_source.index("input_path.read_text")
    backend_read = discovery_source.index("bs_path.read_text")
    marker_read = discovery_source.index("marker_path.read_text")
    assert indexed_skip < symlink_guard < marker_read < backend_read < input_read
    assert "if inspect_input and input_path.exists()" in discovery_source


def _test_lifecycle_candidate_requires_render_correlation() -> None:
    assert run_recovery._matches_unbound_lifecycle_candidate(
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    ) is None
    empty = session_manager.create(
        name="empty lifecycle candidate",
        model="test-model",
        cwd=str(_TMP_HOME),
        orchestration_mode="native",
    )
    assert run_recovery._matches_unbound_lifecycle_candidate(
        empty["id"],
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    ) is None


def _test_noncandidate_backend_runs_do_not_touch_inputs() -> None:
    shutil.rmtree(runs_root(), ignore_errors=True)
    runs_root().mkdir(parents=True, exist_ok=True)

    class _Provider:
        id = "test-provider"
        suspended = False
        defunct = False

    for index in range(2_000):
        run_dir = runs_root() / f"noncandidate-{index:04d}"
        run_dir.mkdir(parents=True)
        (run_dir / "backend_state.json").write_text(json.dumps({
            "provider_id": "test-provider",
            "app_session_id": f"other-session-{index}",
            "target_message_id": f"other-message-{index}",
        }))
        (run_dir / "input.json").write_text(json.dumps({
            "app_session_id": f"other-session-{index}",
            "target_message_id": f"other-message-{index}",
        }))

    input_exists_calls = 0
    input_read_calls = 0
    original_exists = Path.exists
    original_read_text = Path.read_text

    def counted_exists(path: Path) -> bool:
        nonlocal input_exists_calls
        if path.name == "input.json":
            input_exists_calls += 1
        return original_exists(path)

    def counted_read_text(path: Path, *args, **kwargs):
        nonlocal input_read_calls
        if path.name == "input.json":
            input_read_calls += 1
        return original_read_text(path, *args, **kwargs)

    fake_provider = _Provider()
    with (
        patch.object(Path, "exists", counted_exists),
        patch.object(Path, "read_text", counted_read_text),
        patch.object(
            runs_dir,
            "ensure_reconciled_marker_index_backfilled",
            return_value=False,
        ),
        patch.object(
            runs_dir,
            "load_reconciled_marker_index",
            return_value={},
        ),
        patch.object(provider, "get_provider", return_value=fake_provider),
        patch.object(
            provider,
            "_split_recovery_scan_run_ids",
            return_value=(set(), set()),
        ),
    ):
        assert provider.recover_all_in_flight(
            candidate_targets={("candidate-session", "candidate-message")},
        ) == []

    ownership, safe = provider.take_recovery_scan_ownership()
    assert safe is True
    assert ownership == []
    assert input_exists_calls == 0
    assert input_read_calls == 0


async def _test_terminal_and_missing_boundary_are_untouched() -> None:
    terminal_sid, _, terminal_assistant_id, terminal_turn_id = _seed_tail(
        terminal="completed",
    )
    await _publish_started(
        terminal_sid,
        terminal_assistant_id,
        terminal_turn_id,
    )
    missing_sid, _, missing_assistant_id, _ = _seed_tail()
    await publish_event(
        session_id=missing_sid,
        context_id="worker-transport-owner",
        event_type="turn_stopped",
        data={
            "app_session_id": missing_sid,
            "message_id": missing_assistant_id,
        },
        source="test.worker_terminal",
        message_id=missing_assistant_id,
    )

    normalized_sid, _, normalized_assistant_id, normalized_turn_id = _seed_tail()
    await _publish_started(
        normalized_sid,
        normalized_assistant_id,
        normalized_turn_id,
    )
    await publish_event(
        session_id=normalized_sid,
        context_id="worker-transport-owner",
        event_type="turn_stopped",
        data={
            "app_session_id": normalized_sid,
            "message_id": normalized_assistant_id,
        },
        source="test.worker_terminal",
        message_id=normalized_assistant_id,
    )

    assert await reconcile_pre_provider_orphans(_coordinator(), []) == 0
    assert _message(terminal_sid, terminal_assistant_id) is not None
    assert _message(missing_sid, missing_assistant_id) is not None
    assert _message(normalized_sid, normalized_assistant_id) is not None


async def _main() -> None:
    await _test_unmatched_orphan_finalizes_once()
    await _test_descriptor_target_is_protected()
    await _test_run_documents_protect_targets()
    await _test_current_run_state_is_protected()
    await _test_partial_assistants_and_render_facts_are_untouched()
    await _test_provider_fact_order_and_ownership_resolution_are_untouched()
    await _test_current_reconciled_marker_does_not_protect()
    await _test_terminal_and_missing_boundary_are_untouched()
    await _test_malformed_run_document_aborts_sweep()
    await _test_symlink_marker_is_not_read_and_aborts_sweep()
    _test_ownership_scan_is_single_pass_and_marker_first()
    _test_lifecycle_candidate_requires_render_correlation()
    _test_noncandidate_backend_runs_do_not_touch_inputs()
    await _test_unbound_lifecycle_recovery()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
        print("PASS pre-provider turn recovery")
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
