#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="gemini-activity-")
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from runner_gemini import _run, _set_activity_snapshot


def test_semantic_changes_own_revision() -> None:
    state: dict = {}
    assert _set_activity_snapshot(
        state,
        foreground_status="running",
        background_work_ids=[],
    )
    assert state == {
        "activity_revision": 1,
        "foreground_status": "running",
        "background_work_ids": [],
    }
    assert not _set_activity_snapshot(
        state,
        foreground_status="running",
        background_work_ids=[],
    )
    assert state["activity_revision"] == 1
    assert _set_activity_snapshot(
        state,
        foreground_status="completed",
        background_work_ids=[],
    )
    assert state["activity_revision"] == 2


def test_background_ids_are_stable() -> None:
    state: dict = {}
    _set_activity_snapshot(
        state,
        foreground_status="running",
        background_work_ids=["task:b", "task:a", "task:b"],
    )
    assert state["background_work_ids"] == ["task:a", "task:b"]
    assert not _set_activity_snapshot(
        state,
        foreground_status="running",
        background_work_ids=["task:b", "task:a"],
    )
    assert state["activity_revision"] == 1


async def test_startup_failure_is_durable() -> None:
    run_dir = Path(HOME) / "startup-failure"
    run_dir.mkdir(parents=True)
    result = await _run(run_dir, {"app_session_id": "app", "turn_id": "turn-1"})
    assert result == 1
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["turn_id"] == "turn-1"
    assert state["activity_revision"] == 2
    assert state["foreground_status"] == "failed"
    assert state["background_work_ids"] == []
    assert state["complete"] is True


async def main_async() -> None:
    test_semantic_changes_own_revision()
    test_background_ids_are_stable()
    await test_startup_failure_is_durable()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print("PASS Gemini activity snapshot parity")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
