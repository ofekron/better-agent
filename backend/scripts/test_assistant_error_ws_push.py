"""assistant_error_set must reach open clients as a WS frame.

Locks the recovery-visibility contract: stamping a terminal error on an
assistant message (session_manager.set_assistant_error → change kind
"assistant_error_set") dispatches a `message_error_changed` frame instead
of being swallowed as an internal-only kind. Run recovery relies on this
to surface pre-provider run failures on already-open clients.

Run: cd backend && .venv/bin/python scripts/test_assistant_error_ws_push.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home

_HOME = _test_home.isolate("assistant-error-ws-push")

import session_ws_broadcaster as swb  # noqa: E402


def test_assistant_error_set_is_not_internal() -> None:
    assert "assistant_error_set" not in swb._INTERNAL_KINDS


def test_assistant_error_set_dispatches_message_error_changed() -> None:
    broadcaster = swb.SessionWSBroadcaster(coordinator=None)
    frames: list[dict] = []
    broadcaster._dispatch = frames.append  # capture instead of WS fanout
    broadcaster.on_change(
        "sid-1",
        {
            "kind": "assistant_error_set",
            "msg_id": "msg-9",
            "text": "runtime bootstrap unavailable before the turn started",
        },
    )
    assert frames, "no WS frame dispatched for assistant_error_set"
    frame = frames[0]
    assert frame["type"] == "message_error_changed"
    assert frame["data"]["session_id"] == "sid-1"
    assert frame["data"]["msg_id"] == "msg-9"
    assert "bootstrap" in frame["data"]["error_text"]


def main() -> int:
    for fn in (
        test_assistant_error_set_is_not_internal,
        test_assistant_error_set_dispatches_message_error_changed,
    ):
        fn()
        print(f"PASS {fn.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
