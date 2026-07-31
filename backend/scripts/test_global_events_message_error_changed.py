"""Regression test: `message_error_changed` must be a registered global
event type.

`session_ws_broadcaster.on_change` dispatches `message_error_changed`
for the `assistant_error_set` session-store change kind (terminal error
stamped on an assistant message outside live turn framing, e.g.
run-recovery finalize) via `_dispatch` -> `schedule_global` ->
`validate_global_event`. If the type isn't registered in
`global_events.GLOBAL_EVENT_SPECS`, `validate_global_event` raises
`ValueError`, which event_bus's per-subscriber isolation swallows —
the backend doesn't crash, but the WS push notifying an already-open
client of the failed message is silently dropped.

Run with:
    cd backend && .venv/bin/python scripts/test_global_events_message_error_changed.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from global_events import validate_global_event  # noqa: E402


def test_message_error_changed_is_registered() -> None:
    validate_global_event(
        "message_error_changed",
        {"session_id": "sid-1", "msg_id": "msg-1", "error_text": "boom"},
    )


if __name__ == "__main__":
    test_message_error_changed_is_registered()
    print("PASS message_error_changed is registered")
