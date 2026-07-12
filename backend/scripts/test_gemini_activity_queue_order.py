#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="gemini-activity-order-")
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from provider_gemini import GeminiProvider, RunState


class _Popen:
    pid = 123
    returncode = 0

    def poll(self):
        return self.returncode


async def test_terminal_activity_precedes_complete() -> None:
    run_dir = Path(HOME) / "terminal"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "activity_revision": 2,
        "foreground_status": "completed",
        "background_work_ids": [],
        "turn_id": "turn-1",
    }), encoding="utf-8")
    complete_path = run_dir / "complete.json"
    complete_path.write_text(json.dumps({
        "success": True,
        "session_id": "gemini-session",
        "error": None,
        "token_usage": None,
    }), encoding="utf-8")
    queue: asyncio.Queue = asyncio.Queue()
    state = RunState(
        run_id="run-1",
        run_dir=run_dir,
        popen=_Popen(),
        mode="native",
        app_session_id="app-1",
        queue=queue,
    )
    provider = GeminiProvider.__new__(GeminiProvider)

    await provider._emit_complete_from_file(state, complete_path)

    activity = queue.get_nowait()
    complete = queue.get_nowait()
    assert activity.type == "activity_state"
    assert activity.data == {
        "activity_revision": 2,
        "foreground_status": "completed",
        "background_work_ids": [],
        "turn_id": "turn-1",
    }
    assert complete.type == "complete"
    assert complete.data["success"] is True
    assert queue.empty()


async def test_stale_running_snapshot_is_not_terminalized() -> None:
    run_dir = Path(HOME) / "stale"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "activity_revision": 1,
        "foreground_status": "running",
        "background_work_ids": [],
    }), encoding="utf-8")
    queue: asyncio.Queue = asyncio.Queue()
    state = RunState(
        run_id="run-2",
        run_dir=run_dir,
        popen=_Popen(),
        mode="native",
        app_session_id="app-1",
        queue=queue,
    )
    provider = GeminiProvider.__new__(GeminiProvider)

    await provider._emit_complete_from_file(state, run_dir / "missing-complete.json")

    complete = queue.get_nowait()
    assert complete.type == "complete"
    assert complete.data["success"] is False
    assert queue.empty()


async def main_async() -> None:
    await test_terminal_activity_precedes_complete()
    await test_stale_running_snapshot_is_not_terminalized()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print("PASS Gemini activity queue order")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
