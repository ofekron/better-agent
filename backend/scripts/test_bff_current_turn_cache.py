"""Ephemeral current-turn render cache contract.

Locks the non-durable per-(root, turn) cache:

  A. update -> get round-trips the current turn's rendered chat items.
  B. settle drops the entry (durable projection becomes authoritative).
  C. rehydrate reconstructs the cache from events.jsonl after a restart
     (no update ever called), tail-reading past the settled boundary.
  D. the durable admit_canonical_fact path is unaffected by the shared
     render extraction (regression covered by test_bff_chat_tree).

Run with:
    cd backend && .venv/bin/python scripts/test_bff_current_turn_cache.py
"""
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-current-turn-")

from bff_current_turn_cache import CurrentTurnCache  # noqa: E402
from event_ingester import event_ingester  # noqa: E402

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


def _agent_row(seq: int, text: str, *, final: bool) -> dict:
    return {
        "seq": seq,
        "sid": "irrelevant",
        "type": "agent_message",
        "source": "claude",
        "msg_id": "a1",
        "data": {
            "uuid": f"e{seq}",
            "type": "assistant",
            "final_answer": final,
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _turn_result_text(items: list[dict] | None) -> str | None:
    if not items:
        return None
    turn = next((item for item in items if item["type"] == "Turn"), None)
    if turn is None or turn["id"] != "u1" or turn.get("result") is None:
        return None
    return turn["result"]["text"]


def test_update_get_roundtrip() -> None:
    cache = CurrentTurnCache()
    root_id = "update-root"
    session = _session(root_id)
    items = cache.update(root_id, "u1", [_agent_row(1, "hi there", final=True)], session)
    check("update returns the turn's rendered items", _turn_result_text(items) == "hi there")
    check("get round-trips the same items", cache.get(root_id, "u1") == items)

    # Last-write-wins: a newer streaming frame overwrites, no history.
    newer = cache.update(root_id, "u1", [_agent_row(1, "hi there, done", final=True)], session)
    check("update is last-write-wins", _turn_result_text(newer) == "hi there, done")
    check("get reflects the latest write", cache.get(root_id, "u1") == newer)
    check("miss for an unknown turn is None", cache.get(root_id, "nope") is None)


def test_settle_clears_entry() -> None:
    cache = CurrentTurnCache()
    root_id = "settle-root"
    session = _session(root_id)
    cache.update(root_id, "u1", [_agent_row(1, "answer", final=True)], session)
    check("entry present before settle", cache.get(root_id, "u1") is not None)
    cache.settle(root_id, "u1")
    check("settle drops the entry", cache.get(root_id, "u1") is None)


def test_rehydrate_reconstructs_from_jsonl() -> None:
    """Simulated restart: a fresh cache never saw update(); rehydrate must
    rebuild the turn purely from the events.jsonl tail."""
    root_id = "rehydrate-root"
    session = _session(root_id)
    seq = event_ingester.ingest(
        root_id, sid=root_id, event_type="agent_message",
        data={
            "uuid": "r1", "type": "assistant", "final_answer": True,
            "message": {"content": [{"type": "text", "text": "recovered"}]},
        },
        source="claude", msg_id="a1",
    )
    check("event landed on the journal", seq == 1)

    cache = CurrentTurnCache()  # cold: no update() ever called
    check("cold cache is empty", cache.get(root_id, "u1") is None)
    items = cache.rehydrate(root_id, "u1", session)
    check("rehydrate returns the reconstructed turn", _turn_result_text(items) == "recovered")
    check("rehydrate populates the cache", _turn_result_text(cache.get(root_id, "u1")) == "recovered")


if __name__ == "__main__":
    try:
        test_update_get_roundtrip()
        test_settle_clears_entry()
        test_rehydrate_reconstructs_from_jsonl()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all current-turn cache tests passed")
