from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="ba-test-claude-foreground-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from provider_claude import ClaudeProvider, RunState  # noqa: E402


class _Popen:
    pid = os.getpid()

    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode


class _Tailer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Provider(ClaudeProvider):
    def __init__(self) -> None:
        self._runs = {}

    def _cleanup_run(self, run_id: str) -> None:
        rs = self._runs.pop(run_id, None)
        if rs is not None:
            rs.released.set()


async def test_activity_precedes_complete_and_cleanup_waits_for_exit() -> None:
    run_dir = Path(_TMP_HOME) / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "activity_revision": 3,
        "foreground_status": "completed",
        "background_work_ids": ["task:still-live"],
        "turn_id": "run-1",
    }), encoding="utf-8")
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True,
        "session_id": "claude-session",
        "error": None,
        "token_usage": None,
    }), encoding="utf-8")

    provider = _Provider()
    popen = _Popen()
    tailer = _Tailer()
    queue: asyncio.Queue = asyncio.Queue()
    rs = RunState(
        run_id="run-1",
        run_dir=run_dir,
        popen=popen,
        mode="native",
        app_session_id="app-1",
        queue=queue,
        session_id="claude-session",
        tailer=tailer,
    )
    provider._runs[rs.run_id] = rs

    await asyncio.wait_for(provider._watch_complete(rs), timeout=2)
    activity = queue.get_nowait()
    complete = queue.get_nowait()
    assert activity.type == "activity_state"
    assert activity.data == {
        "activity_revision": 3,
        "foreground_status": "completed",
        "background_work_ids": ["task:still-live"],
        "turn_id": "run-1",
    }
    assert complete.type == "complete"
    assert complete.data["success"] is True
    assert provider._runs[rs.run_id] is rs
    assert not rs.released.is_set()
    assert not tailer.stopped

    popen.returncode = 0
    await asyncio.wait_for(rs.released.wait(), timeout=2)
    assert rs.run_id not in provider._runs
    assert tailer.stopped
    assert queue.empty()


if __name__ == "__main__":
    try:
        asyncio.run(test_activity_precedes_complete_and_cleanup_waits_for_exit())
        print("PASS Claude foreground completion ordering")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
