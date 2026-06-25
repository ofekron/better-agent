"""Backend regression test: startup recovery must NOT leak a prior
turn's events onto a later turn's assistant message.

INVARIANT under test: all of a session's turn-runs share ONE cumulative
claude session jsonl. Turns are strictly serial, so only the
latest-started run for a session can be the in-flight/crashed turn that
legitimately needs replay. Every earlier run's events were already
live-ingested onto that run's own assistant message; replaying an
earlier run would slice the shared cumulative jsonl from its (smaller)
`pre_query_byte_offset` and dump a prior turn's events onto whatever is
currently the last assistant message — corrupting it with out-of-order
cross-turn content.

Pre-fix: `integrate_recovered_runs` replays every un-reconciled run onto
`_last_assistant(sess)`; run-1 (pre_query_byte_offset=0) replays the
WHOLE cumulative file onto the final turn-2 message → turn-1 uuids leak
in. Post-fix: only the latest run (run-2) is replayed; run-1 is
reconciled without replay.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_no_cross_turn_leak.py
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
_TMP_HOME = _test_home.isolate("bc-test-xturn-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import integrate_recovered_runs  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _line(text: str) -> dict:
    """A raw claude-jsonl assistant line with a unique uuid."""
    return {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _wrap(raw: dict) -> dict:
    """The on-message shape live ingest leaves behind for a raw line."""
    return {"type": "agent_message", "data": raw}


def _uuids(events: list[dict]) -> set[str]:
    out: set[str] = set()
    for e in events or []:
        u = (e.get("data") or {}).get("uuid") or e.get("uuid")
        if u:
            out.add(u)
    return out


def _seed_run(
    app_sid: str,
    claude_sid: str,
    cumulative_jsonl: Path,
    pre_query_byte_offset: int,
    started_at: str,
    target_message_id: str | None = None,
) -> str:
    """A dead-orphan run dir whose state.json points at the SHARED
    cumulative jsonl. complete.json present (recover_in_flight will tag
    it already_complete); no reconciled.marker. started_at is stamped in
    BOTH state.json and backend_state.json (latest-run selection reads
    backend_state.json first, state.json as fallback)."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "mode": "native",
        "runner_pid": 0,
        "app_session_id": app_sid,
        "session_id": claude_sid,
        "jsonl_path": str(cumulative_jsonl),
        "pre_query_byte_offset": pre_query_byte_offset,
        "started_at": started_at,
        "complete": False,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "app_session_id": app_sid,
        "persist_to": app_sid,
        "mode": "native",
        "runner_pid": 0,  # _pid_alive(0) → False ⇒ dead orphan
        "session_id": claude_sid,
        "jsonl_path": str(cumulative_jsonl),
        "started_at": started_at,
        "processed_byte": 0,
        "cancelled": False,
        **({"target_message_id": target_message_id} if target_message_id else {}),
    }))
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": claude_sid,
        "error": None, "token_usage": None,
    }))
    (run_dir / "pid").write_text("0")
    return run_id


async def test_no_cross_turn_event_leak() -> bool:
    # --- one cumulative claude jsonl: turn-1 lines then turn-2 lines ---
    claude_sid = str(uuid.uuid4())
    cumulative_dir = Path(_TMP_HOME) / "claude_sessions"
    cumulative_dir.mkdir(parents=True, exist_ok=True)
    cumulative_jsonl = cumulative_dir / f"{claude_sid}.jsonl"

    turn1_raw = [_line("t1-a"), _line("t1-b"), _line("t1-c")]
    turn2_raw = [_line("t2-a"), _line("t2-b")]
    with cumulative_jsonl.open("w") as f:
        for r in turn1_raw + turn2_raw:
            f.write(json.dumps(r) + "\n")

    turn1_uuids = {r["uuid"] for r in turn1_raw}
    turn2_uuids = {r["uuid"] for r in turn2_raw}

    # --- session: turn-1 finalized msg + turn-2 streaming (final) msg --
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    app_sid = sess["id"]
    session_manager.set_agent_sid(app_sid, "native", claude_sid)

    u1 = {"id": str(uuid.uuid4()), "role": "user",
          "content": "turn 1", "events": [], "isStreaming": False}
    a1 = {"id": str(uuid.uuid4()), "role": "assistant",
          "content": "t1", "events": [_wrap(r) for r in turn1_raw],
          "isStreaming": False}
    u2 = {"id": str(uuid.uuid4()), "role": "user",
          "content": "turn 2", "events": [], "isStreaming": False}
    a2 = {"id": str(uuid.uuid4()), "role": "assistant",
          "content": "t2", "events": [_wrap(r) for r in turn2_raw],
          "isStreaming": True}
    session_manager.append_user_msg(app_sid, u1)
    session_manager.append_assistant_msg(app_sid, a1)
    session_manager.append_user_msg(app_sid, u2)
    session_manager.append_assistant_msg(app_sid, a2)

    # --- two runs on the SHARED jsonl; run-1 older + baseline 0 --------
    run1 = _seed_run(app_sid, claude_sid, cumulative_jsonl, 0,
                     "2026-05-20T01:00:00.000000")
    turn1_bytes = sum(len(json.dumps(e).encode("utf-8")) + 1 for e in turn1_raw)
    run2 = _seed_run(app_sid, claude_sid, cumulative_jsonl, turn1_bytes,
                     "2026-05-20T02:00:00.000000")

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    if len(recovered) != 2:
        print(f"  expected 2 recovered descriptors, got {len(recovered)}")
        return False

    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    sess = session_manager.get(app_sid)
    msgs = {m["id"]: m for m in (sess or {}).get("messages", [])}
    final = msgs.get(a2["id"])
    first = msgs.get(a1["id"])
    if final is None or first is None:
        print("  an assistant message disappeared")
        return False

    final_uuids = _uuids(final.get("events"))

    # (i) no prior-turn event leaked onto the final (turn-2) message
    leaked = final_uuids & turn1_uuids
    if leaked:
        print(f"  CROSS-TURN LEAK: turn-1 uuids on final msg: {sorted(leaked)}")
        return False
    if not turn2_uuids.issubset(final_uuids):
        print(f"  final msg lost its own turn-2 events: "
              f"{sorted(turn2_uuids - final_uuids)}")
        return False

    # (ii) the earlier turn-1 message was not altered
    if _uuids(first.get("events")) != turn1_uuids:
        print(f"  turn-1 msg event-set changed: "
              f"{sorted(_uuids(first.get('events')))}")
        return False

    # (iii) both runs reconciled so the next scan skips them
    for rid in (run1, run2):
        if not (_runs_root() / rid / "reconciled.marker").exists():
            print(f"  run {rid[:8]} missing reconciled.marker")
            return False

    return True


async def test_single_recovered_old_target_does_not_redigest() -> bool:
    claude_sid = str(uuid.uuid4())
    cumulative_dir = Path(_TMP_HOME) / "claude_sessions_single"
    cumulative_dir.mkdir(parents=True, exist_ok=True)
    cumulative_jsonl = cumulative_dir / f"{claude_sid}.jsonl"

    turn1_raw = [_line("old-target")]
    turn2_raw = [_line("newer-prompt")]
    with cumulative_jsonl.open("w") as f:
        for raw in turn1_raw + turn2_raw:
            f.write(json.dumps(raw) + "\n")

    turn1_uuids = {raw["uuid"] for raw in turn1_raw}
    turn2_uuids = {raw["uuid"] for raw in turn2_raw}

    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    app_sid = sess["id"]
    session_manager.set_agent_sid(app_sid, "native", claude_sid)

    a1 = {"id": str(uuid.uuid4()), "role": "assistant",
          "content": "old-target", "events": [_wrap(raw) for raw in turn1_raw],
          "isStreaming": False}
    a2 = {"id": str(uuid.uuid4()), "role": "assistant",
          "content": "newer-prompt", "events": [_wrap(raw) for raw in turn2_raw],
          "isStreaming": False}
    session_manager.append_user_msg(app_sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "turn 1",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(app_sid, a1)
    session_manager.append_user_msg(app_sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "turn 2",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(app_sid, a2)

    run_id = _seed_run(
        app_sid,
        claude_sid,
        cumulative_jsonl,
        0,
        "2026-05-20T01:00:00.000000",
        target_message_id=a1["id"],
    )

    recovered = default_provider().recover_in_flight()
    recovered = [desc for desc in recovered if desc.get("run_id") == run_id]
    if len(recovered) != 1:
        print(f"  expected one recovered descriptor, got {len(recovered)}")
        return False

    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    sess = session_manager.get(app_sid)
    msgs = {m["id"]: m for m in (sess or {}).get("messages", [])}
    old_uuids = _uuids(msgs.get(a1["id"], {}).get("events"))
    newer_uuids = _uuids(msgs.get(a2["id"], {}).get("events"))
    leaked = old_uuids & turn2_uuids
    if leaked:
        print(f"  old target absorbed newer prompt uuids: {sorted(leaked)}")
        return False
    if old_uuids != turn1_uuids:
        print(f"  old target changed: {sorted(old_uuids)}")
        return False
    if newer_uuids != turn2_uuids:
        print(f"  newer assistant changed: {sorted(newer_uuids)}")
        return False
    if not (_runs_root() / run_id / "reconciled.marker").exists():
        print("  recovered old-target run was not marked reconciled")
        return False
    return True


TESTS = [
    ("recovery does not leak a prior turn's events onto a later turn",
     test_no_cross_turn_event_leak),
    ("single recovered old target does not redigest cumulative stream",
     test_single_recovered_old_target_does_not_redigest),
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
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
