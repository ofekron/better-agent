"""Regression tests for three crash-recovery loss windows.

  1. `_is_consistent` must NOT skip replay for a run that crashed
     mid-turn after the claude sid was stamped: skipping is only legal
     on POSITIVE evidence the run's jsonl was fully live-ingested
     (processed cursor covers the file). Replay is dedup-idempotent;
     skipping is not.
  2. A wholesale replay failure must NOT write `reconciled.marker`
     (the run stays eligible for retry on next startup).
  3. `reconciled.marker` must land only AFTER the replay's
     fire-and-forget events.jsonl writes are durable (journal barrier
     before marker).
  4. A PARTIAL replay failure (one event raising) must still apply the
     remaining events within the attempt, but ANY failure blocks the
     marker — a degraded replay marked reconciled is permanent silent
     loss. The next startup rescan retries; dedup makes the
     already-applied events no-ops.
  5. `_barrier_journal` must fail closed (raise ⇒ no marker) when the
     session's root can't be resolved or the barrier itself fails.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_loss_windows.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-recovery-loss-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from ingestion_versions import current_ingestion_version  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import integrate_recovered_runs  # noqa: E402
import run_recovery  # noqa: E402


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


def _seed_session(*, streaming: bool) -> tuple[str, str]:
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
    from orchs import get_strategy
    asst_msg = get_strategy("native").build_assistant_scaffold()
    asst_msg["isStreaming"] = streaming
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    session_manager.flush_pending_persists()
    return sid, asst_msg["id"]


def _seed_run(
    app_sid: str,
    claude_sid: str,
    events: list[dict],
    *,
    processed_byte: int,
    complete: bool = False,
    target_message_id: str | None = None,
    ingestion_version: int | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    claude_jsonl = run_dir / "fake_claude" / f"{claude_sid}.jsonl"
    claude_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with claude_jsonl.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do a thing", "cwd": "/tmp", "model": "glm-5.1",
        "session_id": claude_sid, "mode": "native",
        "app_session_id": app_sid, "fork": False,
    }))
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id, "mode": "native", "runner_pid": 0,
        "app_session_id": app_sid, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl), "pre_query_byte_offset": 0,
        "complete": False,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id, "app_session_id": app_sid, "mode": "native",
        "runner_pid": 0, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl),
        "processed_byte": processed_byte, "cancelled": False,
        "target_message_id": target_message_id,
        "ingestion_version": ingestion_version,
    }))
    (run_dir / "pid").write_text("0")
    if complete:
        (run_dir / "complete.json").write_text(json.dumps({
            "success": True, "session_id": claude_sid,
            "error": None, "token_usage": None,
        }))
    return run_id


def _msg_event_uuids(app_sid: str, asst_id: str) -> set[str]:
    sess = session_manager.get(app_sid) or {}
    asst = next(
        (m for m in sess.get("messages") or [] if m.get("id") == asst_id),
        None,
    )
    if asst is None:
        return set()
    return {
        (ev.get("data") or {}).get("uuid")
        for ev in (asst.get("events") or [])
        if isinstance(ev, dict)
    }


def _events_jsonl_uuids(root_id: str) -> set[str]:
    rows, _, _ = event_ingester.read_events(root_id, limit=10_000)
    return {
        (r.get("data") or {}).get("uuid")
        for r in rows
        if isinstance(r, dict)
    }


async def test_is_consistent_rejects_undrained_crash() -> bool:
    """Crash between sid-stamping and tailer drain: the persisted
    session looks 'consistent' (sid stamped on session + msg, not
    streaming, no stopped_at) but processed_byte < jsonl size.
    Recovery MUST replay — the tail events must land on msg.events and
    in events.jsonl."""
    app_sid, asst_id = _seed_session(streaming=False)
    claude_sid = str(uuid.uuid4())
    session_manager.set_agent_sid(app_sid, "native", claude_sid)
    session_manager.set_agent_sid_on_msg(app_sid, asst_id, claude_sid)

    raw = [_make_assistant_text_event(t) for t in ("one", "two", "three")]
    _seed_run(app_sid, claude_sid, raw, processed_byte=1, complete=True)

    recovered = default_provider().recover_in_flight()
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    expected = {e["uuid"] for e in raw}
    got_msg = _msg_event_uuids(app_sid, asst_id)
    if not expected <= got_msg:
        print(f"  tail events missing from render tree: {expected - got_msg}")
        return False

    event_journal_writer.barrier_sync(app_sid)
    got_jsonl = _events_jsonl_uuids(app_sid)
    if not expected <= got_jsonl:
        print(f"  tail events missing from events.jsonl: {expected - got_jsonl}")
        return False
    return True


async def test_replay_failure_blocks_marker() -> bool:
    """Wholesale replay failure ⇒ reconciled.marker MUST NOT be
    written, and the next startup scan retries (and succeeds)."""
    app_sid, asst_id = _seed_session(streaming=True)
    claude_sid = str(uuid.uuid4())
    raw = [_make_assistant_text_event(t) for t in ("alpha", "beta")]
    run_id = _seed_run(app_sid, claude_sid, raw, processed_byte=0)

    real_replay = run_recovery._replay_and_apply

    def _boom(**kwargs):
        raise RuntimeError("wholesale replay failure (injected)")

    run_recovery._replay_and_apply = _boom
    try:
        recovered = default_provider().recover_in_flight()
        await integrate_recovered_runs(coordinator=None, recovered=recovered)
    finally:
        run_recovery._replay_and_apply = real_replay

    marker = _runs_root() / run_id / "reconciled.marker"
    if marker.exists():
        print("  marker written despite wholesale replay failure")
        return False

    recovered = default_provider().recover_in_flight()
    if run_id not in {d.get("run_id") for d in recovered}:
        print("  failed run not rescanned on next startup")
        return False
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    expected = {e["uuid"] for e in raw}
    got_msg = _msg_event_uuids(app_sid, asst_id)
    if not expected <= got_msg:
        print(f"  retry pass did not ingest events: {expected - got_msg}")
        return False
    if not marker.exists():
        print("  marker missing after successful retry pass")
        return False
    return True


async def test_partial_replay_failure_blocks_marker() -> bool:
    """One event raising transiently mid-stream: the remaining events
    are still applied within the attempt (per-event isolation), but
    the marker MUST NOT be written — the next startup rescan retries
    the run and only then writes the marker."""
    app_sid, asst_id = _seed_session(streaming=True)
    claude_sid = str(uuid.uuid4())
    raw = [_make_assistant_text_event(t) for t in ("p1", "p2", "p3")]
    poison_uuid = raw[1]["uuid"]
    run_id = _seed_run(
        app_sid,
        claude_sid,
        raw,
        processed_byte=0,
        target_message_id=asst_id,
        ingestion_version=current_ingestion_version("claude"),
    )

    from orchs import get_strategy
    strat = get_strategy("native")
    real_apply = strat.apply_event

    def _transient_apply(**kwargs):
        ev = kwargs.get("event") or {}
        if (ev.get("data") or {}).get("uuid") == poison_uuid:
            raise RuntimeError("transient apply failure (injected)")
        return real_apply(**kwargs)

    strat.apply_event = _transient_apply
    try:
        recovered = default_provider().recover_in_flight()
        await integrate_recovered_runs(coordinator=None, recovered=recovered)
    finally:
        strat.apply_event = real_apply

    survivors = {raw[0]["uuid"], raw[2]["uuid"]}
    got_msg = _msg_event_uuids(app_sid, asst_id)
    if not survivors <= got_msg:
        print(f"  failed event aborted remaining events: {survivors - got_msg}")
        return False
    marker = _runs_root() / run_id / "reconciled.marker"
    if marker.exists():
        print("  marker written despite a degraded (partial-failure) replay")
        return False

    # Next startup: transient fault gone — rescan retries and completes.
    recovered = default_provider().recover_in_flight()
    if run_id not in {d.get("run_id") for d in recovered}:
        print("  degraded run not rescanned on next startup")
        return False
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    expected = {e["uuid"] for e in raw}
    got_msg = _msg_event_uuids(app_sid, asst_id)
    if not expected <= got_msg:
        print(f"  retry pass did not ingest all events: {expected - got_msg}")
        return False
    if not marker.exists():
        print("  marker missing after successful retry pass")
        return False
    return True


async def test_unresolvable_root_blocks_marker() -> bool:
    """`_barrier_journal` must raise (not silently return) when the
    root can't be resolved, and ANY barrier failure on the marker path
    must leave the run unmarked for retry."""
    # Direct: unresolvable root ⇒ raise, never silent success.
    try:
        run_recovery._barrier_journal(f"no-such-session-{uuid.uuid4()}")
        print("  _barrier_journal returned despite unresolvable root")
        return False
    except Exception:
        pass

    # End-to-end: barrier failure after a successful replay ⇒ no marker.
    app_sid, asst_id = _seed_session(streaming=True)
    claude_sid = str(uuid.uuid4())
    raw = [_make_assistant_text_event(t) for t in ("b1", "b2")]
    run_id = _seed_run(app_sid, claude_sid, raw, processed_byte=0)

    real_barrier = event_journal_writer.barrier_sync

    def _boom_barrier(root_id, **kwargs):
        raise RuntimeError("barrier failure (injected)")

    event_journal_writer.barrier_sync = _boom_barrier
    try:
        recovered = default_provider().recover_in_flight()
        await integrate_recovered_runs(coordinator=None, recovered=recovered)
    finally:
        event_journal_writer.barrier_sync = real_barrier

    marker = _runs_root() / run_id / "reconciled.marker"
    if marker.exists():
        print("  marker written despite barrier failure")
        return False

    # Next startup: barrier healthy — rescan retries and completes.
    recovered = default_provider().recover_in_flight()
    if run_id not in {d.get("run_id") for d in recovered}:
        print("  barrier-failed run not rescanned on next startup")
        return False
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    expected = {e["uuid"] for e in raw}
    got_msg = _msg_event_uuids(app_sid, asst_id)
    if not expected <= got_msg:
        print(f"  retry pass did not ingest events: {expected - got_msg}")
        return False
    if not marker.exists():
        print("  marker missing after successful retry pass")
        return False
    return True


async def test_marker_after_journal_drain() -> bool:
    """When reconciled.marker exists, the replayed events MUST already
    be readable from events.jsonl — no fire-and-forget gap between the
    marker and the journal writes. Simulated by slowing the shard
    executor's append; without a barrier the marker lands while writes
    are still queued."""
    app_sid, asst_id = _seed_session(streaming=True)
    claude_sid = str(uuid.uuid4())
    raw = [_make_assistant_text_event(t) for t in ("d1", "d2", "d3")]
    run_id = _seed_run(app_sid, claude_sid, raw, processed_byte=0)

    real_append = event_journal_writer._append_event

    def _slow_append(event):
        time.sleep(0.5)
        return real_append(event)

    event_journal_writer._append_event = _slow_append
    try:
        recovered = default_provider().recover_in_flight()
        await integrate_recovered_runs(coordinator=None, recovered=recovered)

        marker = _runs_root() / run_id / "reconciled.marker"
        if not marker.exists():
            print("  marker never written for a successful replay")
            return False

        expected = {e["uuid"] for e in raw}
        got_jsonl = _events_jsonl_uuids(app_sid)
        if not expected <= got_jsonl:
            print(
                "  marker present but events not yet durable in "
                f"events.jsonl: {expected - got_jsonl}"
            )
            return False
        return True
    finally:
        event_journal_writer._append_event = real_append
        event_journal_writer.barrier_sync(app_sid)


TESTS = [
    ("_is_consistent rejects an undrained mid-turn crash (replays the tail)",
        test_is_consistent_rejects_undrained_crash),
    ("wholesale replay failure blocks marker; rescan retries",
        test_replay_failure_blocks_marker),
    ("reconciled.marker lands only after the journal drain",
        test_marker_after_journal_drain),
    ("partial replay failure applies the rest but blocks the marker",
        test_partial_replay_failure_blocks_marker),
    ("unresolvable root / barrier failure blocks the marker",
        test_unresolvable_root_blocks_marker),
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
