from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from provider import StreamEvent
from provider_claude import ClaudeProvider, RunState


class _Popen:
    pid = 1

    def poll(self):
        return None


async def _test_late_flush_runs_off_loop() -> None:
    provider = object.__new__(ClaudeProvider)
    started = threading.Event()
    release = threading.Event()
    received: list[tuple] = []

    def _blocked_ingest(*args, **kwargs) -> None:
        received.append((args, kwargs))
        started.set()
        release.wait()

    provider._ingest_orphan_line = _blocked_ingest
    state = RunState(
        run_id="run",
        run_dir=Path("/tmp/run"),
        popen=_Popen(),
        mode="native",
        app_session_id="session",
        queue=asyncio.Queue(),
        persist_to="session",
        root_id="root",
        cwd="/workspace",
        turn_finalized=True,
    )

    task = asyncio.create_task(
        provider._dispatch_tailer_line(state, {"uuid": "event"}),
    )
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    heartbeat = 0
    for _ in range(4):
        await asyncio.sleep(0.025)
        heartbeat += 1
    assert heartbeat == 4
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=1)
    args, kwargs = received[0]
    assert args[:3] == ("session", "run", {"uuid": "event"})
    assert kwargs == {"mode": "native", "root_id": "root", "cwd": "/workspace"}
    assert state.queue.empty()


def main() -> int:
    asyncio.run(_test_late_flush_runs_off_loop())
    print("PASS: provider late flush runs off the event loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
