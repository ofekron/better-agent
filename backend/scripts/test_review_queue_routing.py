"""Supervisor review queue-routing regression test.

A `_review` item submitted via submit_prompt must be processed by the
per-session processor — serialized BEHIND any queued prompt — and
dispatched to orchs.supervisor.request_review instead of
handle_prompt.

Run with:
    cd backend && .venv/bin/python scripts/test_review_queue_routing.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_review_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import Coordinator  # noqa: E402
from turn_manager import TurnManager  # noqa: E402
import orchs.supervisor as supervisor_mod  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


class _UPM:
    @staticmethod
    def get_in_flight_lifecycle_msg_id(sid):
        return None

    @staticmethod
    def set_in_flight_lifecycle_msg_id(sid, mid):
        pass

    @staticmethod
    def clear_in_flight_lifecycle_msg_id(sid):
        pass


def main() -> int:
    c = Coordinator.__new__(Coordinator)
    c._prompt_queues = {}
    c._queued_ids = {}
    c._processor_tasks = {}
    c._in_flight_prompts = {}
    c._cancelled_ids = {}
    c._session_cancelled = {}
    c.user_prompt_manager = _UPM()
    c.turn_manager = TurnManager(c)
    order: list[str] = []

    async def dispatch_raw(sid, event):
        pass

    c.dispatch_raw = dispatch_raw

    async def handle_prompt(**params):
        order.append("prompt:" + params.get("prompt", ""))

    c.handle_prompt = handle_prompt

    async def fake_request_review(coord, *, app_session_id, ws_callback):
        order.append("review:" + app_session_id)

    real_review = supervisor_mod.request_review
    supervisor_mod.request_review = fake_request_review

    sid = "sid-review"

    async def _go() -> None:
        # Queue a normal prompt first, then the review — review must
        # run AFTER the prompt, never concurrently.
        c.submit_prompt(sid, {"prompt": "first", "app_session_id": sid})
        c.submit_prompt(sid, {"_review": True, "app_session_id": sid})
        for _ in range(40):
            if len(order) == 2:
                break
            await asyncio.sleep(0.25)

    try:
        asyncio.run(_go())
    finally:
        supervisor_mod.request_review = real_review

    print("review routed through the per-session queue")
    check("both items processed", len(order) == 2)
    check("prompt ran first", order and order[0] == "prompt:first")
    check("review ran second via request_review",
          len(order) == 2 and order[1] == "review:" + sid)
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
