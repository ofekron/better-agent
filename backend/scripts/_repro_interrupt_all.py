#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _test_home
_test_home.isolate("bc-repro-interrupt-all-")

import orchestrator  # noqa: E402
from orchestrator import Coordinator  # noqa: E402


def _new_coord() -> Coordinator:
    coord = Coordinator.__new__(Coordinator)
    coord._active_prompt_client_ids = {}
    coord._prompt_client_id_by_item = {}
    coord._queued_ids = {}
    coord._queued_edit_events = {}
    coord._claimed_queued_ids = {}
    coord._cancelled_ids = {}
    coord._in_flight_prompts = {}
    coord._processor_tasks = {}
    coord._session_cancelled = {}
    return coord


class _SessionManager:
    def __init__(self) -> None:
        self.removed = []
        self.sessions = {"sid": {"messages": [], "queued_prompts": []}}

    def remove_queued_prompt(self, sid, queued_id):
        self.removed.append((sid, queued_id))

    def get(self, sid):
        return self.sessions.get(sid)

    def get_lite(self, sid):
        return self.sessions.get(sid)


async def main() -> None:
    coord = _new_coord()
    sid = "sid"
    coord._prompt_queues = {sid: asyncio.Queue()}
    coord._queued_ids = {sid: ["active", "q1", "q2", "q3"]}

    # An already-running turn occupies the processor first.
    await coord._prompt_queues[sid].put({
        "_queued_id": "active",
        "prompt": "ACTIVE",
        "app_session_id": sid,
        "model": "m",
        "cwd": "/repo",
        "lifecycle_msg_id": "life-active",
    })
    for qid, txt in [("q1", "first"), ("q2", "second"), ("q3", "third")]:
        await coord._prompt_queues[sid].put({
            "_queued_id": qid,
            "prompt": txt,
            "app_session_id": sid,
            "model": "m",
            "cwd": "/repo",
            "lifecycle_msg_id": f"life-{qid}",
        })

    handled = []
    active_started = asyncio.Event()
    active_unblock = asyncio.Event()

    class _TM:
        _pending_cancel = {}

        async def wait_for_clear_runs(self, _sid):
            pass

    class _UPM:
        def set_in_flight_lifecycle_msg_id(self, *a):
            pass

        def clear_in_flight_lifecycle_msg_id(self, *a):
            pass

        def _clear_sent(self, *a):
            pass

        def pop_done_payload(self, *a):
            return None

        async def emit_user_msg_done(self, *a, **k):
            pass

        async def emit_user_msg_failed(self, *a, **k):
            pass

    async def cancel_turn(_sid, interrupted_by_msg_id=None):
        # Real cancel unblocks the active turn so the processor advances.
        active_unblock.set()
        return True

    async def dispatch_raw(_sid, event):
        pass

    async def handle_prompt(**kwargs):
        prompt = kwargs.get("prompt")
        handled.append(prompt)
        if prompt == "ACTIVE":
            active_started.set()
            await active_unblock.wait()
        else:
            await asyncio.sleep(0)

    fake_sm = _SessionManager()
    orchestrator.session_manager = fake_sm  # type: ignore
    coord.turn_manager = _TM()
    coord.user_prompt_manager = _UPM()
    coord.cancel_turn = cancel_turn  # type: ignore
    coord.dispatch_raw = dispatch_raw  # type: ignore
    coord.handle_prompt = handle_prompt  # type: ignore

    # Start the processor; let the ACTIVE turn begin and block.
    task = asyncio.create_task(coord._run_session_processor(sid))
    await asyncio.wait_for(active_started.wait(), timeout=1)
    print("active turn running; queue size:", coord._prompt_queues[sid].qsize())

    ok = await coord.promote_queued(sid, action="interrupt", queued_ids=["q1", "q2", "q3"])
    print("promote returned:", ok)
    print("queue size after promote:", coord._prompt_queues[sid].qsize())

    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print("handled prompts (in order):", handled)
    print("count:", len(handled))


if __name__ == "__main__":
    asyncio.run(main())
