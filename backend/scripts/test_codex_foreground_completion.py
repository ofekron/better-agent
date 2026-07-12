from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from provider_codex import CodexProvider, RunState


class _Popen:
    pid = 123

    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode


class _Normalizer:
    context_window = None
    context_tokens = None


class _Tailer:
    normalizer = _Normalizer()

    def __init__(self) -> None:
        self.drains = 0
        self.stopped = False

    async def drain_available(self) -> None:
        self.drains += 1

    def stop(self) -> None:
        self.stopped = True


class _Provider(CodexProvider):
    def __init__(self) -> None:
        self.cleaned = False

    def _cleanup_run(self, _run_id: str) -> None:
        self.cleaned = True

    async def _flush_backend_state_async(self, _rs: RunState) -> None:
        return None


async def test_foreground_complete_precedes_background_cleanup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        state = {
            "activity_revision": 2,
            "foreground_status": "completed",
            "background_work_ids": ["child:still-live"],
            "turn_id": None,
        }
        foreground = {
            "success": True,
            "session_id": "root-thread",
            "error": None,
            "token_usage": {"total_tokens": 9},
            "finished_at": "now",
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (run_dir / "foreground_complete.json").write_text(
            json.dumps(foreground), encoding="utf-8",
        )

        queue: asyncio.Queue = asyncio.Queue()
        popen = _Popen()
        tailer = _Tailer()
        rs = RunState(
            run_id="run-1",
            run_dir=run_dir,
            popen=popen,
            mode="native",
            app_session_id="app-1",
            queue=queue,
            session_id="root-thread",
            tailer=tailer,
        )
        rs.root_terminal_event.set()
        provider = _Provider()
        watcher = asyncio.create_task(provider._watch_complete(rs))

        activity = await asyncio.wait_for(queue.get(), timeout=1)
        complete = await asyncio.wait_for(queue.get(), timeout=1)
        assert activity.type == "activity_state"
        assert activity.data["background_work_ids"] == ["child:still-live"]
        assert complete.type == "complete"
        assert complete.data["success"] is True
        assert not watcher.done()
        assert not provider.cleaned
        assert not tailer.stopped

        (run_dir / "complete.json").write_text(
            json.dumps(foreground), encoding="utf-8",
        )
        popen.returncode = 0
        await asyncio.wait_for(watcher, timeout=2)
        assert provider.cleaned
        assert tailer.stopped
        assert queue.empty()


if __name__ == "__main__":
    asyncio.run(test_foreground_complete_precedes_background_cleanup())
    print("PASS Codex foreground completion")
