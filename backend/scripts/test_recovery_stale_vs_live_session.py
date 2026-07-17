"""Regression test for the latest-run election bug fixed by
`run_recovery._complete_session_run_descs` (c148fd82 version_identity_changed).

Root cause: `main.py` integrates a session's LIVE runs and its COLD
(completed/stale) runs via SEPARATE `integrate_recovered_runs` calls, each
grouping only the partial descs it was handed. `_integrate_recovered_session_group`
used to elect "latest" via `max(descs, key=_run_order_key)` over whichever
PARTIAL list it received — so when a session's two unreconciled runs land in
different passes, EACH pass's lone desc trivially "wins" its own one-item
election and gets replayed. Since Claude replay has no upper bound (unlike
Codex's `_codex_replay_bound`), the older run's unbounded replay walks past
its own turn's slice of the shared cumulative session jsonl and pollutes its
assistant message with the NEWER turn's text.

This test reproduces the split directly: two run descriptors for ONE session,
sharing ONE synthetic claude session jsonl (older run's slice, then newer
run's slice), fed through `integrate_recovered_runs` via TWO SEPARATE calls —
mirroring main.py's live/cold batching without depending on it. Asserts the
newer run's message gets the newer text and the older run's message is left
alone (not polluted with the newer turn's text) regardless of which call
happens first.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_stale_vs_live_session.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid

os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-recovery-stale-vs-live-")

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

from session_manager import manager as session_manager  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import integrate_recovered_runs  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _assistant_text_event(text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _seed_two_turn_session() -> tuple[str, str, str]:
    """One session, two turns (two user+assistant message pairs)."""
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    from orchs import get_strategy
    strategy = get_strategy("native")

    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "turn A",
        "events": [], "isStreaming": False,
    })
    asst_a = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst_a)

    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "turn B",
        "events": [], "isStreaming": False,
    })
    asst_b = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst_b)

    return sid, asst_a["id"], asst_b["id"]


def _seed_run_dir(
    *, run_id: str, app_sid: str, claude_sid: str, claude_jsonl,
    pre_query_byte_offset: int, jsonl_inode: int, started_at: str,
    target_message_id: str,
) -> None:
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do a thing", "cwd": "/tmp", "model": "glm-5.1",
        "session_id": claude_sid, "mode": "native", "app_session_id": app_sid,
        "fork": False,
    }))
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id, "mode": "native", "runner_pid": 0,
        "app_session_id": app_sid, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl),
        "pre_query_byte_offset": pre_query_byte_offset,
        "pre_query_jsonl_inode": jsonl_inode,
        "started_at": started_at,
        "target_message_id": target_message_id,
        "complete": False,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id, "app_session_id": app_sid, "mode": "native",
        "runner_pid": 0, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl),
        "processed_byte": 0, "cancelled": False,
        "started_at": started_at,
        "target_message_id": target_message_id,
    }))
    (run_dir / "pid").write_text("0")


async def test_partial_view_election_does_not_corrupt_older_message() -> bool:
    app_sid, asst_a_id, asst_b_id = _seed_two_turn_session()
    claude_sid = str(uuid.uuid4())

    run_dir_a = _runs_root() / str(uuid.uuid4())
    claude_jsonl = run_dir_a / "fake_claude" / f"{claude_sid}.jsonl"
    claude_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with claude_jsonl.open("w") as f:
        f.write(json.dumps(_assistant_text_event("AAA_TURN_TEXT")) + "\n")
        offset_b = f.tell()
        f.write(json.dumps(_assistant_text_event("BBB_TURN_TEXT")) + "\n")
    inode = claude_jsonl.stat().st_ino

    run_id_a = run_dir_a.name
    run_id_b = str(uuid.uuid4())
    _seed_run_dir(
        run_id=run_id_a, app_sid=app_sid, claude_sid=claude_sid,
        claude_jsonl=claude_jsonl, pre_query_byte_offset=0,
        jsonl_inode=inode, started_at="2024-01-01T00:00:00.000000",
        target_message_id=asst_a_id,
    )
    _seed_run_dir(
        run_id=run_id_b, app_sid=app_sid, claude_sid=claude_sid,
        claude_jsonl=claude_jsonl, pre_query_byte_offset=offset_b,
        jsonl_inode=inode, started_at="2024-01-01T00:00:10.000000",
        target_message_id=asst_b_id,
    )

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    by_run_id = {d.get("run_id"): d for d in recovered}
    if run_id_a not in by_run_id or run_id_b not in by_run_id:
        print(f"  seeded runs not both recovered: {sorted(by_run_id)}")
        return False

    # The exact live/cold-split partial view the real bug depended on:
    # `integrate_recovered_runs` is handed ONLY the older run's descriptor
    # (mirroring main.py's live pass, say) while the session's true-latest
    # (newer) run sits unreconciled in what would be the OTHER pass's list.
    # Pre-fix, `_integrate_recovered_session_group` elected the older run
    # as "latest" within its own one-item view and replayed it unbounded,
    # pulling the newer turn's slice of the shared jsonl onto the older
    # message. Post-fix, `_complete_session_run_descs` completes the view
    # from disk, correctly identifies the newer run as latest, and the
    # older run takes the non-latest (no-replay) path.
    await integrate_recovered_runs(coordinator=None, recovered=[by_run_id[run_id_a]])

    sess = session_manager.get(app_sid)
    asst_a = next(m for m in sess["messages"] if m["id"] == asst_a_id)
    asst_b = next(m for m in sess["messages"] if m["id"] == asst_b_id)

    content_a = asst_a.get("content") or ""
    content_b = asst_b.get("content") or ""

    if content_a:
        print(
            f"  older run's message was replayed/corrupted despite NOT "
            f"being the session's true latest run (partial-view election "
            f"bug): content_a={content_a!r}"
        )
        return False
    if "BBB_TURN_TEXT" in content_a:
        print(f"  older run's message picked up the newer turn's text: content_a={content_a!r}")
        return False

    # Now feed the SAME two-run session through the pass that DOES see the
    # true-latest run (mirroring the cold pass, or a second startup where
    # main.py's grouping is complete for this session) and confirm it
    # replays correctly.
    await integrate_recovered_runs(coordinator=None, recovered=[by_run_id[run_id_b]])
    sess = session_manager.get(app_sid)
    asst_b = next(m for m in sess["messages"] if m["id"] == asst_b_id)
    content_b = asst_b.get("content") or ""
    if "BBB_TURN_TEXT" not in content_b:
        print(f"  newer (true-latest) run's message never got replayed: content_b={content_b!r}")
        return False
    return True


TESTS = [
    (
        "election under a partial live/cold split does not corrupt the "
        "older run's message",
        test_partial_view_election_does_not_corrupt_older_message,
    ),
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
