"""Regression tests: the per-turn runner must not kill a live Workflow.

The Claude Code CLI runs the internal Workflow tool as an in-process
background task (`local_workflow`): the launching turn's ResultMessage
arrives while the workflow is still running, and on completion the CLI
auto-starts the task-notification turn on the SAME stream (init →
assistant → result). Pre-fix, `runner._run_one_turn` returned at the
FIRST ResultMessage and the runner disconnected — killing the CLI and
the workflow mid-flight, losing the notification turn every time.

Post-fix the runner drains until idle: at a ResultMessage with live
background tasks it keeps consuming (fresh `receive_response()` over the
same stream) and finalizes only when a ResultMessage arrives with no
live task. A cancel during the idle drain must tear down promptly
instead of waiting for a frame that will never come.

Run with:
    cd backend && .venv/bin/python scripts/test_runner_workflow_drain.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-wf-drain-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

import runner  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def _ok(cond: bool, label: str, detail: str = "") -> None:
    global failures
    if cond:
        print(f"{PASS}  {label}")
    else:
        print(f"{FAIL}  {label}  {detail}")
        failures += 1


def _sys_init(sid: str) -> SystemMessage:
    return SystemMessage(subtype="init", data={"subtype": "init", "session_id": sid})


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[{"type": "text", "text": text}], model="test")


def _tool_use(name: str) -> AssistantMessage:
    return AssistantMessage(
        content=[{"type": "tool_use", "id": "tu_1", "name": name, "input": {}}],
        model="test",
    )


def _result(*, is_error: bool = False, subtype: str = "success") -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sid",
        result=subtype,
    )


def _task_started(tid: str) -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={"subtype": "task_started", "task_id": tid},
        task_id=tid,
        description="wf",
        uuid="u-" + tid,
        session_id="sid",
        task_type="local_workflow",
    )


def _task_notification(tid: str, status: str = "completed") -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={"subtype": "task_notification", "task_id": tid},
        task_id=tid,
        status=status,
        output_file="/dev/null",
        summary="wf done",
        uuid="un-" + tid,
        session_id="sid",
    )


def _task_updated(tid: str, status: str) -> TaskUpdatedMessage:
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={"subtype": "task_updated", "task_id": tid},
        task_id=tid,
        patch={"status": status},
        status=status,
    )


def _roster(*tids: str) -> SystemMessage:
    return SystemMessage(
        subtype="background_tasks_changed",
        data={
            "subtype": "background_tasks_changed",
            "tasks": [{"task_id": t, "task_type": "local_workflow"} for t in tids],
        },
    )


class FakeClient:
    """Models the CLI's SINGLE output stream as one shared queue.

    `receive_response()` mirrors the SDK exactly: yields until (and
    including) a ResultMessage, then terminates — a fresh call continues
    from the same stream, which is what the runner's drain relies on."""

    def __init__(self, frames: list) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        self._frames = frames
        self.interrupted = False

    async def query(self, prompt) -> None:
        for m in self._frames:
            await self._q.put(m)

    async def interrupt(self) -> None:
        # Models a cancel landing while NO model turn is in flight (idle
        # workflow drain): the CLI has nothing to wind down, no frames
        # follow.
        self.interrupted = True

    async def receive_response(self):
        while True:
            msg = await self._q.get()
            yield msg
            if isinstance(msg, ResultMessage):
                return


async def _drive_turn(client, run_dir, turn_id, *, trigger_cancel=False):
    log = logging.getLogger("test")
    cancel_path = run_dir / f"cancel-{turn_id}"

    async def _fire():
        await asyncio.sleep(0.3)
        cancel_path.write_text("1")

    fire_task = asyncio.create_task(_fire()) if trigger_cancel else None
    result = await runner._run_one_turn(
        client=client,
        prompt="p",
        images=[],
        files=[],
        run_dir=run_dir,
        turn_id=turn_id,
        pre_query_byte_offset=0,
        state={},
        state_path=run_dir / "state.json",
        cwd="/tmp/x",
        claude_config_dir=run_dir / "cfg",
        log=log,
        cancel_path=cancel_path,
    )
    if fire_task:
        await fire_task
    return result


def test_apply_task_message_terminal_vocabularies() -> None:
    start = failures
    tasks: set[str] = set()
    runner._apply_task_message(_task_started("w1"), tasks)
    _ok(tasks == {"w1"}, "task_started adds the task")

    runner._apply_task_message(_task_updated("w1", "running"), tasks)
    _ok(tasks == {"w1"}, "non-terminal task_updated keeps the task")

    runner._apply_task_message(_task_updated("w1", "killed"), tasks)
    _ok(tasks == set(),
        "terminal task_updated (killed, notification suppressed) clears")

    runner._apply_task_message(_task_started("w2"), tasks)
    runner._apply_task_message(_task_notification("w2", "completed"), tasks)
    _ok(tasks == set(), "task_notification clears")

    runner._apply_task_message(_task_started("w3"), tasks)
    runner._apply_task_message(_roster("w4"), tasks)
    _ok(tasks == {"w4"},
        "background_tasks_changed roster is authoritative (replaces set)")
    runner._apply_task_message(_roster(), tasks)
    _ok(tasks == set(), "empty roster clears the set")
    assert failures == start


async def _drains_workflow_to_notification_turn() -> None:
    """THE regression: launching turn's result arrives with the workflow
    still live; the CLI's auto notification turn follows on the same
    stream. Pre-fix the runner returned at the first result (final text
    'WAITING', stream abandoned); post-fix it drains to the notification
    turn's result."""
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    client = FakeClient([
        # Turn 1 — model launches the workflow, says WAITING, turn ends.
        _sys_init("sid"),
        _tool_use("Workflow"),
        _task_started("wf_1"),
        _assistant("WAITING"),
        _result(),
        # Workflow completes: lifecycle frames, then the CLI auto-starts
        # the task-notification turn on the same stream.
        _roster(),
        _task_updated("wf_1", "completed"),
        _task_notification("wf_1"),
        _sys_init("sid"),
        _assistant("workflow done"),
        _result(),
    ])
    r = await asyncio.wait_for(
        _drive_turn(client, run_dir, "turn1"), timeout=10.0,
    )
    _ok(r.get("final_assistant_text") == "workflow done",
        "turn finalizes on the notification turn's text",
        f"r={r}")
    _ok(r.get("final_success") is True, "turn succeeds", f"r={r}")
    _ok(client._q.empty(),
        "stream fully drained (no abandoned notification turn)",
        f"qsize={client._q.qsize()}")
    complete = json.loads(
        (run_dir / "turns" / "turn1" / "complete.json").read_text()
    )
    _ok(complete.get("final_assistant_text") == "workflow done",
        "durable completion carries the workflow outcome",
        f"complete={complete}")


async def _no_live_tasks_no_drain() -> None:
    """Baseline: a workflow that already reached terminal state before
    the result (fast workflow) must not delay finalization."""
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    client = FakeClient([
        _sys_init("sid"),
        _tool_use("Workflow"),
        _task_started("wf_1"),
        _roster(),
        _task_notification("wf_1"),
        _assistant("done inline"),
        _result(),
    ])
    r = await asyncio.wait_for(
        _drive_turn(client, run_dir, "turn1"), timeout=10.0,
    )
    _ok(r.get("final_assistant_text") == "done inline",
        "finalizes at the first result when no task is live", f"r={r}")
    _ok(r.get("final_success") is True, "turn succeeds", f"r={r}")


async def _cancel_during_idle_drain() -> None:
    """A stop during the drain (workflow hung, no frames arriving) must
    tear down promptly — event-driven, not waiting on the next frame."""
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    client = FakeClient([
        _sys_init("sid"),
        _tool_use("Workflow"),
        _task_started("wf_1"),
        _assistant("WAITING"),
        _result(),
        # Nothing follows: the workflow never completes.
    ])
    r = await asyncio.wait_for(
        _drive_turn(client, run_dir, "turn1", trigger_cancel=True),
        timeout=10.0,
    )
    _ok(r.get("cancelled") is True, "drain cancel ends the turn cancelled",
        f"r={r}")
    _ok(client.interrupted, "cancel watcher fired interrupt()")


def test_drains_workflow_to_notification_turn() -> None:
    start = failures
    asyncio.run(_drains_workflow_to_notification_turn())
    assert failures == start


def test_no_live_tasks_no_drain() -> None:
    start = failures
    asyncio.run(_no_live_tasks_no_drain())
    assert failures == start


def test_cancel_during_idle_drain() -> None:
    start = failures
    asyncio.run(_cancel_during_idle_drain())
    assert failures == start


def main() -> None:
    test_apply_task_message_terminal_vocabularies()
    test_drains_workflow_to_notification_turn()
    test_no_live_tasks_no_drain()
    test_cancel_during_idle_drain()

    print()
    if failures:
        print(f"{FAIL}  {failures} assertion(s) failed")
        sys.exit(1)
    print(f"{PASS}  all assertions passed")


if __name__ == "__main__":
    main()
