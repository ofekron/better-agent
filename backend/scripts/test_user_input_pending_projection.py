from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-user-input-pending-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import user_input_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_pending_for_session_uses_projection() -> None:
    req = user_input_store.create_request(
        app_session_id="sid-1",
        questions=[{
            "id": "q1",
            "header": "H",
            "question": "Q",
            "options": [],
        }],
        timeout_seconds=60,
    )
    original_read = user_input_store._read_locked

    def fail_read():
        raise AssertionError("pending_for_session should use the in-memory projection")

    # pending_for_session serves from the in-memory projection built once on
    # first load (create_request already populated it); if it re-read the file
    # under lock on every call, this monkeypatched _read_locked would raise and
    # every polling reader would contend on the file lock instead of the cheap
    # projection lookup -- stale pending prompts would deadlock the user.
    user_input_store._read_locked = fail_read
    try:
        pending = user_input_store.pending_for_session("sid-1")
    finally:
        user_input_store._read_locked = original_read
    assert len(pending) == 1, (
        "pending_for_session must serve exactly one pending request (the one just "
        f"created) from the in-memory projection, got {len(pending)}"
    )
    assert pending[0].get("request_id") == req["request_id"], (
        "pending_for_session must return the matching request_id from the in-memory "
        f"projection, got {pending[0].get('request_id')!r}"
    )


def main() -> int:
    try:
        test_pending_for_session_uses_projection()
        print(f"{PASS} user input pending uses projection")
        return 0
    except AssertionError as exc:
        print(f"{FAIL} user input pending uses projection: {exc}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
