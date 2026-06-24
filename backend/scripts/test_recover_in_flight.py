"""Backend regression test for events.jsonl → assistant_msg replay
during startup recovery.

Pins the contract that when `recover_in_flight` finds an orphaned run
(no complete.json, runner pid dead) and `integrate_recovered_runs`
finalizes it, the runner's `events.jsonl` is replayed onto the
session's last assistant message — so a backend restart mid-run leaves
no empty "Stopped" bubbles for content the runner already produced.

Run with:
    cd backend && .venv/bin/python scripts/test_recover_in_flight.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-recover-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from ingestion_versions import CLAUDE_INGESTION_VERSION  # noqa: E402
from run_recovery import integrate_recovered_runs  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _seed_session_with_streaming_assistant() -> tuple[str, str, str]:
    """Create a session with a user_msg + an empty streaming assistant_msg
    (the shape lazy-creation leaves on disk after one event mirrors but
    before any text accumulates). Returns (app_session_id, user_msg_id,
    assistant_msg_id)."""
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "do a thing",
        "events": [],
        "isStreaming": False,
    }
    asst_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    }
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, user_msg["id"], asst_msg["id"]


def _seed_orphan_run(
    app_sid: str,
    claude_sid: str,
    events: list[dict],
    *,
    processed_byte: int = 0,
) -> str:
    """Synthesize a run dir on disk shaped like an orphan: input.json,
    backend_state.json with a dead pid, and a claude session jsonl on
    disk holding the raw claude-jsonl lines. No complete.json — the
    `recover_in_flight` scan will synthesize one. `state.jsonl_path`
    points at the claude jsonl, which is what `_replay_from_claude_jsonl`
    actually reads (the runner-local events.jsonl path was removed when
    the architecture switched to tailing claude's session jsonl
    directly). Returns run_id."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Write a fake claude session jsonl. Placement under a per-run
    # subdir of the test home so multiple runs don't collide.
    claude_jsonl = run_dir / "fake_claude" / f"{claude_sid}.jsonl"
    claude_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with claude_jsonl.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do a thing",
        "cwd": "/tmp",
        "model": "glm-5.1",
        "session_id": claude_sid,
        "mode": "native",
        "app_session_id": app_sid,
        "fork": False,
    }))
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "mode": "native",
        "runner_pid": 0,
        "app_session_id": app_sid,
        "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl),
        "pre_query_byte_offset": 0,
        "complete": False,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "app_session_id": app_sid,
        "mode": "native",
        "runner_pid": 0,  # _pid_alive(0) → False, so this is a dead orphan
        "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl),
        "processed_byte": processed_byte,
        "cancelled": False,
    }))
    (run_dir / "pid").write_text("0")
    return run_id


def _make_assistant_text_event(text: str) -> dict:
    """Raw claude-jsonl assistant entry with one text block. Real claude
    lines always carry a uuid; apply_event uses it for idempotent dedup,
    and the no-uuid branch is reserved for wire markers (turn_start
    etc.) that don't belong in msg.events. Synthesize one per call."""
    return {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


async def test_dead_orphan_replays_events_jsonl_into_assistant_msg() -> bool:
    """Smoking gun: orphaned native run with content in events.jsonl —
    after recovery, the assistant_msg has events + content extracted."""
    app_sid, _, asst_id = _seed_session_with_streaming_assistant()
    claude_sid = str(uuid.uuid4())
    raw_events = [
        _make_assistant_text_event("Hello"),
        _make_assistant_text_event("world"),
    ]
    _seed_orphan_run(app_sid, claude_sid, raw_events)

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    if not recovered:
        print("  recover_in_flight returned no descriptors")
        return False

    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    sess = session_manager.get(app_sid)
    asst = next(
        (m for m in (sess or {}).get("messages", []) if m.get("id") == asst_id), None
    )
    if asst is None:
        print("  assistant message disappeared")
        return False

    if len(asst.get("events") or []) != len(raw_events):
        print(f"  expected {len(raw_events)} events, got {len(asst.get('events') or [])}")
        return False
    # Each replayed event must be wrapped as `agent_message` so the
    # frontend renderer + `_extract_output_text` see the same shape as a
    # finalized turn.
    for e in asst.get("events") or []:
        if e.get("type") != "agent_message":
            print(f"  expected agent_message envelope, got {e.get('type')!r}")
            return False
    if "world" not in (asst.get("content") or ""):
        print(f"  expected latest assistant text in content, got {asst.get('content')!r}")
        return False
    if asst.get("isStreaming") is not False:
        print(f"  expected isStreaming=False post-recovery, got {asst.get('isStreaming')!r}")
        return False
    return True


async def test_recovery_skips_replay_for_legitimately_completed_run() -> bool:
    """If complete.json already exists with success=True, recovery must NOT
    overwrite content/events — `_is_consistent` short-circuits and the
    finalized message is preserved."""
    app_sid, _, asst_id = _seed_session_with_streaming_assistant()
    claude_sid = str(uuid.uuid4())

    # Pre-populate the assistant_msg as if finalize ran cleanly.
    finalized_event = {
        "type": "agent_message",
        "data": _make_assistant_text_event("real finalized content"),
    }
    session_manager.set_native_events(app_sid, asst_id, [finalized_event])
    session_manager.update_running_content(
        app_sid, asst_id, "real finalized content",
    )
    session_manager.set_streaming(app_sid, asst_id, False)
    session_manager.set_agent_sid_on_msg(app_sid, asst_id, claude_sid)
    session_manager.set_agent_sid(app_sid, "native", claude_sid)

    garbage = [_make_assistant_text_event("garbage")]
    run_id = _seed_orphan_run(app_sid, claude_sid, garbage)
    run_dir = _runs_root() / run_id
    bs_path = run_dir / "backend_state.json"
    bs = json.loads(bs_path.read_text())
    bs["processed_byte"] = Path(bs["jsonl_path"]).stat().st_size
    bs["ingestion_version"] = CLAUDE_INGESTION_VERSION
    bs_path.write_text(json.dumps(bs))
    # Mark run completed successfully — recovery should leave the message
    # alone.
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": claude_sid, "error": None, "token_usage": None,
    }))

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    sess = session_manager.get(app_sid)
    asst = next((m for m in sess["messages"] if m["id"] == asst_id), None)
    if asst is None:
        print("  assistant disappeared")
        return False
    if asst.get("content") != "real finalized content":
        print(f"  recovery clobbered finalized content: {asst.get('content')!r}")
        return False
    return True


TESTS = [
    ("dead orphan replays events.jsonl into assistant_msg", test_dead_orphan_replays_events_jsonl_into_assistant_msg),
    ("completed run is not clobbered by recovery", test_recovery_skips_replay_for_legitimately_completed_run),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = asyncio.run(fn())
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
