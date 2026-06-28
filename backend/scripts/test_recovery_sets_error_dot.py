"""Locks that run-recovery surfaces the sidebar error dot when a recovered
run FAILED (non-success, non-cancelled) — closing the gap where a failure
discovered during backend restart never reached `turn_manager`'s live
except block.

`_apply_completion_state` is recovery's completion chokepoint (called for
finalized/dead recovered runs). It now reads complete.json and sets the
dot on failure, mirroring the live `_finalize_turn_messages` path.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_sets_error_dot.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-recovery-error-dot-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from runs_dir import runs_root as _runs_root  # noqa: E402
from run_recovery import _apply_completion_state  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _seed() -> tuple[str, str]:
    sess = session_manager.create(
        name="t", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {"id": "u1", "role": "user", "content": "go"})
    session_manager.append_assistant_msg(sid, {
        "id": "a1", "role": "assistant", "content": "", "events": [],
    })
    return sid, "a1"


def _write_complete(run_id: str, *, success: bool, error=None) -> None:
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"success": success, "session_id": None, "token_usage": None,
               "finished_at": "2026-06-28T00:00:00"}
    if error is not None:
        payload["error"] = error
    (run_dir / "complete.json").write_text(json.dumps(payload), encoding="utf-8")


def _run(sid: str, msg_id: str, run_id: str, *, cancelled: bool) -> None:
    # `_apply_completion_state` runs inside a recovery batch in production.
    with session_manager.batch(sid, bump_updated_at=False):
        _apply_completion_state(
            sid, msg_id, run_id=run_id, cancelled=cancelled,
        )


def test_recovered_failure_sets_dot() -> None:
    sid, msg_id = _seed()
    run_id = f"run-{uuid.uuid4()}"
    _write_complete(run_id, success=False, error="HTTP 500: upstream")
    _run(sid, msg_id, run_id, cancelled=False)
    assert session_manager.has_unseen_error(sid) is True, (
        "a recovered failed run must set the error dot"
    )
    print(f"{PASS} recovered_failure_sets_dot")


def test_recovered_success_no_dot() -> None:
    sid, msg_id = _seed()
    run_id = f"run-{uuid.uuid4()}"
    _write_complete(run_id, success=True)
    _run(sid, msg_id, run_id, cancelled=False)
    assert session_manager.has_unseen_error(sid) is False, (
        "a recovered successful run must not set the dot"
    )
    print(f"{PASS} recovered_success_no_dot")


def test_recovered_cancelled_no_dot() -> None:
    sid, msg_id = _seed()
    run_id = f"run-{uuid.uuid4()}"
    _write_complete(run_id, success=False, error="cancelled")
    _run(sid, msg_id, run_id, cancelled=True)
    assert session_manager.has_unseen_error(sid) is False, (
        "a recovered cancelled run must not set the dot"
    )
    print(f"{PASS} recovered_cancelled_no_dot")


def main() -> int:
    try:
        test_recovered_failure_sets_dot()
        test_recovered_success_no_dot()
        test_recovered_cancelled_no_dot()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
