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


def test_worker_frame_refetches_the_session_snapshot() -> None:
    """A worker_start/worker_event/worker_complete frame must refetch the
    session scaffold even though one is already cached — the runtime
    mutates `message.workers` out of band from this cache's row
    accumulation, so a stale cached scaffold would render the panel
    without its worker_description (or omit it entirely) for the rest of
    the turn. Non-worker frames (agent_message) must NOT trigger a
    refetch — the cache-once optimization stays intact for pure text.

    Regression test for a gap found driving a real worker delegation
    through the live BFF: the assistant snapshot's `workers` array never
    appeared in the published deltas because the session was fetched
    once at the first content frame and never refreshed."""
    async def scenario() -> None:
        root_id = "worker-refresh-root"
        # The reader simulates the runtime's session mutating between
        # calls: no `workers` on the first read (before worker_start),
        # populated on the second (after).
        state = {"n": 0}
        published: list[tuple[str, object]] = []

        def session_with_workers(has_workers: bool) -> dict:
            session = _session(root_id)
            if has_workers:
                session["messages"][1]["workers"] = [
                    {"delegation_id": "d1", "worker_description": "Researcher", "events": []},
                ]
            return session

        async def reader(rid: str):
            state["n"] += 1
            return session_with_workers(has_workers=state["n"] >= 2)

        async def publisher(rid: str, turn_id: str, phase: str, delta) -> None:
            published.append((phase, delta))

        feed = CurrentTurnFeed(session_reader=reader, delta_publisher=publisher)
        feed.start()
        try:
            feed.submit(_raw_frame(root_id, "agent_message", 1, text="hi"))
            ok = await _wait_until(lambda: len(published) == 1)
            check("first content frame renders and publishes", ok)
            check("first frame fetched the session once", state["n"] == 1)

            feed.submit(_raw_frame(root_id, "agent_message", 2, text="hi there"))
            await _wait_until(lambda: len(published) == 2)
            check("a second non-worker frame does NOT refetch (cache-once holds)",
                  state["n"] == 1)

            feed.submit({
                "type": "raw_event", "root_id": root_id, "event_type": "worker_start",
                "seq": 3, "sid": root_id, "source": "claude", "msg_id": "a1",
                "data": {"delegation_id": "d1"},
            })
            ok = await _wait_until(lambda: len(published) == 3)
            check("worker_start triggers a render + publish", ok)
            check("worker_start triggers a session refetch", state["n"] == 2)

            _phase, delta = published[-1]
            snapshot = delta.lookup.get("a1", {}).get("snapshot") or {}
            check("post-refetch delta's lookup carries the worker panel",
                  bool(snapshot.get("workers")))
            current_turn_cache.settle(root_id, "u1")
        finally:
            await feed.stop()

    asyncio.run(scenario())


def test_delta_publisher_receives_streaming_then_settled_phase() -> None:
    """Locks the live WS delta contract `bff_server.py` will push to the
    browser: every content frame publishes phase="streaming" with the
    turn's rendered items+lookup, and the settling frame publishes the
    final snapshot under its mapped terminal phase before the cache
    entry is dropped."""
    async def scenario() -> None:
        root_id = "publish-root"
        session = _session(root_id)
        published: list[tuple[str, str, str, object]] = []

        async def reader(rid: str):
            return session

        async def publisher(rid: str, turn_id: str, phase: str, delta) -> None:
            published.append((rid, turn_id, phase, delta))

        feed = CurrentTurnFeed(session_reader=reader, delta_publisher=publisher)
        feed.start()
        try:
            feed.submit(_raw_frame(root_id, "agent_message", 1, text="hi", final=True))
            ok = await _wait_until(lambda: len(published) == 1)
            check("first content frame publishes one delta", ok)
            if ok:
                rid, turn_id, phase, delta = published[0]
                check("published delta targets the frame's root", rid == root_id)
                check("published delta targets the resolved turn", turn_id == "u1")
                check("in-flight delta phase is streaming", phase == "streaming")
                check("in-flight delta carries the rendered turn text",
                      _turn_result_text(delta.items) == "hi")
                check("in-flight delta carries a lookup sidecar",
                      isinstance(delta.lookup, dict) and len(delta.lookup) > 0)

            feed.submit(_raw_frame(root_id, "turn_stopped", 2))
            ok = await _wait_until(lambda: len(published) == 2)
            check("settle publishes one final delta", ok)
            if ok:
                _rid, _turn_id, phase, delta = published[1]
                check("settle delta phase maps turn_stopped -> stopped", phase == "stopped")
                check("settle delta still carries the last rendered content",
                      _turn_result_text(delta.items) == "hi")
            check("cache entry is dropped after settle publish",
                  current_turn_cache.get(root_id, "u1") is None)
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
        test_worker_frame_refetches_the_session_snapshot()
        test_delta_publisher_receives_streaming_then_settled_phase()
        test_no_prompt_is_noop()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all current-turn feed tests passed")
