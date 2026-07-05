"""Regression: a successful turn whose task is cancelled (asyncio.CancelledError)
after the run already completed must still seal its assistant message — without
depending on a backend restart.

The race that produced the original bug (session 1a4b7305, z.ai/GLM "linkedin
post script" turn): a lingering babysitter runner wrote complete.json with
success=True, the complete event was consumed (`primary_result.success=True`,
trace saved), and then an asyncio.CancelledError landed in the window before /
during `_finalize_turn_messages`. The CancelledError branch detached and
deferred to run_recovery — but recovery is STARTUP-ONLY, and the backend never
restarted, so the assistant message stayed a blank non-terminal collapsed
bubble forever.

Fix: `TurnManager._seal_completed_turn_on_cancel` re-runs the success
finalization on the spot when `primary_result.success` is True and the message
isn't already terminal. This test exercises that helper directly with a real
session_manager-backed session.

Run with:
    cd backend && .venv/bin/python scripts/test_seal_completed_turn_on_cancel.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-seal-cancel-")

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _StubCoordinator:
    """Minimal stand-in: `_finalize_turn_messages` delegates to the real
    Coordinator method (its success path never dereferences `self`, so
    `object()` is a safe instance — same trick test_stopped_no_output_message
    uses). For the negative cases we swap in a recording stub instead."""


def _real_finalize(**kw) -> None:
    Coordinator._finalize_turn_messages(object(), **kw)


def _seed() -> tuple[str, dict, dict]:
    sess = session_manager.create(
        name="t", model="gpt-5.5", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    user_msg = {"id": "user-1", "role": "user", "content": "hi"}
    asst = {"id": "asst-1", "role": "assistant", "content": "", "events": []}
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst)
    return sid, user_msg, asst


def _asst(sid: str, asst_id: str = "asst-1") -> dict:
    sess = session_manager.get(sid) or {}
    return next(m for m in sess.get("messages", []) if m.get("id") == asst_id)


def test_success_sealed_on_cancel() -> bool:
    sid, user_msg, asst = _seed()
    c = _StubCoordinator()
    c._finalize_turn_messages = _real_finalize
    tm = TurnManager(c)
    session = session_manager.get(sid)
    tm._seal_completed_turn_on_cancel(
        session=session,
        persist_to=sid,
        user_msg=user_msg,
        assistant_msg=asst,
        primary_result={"success": True, "events": [], "sdk_output": "done"},
        workers=[],
        trace_id="tr-test",
    )
    sealed = _asst(sid)
    if not sealed.get("completed_at"):
        print(f"  success turn not sealed: {sealed!r}")
        return False
    if sealed.get("content") != "done":
        print(f"  content not extracted from sdk_output: {sealed.get('content')!r}")
        return False
    if sealed.get("trace_id") != "tr-test":
        print(f"  trace_id not stamped: {sealed.get('trace_id')!r}")
        return False
    return True


def test_no_seal_when_not_success() -> bool:
    sid, user_msg, asst = _seed()
    c = _StubCoordinator()
    calls: list[dict] = []
    c._finalize_turn_messages = lambda **kw: calls.append(kw)
    tm = TurnManager(c)
    session = session_manager.get(sid)
    tm._seal_completed_turn_on_cancel(
        session=session, persist_to=sid, user_msg=user_msg,
        assistant_msg=asst,
        primary_result={"success": False, "error": "boom"},
        workers=[], trace_id="tr",
    )
    if calls:
        print(f"  finalize should not run on failed result: {calls}")
        return False
    if _asst(sid).get("completed_at"):
        print("  failed turn got completed_at")
        return False
    return True


def test_no_seal_when_already_terminal() -> bool:
    sid, user_msg, asst = _seed()
    c = _StubCoordinator()
    calls: list[dict] = []
    c._finalize_turn_messages = lambda **kw: calls.append(kw)
    tm = TurnManager(c)
    session = session_manager.get(sid)
    # Simulate the success path having already finalized this turn.
    already = dict(asst)
    already["completed_at"] = "2026-07-01T00:00:00"
    tm._seal_completed_turn_on_cancel(
        session=session, persist_to=sid, user_msg=user_msg,
        assistant_msg=already,
        primary_result={"success": True, "events": [], "sdk_output": "done"},
        workers=[], trace_id="tr",
    )
    if calls:
        print(f"  finalize should not re-run on already-terminal msg: {calls}")
        return False
    return True


def test_no_seal_when_assistant_msg_none() -> bool:
    sid, user_msg, asst = _seed()
    c = _StubCoordinator()
    calls: list[dict] = []
    c._finalize_turn_messages = lambda **kw: calls.append(kw)
    tm = TurnManager(c)
    session = session_manager.get(sid)
    tm._seal_completed_turn_on_cancel(
        session=session, persist_to=sid, user_msg=user_msg,
        assistant_msg=None,
        primary_result={"success": True, "events": []},
        workers=[], trace_id="tr",
    )
    if calls:
        print(f"  finalize should not run when assistant_msg is None: {calls}")
        return False
    return True


def main() -> int:
    cases = [
        ("success turn sealed on cancel (completed_at + content + trace_id)",
         test_success_sealed_on_cancel),
        ("failed result is not sealed", test_no_seal_when_not_success),
        ("already-terminal message is not re-finalized",
         test_no_seal_when_already_terminal),
        ("None assistant message is a no-op", test_no_seal_when_assistant_msg_none),
    ]
    failures: list[str] = []
    for name, fn in cases:
        ok = fn()
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failures.append(name)

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print(f"\nFAILED: {len(failures)} check(s)")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
