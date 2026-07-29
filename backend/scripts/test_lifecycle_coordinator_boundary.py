"""Coordinator-boundary proof for logical user-turn lifecycle ownership."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-lifecycle-coordinator-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_installation  # noqa: E402

_test_installation.activate(Path(_TMP_HOME))

import lifecycle_command_store  # noqa: E402
from lifecycle_command_model import UserTurnIdentity  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
import run_recovery  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import user_msg_lifecycle  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS: {name}")


def create_session(name: str) -> str:
    return session_manager.create(
        name=name,
        cwd=str(_TMP_HOME),
        bare_config=True,
    )["id"]


async def arm_idle_wait(
    coordinator: Coordinator,
    session_id: str,
) -> asyncio.Task:
    revision = coordinator.lifecycle_commands.snapshot(session_id).revision
    waiter = asyncio.create_task(
        coordinator.lifecycle_commands.wait_for_phase(
            session_id,
            {"idle"},
            min_revision=revision + 1,
        )
    )
    await coordinator.lifecycle_commands.wait_until_waiter_registered(session_id)
    return waiter


async def prove_serialized_turns(coordinator: Coordinator) -> None:
    session_id = create_session("serialized")
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    observed: list[str] = []

    async def handle_prompt(**params) -> None:
        prompt = params["prompt"]
        lifecycle_id = "turn-A" if prompt == "A" else "turn-B"
        await coordinator.lifecycle_commands.confirm_active_started(
            session_id,
            lifecycle_message_id=lifecycle_id,
        )
        observed.append(prompt)
        if prompt == "A":
            a_started.set()
            await release_a.wait()
            return
        b_started.set()
        await release_b.wait()

    coordinator.handle_prompt = handle_prompt
    coordinator.submit_prompt(session_id, {
        "prompt": "A",
        "lifecycle_msg_id": "turn-A",
    })
    coordinator.submit_prompt(session_id, {
        "prompt": "B",
        "lifecycle_msg_id": "turn-B",
    })

    await a_started.wait()
    snapshot_a = coordinator.lifecycle_commands.snapshot(session_id)
    check("A owns the only active logical turn", (
        snapshot_a.phase == "running"
        and snapshot_a.identity is not None
        and snapshot_a.identity.user_turn_id == "turn-A"
    ))
    check("B cannot begin before A is terminal", not b_started.is_set())

    release_a.set()
    await b_started.wait()
    snapshot_b = coordinator.lifecycle_commands.snapshot(session_id)
    check("B begins exactly once after A is terminal", (
        observed == ["A", "B"]
        and snapshot_b.phase == "running"
        and snapshot_b.identity is not None
        and snapshot_b.identity.user_turn_id == "turn-B"
    ))

    idle = await arm_idle_wait(coordinator, session_id)
    release_b.set()
    await idle
    check(
        "serialized queue returns lifecycle authority to idle",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "idle",
    )


async def prove_running_stop_transition(coordinator: Coordinator) -> None:
    session_id = create_session("running-stop")
    provider_started = asyncio.Event()
    cancel_event = asyncio.Event()

    async def handle_prompt(**_params) -> None:
        coordinator.turn_manager.cancel_events[session_id] = cancel_event
        await coordinator.lifecycle_commands.confirm_active_started(
            session_id,
            lifecycle_message_id="turn-running-stop",
        )
        provider_started.set()
        await cancel_event.wait()

    coordinator.handle_prompt = handle_prompt
    coordinator.submit_prompt(session_id, {
        "prompt": "stop after provider admission",
        "lifecycle_msg_id": "turn-running-stop",
    })
    await provider_started.wait()
    check(
        "provider admission transitions starting to running",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "running",
    )
    idle = await arm_idle_wait(coordinator, session_id)
    check(
        "active cancellation intent lands",
        await coordinator.cancel_turn(session_id),
    )
    check(
        "cancellation intent transitions running to stopping",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "stopping",
    )
    await idle
    coordinator.turn_manager.cancel_events.pop(session_id, None)
    check(
        "stopped provider work releases the logical turn",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "idle",
    )


async def prove_cancel_before_execution(coordinator: Coordinator) -> None:
    session_id = create_session("cancel-gap")
    at_gap = asyncio.Event()
    release_gap = asyncio.Event()
    executed = False
    original_wait = coordinator.turn_manager.wait_for_clear_runs

    async def wait_for_clear_runs(candidate_session_id: str) -> None:
        if candidate_session_id != session_id:
            await original_wait(candidate_session_id)
            return
        at_gap.set()
        await release_gap.wait()

    async def handle_prompt(**_params) -> None:
        nonlocal executed
        executed = True

    coordinator.turn_manager.wait_for_clear_runs = wait_for_clear_runs
    coordinator.handle_prompt = handle_prompt
    queued_id = coordinator.submit_prompt(session_id, {
        "prompt": "cancel me",
        "lifecycle_msg_id": "turn-cancelled",
    })

    await at_gap.wait()
    check(
        "dequeued prompt holds a logical claim during the cancellation gap",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "starting",
    )
    check(
        "public queue cancellation reaches the dequeued prompt",
        coordinator.cancel_queued(session_id, queued_id),
    )
    idle = await arm_idle_wait(coordinator, session_id)
    release_gap.set()
    await idle
    coordinator.turn_manager.wait_for_clear_runs = original_wait

    check("cancelled prompt never executes provider work", not executed)
    check(
        "cancel-before-execution releases the logical claim",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "idle",
    )
    transition = lifecycle_command_store.required_transition(
        session_id,
        "user-turn:turn-cancelled:0:finish",
    )
    check(
        "cancel-before-execution is durably terminal as stopped",
        transition["notification_payload"]["outcome"] == "stopped",
    )


async def prove_retry_reclaims_identity(coordinator: Coordinator) -> None:
    session_id = create_session("retry")
    queued_id = "retry-queue-item"
    lifecycle_id = "turn-retry"
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    attempts = 0

    session_manager.add_queued_prompt(session_id, {
        "id": queued_id,
        "content": "retry me",
        "lifecycle_msg_id": lifecycle_id,
    })

    async def handle_prompt(**_params) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry proof")
        second_started.set()
        await release_second.wait()

    coordinator.handle_prompt = handle_prompt
    coordinator.submit_prompt(session_id, {
        "_queued_id": queued_id,
        "prompt": "retry me",
        "lifecycle_msg_id": lifecycle_id,
    })

    await second_started.wait()
    snapshot = coordinator.lifecycle_commands.snapshot(session_id)
    check("retry begins a second delivery exactly once", (
        attempts == 2
        and snapshot.phase == "starting"
        and snapshot.identity is not None
        and snapshot.identity.user_turn_id == lifecycle_id
    ))
    first_finish = lifecycle_command_store.required_transition(
        session_id,
        f"user-turn:{lifecycle_id}:0:finish",
    )
    check(
        "failed delivery releases its claim before retry",
        first_finish["notification_payload"]["outcome"] == "failed",
    )

    idle = await arm_idle_wait(coordinator, session_id)
    release_second.set()
    await idle
    check(
        "successful retry returns lifecycle authority to idle",
        coordinator.lifecycle_commands.snapshot(session_id).phase == "idle",
    )
    retry_finish = lifecycle_command_store.required_transition(
        session_id,
        f"user-turn:{lifecycle_id}:1:finish",
    )
    check(
        "retry has its own idempotent terminal command",
        retry_finish["notification_payload"]["outcome"] == "complete",
    )


async def prove_recovery_uses_execution_owner(
    coordinator: Coordinator,
) -> None:
    app_session_id = create_session("recovery-owner")
    persist_session_id = create_session("recovery-persist-target")
    lifecycle_id = "turn-recovery-owner"
    identity = UserTurnIdentity(
        user_turn_id=lifecycle_id,
        lifecycle_message_id=lifecycle_id,
    )
    await coordinator.lifecycle_commands.begin_turn(
        request_id="recovery-owner:begin",
        session_id=app_session_id,
        identity=identity,
    )
    original_terminal = (
        user_msg_lifecycle.terminal_event_for_lifecycle_async
    )

    async def existing_terminal(*_args, **_kwargs) -> dict:
        return {"type": "user_message_done"}

    user_msg_lifecycle.terminal_event_for_lifecycle_async = existing_terminal
    try:
        await run_recovery._emit_recovered_user_message_terminal(
            coordinator=coordinator,
            app_session_id=app_session_id,
            persist_sid=persist_session_id,
            mode="native",
            agent_sid=None,
            run_id="recovery-owner-run",
            cancelled=True,
            sess={
                "messages": [
                    {
                        "id": "recovery-user",
                        "role": "user",
                        "lifecycle_msg_id": lifecycle_id,
                    },
                    {
                        "id": "recovery-assistant",
                        "role": "assistant",
                    },
                ],
            },
            assistant_msg={
                "id": "recovery-assistant",
                "role": "assistant",
            },
        )
    finally:
        user_msg_lifecycle.terminal_event_for_lifecycle_async = original_terminal
    check(
        "terminal recovery finishes the execution owner lifecycle",
        coordinator.lifecycle_commands.snapshot(app_session_id).phase == "idle",
    )
    check(
        "terminal recovery does not mutate the persistence target lifecycle",
        coordinator.lifecycle_commands.snapshot(persist_session_id).phase == "idle",
    )


async def run() -> None:
    coordinator = Coordinator()
    await coordinator.lifecycle_commands.bind()
    try:
        await prove_serialized_turns(coordinator)
        await prove_running_stop_transition(coordinator)
        await prove_cancel_before_execution(coordinator)
        await prove_retry_reclaims_identity(coordinator)
        await prove_recovery_uses_execution_owner(coordinator)
    finally:
        await coordinator.quiesce_prompt_processors()
        await coordinator.lifecycle_commands.close()


def main() -> int:
    try:
        asyncio.run(run())
        print("ok")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
