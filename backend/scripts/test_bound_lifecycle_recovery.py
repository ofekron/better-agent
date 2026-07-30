from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = Path(_test_home.isolate("ba-test-bound-lifecycle-recovery-"))

from event_bus import EventBus  # noqa: E402
from event_journal import publish_event  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from lifecycle_command_model import (  # noqa: E402
    ExecutionTurnIdentity,
    UserTurnIdentity,
)
from provider import BoundRunAuthority  # noqa: E402
from run_recovery import reconcile_missing_bound_lifecycle_orphans  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import user_msg_lifecycle  # noqa: E402


class _TurnManager:
    def __init__(self) -> None:
        self.states: dict[str, list[dict]] = {}

    def get_run_state(self, session_id: str) -> list[dict]:
        return list(self.states.get(session_id, []))

    async def emit_run_state(self, session_id: str) -> None:
        return None


async def _seed_bound(
    engine: LifecycleCommandEngine,
    *,
    publish_provider_start: bool = True,
) -> tuple[str, UserTurnIdentity, ExecutionTurnIdentity, str, str]:
    provider_id = str(uuid.uuid4())
    provider_run_id = str(uuid.uuid4())
    session = session_manager.create(
        name="bound lifecycle recovery",
        model="test-model",
        cwd=str(TEST_HOME),
        orchestration_mode="native",
    )
    session_id = session["id"]
    lifecycle_id = str(uuid.uuid4())
    assistant_id = str(uuid.uuid4())
    execution_turn_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    session_manager.append_user_msg(session_id, {
        "id": user_id,
        "role": "user",
        "content": "test",
        "events": [],
        "lifecycle_msg_id": lifecycle_id,
    })
    session_manager.append_assistant_msg(session_id, {
        "id": assistant_id,
        "role": "assistant",
        "content": "",
        "events": [],
        "run_meta": {"provider_id": provider_id},
    })
    session_manager.flush_pending_persists()
    identity = UserTurnIdentity(lifecycle_id, lifecycle_id)
    execution = ExecutionTurnIdentity(
        execution_turn_id,
        assistant_id,
        "native",
    )
    await engine.begin_turn(
        request_id=f"begin:{session_id}",
        session_id=session_id,
        identity=identity,
    )
    await engine.start_execution(
        session_id,
        execution_identity=execution,
    )
    await engine.bind_execution_run(
        session_id,
        execution_identity=execution,
        provider_run_id=provider_run_id,
    )
    await publish_event(
        session_id=session_id,
        context_id=session_id,
        event_type="turn_started",
        data={
            "app_session_id": session_id,
            "message_id": assistant_id,
            "turn_id": execution_turn_id,
        },
        source="test.turn",
        message_id=assistant_id,
        turn_id=execution_turn_id,
        run_id=execution_turn_id,
    )
    if publish_provider_start:
        await publish_event(
            session_id=session_id,
            context_id=session_id,
            event_type="turn_start",
            data={
                "app_session_id": session_id,
                "message_id": assistant_id,
                "manager_session_id": str(uuid.uuid4()),
            },
            source="provider_stream",
            message_id=assistant_id,
            run_id=execution_turn_id,
        )
    return session_id, identity, execution, provider_id, provider_run_id


async def _durable_failed(**kwargs) -> None:
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


def _target_message(session_id: str, assistant_id: str) -> dict | None:
    session = session_manager.get(session_id) or {}
    return next(
        (
            message
            for message in session.get("messages") or []
            if message.get("id") == assistant_id
        ),
        None,
    )


async def main() -> None:
    engine = LifecycleCommandEngine(EventBus())
    turn_manager = _TurnManager()
    coordinator = SimpleNamespace(
        lifecycle_commands=engine,
        turn_manager=turn_manager,
    )
    try:
        sid, _, execution, provider_id, run_id = await _seed_bound(engine)
        observed: list[tuple[str, str, float]] = []

        def absent(
            actual_provider_id: str,
            actual_run_id: str,
            *,
            started_after: float,
        ) -> BoundRunAuthority:
            observed.append(
                (actual_provider_id, actual_run_id, started_after)
            )
            return BoundRunAuthority("absent", "test")

        with patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=absent,
        ), patch.object(user_msg_lifecycle, "emit_failed", _durable_failed):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 1
        assert len(observed) == 1
        assert observed[0][:2] == (provider_id, run_id)
        assert observed[0][2] > 0
        assert engine.snapshot(sid).phase == "idle"
        assert _target_message(sid, execution.assistant_message_id) is None

        crash_sid, _, crash_execution, _, crash_run_id = await _seed_bound(engine)
        crash_snapshot = engine.snapshot(crash_sid)
        assert await engine.recover_missing_bound_execution(
            crash_sid,
            expected_snapshot=crash_snapshot,
            resolve_outcome=lambda: asyncio.sleep(0, result="failed"),
        )
        assert _target_message(
            crash_sid,
            crash_execution.assistant_message_id,
        ) is not None
        with patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=AssertionError("idle lifecycle was reclassified"),
        ), patch.object(
            user_msg_lifecycle,
            "terminal_event_for_lifecycle_async",
            return_value={
                "type": "user_message_failed",
                "data": {"error": "durable crash-window failure"},
            },
        ):
            turn_manager.states[crash_sid] = [{"run_id": crash_run_id}]
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
            assert _target_message(
                crash_sid,
                crash_execution.assistant_message_id,
            ) is not None
            turn_manager.states.pop(crash_sid)
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert _target_message(
            crash_sid,
            crash_execution.assistant_message_id,
        ) is None

        unknown_sid, _, _, _, unknown_run_id = await _seed_bound(engine)
        with patch(
            "run_recovery.classify_bound_run_authority",
            return_value=BoundRunAuthority("unknown", "test"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(unknown_sid).phase == "starting"
        turn_manager.states[unknown_sid] = [{"run_id": unknown_run_id}]

        uncorrelated_sid, _, _, _, _ = await _seed_bound(
            engine,
            publish_provider_start=False,
        )
        with patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=AssertionError("uncorrelated run was classified"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(uncorrelated_sid).phase == "starting"

        nonempty_sid, _, _, _, nonempty_run_id = await _seed_bound(engine)
        with session_manager.batch(nonempty_sid):
            session = session_manager.get_ref(nonempty_sid)
            assert session is not None
            session["messages"][-1]["content"] = "partial"
        with patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=AssertionError("partial render was classified"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(nonempty_sid).phase == "starting"
        turn_manager.states[nonempty_sid] = [{"run_id": nonempty_run_id}]

        protected_sid, _, protected_execution, _, protected_run_id = (
            await _seed_bound(engine)
        )
        assert await reconcile_missing_bound_lifecycle_orphans(
            coordinator,
            [{
                "app_session_id": protected_sid,
                "target_message_id": (
                    protected_execution.assistant_message_id
                ),
            }],
        ) == 0
        assert engine.snapshot(protected_sid).phase == "starting"
        turn_manager.states[protected_sid] = [{"run_id": protected_run_id}]

        document_sid, _, document_execution, _, document_run_id = (
            await _seed_bound(engine)
        )
        assert await reconcile_missing_bound_lifecycle_orphans(
            coordinator,
            [],
            ownership_documents=[{
                "app_session_id": document_sid,
                "target_message_id": (
                    document_execution.assistant_message_id
                ),
            }],
        ) == 0
        assert engine.snapshot(document_sid).phase == "starting"
        turn_manager.states[document_sid] = [{"run_id": document_run_id}]

        live_sid, _, _, _, live_run_id = await _seed_bound(engine)
        turn_manager.states[live_sid] = [{"run_id": live_run_id}]
        assert await reconcile_missing_bound_lifecycle_orphans(
            coordinator,
            [],
        ) == 0
        assert engine.snapshot(live_sid).phase == "starting"

        owned_sid, _, _, _, owned_run_id = await _seed_bound(engine)
        with patch(
            "run_recovery.classify_bound_run_authority",
            return_value=BoundRunAuthority("owned", "test"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(owned_sid).phase == "starting"
        turn_manager.states[owned_sid] = [{"run_id": owned_run_id}]

        final_live_sid, _, _, _, final_live_run_id = await _seed_bound(engine)
        original_recover = engine.recover_missing_bound_execution

        async def add_final_live_state(*args, **kwargs) -> bool:
            turn_manager.states[final_live_sid] = [{
                "run_id": final_live_run_id,
            }]
            return await original_recover(*args, **kwargs)

        with patch.object(
            engine,
            "recover_missing_bound_execution",
            side_effect=add_final_live_state,
        ), patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=AssertionError("final live run was classified"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(final_live_sid).phase == "starting"

        render_race_sid, _, _, _, render_race_run_id = await _seed_bound(engine)

        async def change_final_render(*args, **kwargs) -> bool:
            with session_manager.batch(render_race_sid):
                session = session_manager.get_ref(render_race_sid)
                assert session is not None
                session["messages"][-1]["run_meta"] = {
                    "provider_id": "replacement-provider",
                }
            return await original_recover(*args, **kwargs)

        with patch.object(
            engine,
            "recover_missing_bound_execution",
            side_effect=change_final_render,
        ), patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=AssertionError("changed render was classified"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(render_race_sid).phase == "starting"
        turn_manager.states[render_race_sid] = [{"run_id": render_race_run_id}]

        cas_sid, _, cas_execution, _, cas_run_id = await _seed_bound(engine)

        async def advance_snapshot(*args, **kwargs) -> bool:
            await engine.confirm_execution_started(
                cas_sid,
                execution_identity=cas_execution,
                provider_run_id=cas_run_id,
            )
            return await original_recover(*args, **kwargs)

        with patch.object(
            engine,
            "recover_missing_bound_execution",
            side_effect=advance_snapshot,
        ), patch(
            "run_recovery.classify_bound_run_authority",
            side_effect=AssertionError("stale snapshot was classified"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(cas_sid).phase == "running"

        missing_terminal_sid, _, _, _, missing_terminal_run_id = (
            await _seed_bound(engine)
        )
        missing_terminal = AsyncMock(side_effect=[None, None])
        with patch(
            "run_recovery.classify_bound_run_authority",
            return_value=BoundRunAuthority("absent", "test"),
        ), patch.object(
            user_msg_lifecycle,
            "terminal_event_for_lifecycle_async",
            missing_terminal,
        ), patch.object(
            user_msg_lifecycle,
            "emit_failed",
            new_callable=AsyncMock,
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(missing_terminal_sid).phase == "starting"
        turn_manager.states[missing_terminal_sid] = [{
            "run_id": missing_terminal_run_id,
        }]

        conflicting_sid, _, _, _, conflicting_run_id = await _seed_bound(engine)
        conflicting_terminal = AsyncMock(side_effect=[
            None,
            {"type": "user_message_done", "data": {"success": True}},
        ])
        with patch(
            "run_recovery.classify_bound_run_authority",
            return_value=BoundRunAuthority("absent", "test"),
        ), patch.object(
            user_msg_lifecycle,
            "terminal_event_for_lifecycle_async",
            conflicting_terminal,
        ), patch.object(
            user_msg_lifecycle,
            "emit_failed",
            new_callable=AsyncMock,
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 0
        assert engine.snapshot(conflicting_sid).phase == "starting"
        turn_manager.states[conflicting_sid] = [{"run_id": conflicting_run_id}]

        for suffix, terminal in (
            (
                "cancelled",
                {
                    "type": "user_message_done",
                    "data": {"success": False, "cancelled": True},
                },
            ),
            (
                "failed",
                {"type": "user_message_failed", "data": {"reason": "test"}},
            ),
        ):
            terminal_sid, _, terminal_execution, _, _ = await _seed_bound(engine)
            with patch(
                "run_recovery.classify_bound_run_authority",
                return_value=BoundRunAuthority("absent", "test"),
            ), patch.object(
                user_msg_lifecycle,
                "terminal_event_for_lifecycle_async",
                return_value=terminal,
            ), patch.object(
                user_msg_lifecycle,
                "emit_failed",
                side_effect=AssertionError(
                    f"{suffix} terminal was duplicated"
                ),
            ):
                assert await reconcile_missing_bound_lifecycle_orphans(
                    coordinator,
                    [],
                ) == 1
            assert engine.snapshot(terminal_sid).phase == "idle"
            terminal_message = _target_message(
                terminal_sid,
                terminal_execution.assistant_message_id,
            )
            if suffix == "failed":
                assert terminal_message is None
                assert (
                    (session_manager.get(terminal_sid) or {}).get(
                        "unseen_error"
                    )
                    == "test"
                )
            else:
                assert (
                    terminal_message is not None
                    and terminal_message.get("stopped_at")
                )

        terminal_sid, _, terminal_execution, _, _ = await _seed_bound(engine)
        with patch(
            "run_recovery.classify_bound_run_authority",
            return_value=BoundRunAuthority("absent", "test"),
        ), patch.object(
            user_msg_lifecycle,
            "terminal_event_for_lifecycle_async",
            return_value={
                "type": "user_message_done",
                "data": {"success": True},
            },
        ), patch.object(
            user_msg_lifecycle,
            "emit_failed",
            side_effect=AssertionError("terminal truth was overwritten"),
        ):
            assert await reconcile_missing_bound_lifecycle_orphans(
                coordinator,
                [],
            ) == 1
        assert engine.snapshot(terminal_sid).phase == "idle"
        successful = _target_message(
            terminal_sid,
            terminal_execution.assistant_message_id,
        )
        assert successful is not None and successful.get("completed_at")
    finally:
        await engine.close()
        session_manager.flush_pending_persists()
    print("bound lifecycle recovery integration: ok")


if __name__ == "__main__":
    asyncio.run(main())
