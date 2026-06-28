"""Locks that a turn which fails WITHOUT raising (the common case:
provider API errors caught and returned as success=False) still sets the
red sidebar error dot.

Regression for a real bug: `_finalize_turn_messages` surfaced the failure
on the assistant message via `set_assistant_error`, but the dot hook lived
only in turn_manager's `except Exception` block — which never fires when
the runner returns success=False. So most real errors (HTTP 400, quota,
silent exits) showed a red message bubble but no sidebar dot.

Run with:
    cd backend && .venv/bin/python scripts/test_finalize_sets_error_dot.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-finalize-error-dot-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _seed() -> tuple[str, dict, dict]:
    sess = session_manager.create(
        name="t", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    user_msg = {"id": "u1", "role": "user", "content": "go"}
    assistant_msg = {
        "id": "a1", "role": "assistant", "content": "", "events": [],
    }
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, assistant_msg)
    return sid, user_msg, assistant_msg


def _finalize(sid: str, user_msg: dict, assistant_msg: dict, *, primary_result: dict,
              stopped_at=None, error_text=None) -> None:
    Coordinator._finalize_turn_messages(
        object(),
        session=session_manager.get(sid),
        app_session_id=sid,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        primary_result=primary_result,
        workers=[],
        stopped_at=stopped_at,
        trace_id="trace-1",
        error_text=error_text,
    )


def test_non_exception_failure_sets_dot() -> None:
    """Reproduces the reported bug: a run returning success=False with a
    typed error (e.g. HTTP 400 caught by the provider) must set the dot."""
    sid, user_msg, assistant_msg = _seed()
    assert session_manager.has_unseen_error(sid) is False

    _finalize(
        sid, user_msg, assistant_msg,
        primary_result={
            "success": False,
            "events": [],
            "error": "RuntimeError: HTTP 400: invalid reasoning_effort",
            "sdk_output": "",
        },
        stopped_at=None,
    )
    assert session_manager.has_unseen_error(sid) is True, (
        "non-exception failure must set the unseen-error dot"
    )
    print(f"{PASS} non_exception_failure_sets_dot")


def test_exception_failure_sets_dot() -> None:
    """The exception path (error_text) must also set the dot."""
    sid, user_msg, assistant_msg = _seed()
    _finalize(
        sid, user_msg, assistant_msg,
        primary_result={"success": False, "events": [], "sdk_output": ""},
        stopped_at=None,
        error_text="ValueError: boom",
    )
    assert session_manager.has_unseen_error(sid) is True, (
        "exception-path failure must set the unseen-error dot"
    )
    print(f"{PASS} exception_failure_sets_dot")


def test_success_does_not_set_dot() -> None:
    """A successful turn must not set the dot."""
    sid, user_msg, assistant_msg = _seed()
    _finalize(
        sid, user_msg, assistant_msg,
        primary_result={"success": True, "events": [], "sdk_output": "ok"},
        stopped_at=None,
    )
    assert session_manager.has_unseen_error(sid) is False, (
        "successful turn must not set the error dot"
    )
    print(f"{PASS} success_does_not_set_dot")


def test_cancelled_does_not_set_dot() -> None:
    """A cancelled run (stopped_at set) must not set the dot — cancel is
    user-initiated, not an error."""
    sid, user_msg, assistant_msg = _seed()
    _finalize(
        sid, user_msg, assistant_msg,
        primary_result={
            "success": False, "events": [], "error": "cancelled",
            "sdk_output": "",
        },
        stopped_at="2026-06-28T00:00:00",
    )
    assert session_manager.has_unseen_error(sid) is False, (
        "cancelled run must not set the error dot"
    )
    print(f"{PASS} cancelled_does_not_set_dot")


def main() -> int:
    try:
        test_non_exception_failure_sets_dot()
        test_exception_failure_sets_dot()
        test_success_does_not_set_dot()
        test_cancelled_does_not_set_dot()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
