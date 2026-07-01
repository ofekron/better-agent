"""Regression: a backend-restart must NOT mislabel a shutdown-killed turn
as "Stopped by the user".

The user Stop button / interrupt path is SOFT: it writes a sentinel and
never sets `rs.cancelled`. Only `cancel_run` (hard kill) sets
`rs.cancelled=True`, and `cancel_run` is invoked by session DELETE and
by backend-shutdown `cancel_all`. So a recovered run with `cancelled=True`
was HARD-KILLED — never user-stopped — and its `stopped_at` must stay
None. `stopped_at` is owned by the live turn path
(`turn_manager._Cancelled`), which fires only on a real user stop.

Pre-fix, `run_recovery._apply_completion_state` stamped `stopped_at`
(recovery wall-clock) whenever a recovered run's descriptor had
`cancelled=True`. After a Ctrl+C+restart, every shutdown-killed in-flight
turn then rendered "Stopped at <restart-time>" — a false attribution.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_no_false_stopped_at.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-no-false-stopped-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import _apply_completion_state, integrate_recovered_runs  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _make_assistant_text_event(text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _seed_streaming_assistant(mode: str = "native") -> tuple[str, str]:
    """Create a session with one in-flight (streaming) assistant message.
    Returns (app_sid, asst_msg_id)."""
    from orchs import get_strategy
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode=mode,
    )
    sid = sess["id"]
    user_msg = {
        "id": str(uuid.uuid4()), "role": "user",
        "content": "do a thing", "events": [],
    }
    asst_msg = get_strategy(mode).build_assistant_scaffold()
    asst_msg["isStreaming"] = True
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, asst_msg["id"]


def _seed_run(
    app_sid: str,
    asst_id: str,
    claude_sid: str,
    *,
    cancelled: bool,
    mode: str = "native",
) -> str:
    """Seed an orphan run dir whose runner pid is dead. `cancelled` is
    written to backend_state.json so recovery's descriptor carries it.
    `target_message_id` is set so recovery actually integrates the run
    onto the seeded assistant message (without it, _integrate_one skips
    at the missing-target gate)."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    claude_jsonl = run_dir / "fake_claude" / f"{claude_sid}.jsonl"
    claude_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with claude_jsonl.open("w") as f:
        f.write(json.dumps(_make_assistant_text_event("partial work")) + "\n")
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do a thing", "cwd": "/tmp", "model": "glm-5.1",
        "session_id": claude_sid, "mode": mode, "app_session_id": app_sid,
        "fork": False,
    }))
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id, "mode": mode, "runner_pid": 0,
        "app_session_id": app_sid, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl), "pre_query_byte_offset": 0,
        "complete": False,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id, "app_session_id": app_sid, "mode": mode,
        "runner_pid": 0, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl),
        "processed_byte": claude_jsonl.stat().st_size,
        "cancelled": cancelled,
        "target_message_id": asst_id,
    }))
    (run_dir / "pid").write_text("0")
    return run_id


def _asst(app_sid: str, asst_id: str) -> dict:
    sess = session_manager.get(app_sid)
    return next(m for m in sess["messages"] if m["id"] == asst_id)


async def test_shutdown_killed_run_not_marked_stopped() -> bool:
    """A recovered run with cancelled=True (hard-killed by backend
    shutdown) must NOT get stopped_at stamped. Pre-fix this stamped
    stopped_at at recovery wall-clock, misattributing a shutdown-killed
    turn as a user stop."""
    app_sid, asst_id = _seed_streaming_assistant("native")
    _seed_run(app_sid, asst_id, str(uuid.uuid4()), cancelled=True)

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    asst = _asst(app_sid, asst_id)
    if asst.get("isStreaming") is True:
        print("  streaming not pinned to False after recovery")
        return False
    if asst.get("stopped_at") not in (None, ""):
        print(f"  cancelled run got a false stopped_at: {asst.get('stopped_at')!r}")
        return False
    return True


async def test_real_user_stop_preserved_across_recovery() -> bool:
    """A turn the user actually stopped already has stopped_at persisted
    by the live path (cancelled=False — soft stop). Recovery must PRESERVE
    it, not clear it. Pre-fix the else-branch of _apply_completion_state
    wiped stopped_at for non-cancelled runs, hiding a real user stop."""
    app_sid, asst_id = _seed_streaming_assistant("native")
    # Simulate the live _Cancelled path having persisted a real stop.
    session_manager.set_stopped_at(app_sid, asst_id, "2026-06-20T10:00:00.000000")
    _seed_run(app_sid, asst_id, str(uuid.uuid4()), cancelled=False)

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    asst = _asst(app_sid, asst_id)
    if asst.get("stopped_at") != "2026-06-20T10:00:00.000000":
        print(f"  real user-stop stopped_at not preserved: {asst.get('stopped_at')!r}")
        return False
    return True


async def test_recovered_success_gets_completed_at() -> bool:
    """A recovered successful run must be terminal even when events were
    already fully live-ingested before a backend restart. Without this, the
    assistant bubble can remain blank/non-terminal forever after recovery marks
    the run reconciled."""
    app_sid, asst_id = _seed_streaming_assistant("native")
    run_id = _seed_run(app_sid, asst_id, str(uuid.uuid4()), cancelled=False)
    complete_path = _runs_root() / run_id / "complete.json"
    complete_path.write_text(json.dumps({
        "success": True,
        "session_id": "agent-1",
        "error": None,
        "token_usage": None,
        "finished_at": "2026-06-20T11:00:00.000000",
    }), encoding="utf-8")

    _apply_completion_state(app_sid, asst_id, run_id=run_id, cancelled=False)

    asst = _asst(app_sid, asst_id)
    if not asst.get("completed_at"):
        print(f"  recovered success missing completed_at: {asst!r}")
        return False
    if asst.get("stopped_at"):
        print(f"  recovered success got stopped_at: {asst.get('stopped_at')!r}")
        return False
    return True


async def test_recovered_failure_gets_assistant_error() -> bool:
    """A recovered failed run must be terminal on the assistant message,
    not only represented by the sidebar dot."""
    app_sid, asst_id = _seed_streaming_assistant("native")
    run_id = _seed_run(app_sid, asst_id, str(uuid.uuid4()), cancelled=False)
    complete_path = _runs_root() / run_id / "complete.json"
    complete_path.write_text(json.dumps({
        "success": False,
        "session_id": "agent-1",
        "error": "HTTP 500: upstream",
        "token_usage": None,
        "finished_at": "2026-06-20T11:00:00.000000",
    }), encoding="utf-8")

    _apply_completion_state(app_sid, asst_id, run_id=run_id, cancelled=False)

    asst = _asst(app_sid, asst_id)
    if not asst.get("error") or asst.get("errorText") != "HTTP 500: upstream":
        print(f"  recovered failure missing assistant error: {asst!r}")
        return False
    if asst.get("completed_at"):
        print(f"  recovered failure got completed_at: {asst.get('completed_at')!r}")
        return False
    return True


TESTS = [
    ("shutdown-killed run (cancelled=True) is NOT marked Stopped",
        test_shutdown_killed_run_not_marked_stopped),
    ("real user-stop stopped_at is preserved across recovery",
        test_real_user_stop_preserved_across_recovery),
    ("recovered successful run gets completed_at",
        test_recovered_success_gets_completed_at),
    ("recovered failed run gets assistant error",
        test_recovered_failure_gets_assistant_error),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = asyncio.run(fn()) if inspect.iscoroutinefunction(fn) else fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
