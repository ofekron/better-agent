from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="ba-test-claude-activity-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import runner  # noqa: E402


class _TaskStarted:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _TaskTerminal:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_activity_snapshot_is_orthogonal_stable_and_monotonic() -> None:
    state_path = Path(_TMP_HOME) / "runs" / "run-1" / "state.json"
    state_path.parent.mkdir(parents=True)
    state = {
        "run_id": "run-1",
        "foreground_status": "running",
        "background_work_ids": [],
        "activity_revision": 1,
    }
    runner._atomic_write_json(state_path, state)

    tasks: set[str] = set()
    original_started = runner.TaskStartedMessage
    original_terminal = runner.TaskNotificationMessage
    runner.TaskStartedMessage = _TaskStarted
    runner.TaskNotificationMessage = _TaskTerminal
    try:
        assert runner._apply_task_message(_TaskStarted("task-b"), tasks)
        runner._persist_activity_state(
            state, state_path, background_work_ids=tasks,
        )
        assert runner._apply_task_message(_TaskStarted("task-a"), tasks)
        runner._persist_activity_state(
            state, state_path, background_work_ids=tasks,
        )
        assert not runner._apply_task_message(_TaskStarted("task-a"), tasks)
        assert state["activity_revision"] == 3
        assert _read(state_path)["background_work_ids"] == ["task-a", "task-b"]

        runner._persist_activity_state(
            state,
            state_path,
            foreground_status="completed",
            background_work_ids=tasks,
        )
        waiting = _read(state_path)
        assert waiting["foreground_status"] == "completed"
        assert waiting["background_work_ids"] == ["task-a", "task-b"]
        assert waiting["activity_revision"] == 4

        assert runner._apply_task_message(_TaskTerminal("task-a"), tasks)
        runner._persist_activity_state(
            state, state_path, background_work_ids=tasks,
        )
        assert runner._apply_task_message(_TaskTerminal("task-b"), tasks)
        runner._persist_activity_state(
            state, state_path, background_work_ids=tasks,
        )
        terminal = _read(state_path)
        assert terminal["foreground_status"] == "completed"
        assert terminal["background_work_ids"] == []
        assert terminal["activity_revision"] == 6
    finally:
        runner.TaskStartedMessage = original_started
        runner.TaskNotificationMessage = original_terminal


def test_failed_write_does_not_advance_memory_revision() -> None:
    state_path = Path(_TMP_HOME) / "runs" / "run-2" / "state.json"
    state_path.parent.mkdir(parents=True)
    state = {
        "foreground_status": "running",
        "background_work_ids": [],
        "activity_revision": 1,
    }
    original_write = runner._atomic_write_json

    def fail_write(_path: Path, _data: dict) -> None:
        raise OSError("disk unavailable")

    runner._atomic_write_json = fail_write
    try:
        try:
            runner._persist_activity_state(
                state, state_path, background_work_ids={"task-a"},
            )
        except OSError:
            pass
        else:
            raise AssertionError("failed activity write unexpectedly succeeded")
    finally:
        runner._atomic_write_json = original_write

    assert state["activity_revision"] == 1
    assert state["background_work_ids"] == []
    assert runner._persist_activity_state(
        state, state_path, background_work_ids={"task-a"},
    )
    assert _read(state_path)["activity_revision"] == 2


if __name__ == "__main__":
    try:
        test_activity_snapshot_is_orthogonal_stable_and_monotonic()
        test_failed_write_does_not_advance_memory_revision()
        print("PASS Claude runner durable activity state")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
