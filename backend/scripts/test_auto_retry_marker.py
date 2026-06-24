"""Regression: a turn that succeeds after auto-retries gets a durable
`auto_retry` marker on its assistant message, and the change event that
drives the WS `message_auto_retry_changed` frame is emitted.

Run:
    cd backend && .venv/bin/python scripts/test_auto_retry_marker.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-auto-retry-marker-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402,F401  (initializes state dirs / paths)
import session_store  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def _seed_turn() -> str:
    _reset_home()
    root = session_manager.create(name="auto-retry", cwd="/tmp")
    sid = root["id"]
    session_manager.append_user_msg(sid, {
        "id": "u1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-06-16T20:31:53", "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": "a1", "role": "assistant", "content": "ok now", "events": [],
        "timestamp": "2026-06-16T20:32:00", "isStreaming": False,
    })
    return sid


def test_record_auto_retry_stamps_message() -> bool:
    sid = _seed_turn()
    session_manager.record_auto_retry(sid, "a1", 2, "rate_limit")

    current = session_manager.get(sid) or {}
    msg = next((m for m in current.get("messages") or [] if m["id"] == "a1"), {})
    marker = msg.get("auto_retry")

    ok = marker == {"count": 2, "kind": "rate_limit"}
    print(f"{PASS if ok else FAIL} record_auto_retry stamps durable auto_retry marker")
    if not ok:
        print({"marker": marker})
    return ok


def test_broadcaster_maps_to_ws_frame() -> bool:
    b = SessionWSBroadcaster(coordinator=None)
    captured: list[dict] = []
    b._dispatch = lambda payload: captured.append(payload)  # type: ignore[assignment]
    b.on_change("sid-1", {
        "kind": "msg_auto_retry_set",
        "msg_id": "a1",
        "auto_retry": {"count": 3, "kind": "transient"},
    })
    ok = (
        len(captured) == 1
        and captured[0].get("type") == "message_auto_retry_changed"
        and captured[0]["data"] == {
            "session_id": "sid-1",
            "msg_id": "a1",
            "auto_retry": {"count": 3, "kind": "transient"},
        }
    )
    print(f"{PASS if ok else FAIL} broadcaster maps msg_auto_retry_set -> message_auto_retry_changed")
    if not ok:
        print({"captured": captured})
    return ok


def test_clean_turn_has_no_marker() -> bool:
    sid = _seed_turn()
    current = session_manager.get(sid) or {}
    msg = next((m for m in current.get("messages") or [] if m["id"] == "a1"), {})
    ok = "auto_retry" not in msg
    print(f"{PASS if ok else FAIL} a non-retried turn carries no auto_retry marker")
    return ok


def main_run() -> int:
    try:
        results = [
            test_record_auto_retry_stamps_message(),
            test_broadcaster_maps_to_ws_frame(),
            test_clean_turn_has_no_marker(),
        ]
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
