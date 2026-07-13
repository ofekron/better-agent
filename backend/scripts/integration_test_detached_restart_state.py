from __future__ import annotations

import asyncio
import os
import shutil
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-detached-restart-")

import startup_recovery_gate  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _session(name: str) -> str:
    return session_manager.create(
        name=name,
        model="sonnet",
        cwd="/tmp/detached-restart-state",
        orchestration_mode="native",
        source="cli",
    )["id"]


def _coordinator() -> Coordinator:
    coordinator = Coordinator()
    session_manager.bind_running_check(coordinator.is_running)
    session_manager.bind_monitoring_check(
        coordinator.turn_manager.monitoring_state,
    )
    return coordinator


def _register(
    coordinator: Coordinator,
    parent: str,
    target: str,
    lifecycle: str,
) -> None:
    coordinator.register_detached_mssg_background(
        sender_session_id=parent,
        target_session_id=target,
        lifecycle_msg_id=lifecycle,
    )


def _delegate_record(record_id: str, sender: str, lifecycle: str) -> dict:
    return {
        "id": record_id,
        "role": "user",
        "content": "detached restart test",
        "source": "delegate_task",
        "sender_session_id": sender,
        "lifecycle_msg_id": lifecycle,
    }


def _assert_waiting(coordinator: Coordinator, session_id: str) -> None:
    state = coordinator.turn_manager.monitoring_state(session_id)
    assert state == "waiting_on_background", (
        f"expected waiting_on_background for {session_id}, got {state}"
    )


def test_fresh_coordinator_restores_detached_parent_edge() -> None:
    parent = _session("restart-parent")
    target = _session("restart-target")
    first = _coordinator()
    _register(first, parent, target, "lifecycle-a")
    _assert_waiting(first, parent)

    restarted = _coordinator()
    _assert_waiting(restarted, parent)
    links = restarted.turn_manager.get_run_state(parent)
    assert [entry.get("delegation_id") for entry in links] == ["lifecycle-a"]


def test_recovery_stamps_only_active_lifecycle() -> None:
    parent = _session("exact-parent")
    target = _session("exact-target")
    first = _coordinator()
    _register(first, parent, target, "lifecycle-active")
    _register(first, parent, target, "lifecycle-queued")

    session_manager.append_user_msg(
        target,
        _delegate_record("active-user", parent, "lifecycle-active"),
    )
    session_manager.append_assistant_msg(
        target,
        {"id": "active-assistant", "role": "assistant", "content": ""},
    )
    session_manager.add_queued_prompt(
        target,
        _delegate_record("queued-user", parent, "lifecycle-queued"),
    )

    restarted = _coordinator()
    run = restarted.run_state_add(
        target,
        run_id="recovered-active-run",
        kind="native",
        target_message_id="active-assistant",
        lifecycle_msg_id="lifecycle-active",
    )
    assert run.get("detached_lifecycle_ids") == ["lifecycle-active"], run

    restarted.run_state_remove(target, "recovered-active-run")
    _assert_waiting(restarted, parent)
    remaining = restarted.turn_manager.get_run_state(parent)
    assert [entry.get("delegation_id") for entry in remaining] == [
        "lifecycle-queued",
    ]

    asyncio.run(restarted.turn_manager.cancel_turn_with_detached(parent))
    queued = (session_manager.get(target) or {}).get("queued_prompts") or []
    assert not queued, queued
    assert restarted.turn_manager.monitoring_state(parent) == "stopped"


def test_nested_detached_edges_survive_restart() -> None:
    root = _session("nested-root")
    middle = _session("nested-middle")
    leaf = _session("nested-leaf")
    first = _coordinator()
    _register(first, root, middle, "lifecycle-root-middle")
    _register(first, middle, leaf, "lifecycle-middle-leaf")

    restarted = _coordinator()
    _assert_waiting(restarted, root)
    _assert_waiting(restarted, middle)
    root_links = restarted.turn_manager.get_run_state(root)
    middle_links = restarted.turn_manager.get_run_state(middle)
    assert [entry.get("target_session_id") for entry in root_links] == [middle]
    assert [entry.get("target_session_id") for entry in middle_links] == [leaf]


def test_stop_waits_for_recovery_and_cancels_exact_work() -> None:
    parent = _session("gated-parent")
    active_target = _session("gated-active-target")
    queued_target = _session("gated-queued-target")
    first = _coordinator()
    _register(first, parent, active_target, "lifecycle-active")
    _register(first, parent, queued_target, "lifecycle-queued")
    session_manager.add_queued_prompt(
        queued_target,
        _delegate_record("queued-stop", parent, "lifecycle-queued"),
    )

    async def scenario() -> None:
        startup_recovery_gate.begin_recovery()
        restarted = _coordinator()
        fanout: list[str] = []
        restarted._cancel_turn_fanout = fanout.append
        restarted.turn_manager._schedule_recovered_cancel_escalation = (
            lambda _session_id, _run_id: None
        )

        stop = asyncio.create_task(
            restarted.turn_manager.cancel_turn_with_detached(parent),
        )
        await asyncio.sleep(0)
        assert not stop.done(), "Stop bypassed the startup recovery gate"
        assert fanout == []

        run = restarted.run_state_add(
            active_target,
            run_id="recovered-exact-run",
            kind="native",
            lifecycle_msg_id="lifecycle-active",
        )
        assert run.get("detached_lifecycle_ids") == ["lifecycle-active"], run
        startup_recovery_gate.mark_recovery_done()
        assert await asyncio.wait_for(stop, timeout=1.0) is True
        assert fanout == ["recovered-exact-run"], fanout
        queued = (
            (session_manager.get(queued_target) or {}).get("queued_prompts")
            or []
        )
        assert queued == [], queued

    try:
        asyncio.run(scenario())
    finally:
        startup_recovery_gate.reset_for_tests()


def main() -> int:
    tests = [
        test_fresh_coordinator_restores_detached_parent_edge,
        test_recovery_stamps_only_active_lifecycle,
        test_nested_detached_edges_survive_restart,
        test_stop_waits_for_recovery_and_cancels_exact_work,
    ]
    failures: list[str] = []
    try:
        for test in tests:
            try:
                test()
                print(f"{PASS} {test.__name__}")
            except Exception as exc:
                failures.append(f"{test.__name__}: {exc}")
                print(f"{FAIL} {test.__name__}: {exc}")
        if failures:
            print(f"{len(failures)} FAILED")
            return 1
        print("ALL PASSED")
        return 0
    finally:
        startup_recovery_gate.reset_for_tests()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
