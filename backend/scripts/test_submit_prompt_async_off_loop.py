import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_submit_async_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator  # noqa: E402
from orchestrator import Coordinator  # noqa: E402


def _coord() -> Coordinator:
    c = Coordinator.__new__(Coordinator)
    c._prompt_queues = {}
    c._queued_ids = {}
    c._processor_tasks = {}
    return c


async def _run_session_processor(_sid: str) -> None:
    await asyncio.Event().wait()


class _SlowSessionManager:
    def get(self, _sid: str):
        time.sleep(0.35)
        return None


async def _heartbeat(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        ticks += 1
        await asyncio.sleep(0.05)
    return ticks


async def _test_submit_prompt_async_keeps_loop_responsive() -> None:
    c = _coord()
    c._run_session_processor = _run_session_processor
    original_session_manager = orchestrator.session_manager
    orchestrator.session_manager = _SlowSessionManager()
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop))
    try:
        item_id = await c.submit_prompt_async("sid", {"prompt": "hi"})
        stop.set()
        ticks = await beat
        assert item_id
        assert ticks >= 3, f"event loop blocked; heartbeat ticks={ticks}"
    finally:
        orchestrator.session_manager = original_session_manager
        stop.set()
        if not beat.done():
            beat.cancel()
        for task in c._processor_tasks.values():
            task.cancel()
        await asyncio.gather(*c._processor_tasks.values(), return_exceptions=True)


def main() -> int:
    try:
        asyncio.run(_test_submit_prompt_async_keeps_loop_responsive())
        print("PASS: submit_prompt_async keeps event loop responsive")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
