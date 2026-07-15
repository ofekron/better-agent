"""BFF raw-event consumption contract.

Locks that `CurrentTurnFeed` turns runtime's ordered `raw_event` frames
into `current_turn_cache` state:

  A. content frames accumulate + render into the current turn (via the
     shared funnel), keyed by the session's prompt id.
  B. a `turn_complete`/`turn_stopped`/`turn_detached` frame settles the
     entry (durable projection becomes authoritative).
  C. `turn_start` resets accumulation so a new turn does not inherit the
     previous turn's rows.
  D. out-of-turn safety: a frame for a session with no user prompt is a
     no-op, not a crash.

Run with:
    cd backend && .venv/bin/python scripts/test_bff_current_turn_feed.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-current-turn-feed-")

from bff_current_turn_cache import current_turn_cache  # noqa: E402
from bff_current_turn_feed import CurrentTurnFeed  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def _session(root_id: str) -> dict:
    return {
        "id": root_id,
        "provider_id": "claude",
        "model": "sonnet",
        "reasoning_effort": "high",
        "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "do it"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "",
             "run_meta": {"provider_id": "claude", "model": "sonnet",
                          "reasoning_effort": "high"}},
        ],
    }


def _raw_frame(root_id: str, etype: str, seq: int, *, text: str = "",
               final: bool = False) -> dict:
    return {
        "type": "raw_event",
        "root_id": root_id,
        "event_type": etype,
        "seq": seq,
        "sid": root_id,
        "source": "claude",
        "msg_id": "a1",
        "data": {
            "uuid": f"e{seq}",
            "type": "assistant",
            "final_answer": final,
            "message": {"content": [{"type": "text", "text": text}]},
        } if etype == "agent_message" else {"session_id": root_id},
    }


def _turn_result_text(items) -> str | None:
    if not items:
        return None
    turn = next((item for item in items if item["type"] == "Turn"), None)
    if turn is None or turn["id"] != "u1" or turn.get("result") is None:
        return None
    return turn["result"]["text"]


async def _wait_until(pred, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def test_content_accumulates_and_settles() -> None:
    async def scenario() -> None:
        root_id = "feed-root"
        session = _session(root_id)

        async def reader(rid: str):
            check("session reader called with the frame's root", rid == root_id)
            return session

        feed = CurrentTurnFeed(session_reader=reader)
        feed.start()
        try:
            feed.submit(_raw_frame(root_id, "agent_message", 1, text="hi"))
            ok = await _wait_until(
                lambda: _turn_result_text(current_turn_cache.get(root_id, "u1")) == "hi"
            )
            check("first content frame renders into the current turn", ok)

            # Streaming delta: cumulative text, last-write-wins on same uuid.
            feed.submit(_raw_frame(root_id, "agent_message", 1, text="hi there", final=True))
            ok = await _wait_until(
                lambda: _turn_result_text(current_turn_cache.get(root_id, "u1")) == "hi there"
            )
            check("streaming delta updates the rendered turn", ok)

            feed.submit(_raw_frame(root_id, "turn_complete", 2))
            ok = await _wait_until(
                lambda: current_turn_cache.get(root_id, "u1") is None
            )
            check("turn_complete settles (drops) the current-turn entry", ok)
        finally:
            await feed.stop()

    asyncio.run(scenario())


def test_turn_start_resets_accumulation() -> None:
    async def scenario() -> None:
        root_id = "reset-root"
        session = _session(root_id)
        fetches = {"n": 0}

        async def reader(rid: str):
            fetches["n"] += 1
            return session

        feed = CurrentTurnFeed(session_reader=reader)
        feed.start()
        try:
            feed.submit(_raw_frame(root_id, "agent_message", 1, text="old"))
            await _wait_until(
                lambda: current_turn_cache.get(root_id, "u1") is not None
            )
            feed.submit(_raw_frame(root_id, "turn_start", 2))
            feed.submit(_raw_frame(root_id, "agent_message", 3, text="new", final=True))
            ok = await _wait_until(
                lambda: _turn_result_text(current_turn_cache.get(root_id, "u1")) == "new"
            )
            check("turn_start reset drops prior rows; new turn renders fresh", ok)
            check("session refetched after reset (once per turn)", fetches["n"] == 2)
            current_turn_cache.settle(root_id, "u1")
        finally:
            await feed.stop()

    asyncio.run(scenario())


def test_no_prompt_is_noop() -> None:
    async def scenario() -> None:
        root_id = "no-prompt-root"

        async def reader(rid: str):
            return {"id": rid, "provider_id": "claude", "messages": []}

        feed = CurrentTurnFeed(session_reader=reader)
        feed.start()
        try:
            feed.submit(_raw_frame(root_id, "agent_message", 1, text="x"))
            await asyncio.sleep(0.1)
            check("promptless session yields no cache entry",
                  current_turn_cache.get(root_id, "u1") is None)
        finally:
            await feed.stop()

    asyncio.run(scenario())


if __name__ == "__main__":
    try:
        test_content_accumulates_and_settles()
        test_turn_start_resets_accumulation()
        test_no_prompt_is_noop()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all current-turn feed tests passed")
