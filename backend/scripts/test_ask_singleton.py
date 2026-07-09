"""Regression tests for the stable Ask session.

Pure-Python unit-style assertions (no real claude — that's covered by the
slower `integration_test_session_search.py`). Runs in <1s.

Pins:
  1. `ensure_ask_session()` is idempotent under concurrent
     `asyncio.gather` — exactly one record is created, all callers observe
     the same id.
  2. The Ask session's `working_mode` mark hides it from `_build_index` —
     it never appears in its own search results.
  3. Rehydration: the per-turn `ask_result` (stamped by `propose_sessions`)
     and `chosen_session_id` (the user's pick) on the producing assistant
     message survive a re-read of the authoritative session record. This is
     the picker's source of truth.

Run with:
    cd backend && .venv/bin/python scripts/test_ask_singleton.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ask-virtual-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_search  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import virtual_session_store  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _ok(name: str) -> None:
    print(f"{PASS} {name}")


def _fail(name: str, msg: str) -> None:
    print(f"{FAIL} {name}: {msg}")
    raise SystemExit(1)


# ─── tests ────────────────────────────────────────────────────────


async def test_ensure_ask_session_idempotent_concurrent() -> None:
    """N concurrent ensure_ask_session calls produce a single record."""
    results = await asyncio.gather(
        *[session_search.ensure_ask_session() for _ in range(5)]
    )
    ids = {r["id"] for r in results}
    if ids != {session_search.ASK_SINGLETON_ID}:
        _fail("ensure_ask_session_idempotent_concurrent", f"unexpected ids: {ids}")
    rec = virtual_session_store.get(session_search.ASK_SINGLETON_ID) or {}
    if rec.get("orchestration_mode") != "virtual":
        _fail(
            "ensure_ask_session_idempotent_concurrent",
            f"not a virtual session: {rec.get('orchestration_mode')}",
        )
    _ok("ensure_ask_session_idempotent_concurrent")


def test_ask_session_hidden_from_index() -> None:
    """The Ask session never appears in its own search index even after
    accumulating user/assistant turns."""
    sid = session_search.ASK_SINGLETON_ID
    live = virtual_session_store.get(sid)
    if live is None:
        _fail("ask_session_hidden_from_index", "live record missing")
    virtual_session_store.replace_messages(
        session_search.ASK_EXTENSION_ID,
        sid,
        [
            {"id": "ask-user", "role": "user", "content": "test query"},
            {
                "id": "ask-asst",
                "role": "assistant",
                "content": json.dumps({"session_ids": [], "reasoning": "no matches"}),
            },
        ],
    )

    index = session_search._build_index()
    if any(entry["id"] == sid for entry in index):
        _fail("ask_session_hidden_from_index", "Ask session appears in its own index")
    _ok("ask_session_hidden_from_index")


def test_ask_result_rehydration_survives() -> None:
    """The picker's source of truth is the per-turn `ask_result` (and the
    user's `chosen_session_id`) stamped on the producing assistant message.
    Both must survive a re-read of the authoritative session record and
    carry the right shape."""
    sid = session_search.ASK_SINGLETON_ID
    msg_id = "asst-rehydrate-1"
    session_manager.create(
        id="abc",
        name="Target",
        cwd="/repo",
        orchestration_mode="native",
    )
    virtual_session_store.replace_messages(
        session_search.ASK_EXTENSION_ID,
        sid,
        [
            {
                "id": msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "isStreaming": False,
            },
        ],
    )
    session_search.propose_sessions(
        ["abc"],
        "because",
        target_sid=sid,
        msg_id=msg_id,
    )
    session_search.set_ask_choice(msg_id, "abc")

    rec = virtual_session_store.get(sid) or {}
    msg = next(
        (m for m in (rec.get("messages") or []) if m.get("id") == msg_id), None
    )
    if msg is None:
        _fail("ask_result_rehydration", f"assistant msg {msg_id!r} not found")
    result = msg.get("ask_result")
    if not isinstance(result, dict):
        _fail("ask_result_rehydration", f"ask_result not a dict: {result!r}")
    if [row.get("id") for row in result.get("results", [])] != ["abc"] or result.get("reasoning") != "because":
        _fail("ask_result_rehydration", f"shape lost on rehydrate: {result!r}")
    if msg.get("chosen_session_id") != "abc":
        _fail(
            "ask_result_rehydration",
            f"chosen_session_id lost on rehydrate: {msg.get('chosen_session_id')!r}",
        )
    _ok("ask_result_rehydration")


async def main() -> None:
    await test_ensure_ask_session_idempotent_concurrent()
    test_ask_session_hidden_from_index()
    test_ask_result_rehydration_survives()
    print("\x1b[32mall green\x1b[0m")


def _cleanup() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        _cleanup()
