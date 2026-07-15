"""Regression tests for the render consistency of recovered sessions.

Pins two specific bugs that produced visibly-empty assistant bubbles on
sessions that crashed mid-turn:

  A. `run_recovery._integrate_one` must pin the recovered primary sid
     onto flat `msg.agent_session_id` via `set_agent_sid_on_msg` so
     the recovered assistant message resumes the right CLI session.

  B. `_reconcile_msg_events_from_jsonl` (called on every REST GET)
     re-applied events.jsonl entries onto `msg.events` via apply_event
     but never re-derived `msg.content`. Recovery-survivors whose
     content was lost during the crash stayed with content='' forever
     even though msg.events had assistant text events.

Run with:
    cd backend && .venv/bin/python scripts/test_recovery_render_consistency.py
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

os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-recovery-render-")

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

from session_manager import manager as session_manager  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from run_recovery import integrate_recovered_runs  # noqa: E402
from render_tree_hydrate import _bracket_orphan_rows  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _make_assistant_text_event(text: str) -> dict:
    """Raw claude-jsonl assistant entry with one text block + uuid."""
    return {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _seed_session_with_streaming_assistant(mode: str = "native") -> tuple[str, str, str]:
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode=mode,
    )
    sid = sess["id"]
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "do a thing",
        "events": [],
        "isStreaming": False,
    }
    # Use the strategy's scaffold so manager mode gets the `manager`
    # scope key — native must NOT get one.
    from orchs import get_strategy
    asst_msg = get_strategy(mode).build_assistant_scaffold()
    asst_msg["isStreaming"] = True
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, user_msg["id"], asst_msg["id"]


def _seed_orphan_run(
    app_sid: str, claude_sid: str, events: list[dict], *, mode: str = "native",
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
        "processed_byte": claude_jsonl.stat().st_size, "cancelled": False,
    }))
    (run_dir / "pid").write_text("0")
    return run_id


async def test_native_recovery_does_not_add_manager_scope() -> bool:
    """A native-mode session that crashes mid-turn must NOT come back
    with a `manager` scope on its assistant message after recovery.
    Pre-fix, run_recovery unconditionally pinned `manager.session_id`,
    grafting a manager dict onto native msgs."""
    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    claude_sid = str(uuid.uuid4())
    raw_events = [_make_assistant_text_event("Hello world from recovery")]
    _seed_orphan_run(app_sid, claude_sid, raw_events, mode="native")

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    sess = session_manager.get(app_sid)
    asst = next(
        (m for m in sess["messages"] if m["id"] == asst_id), None
    )
    if asst is None:
        print("  assistant msg disappeared after recovery")
        return False
    if "manager" in asst:
        print(f"  native msg got a `manager` scope: {asst.get('manager')!r}")
        return False
    return True


async def test_manager_recovery_still_pins_session_id() -> bool:
    """A manager-mode session that crashes mid-turn must still come back
    with `manager.session_id` populated (mode-gate must not over-suppress)."""
    app_sid, _, asst_id = _seed_session_with_streaming_assistant("manager")
    claude_sid = str(uuid.uuid4())
    raw_events = [_make_assistant_text_event("Recovered")]
    _seed_orphan_run(app_sid, claude_sid, raw_events, mode="manager")

    bridge = default_provider()
    recovered = bridge.recover_in_flight()
    await integrate_recovered_runs(coordinator=None, recovered=recovered)

    sess = session_manager.get(app_sid)
    asst = next((m for m in sess["messages"] if m["id"] == asst_id), None)
    if asst is None:
        print("  manager assistant msg disappeared after recovery")
        return False
    if asst.get("agent_session_id") != claude_sid:
        print(f"  agent_session_id not pinned: {asst.get('agent_session_id')!r}")
        return False
    return True


def test_reconcile_reextracts_content_from_jsonl_only_events() -> bool:
    """When events.jsonl has assistant text but the persisted msg.events
    is empty AND msg.content is empty (the recovery-orphan shape: the
    runner's tailer wrote events.jsonl before the crash; recovery couldn't
    derive text from its own state.json replay), the REST-time
    reconcile must re-apply events to msg.events AND re-derive content
    so the bubble actually shows the assistant's text. Pre-fix,
    reconcile populated msg.events but never updated content."""
    from main import _reconcile_msg_events_from_jsonl
    from orchs import get_strategy

    # Seed a native session with a finalized-but-empty assistant msg.
    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    session_manager.set_streaming(app_sid, asst_id, False)

    # Pre-seed events.jsonl with two assistant text events tagged to
    # this msg_id (simulating what OwnedClaudeJsonlTailer would have
    # written from the live claude jsonl tail before the crash). Distinct
    # UUIDs represent separate API messages with an implicit tool boundary,
    # so the plain-content snapshot is the final contiguous batch: "world".
    for text in ("Hello", "world"):
        enriched = {
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
        # Legacy-coverage: "claude_tailer" is what the pre-fork-identity
        # PRIMARY tailer stamped; such rows are the sole copy of primary
        # content on real disks and must keep rendering (the fork
        # discriminator is FORK_BACKUP_SOURCE="fork_backup").
        event_ingester.ingest(
            app_sid, sid=app_sid, event_type="agent_message",
            data=enriched, source="claude_tailer", msg_id=asst_id,
        )

    # Sanity: before reconcile, msg.events empty and content empty.
    sess = session_manager.get(app_sid)
    asst = next(m for m in sess["messages"] if m["id"] == asst_id)
    assert (asst.get("events") or []) == [], "pre-reconcile msg.events must be empty"
    assert (asst.get("content") or "") == "", "pre-reconcile msg.content must be empty"

    # Reconcile the tree (the REST handler calls this on every GET).
    tree = session_manager.get_root_tree(app_sid)
    _reconcile_msg_events_from_jsonl(tree)

    # After reconcile, msg.events grows AND msg.content is set.
    sess = session_manager.get(app_sid)
    asst = next(m for m in sess["messages"] if m["id"] == asst_id)
    if len(asst.get("events") or []) != 2:
        print(f"  expected 2 reconciled events, got {len(asst.get('events') or [])}")
        return False
    content = asst.get("content") or ""
    if content != "world":
        print(f"  reconcile didn't preserve final text batch: {content!r}")
        return False
    return True


def test_reconcile_repairs_empty_content_when_event_counts_match() -> bool:
    from main import _reconcile_msg_events_from_jsonl
    from orchs import ApplyEventCtx, get_strategy

    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    strategy = get_strategy("native")
    sess = session_manager.get_ref(app_sid)
    asst = next(m for m in sess["messages"] if m["id"] == asst_id)
    strategy.apply_event(
        app_session_id=app_sid,
        msg=asst,
        event={
            "type": "agent_message",
            "data": {
                "uuid": "matched-count-content",
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "matched text"}],
                },
            },
        },
        ctx=ApplyEventCtx(root_id=app_sid),
        source_is_provider_stream=True,
    )
    session_manager.set_streaming(app_sid, asst_id, False)
    event_ingester.close(app_sid)

    live = next(
        m for m in session_manager.get_ref(app_sid)["messages"]
        if m["id"] == asst_id
    )
    live["content"] = ""
    live["_content_dirty"] = True

    tree = session_manager.get_root_tree(app_sid)
    _reconcile_msg_events_from_jsonl(tree)
    repaired = next(
        m for m in session_manager.get(app_sid)["messages"]
        if m["id"] == asst_id
    )
    if repaired.get("content") != "matched text":
        print(f"  count-match reconcile skipped content repair: {repaired.get('content')!r}")
        return False
    return repaired.get("_content_dirty") is not True


def test_reconcile_does_not_clobber_streaming_msg_content() -> bool:
    """Reconcile must NOT update content on a still-streaming msg —
    live content is owned by apply_event during the turn; rewriting it
    from reconcile during a REST GET would race the live writer."""
    from main import _reconcile_msg_events_from_jsonl

    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    # Pre-set a synthetic streaming content (as if mid-turn).
    session_manager.update_running_content(app_sid, asst_id, "WIP partial")
    # Confirm streaming flag stays True (from seed).
    sess = session_manager.get(app_sid)
    asst = next(m for m in sess["messages"] if m["id"] == asst_id)
    assert asst.get("isStreaming") is True

    # Seed events.jsonl with assistant text that disagrees with WIP.
    enriched = {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Different content"}],
        },
    }
    event_ingester.ingest(
        app_sid, sid=app_sid, event_type="agent_message",
        data=enriched, source="claude_tailer", msg_id=asst_id,
    )

    tree = session_manager.get_root_tree(app_sid)
    _reconcile_msg_events_from_jsonl(tree)

    sess = session_manager.get(app_sid)
    asst = next(m for m in sess["messages"] if m["id"] == asst_id)
    if asst.get("content") != "WIP partial":
        print(f"  reconcile clobbered streaming content: {asst.get('content')!r}")
        return False
    return True


def _ws_subscribe_projection(app_sid: str, since_seq: int = 0) -> list[dict]:
    """Mirror of the WS subscribe handler's projection in main.py
    (the messages_replay branch). Kept in sync with the production
    handler: get_root_tree → reconcile (mutates cache, NOT the local
    tree, which is the subtle reason for using session_manager.get
    below) → read fresh state from session_manager.get → since_seq
    filter.

    Used by DIV-1 regression tests so an accidental refactor of either
    the WS path or this helper is caught by the test failing."""
    from main import _reconcile_msg_events_from_jsonl, _strip_synthetic_events_from_tree
    tree = session_manager.get_root_tree(app_sid)
    if tree is None:
        return []
    _strip_synthetic_events_from_tree(tree)
    _reconcile_msg_events_from_jsonl(tree)
    sess = session_manager.get(app_sid)
    if sess is None:
        return []
    persisted = sess.get("messages") or []
    # No in-flight substitution in tests — coordinator isn't in play.
    return [m for m in persisted if int(m.get("seq", 0)) >= since_seq]


def test_ws_replay_carries_orphan_events_DIV_1() -> bool:
    """DIV-1 regression: WS messages_replay MUST carry orphan events
    (msg_id=None) the same way REST GET /api/sessions/{id} does.

    Before this fix, the WS subscribe handler ran a per-msg apply loop
    using `read_ws_events(msg_id_filter=msg_id)` — which only returns
    msg_id-tagged events, silently dropping orphans. After the fix, the
    WS path uses the SAME `_reconcile_msg_events_from_jsonl` projection
    REST uses, so orphans flow through identically (INV-15 / ADR-1).

    This test asserts: an orphan event (msg_id=None) appears in the
    WS-projected messages, and matches what REST would project."""
    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    session_manager.set_streaming(app_sid, asst_id, False)

    # Seed one named event (so the assistant_msg has a seq floor) and
    # one orphan event (msg_id=None) at a higher seq, simulating the
    # ClaudeJsonlTailer landing extra events after finalize.
    named_uuid = str(uuid.uuid4())
    orphan_uuid = str(uuid.uuid4())
    event_ingester.ingest(
        app_sid, sid=app_sid, event_type="agent_message",
        data={
            "type": "assistant", "uuid": named_uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
        },
        source="claude_tailer", msg_id=asst_id,
    )
    event_ingester.ingest(
        app_sid, sid=app_sid, event_type="agent_message",
        data={
            "type": "assistant", "uuid": orphan_uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Orphan"}]},
        },
        source="claude_tailer", msg_id=None,  # ← the orphan
    )

    ws_msgs = _ws_subscribe_projection(app_sid)
    asst = next((m for m in ws_msgs if m.get("id") == asst_id), None)
    if asst is None:
        print("  WS replay didn't return the assistant msg at all")
        return False
    event_uuids = {
        ev.get("data", {}).get("uuid")
        for ev in (asst.get("events") or [])
        if isinstance(ev, dict)
    }
    if named_uuid not in event_uuids:
        print(f"  named event missing from WS replay: events={event_uuids}")
        return False
    if orphan_uuid not in event_uuids:
        print(f"  ORPHAN event missing from WS replay (DIV-1 regression): events={event_uuids}")
        return False

    # Cross-check: REST projection on the SAME tree returns the same
    # asst events. If WS and REST disagree, INV-15 is broken regardless
    # of whether orphans land — the projections must be identical.
    from main import _reconcile_msg_events_from_jsonl
    rest_tree = session_manager.get_root_tree(app_sid)
    _reconcile_msg_events_from_jsonl(rest_tree)
    rest_asst = next(
        (m for m in (rest_tree.get("messages") or []) if m.get("id") == asst_id),
        None,
    )
    rest_uuids = {
        ev.get("data", {}).get("uuid")
        for ev in (rest_asst.get("events") or [])
        if isinstance(rest_asst, dict) and isinstance(ev, dict)
    }
    if rest_uuids != event_uuids:
        print(f"  WS/REST projection diverged: ws={event_uuids} rest={rest_uuids}")
        return False
    return True


def test_recovery_finalize_does_not_bump_updated_at() -> bool:
    """`_finalize_sync` (run_recovery's finalize-time replay) must NOT
    bump `updated_at`. It re-projects the run's already-happened events
    and stamps completion state — not user activity — so it must not
    reorder the session in the sidebar. Pre-fix the replay ran unwrapped
    (default bump=True) AND its completion batch defaulted to bump=True,
    so every recovered/finalized session jumped to the top."""
    from run_recovery import _finalize_sync

    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    claude_sid = str(uuid.uuid4())
    run_id = _seed_orphan_run(
        app_sid, claude_sid,
        [_make_assistant_text_event("recovered finalize text")],
        mode="native",
    )

    before = session_manager.get(app_sid)["updated_at"]
    sess = session_manager.get(app_sid)
    last_asst = next(m for m in sess["messages"] if m.get("role") == "assistant")

    _finalize_sync(
        persist_sid=app_sid, run_id=run_id, mode="native",
        claude_sid=claude_sid, sess=sess, last_asst=last_asst,
        msg_id=asst_id, cancelled=False,
    )

    after = session_manager.get(app_sid)
    content = next(
        (m.get("content") or "") for m in after["messages"]
        if m.get("role") == "assistant"
    )
    if "recovered finalize text" not in content:
        print(f"  finalize did not replay content (no real work): {content!r}")
        return False
    # finalize must never move updated_at FORWARD to a fresh wall-clock
    # (the original reorder bug). With bump=False + the re-ingestion
    # repair it either stays put or is set back to the real last-activity
    # time, which is always <= the pre-finalize value.
    if after["updated_at"] > before:
        print(
            f"  finalize moved updated_at forward: {before!r} -> "
            f"{after['updated_at']!r}"
        )
        return False
    return True


def test_reconcile_does_not_bump_updated_at() -> bool:
    """Reconcile (the REST GET / WS-replay projection) re-derives
    msg.content from events.jsonl. That re-derivation must NOT bump
    `updated_at` — reconcile is a read-time projection, not user
    activity, so it must not reorder the sidebar. Pre-fix
    `update_running_content` inside the reconcile body defaulted to
    bump=True."""
    from main import _reconcile_msg_events_from_jsonl

    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    session_manager.set_streaming(app_sid, asst_id, False)

    # Seed events.jsonl with assistant text the persisted content lacks,
    # so reconcile's content re-derivation actually fires.
    event_ingester.ingest(
        app_sid, sid=app_sid, event_type="agent_message",
        data={
            "type": "assistant", "uuid": str(uuid.uuid4()),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "reconciled text"}],
            },
        },
        source="claude_tailer", msg_id=asst_id,
    )

    before = session_manager.get(app_sid)["updated_at"]
    tree = session_manager.get_root_tree(app_sid)
    _reconcile_msg_events_from_jsonl(tree)

    after = session_manager.get(app_sid)
    content = next(
        (m.get("content") or "") for m in after["messages"]
        if m.get("id") == asst_id
    )
    if content != "reconciled text":
        print(f"  reconcile did not re-derive content (no real work): {content!r}")
        return False
    if after["updated_at"] != before:
        print(
            f"  reconcile bumped updated_at: {before!r} -> {after['updated_at']!r}"
        )
        return False
    return True


def test_reingest_repairs_spurious_updated_at() -> bool:
    """Re-ingestion must repair `updated_at` to the session's real last-
    activity time. A session whose `updated_at` was spuriously bumped
    (the original sidebar-reorder bug) must, after re-ingestion, sort by
    its true last activity (max of the re-ingested event timestamps and
    the last message ts) — NOT the spurious value. Pre-fix, re-ingestion
    left the bad `updated_at` untouched."""
    from run_recovery import _finalize_sync

    app_sid, _, asst_id = _seed_session_with_streaming_assistant("native")
    sess0 = session_manager.get(app_sid)
    # Real last-activity time = the last message's timestamp (the replayed
    # event's 2024 ts below predates it, so the max() repair must pick this).
    real_last = next(
        (m["timestamp"] for m in reversed(sess0["messages"])
         if isinstance(m.get("timestamp"), str) and m["timestamp"]),
        "",
    )

    # Simulate the original bug: a spuriously-future updated_at.
    session_manager.set_updated_at(app_sid, "2099-12-31T23:59:59.999999")

    claude_sid = str(uuid.uuid4())
    ev = _make_assistant_text_event("reingested body")
    ev["timestamp"] = "2024-01-01T00:00:00.000000"  # older than real_last
    run_id = _seed_orphan_run(app_sid, claude_sid, [ev], mode="native")

    sess = session_manager.get(app_sid)
    last_asst = next(m for m in sess["messages"] if m.get("role") == "assistant")
    _finalize_sync(
        persist_sid=app_sid, run_id=run_id, mode="native",
        claude_sid=claude_sid, sess=sess, last_asst=last_asst,
        msg_id=asst_id, cancelled=False,
    )

    after = session_manager.get(app_sid)["updated_at"]
    if after == "2099-12-31T23:59:59.999999":
        print(f"  re-ingest did not repair spurious updated_at (still {after!r})")
        return False
    # Repaired to the real last-activity time.
    if after != real_last:
        print(f"  re-ingest set updated_at to {after!r}, expected {real_last!r}")
        return False
    return True


def test_bracket_orphan_rows_does_not_swallow_new_turn_into_old_one() -> bool:
    """A message created mid-restart-race has zero named (msg_id-stamped)
    rows yet. Without a creation-time floor, `_bracket_orphan_rows` used to
    scan forward past it looking for the first message WITH named rows,
    leaving the OLDER message's ceiling unbounded — so an orphan row that
    actually belongs to the new (still-empty) message got swallowed into
    the older message's window instead, rendering as a stale duplicate
    there. `_events_seq_floor` (stamped at message creation by
    `session_manager.append_assistant_msg`) closes that window even before
    the new message has any named rows."""
    msg_old = {"id": "m-old"}
    msg_new = {"id": "m-new", "_events_seq_floor": 20}
    assistant_msgs = [(0, msg_old), (1, msg_new)]
    by_msg_id = {"m-old": [{"seq": 12}]}
    orphan_raw = [{"seq": 25, "data": {"uuid": "u-belongs-to-new"}}]

    out = _bracket_orphan_rows(assistant_msgs, by_msg_id, orphan_raw)

    if "m-old" in out:
        print(f"  orphan seq=25 swallowed into m-old (belongs to m-new): {out}")
        return False
    if out.get("m-new") != orphan_raw:
        print(f"  expected orphan bracketed onto m-new, got {out}")
        return False
    return True


TESTS = [
    ("native dead-orphan recovery does NOT add manager scope",
        test_native_recovery_does_not_add_manager_scope),
    ("manager dead-orphan recovery still pins manager.session_id",
        test_manager_recovery_still_pins_session_id),
    ("reconcile re-extracts content from events.jsonl-only assistant text",
        test_reconcile_reextracts_content_from_jsonl_only_events),
    ("reconcile repairs empty content when event counts match",
        test_reconcile_repairs_empty_content_when_event_counts_match),
    ("reconcile does NOT clobber streaming msg content",
        test_reconcile_does_not_clobber_streaming_msg_content),
    ("DIV-1: WS messages_replay carries orphan events (parity with REST)",
        test_ws_replay_carries_orphan_events_DIV_1),
    ("recovery finalize does NOT bump updated_at (no sidebar reorder)",
        test_recovery_finalize_does_not_bump_updated_at),
    ("reconcile does NOT bump updated_at (no sidebar reorder)",
        test_reconcile_does_not_bump_updated_at),
    ("re-ingestion repairs spurious updated_at to last-activity ts",
        test_reingest_repairs_spurious_updated_at),
    ("bracket_orphan_rows does not swallow a new turn's orphan into the old turn",
        test_bracket_orphan_rows_does_not_swallow_new_turn_into_old_one),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                if asyncio.iscoroutinefunction(fn):
                    ok = asyncio.run(fn())
                else:
                    ok = fn()
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
