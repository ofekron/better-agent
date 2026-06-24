"""Regression test for the live WS frame carrying the owning `msg_id`.

Bug: the last assistant message rendered as TWO identical bubbles in a
native/glm session. A provider can re-emit its FINAL consolidated
`agent_message` (full text + stop_reason) AFTER the turn already emitted
`complete` and the run was cleared. That late event still needs to reach
its real assistant message, but the wire tailer's `_entry_to_ws_frame`
only injected `app_session_id` into the frame — NOT the entry-level
`msg_id`. With no owning-message id on the frame, the frontend's
`applyLiveEvent` could not route the late event to its finalized message
and instead spawned a duplicate placeholder bubble.

Fix: `_entry_to_ws_frame` annotates the frame's `data` with `msg_id`
(from the events.jsonl entry) so the frontend routes by it.

This test locks:
  A. agent_message entry with msg_id -> frame.data.msg_id is set.
  B. entry with no msg_id -> frame.data.msg_id is absent.
  C. legacy manager_event unwrap -> frame type agent_message + msg_id carried.
  D. app_session_id still injected from entry.sid.
  E. pre-existing data.msg_id is not overwritten by the entry's.

Run with:
    cd backend && .venv/bin/python scripts/test_ws_frame_msg_id.py
"""

from __future__ import annotations

import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-ws-frame-msg-id-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from jsonl_tailer import BetterAgentJsonlTailer  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_frame = BetterAgentJsonlTailer._entry_to_ws_frame


def _check(label: str, cond: bool) -> bool:
    print(f"  {'OK ' if cond else 'BAD'} {label}")
    return cond


def main() -> int:
    failures = 0

    # A. agent_message entry with msg_id -> frame carries msg_id
    entry_a = {
        "seq": 481,
        "sid": "56482b82",
        "type": "agent_message",
        "msg_id": "3be27578-bb48-4a86-beac-12d1bee9c02c",
        "data": {"uuid": "51bfa95f", "type": "assistant"},
    }
    frame_a = _frame(entry_a)
    ok = _check(
        "A: agent_message frame carries msg_id",
        frame_a is not None
        and frame_a["data"].get("msg_id") == "3be27578-bb48-4a86-beac-12d1bee9c02c",
    )
    failures += not ok

    # B. entry with no msg_id -> msg_id absent
    entry_b = {
        "seq": 5,
        "sid": "56482b82",
        "type": "agent_message",
        "data": {"uuid": "abc"},
    }
    frame_b = _frame(entry_b)
    ok = _check(
        "B: no msg_id on entry -> frame omits msg_id",
        frame_b is not None and "msg_id" not in frame_b["data"],
    )
    failures += not ok

    # C. legacy manager_event unwrap -> type agent_message + msg_id carried
    inner = {"type": "agent_message", "data": {"uuid": "z", "type": "assistant"}}
    entry_c = {
        "seq": 9,
        "sid": "56482b82",
        "type": "manager_event",
        "msg_id": "msg-c",
        "data": {"event": inner},
    }
    frame_c = _frame(entry_c)
    ok = _check(
        "C: manager_event unwrapped to agent_message with msg_id",
        frame_c is not None
        and frame_c["type"] == "agent_message"
        and frame_c["data"].get("msg_id") == "msg-c",
    )
    failures += not ok

    # D. app_session_id still injected from entry.sid
    ok = _check(
        "D: app_session_id injected from sid",
        frame_a is not None and frame_a["data"].get("app_session_id") == "56482b82",
    )
    failures += not ok

    # E. pre-existing data.msg_id is NOT overwritten by the entry's
    entry_e = {
        "seq": 7,
        "sid": "56482b82",
        "type": "agent_message",
        "msg_id": "entry-msg-id",
        "data": {"uuid": "q", "msg_id": "data-msg-id"},
    }
    frame_e = _frame(entry_e)
    ok = _check(
        "E: pre-existing data.msg_id preserved (not overwritten)",
        frame_e is not None and frame_e["data"].get("msg_id") == "data-msg-id",
    )
    failures += not ok

    # Non-string / empty msg_id never injected
    entry_f = {
        "seq": 8,
        "sid": "56482b82",
        "type": "agent_message",
        "msg_id": None,
        "data": {"uuid": "n"},
    }
    frame_f = _frame(entry_f)
    ok = _check(
        "F: None msg_id -> frame omits msg_id",
        frame_f is not None and "msg_id" not in frame_f["data"],
    )
    failures += not ok

    print()
    if failures:
        print(f"{FAIL} {failures} check(s) failed")
        return 1
    print(f"{PASS} all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
