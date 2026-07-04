from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-session-message-count-")

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"


def _summary(sid: str) -> dict:
    return next(
        (summary for summary in session_store.list_sessions() if summary.get("id") == sid),
        {},
    )


def _append_user(sid: str, msg_id: str) -> None:
    session_manager.append_user_msg(
        sid,
        {
            "id": msg_id,
            "role": "user",
            "content": msg_id,
            "timestamp": "2026-01-01T00:00:00",
            "events": [],
        },
    )


def _append_assistant(sid: str, msg_id: str, streaming: bool) -> None:
    session_manager.append_assistant_msg(
        sid,
        {
            "id": msg_id,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-01-01T00:00:01",
            "events": [],
            "isStreaming": streaming,
        },
    )


def check(label: str, condition: bool) -> bool:
    print((PASS if condition else FAIL) + " " + label)
    return condition


def main() -> int:
    ok = True
    session = session_manager.create(
        name="count-test",
        model="model",
        cwd="/tmp/project",
        orchestration_mode="native",
        source="cli",
        user_initiated=True,
    )
    sid = session["id"]

    ok &= check("empty session count is 0", _summary(sid).get("message_count") == 0)
    _append_user(sid, "user-1")
    _append_assistant(sid, "assistant-streaming", streaming=True)
    session_manager.flush_pending_persists()
    ok &= check(
        "streaming assistant scaffold does not increment count",
        _summary(sid).get("message_count") == 1,
    )
    _append_user(sid, "user-2")
    _append_assistant(sid, "assistant-final", streaming=False)
    session_manager.flush_pending_persists()
    ok &= check(
        "assistant responses do not increment count",
        _summary(sid).get("message_count") == 2,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
