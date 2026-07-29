from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import _test_home

_test_home.isolate("ba-test-recovery-live-identity-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_bus import EventBus  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from lifecycle_command_model import ExecutionTurnIdentity, UserTurnIdentity  # noqa: E402
import run_recovery  # noqa: E402


async def _running_execution(
    engine: LifecycleCommandEngine,
    *,
    session_id: str,
    provider_run_id: str,
) -> ExecutionTurnIdentity:
    turn_identity = UserTurnIdentity(
        user_turn_id=f"user-{session_id}",
        lifecycle_message_id=f"message-{session_id}",
    )
    execution_identity = ExecutionTurnIdentity(
        execution_turn_id=f"execution-{session_id}",
        assistant_message_id=f"assistant-{session_id}",
        role="native",
    )
    await engine.begin_turn(
        request_id=f"{session_id}:begin",
        session_id=session_id,
        identity=turn_identity,
    )
    await engine.start_execution(
        session_id,
        execution_identity=execution_identity,
    )
    await engine.elect_execution_attempt(
        session_id,
        execution_identity=execution_identity,
        provider_run_id=provider_run_id,
    )
    await engine.admit_execution_attempt(
        session_id,
        execution_identity=execution_identity,
        provider_run_id=provider_run_id,
    )
    return execution_identity


async def test_repeated_live_recovery_is_idempotent() -> None:
    engine = LifecycleCommandEngine(EventBus())
    session_id = "repeated-live-recovery"
    provider_run_id = "provider-run"
    execution_identity = await _running_execution(
        engine,
        session_id=session_id,
        provider_run_id=provider_run_id,
    )
    await engine.detach_execution(
        session_id,
        execution_identity=execution_identity,
    )

    await run_recovery._adopt_recovered_live_execution(
        engine,
        app_session_id=session_id,
        lifecycle_message_id=f"message-{session_id}",
        execution_turn_id=execution_identity.execution_turn_id,
        assistant_message_id=execution_identity.assistant_message_id,
        provider_run_id=provider_run_id,
    )
    first = engine.snapshot(session_id)
    assert first.execution is not None
    assert first.execution.phase == "running"

    await run_recovery._adopt_recovered_live_execution(
        engine,
        app_session_id=session_id,
        lifecycle_message_id=f"message-{session_id}",
        execution_turn_id=execution_identity.execution_turn_id,
        assistant_message_id=execution_identity.assistant_message_id,
        provider_run_id=provider_run_id,
    )
    second = engine.snapshot(session_id)
    assert second == first

    await engine.close()


async def test_mismatch_fails_before_live_handoff_mutates_provider() -> None:
    engine = LifecycleCommandEngine(EventBus())
    session_id = "mismatched-live-recovery"
    execution_identity = await _running_execution(
        engine,
        session_id=session_id,
        provider_run_id="authoritative-run",
    )
    await engine.detach_execution(
        session_id,
        execution_identity=execution_identity,
    )
    before = engine.snapshot(session_id)

    try:
        await run_recovery._adopt_recovered_live_execution(
            engine,
            app_session_id=session_id,
            lifecycle_message_id=f"message-{session_id}",
            execution_turn_id=execution_identity.execution_turn_id,
            assistant_message_id=execution_identity.assistant_message_id,
            provider_run_id="foreign-run",
        )
    except RuntimeError as exc:
        assert str(exc) == "recovered live execution identity is ambiguous"
    else:
        raise AssertionError("mismatched recovered execution was accepted")
    assert engine.snapshot(session_id) == before

    source = inspect.getsource(run_recovery._integrate_one_locked)
    preflight = source.index("_validate_recovered_live_execution")
    assert preflight < source.index("attach_recovered_run")
    assert preflight < source.index("active_run_ids.setdefault")
    assert preflight < source.index("run_state_add")
    adoption = source.index("_adopt_recovered_live_execution")
    assert adoption > source.index("run_state_add")

    await engine.close()


async def main() -> None:
    await test_repeated_live_recovery_is_idempotent()
    await test_mismatch_fails_before_live_handoff_mutates_provider()


if __name__ == "__main__":
    asyncio.run(main())
