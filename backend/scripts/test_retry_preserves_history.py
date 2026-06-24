"""Regression: retrying a stopped assistant turn preserves history.

Run:
    cd backend && .venv/bin/python scripts/test_retry_preserves_history.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import asyncio
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-retry-preserves-history-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_store  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def test_retry_preserves_previous_turn() -> bool:
    _reset_home()
    root = session_manager.create(name="retry", cwd="/tmp")
    sid = root["id"]
    user = {
        "id": "u1",
        "role": "user",
        "content": "codex state missing native rollout path",
        "events": [],
        "timestamp": "2026-06-16T20:31:53",
        "isStreaming": False,
    }
    assistant = {
        "id": "a1",
        "role": "assistant",
        "content": "failed before completion",
        "events": [],
        "timestamp": "2026-06-16T20:32:00",
        "isStreaming": False,
        "stopped_at": "2026-06-16T20:32:00",
    }
    session_manager.append_user_msg(sid, user)
    session_manager.append_assistant_msg(sid, assistant)

    async def fail_rewind_files(*_args, **_kwargs):
        raise AssertionError("retry endpoint must not rewind files")

    original = main.coordinator.rewind_files
    main.coordinator.rewind_files = fail_rewind_files
    try:
        body = asyncio.run(
            main.rewind_and_retry(
                sid,
                {"assistant_message_id": "a1"},
            )
        )
    finally:
        main.coordinator.rewind_files = original

    current = session_manager.get(sid) or {}
    messages = current.get("messages") or []
    ok = (
        body.get("retry_prompt") == user["content"]
        and [m.get("id") for m in messages] == ["u1", "a1"]
        and current.get("next_seq") == 2
    )
    print(f"{PASS if ok else FAIL} retry preserves previous user+assistant turn")
    if not ok:
        print({"body": body, "messages": messages, "next_seq": current.get("next_seq")})
    return ok


def main_run() -> int:
    try:
        return 0 if test_retry_preserves_previous_turn() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
