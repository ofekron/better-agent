"""Regression tests for the unified `OrchestrationStrategy.apply_event`
delta-applier.

Pins the contract that one code path mutates the session render tree
for both live ingest and disk replay:

  1. Idempotence on event uuid — re-applying a uuid-bearing event
     does not duplicate it on msg.events.
  2. No-uuid wire markers (turn_start, turn_complete, system
     frames) are NOT appended to msg.events even though they drive
     derived state and reach events.jsonl.
  3. Live ingest tags every events.jsonl entry with msg_id.
  4. source_is_provider_stream=True writes to events.jsonl; source_is_provider_stream=False does not.
  5. `_after_event` pins `msg.agent_session_id` from the sid holder
     (one flat shape for both manager and native modes).
  6. Both modes write events to flat `msg.events` and never carry a
     `manager` scope on the msg.
  7. REST reconcile (`_reconcile_msg_events_from_jsonl` semantics)
     picks up the deterministic per-msg_id slice from events.jsonl
     and applies via `apply_event(source_is_provider_stream=False)`.
  8. Shape normalization: manager_event-wrapped events land on
     msg.events as their inner agent_message, mirroring the
     frontend's expected shape.

Run with:
    cd backend && .venv/bin/python scripts/test_apply_event_unified.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-apply-event-")

from event_ingester import event_ingester  # noqa: E402
from event_shape import frontend_events_from_journal_rows  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── helpers ──────────────────────────────────────────────────────

def _mk_session(mode: str) -> tuple[str, dict]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode=mode, source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy(mode)
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    return sid, scaffold


def _drain_journal(sid: str, expected_seq: int, timeout: float = 2.0) -> None:
    """Wait for apply_event's fire-and-forget journal writes to land."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event_ingester.max_seq_for_sid(sid, sid) >= expected_seq:
            return
        time.sleep(0.01)
    raise TimeoutError(
        f"journal not drained after {timeout}s: "
        f"expected seq>={expected_seq}, got {event_ingester.max_seq_for_sid(sid, sid)}"
    )


def _manager_event(uuid: str, text: str = "x") -> dict:
    """Live-ingest shape: agent_message wrapped under manager_event."""
    return {
        "type": "manager_event",
        "data": {
            "event": {
                "type": "agent_message",
                "data": {
                    "uuid": uuid,
                    "type": "assistant",
                    "message": {"content": text},
                },
            },
        },
    }


def _agent_message(uuid: str, text: str = "x") -> dict:
    """Recovery-replay shape: raw agent_message (no manager_event wrap)."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": text},
        },
    }


def _list_for(msg: dict, mode: str) -> list:
    return msg.get("events") or []


# ─── tests ───────────────────────────────────────────────────────

def test_idempotent_reapply_does_not_duplicate() -> bool:
    """apply_event called twice with the same uuid leaves msg.events
    at length 1 — the dedup-by-uuid guard short-circuits the second
    call before the append fires."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    ev = _manager_event("u1")
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=False)
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=False)
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    return len(asst["events"]) == 1


def test_idempotent_reapply_repairs_empty_content() -> bool:
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(root_id=sid)
    event = _manager_event("content-repair", "repaired answer")

    strategy.apply_event(
        app_session_id=sid,
        msg=msg,
        event=event,
        ctx=ctx,
        source_is_provider_stream=False,
    )
    msg["content"] = ""
    msg["_content_dirty"] = True
    strategy.apply_event(
        app_session_id=sid,
        msg=msg,
        event=event,
        ctx=ctx,
        source_is_provider_stream=False,
    )

    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    return (
        len(asst["events"]) == 1
        and asst.get("content") == "repaired answer"
        and asst.get("_content_dirty") is False
    )


def test_wire_markers_skip_msg_events() -> bool:
    """turn_start / turn_complete have no claude uuid and must
    NOT be appended to msg.events — they're wire-routing frames the
    frontend's flattenClaudeMessages ignores anyway. Re-applying them
    must remain a no-op (else REST reconcile multiplies them)."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    events = [
        {"type": "turn_start", "data": {"manager_session_id": "s1"}},
        _manager_event("u1"),
        {"type": "turn_complete", "data": {"session_id": "s1", "success": True}},
    ]
    for _ in range(3):  # re-apply triples them if the guard breaks
        for e in events:
            strategy.apply_event(app_session_id=sid, msg=msg, event=e,
                                 ctx=ctx, source_is_provider_stream=False)
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    if len(asst["events"]) != 1:
        print(f"  expected 1 uuid-bearing event, got {len(asst['events'])}")
        return False
    if asst["events"][0].get("type") != "agent_message":
        print(f"  expected agent_message shape, got {asst['events'][0]}")
        return False
    return True


def test_live_true_writes_events_jsonl_with_msg_id() -> bool:
    """Every events.jsonl row produced by apply_event(source_is_provider_stream=True) carries
    msg_id — so REST reconcile can pull a deterministic per-msg slice
    without heuristic turn-grouping."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg,
                         event={"type": "turn_start", "data": {"manager_session_id": "s1"}},
                         ctx=ctx, source_is_provider_stream=True)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_manager_event("u1"),
                         ctx=ctx, source_is_provider_stream=True)
    # source_is_provider_stream=True journals fire-and-forget onto the
    # per-root shard executor — drain it before reading events.jsonl.
    event_journal_writer.barrier_sync(sid)
    log, _, _ = event_ingester.read_events(sid)
    if len(log) != 2:
        print(f"  expected 2 events.jsonl rows, got {len(log)}")
        return False
    for entry in log:
        if entry.get("msg_id") != msg["id"]:
            print(f"  row missing msg_id: {entry}")
            return False
    return True


def test_live_false_skips_events_jsonl() -> bool:
    """apply_event(source_is_provider_stream=False) mutates the render tree but doesn't
    write to events.jsonl — that's how replay/reconcile use the same
    code without double-writing the source."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_manager_event("u1"),
                         ctx=ctx, source_is_provider_stream=False)
    log, _, _ = event_ingester.read_events(sid)
    if log:
        print(f"  events.jsonl should be empty, got {len(log)} rows")
        return False
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    return len(asst["events"]) == 1


def test_live_true_can_skip_journal_write() -> bool:
    """Journal-written projection needs provider-stream side effects
    without recursively appending the same event back to events.jsonl."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_manager_event("u1"),
                         ctx=ctx, source_is_provider_stream=True,
                         write_journal=False)
    event_journal_writer.barrier_sync(sid)
    log, _, _ = event_ingester.read_events(sid)
    if log:
        print(f"  events.jsonl should be empty, got {len(log)} rows")
        return False
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    return len(asst["events"]) == 1


def test_manager_after_event_pins_session_id() -> bool:
    """`_after_event` propagates the latest primary sid from the holder
    onto flat `msg.agent_session_id` — one flat shape for all modes."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    holder = {"id": None}
    ctx = ApplyEventCtx(manager_sid_holder=holder, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg,
                         event={"type": "turn_start", "data": {"manager_session_id": "sess-A"}},
                         ctx=ctx, source_is_provider_stream=False)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_manager_event("u1"),
                         ctx=ctx, source_is_provider_stream=False)
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    return asst["agent_session_id"] == "sess-A"


def test_native_has_no_manager_scope() -> bool:
    """Every mode writes events to flat msg.events and never carries a
    `manager` key on the scaffold."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_manager_event("u1"),
                         ctx=ctx, source_is_provider_stream=False)
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    if "manager" in asst:
        print(f"  native msg unexpectedly has manager scope: {asst}")
        return False
    return len(asst.get("events") or []) == 1


def test_shape_normalization_manager_event_unwraps() -> bool:
    """A live-ingest manager_event wrapper is normalized to its inner
    agent_message before landing on msg.events — same shape the
    recovery path stores raw — so the frontend's flattenClaudeMessages
    sees one uniform stream regardless of path of origin."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_manager_event("u1", "wrapped"),
                         ctx=ctx, source_is_provider_stream=False)
    strategy.apply_event(app_session_id=sid, msg=msg, event=_agent_message("u2", "raw"),
                         ctx=ctx, source_is_provider_stream=False)
    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    events = asst["events"]
    if len(events) != 2:
        print(f"  expected 2 events, got {len(events)}")
        return False
    for i, e in enumerate(events):
        if e.get("type") != "agent_message":
            print(f"  event[{i}] wrong shape: {e}")
            return False
    return True


def test_reconcile_per_msg_id_slice() -> bool:
    """Simulate events.jsonl having entries beyond what's on msg.events
    (e.g. last persist happened pre-crash). Reconcile re-applies them
    via apply_event(source_is_provider_stream=False) using a per-msg_id filter — no
    heuristic turn-grouping, no risk of cross-turn bleed."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")

    # Mimic a live write that landed in events.jsonl but never made it
    # onto msg.events (write-then-crash window).
    inner = _manager_event("u_jsonl")["data"]["event"]
    event_ingester.ingest(
        sid, sid=sid, event_type="manager_event",
        data={"event": inner}, source="orchestrator", msg_id=msg["id"],
    )

    # Reconcile: per-msg_id read, apply through strategy.apply_event.
    ws_events = event_ingester.read_ws_events(
        sid, sid_filter=sid, msg_id_filter=msg["id"],
    )
    ctx = ApplyEventCtx(root_id=sid)
    for ev in ws_events:
        strategy.apply_event(app_session_id=sid, msg=msg, event=ev,
                             ctx=ctx, source_is_provider_stream=False)

    fresh = session_manager.get(sid)
    asst = next(m for m in fresh["messages"] if m["role"] == "assistant")
    if len(asst["events"]) != 1:
        print(f"  expected 1 reconciled event, got {len(asst['events'])}")
        return False

    # And reconciling again is idempotent — second sweep doesn't dup.
    for ev in ws_events:
        strategy.apply_event(app_session_id=sid, msg=msg, event=ev,
                             ctx=ctx, source_is_provider_stream=False)
    fresh2 = session_manager.get(sid)
    asst2 = next(m for m in fresh2["messages"] if m["role"] == "assistant")
    return len(asst2["events"]) == 1


def test_user_claude_uuid_anchor_wired_from_both_shapes() -> bool:
    """The rewind-anchor (user_msg.agent_message_uuid) is wired by
    apply_event for both the live shape (manager_event wrapping a user
    claude entry) and the recovery shape (raw agent_message of type
    'user'). Same code path, two inputs, same outcome."""
    # Live wrap shape.
    sid_a, asst_a = _mk_session("manager")
    user_a = {"id": "u-a", "role": "user", "content": "hi", "events": []}
    session_manager.append_user_msg(sid_a, user_a)
    user_a_ref = session_manager.get(sid_a)["messages"][0]
    strategy_a = get_strategy("manager")
    live_user_evt = {
        "type": "manager_event",
        "data": {"event": {"type": "user", "uuid": "claude-uuid-1",
                            "message": {"content": "hi"}, "isSidechain": False}},
    }
    strategy_a.apply_event(app_session_id=sid_a, msg=asst_a, event=live_user_evt,
                           ctx=ApplyEventCtx(manager_sid_holder={"id": None},
                                             workers_list=[], user_msg=user_a_ref,
                                             root_id=sid_a),
                           source_is_provider_stream=False)
    refreshed_a = session_manager.get(sid_a)["messages"][0]
    if refreshed_a.get("agent_message_uuid") != "claude-uuid-1":
        print(f"  live shape failed to anchor uuid: {refreshed_a}")
        return False

    # Recovery raw shape.
    sid_b, asst_b = _mk_session("native")
    user_b = {"id": "u-b", "role": "user", "content": "hi", "events": []}
    session_manager.append_user_msg(sid_b, user_b)
    user_b_ref = session_manager.get(sid_b)["messages"][0]
    strategy_b = get_strategy("native")
    raw_user_evt = {
        "type": "agent_message",
        "data": {"type": "user", "uuid": "claude-uuid-2",
                 "message": {"content": "hi"}, "isSidechain": False},
    }
    strategy_b.apply_event(app_session_id=sid_b, msg=asst_b, event=raw_user_evt,
                           ctx=ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                                             user_msg=user_b_ref, root_id=sid_b),
                           source_is_provider_stream=False)
    refreshed_b = session_manager.get(sid_b)["messages"][0]
    if refreshed_b.get("agent_message_uuid") != "claude-uuid-2":
        print(f"  recovery shape failed to anchor uuid: {refreshed_b}")
        return False
    return True


def test_non_render_etypes_skip_msg_events_but_reach_events_jsonl() -> bool:
    """Non-render outer event types (`command_received`, `run_state`,
    `user_message_received`, `trace_step`) MUST NOT land on
    `msg.events` — those are audit-trail rows whose only home is
    `events.jsonl` (for the frontend's WS broadcast audit channel).

    Pins the leak that caused 97 REST `command_received` envelopes to
    pollute session `46bafa51…`'s assistant messages via
    `_reconcile_msg_events_from_jsonl` orphan-bracketing. The gate
    lives in `OrchestrationStrategy.apply_event` at the
    append/replace block on `msg.events`. The events.jsonl ingest
    tail still fires (source_is_provider_stream=True) so audit-trail visibility is
    preserved.
    """
    import uuid as _uuid
    # Parametrize across outer etypes that DON'T render. Each test
    # event carries a `data.uuid` — the property the old leak relied
    # on (uuid present → passed apply_event's dedup gate → appended).
    cases = ["command_received", "run_state", "user_message_received", "trace_step"]
    for etype in cases:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        # source_is_provider_stream=True path: should reach events.jsonl, NOT msg.events.
        ev = {"type": etype, "data": {"uuid": str(_uuid.uuid4()),
                                       "method": "POST", "path": "/api/x"}}
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev,
            ctx=ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                              user_msg=None, root_id=sid),
            source_is_provider_stream=True,
        )
        _drain_journal(sid, 1)
        refreshed = session_manager.get(sid)
        asst = next(m for m in refreshed["messages"] if m["id"] == msg["id"])
        if (asst.get("events") or []):
            print(f"  {etype}: msg.events should be empty, got {len(asst['events'])}")
            return False
        event_journal_writer.barrier_sync(sid)
        rows, _, _ = event_ingester.read_events(sid, limit=100)
        ej_outer = [r.get("type") for r in rows]
        if etype not in ej_outer:
            print(f"  {etype}: missing from events.jsonl (source_is_provider_stream=True must still ingest)")
            return False

        # source_is_provider_stream=False path: should NOT write events.jsonl AND NOT land on msg.events.
        sid2, msg2 = _mk_session("native")
        ev2 = {"type": etype, "data": {"uuid": str(_uuid.uuid4()),
                                        "method": "GET", "path": "/api/y"}}
        strategy.apply_event(
            app_session_id=sid2, msg=msg2, event=ev2,
            ctx=ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                              user_msg=None, root_id=sid2),
            source_is_provider_stream=False,
        )
        refreshed2 = session_manager.get(sid2)
        asst2 = next(m for m in refreshed2["messages"] if m["id"] == msg2["id"])
        if (asst2.get("events") or []):
            print(f"  {etype} (source_is_provider_stream=False): msg.events should be empty")
            return False
        rows2, _, _ = event_ingester.read_events(sid2, limit=100)
        if rows2:
            print(f"  {etype} (source_is_provider_stream=False): events.jsonl should be empty, got {len(rows2)}")
            return False
    return True


def test_same_uuid_streaming_update_dual_surface_dedup() -> bool:
    """Streaming-provider behavior: when a provider re-emits the SAME
    uuid with MUTATED data (Gemini-style cumulative-text streaming),
    the two dedup surfaces behave asymmetrically per CLAUDE.md
    "Dedup semantics differ by surface":

      - msg.events dedupes by uuid alone → `_replace_event` updates
        the existing entry in place. Stays length 1, content = latest.
      - events.jsonl dedupes by `uid:sha256(data)` (`event_ingester.py:307-320`)
        → mutated data → new row. Grows to 2 rows; the second row
        carries the LATEST snapshot.

    This is essential for crash recovery: after a restart, reconcile
    re-applies events.jsonl in seq order; the latest row's data
    "last-write-wins" against the existing `msg.events` entry, so
    the render tree preserves the latest snapshot. If events.jsonl
    only held the FIRST snapshot, reconcile would REGRESS the in-memory
    "pong" back to the on-disk "p" — see
    `test_reconcile_after_streaming_preserves_latest_snapshot` below.
    """
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    uid = "stable-streaming-uuid"
    ev_p = {"type": "agent_message",
            "data": {"uuid": uid, "type": "assistant",
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "p"}]}}}
    ev_pong = {"type": "agent_message",
               "data": {"uuid": uid, "type": "assistant",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "pong"}]}}}
    ctx = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev_p,
                         ctx=ctx, source_is_provider_stream=True)
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev_pong,
                         ctx=ctx, source_is_provider_stream=True)
    _drain_journal(sid, 2)

    refreshed = session_manager.get(sid)
    asst = next(m for m in refreshed["messages"] if m["id"] == msg["id"])
    evs = asst.get("events") or []
    if len(evs) != 1:
        print(f"  msg.events len: expected 1 (replace-by-uuid), got {len(evs)}")
        return False
    final_text = (((evs[0].get("data") or {}).get("message") or {})
                  .get("content") or [{}])[0].get("text")
    if final_text != "pong":
        print(f"  expected msg.events[0] text='pong' (last write wins), got {final_text!r}")
        return False

    event_journal_writer.barrier_sync(sid)
    rows, _, _ = event_ingester.read_events(sid, limit=100)
    am_rows = [r for r in rows if r.get("type") == "agent_message"]
    if len(am_rows) != 2:
        print(f"  events.jsonl agent_message rows: expected 2 "
              f"(uid:sha256(data) dedup, mutated data → new row), got {len(am_rows)}")
        return False
    # The second row's data MUST carry the latest snapshot, so reconcile-
    # replay's last-write-wins lands on "pong".
    last_data = am_rows[-1].get("data") or {}
    last_text = (((last_data.get("message") or {}).get("content")) or [{}])[0].get("text")
    if last_text != "pong":
        print(f"  events.jsonl[-1] data must carry latest snapshot 'pong', got {last_text!r}")
        return False
    return True


def test_lifecycle_notice_same_uuid_replaces_render_tree_and_projects_latest() -> bool:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    uid = "codex-context-compacted-uuid"
    notice = {
        "type": "lifecycle_notice",
        "data": {
            "kind": "context_compacted",
            "message": "Context compacted",
        },
        "uuid": uid,
    }
    detail = {
        "type": "lifecycle_notice",
        "data": {
            "kind": "compacted",
            "message": "Context compacted",
            "replacement_history": [{"role": "user", "text": "original ask"}],
        },
        "uuid": uid,
    }
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid,
        msg=msg,
        event=notice,
        ctx=ctx,
        source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid,
        msg=msg,
        event=detail,
        ctx=ctx,
        source_is_provider_stream=True,
    )
    _drain_journal(sid, 2)

    refreshed = session_manager.get(sid)
    asst = next(m for m in refreshed["messages"] if m["id"] == msg["id"])
    evs = asst.get("events") or []
    if len(evs) != 1:
        print(f"  msg.events len: expected 1 lifecycle notice, got {len(evs)}")
        return False
    if evs[0].get("uuid") != uid:
        print(f"  lifecycle notice lost top-level uuid: {evs[0].get('uuid')!r}")
        return False
    data = evs[0].get("data") or {}
    if data.get("kind") != "compacted" or data.get("replacement_history") != [{"role": "user", "text": "original ask"}]:
        print(f"  render tree kept wrong lifecycle data: {data!r}")
        return False

    rows, _, _ = event_ingester.read_events(sid, limit=10)
    lifecycle_rows = [row for row in rows if row.get("type") == "lifecycle_notice"]
    if len(lifecycle_rows) != 2:
        print(f"  expected 2 journal lifecycle snapshots, got {len(lifecycle_rows)}")
        return False
    if any((row.get("data") or {}).get("uuid") != uid for row in lifecycle_rows):
        print(f"  journal rows did not preserve lifecycle uuid: {lifecycle_rows!r}")
        return False
    projected = frontend_events_from_journal_rows(lifecycle_rows)
    if len(projected) != 1:
        print(f"  frontend projection should coalesce to 1 row, got {len(projected)}")
        return False
    projected_data = projected[0].get("data") or {}
    if projected_data.get("kind") != "compacted":
        print(f"  frontend projection kept stale lifecycle notice: {projected_data!r}")
        return False
    return True


def test_reconcile_after_streaming_preserves_latest_snapshot() -> bool:
    """End-to-end regression for the "render tree regresses to older
    snapshot" bug an earlier hostile review surfaced.

    Scenario:
      1. Streaming live ingest: same uuid emits "p" then "pong" →
         msg.events ends with "pong", events.jsonl has 2 rows.
      2. "Backend restart": the in-memory `_seen_uuids` cache is
         dropped; events.jsonl on disk is the source of truth.
      3. Frontend reconnect → `_reconcile_msg_events_from_jsonl`
         re-walks events.jsonl and re-applies via
         `apply_event(source_is_provider_stream=False)`.
      4. Result MUST be msg.events still carries "pong" (latest
         snapshot wins), NOT regressed to "p".

    Pre-fix (with early-return suppressing the second ingest):
    events.jsonl only had the "p" row; reconcile would re-apply it
    against existing "pong"; `existing != normalized` would trigger
    `_replace_event` with the older snapshot, REGRESSING the render
    tree.
    """
    sid, msg = _mk_session("native")
    msg_id = msg["id"]
    strategy = get_strategy("native")
    uid = "stable-streaming-uuid-2"
    ev_p = {"type": "agent_message",
            "data": {"uuid": uid, "type": "assistant",
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "p"}]}}}
    ev_pong = {"type": "agent_message",
               "data": {"uuid": uid, "type": "assistant",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "pong"}]}}}
    ctx = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                        user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev_p,
                         ctx=ctx, source_is_provider_stream=True)
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev_pong,
                         ctx=ctx, source_is_provider_stream=True)
    # Drain the fire-and-forget journal writes before simulating the
    # restart — reconcile reads events.jsonl as the source of truth.
    event_journal_writer.barrier_sync(sid)
    # Simulate a clean restart: drop the in-memory dedup caches so
    # reconcile starts fresh from disk.
    event_ingester.close_all()
    # Trigger reconcile (the path the REST GET /api/sessions/{sid}
    # handler runs after every backend restart).
    from render_tree_hydrate import reconcile_msg_events_from_jsonl as _reconcile_msg_events_from_jsonl
    # Force msg.events to look "lagging" so the reconcile path actually
    # walks events.jsonl — emulates the post-restart state where the
    # session was hydrated from disk and msg.events == disk state.
    with session_manager.batch(sid):
        sess = session_manager.get(sid)
        m = next(mm for mm in sess["messages"] if mm["id"] == msg_id)
        # Persisted state already carries "pong" (via _replace_event
        # during live). DON'T clobber it — reconcile must merge into
        # this state without regressing it.
        session_manager.set_streaming(sid, msg_id, False)
    tree = session_manager.get_root_tree(sid)
    _reconcile_msg_events_from_jsonl(tree)
    after = session_manager.get(sid)
    asst = next(mm for mm in after["messages"] if mm["id"] == msg_id)
    evs = asst.get("events") or []
    if len(evs) != 1:
        print(f"  msg.events len after reconcile: expected 1, got {len(evs)}")
        return False
    final_text = (((evs[0].get("data") or {}).get("message") or {})
                  .get("content") or [{}])[0].get("text")
    if final_text != "pong":
        print(f"  RENDER REGRESSED: msg.events[0] text expected 'pong' "
              f"(latest snapshot survives reconcile), got {final_text!r}")
        return False
    return True


def test_reconcile_fills_partial_finalized_msg_from_orphan_tail() -> bool:
    """Regression for: a FINALIZED assistant msg with a PARTIAL cache
    event list is never re-hydrated, freezing the render tree at the
    last live-streamed event (e.g. ending at a `Bash` tool_use, with
    the closing assistant `text` missing).

    Scenario (warm backend, live turn left a gap):
      1. Live ingest applies ONE event (the Bash tool_use) → it lands
         on msg.events AND in events.jsonl (msg_id stamped).
      2. The turn's tail (final `text`) reaches events.jsonl via the
         ORPHAN ingest path (event_ingester.ingest with the same
         msg_id) but is NEVER applied to the cache msg.
      3. Msg is finalized (isStreaming=False).
      4. Reconcile runs (REST GET path).

    Pre-fix: the hydrate "finalized + has events → skip" fast path saw
    a non-empty list and returned before reading events.jsonl, so the
    text never reconciled — msg.events stayed at length 1 (Bash only).
    Post-fix: hydrate always reads the journal; the count-match guard
    sees jsonl_count(2) > msg_count(1) and applies the missing text.
    """
    sid, msg = _mk_session("native")
    msg_id = msg["id"]
    strategy = get_strategy("native")

    bash_uid = "partial-bash-uuid"
    text_uid = "partial-text-uuid"
    ev_bash = {"type": "agent_message",
               "data": {"uuid": bash_uid, "type": "assistant",
                        "message": {"role": "assistant",
                                    "content": [{"type": "tool_use",
                                                 "name": "Bash", "input": {}}]}}}
    ctx = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                        user_msg=None, root_id=sid)
    # 1. Live stream applies ONLY the Bash → cache msg + events.jsonl.
    strategy.apply_event(app_session_id=sid, msg=msg, event=ev_bash,
                         ctx=ctx, source_is_provider_stream=True)
    # Drain the fire-and-forget journal write so the Bash row is on
    # disk BEFORE the direct ingest below — keeps jsonl seq order
    # deterministic (Bash, then text).
    event_journal_writer.barrier_sync(sid)
    # 2. Tail (final text) lands in events.jsonl via the orphan ingest
    #    path with the SAME msg_id, but never touches the cache msg.
    #    Legacy-coverage: "claude_tailer" is what the pre-fork-identity
    #    PRIMARY tailer stamped; real disks hold such rows as the sole
    #    copy of primary content, so reconcile must keep rendering them
    #    (the fork discriminator is FORK_BACKUP_SOURCE="fork_backup").
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={"uuid": text_uid, "type": "assistant",
              "message": {"role": "assistant",
                          "content": [{"type": "text",
                                       "text": "final answer"}]}},
        source="claude_tailer", msg_id=msg_id,
    )
    # 3. Finalize the msg (post-turn) and simulate a clean cache state.
    event_ingester.close_all()
    with session_manager.batch(sid):
        session_manager.set_streaming(sid, msg_id, False)

    # Sanity: cache msg holds ONLY the Bash before reconcile.
    pre = session_manager.get(sid)
    pre_asst = next(m for m in pre["messages"] if m["id"] == msg_id)
    if len(pre_asst.get("events") or []) != 1:
        print(f"  setup wrong: expected 1 cache event pre-reconcile, "
              f"got {len(pre_asst.get('events') or [])}")
        return False

    # 4. Reconcile (the REST GET path).
    from render_tree_hydrate import reconcile_msg_events_from_jsonl as _reconcile_msg_events_from_jsonl
    tree = session_manager.get_root_tree(sid)
    _reconcile_msg_events_from_jsonl(tree)

    after = session_manager.get(sid)
    asst = next(m for m in after["messages"] if m["id"] == msg_id)
    evs = asst.get("events") or []
    if len(evs) != 2:
        print(f"  FROZEN AT PARTIAL: expected 2 events after reconcile "
              f"(Bash + text), got {len(evs)}")
        return False
    last_blocks = (((evs[-1].get("data") or {}).get("message") or {})
                   .get("content") or [{}])
    last_text = last_blocks[0].get("text")
    if last_text != "final answer":
        print(f"  expected closing text 'final answer', got {last_text!r}")
        return False
    return True


def test_convergence_manager_event_and_agent_message_write_identical_jsonl() -> bool:
    """Convergence invariant: the live path (manager_event wrapper) and
    the restore path (raw agent_message) must produce IDENTICAL entries
    in events.jsonl — same type, same data, same msg_id.  Before the fix,
    the live path wrote type="manager_event" with wrapped data while the
    restore path wrote type="agent_message" with raw data, violating the
    invariant and causing ~2.7x event duplication."""
    uid = "convergence-test-uuid"
    text = "convergence body"

    # Live path: manager_event wrapper
    sid1, msg1 = _mk_session("native")
    strategy = get_strategy("native")
    ctx1 = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                         user_msg=None, root_id=sid1)
    ev_live = _manager_event(uid, text)
    strategy.apply_event(app_session_id=sid1, msg=msg1, event=ev_live,
                         ctx=ctx1, source_is_provider_stream=True)
    _drain_journal(sid1, 1)

    event_journal_writer.barrier_sync(sid1)
    rows1, _, _ = event_ingester.read_events(sid1, limit=100)
    am1 = [r for r in rows1 if r.get("type") == "agent_message"
           and (r.get("data") or {}).get("uuid") == uid]
    me1 = [r for r in rows1 if r.get("type") == "manager_event"]

    # Restore path: raw agent_message
    sid2, msg2 = _mk_session("native")
    ctx2 = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                         user_msg=None, root_id=sid2)
    ev_restore = _agent_message(uid, text)
    strategy.apply_event(app_session_id=sid2, msg=msg2, event=ev_restore,
                         ctx=ctx2, source_is_provider_stream=True)
    _drain_journal(sid2, 1)

    event_journal_writer.barrier_sync(sid2)
    rows2, _, _ = event_ingester.read_events(sid2, limit=100)
    am2 = [r for r in rows2 if r.get("type") == "agent_message"
           and (r.get("data") or {}).get("uuid") == uid]

    # 1. Live path must NOT write manager_event — it normalizes.
    if me1:
        print(f"  live path wrote {len(me1)} manager_event row(s) — "
              f"should normalize to agent_message")
        return False

    # 2. Both paths must write exactly 1 agent_message row.
    if len(am1) != 1 or len(am2) != 1:
        print(f"  expected 1 agent_message row each, got {len(am1)} / {len(am2)}")
        return False

    # 3. The rows must be identical in type + data (msg_id differs by
    #    scaffold, so compare data payload).
    d1 = am1[0].get("data")
    d2 = am2[0].get("data")
    if d1 != d2:
        print(f"  data mismatch:\n    live:   {json.dumps(d1, sort_keys=True)[:200]}\n"
              f"    restore:{json.dumps(d2, sort_keys=True)[:200]}")
        return False

    # 4. Both must carry msg_id.
    if not am1[0].get("msg_id") or not am2[0].get("msg_id"):
        print(f"  missing msg_id: source_is_provider_stream={am1[0].get('msg_id')} restore={am2[0].get('msg_id')}")
        return False

    return True


def test_live_path_sets_attention_marker_from_raw_tag() -> bool:
    """Regression: `save_ws_callback` (turn_manager.py) calls
    `prepare_provider_event_for_journal` (which rewrites/strips the
    `<TAG>` wrapper out of the event's data in place, for journal
    persistence + display styling) BEFORE calling `apply_event` on
    that SAME event dict. `apply_event`'s own marker detection reads
    `norm_data`, which by then has already been stripped — so the
    live path silently never called `session_manager.set_marker`,
    even though the emitted text visibly carried the tag. Pins that
    `prepare_provider_event_for_journal` applies the marker itself,
    from genuinely raw text, before the strip."""
    import file_ref_resolver
    import session_store

    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(manager_sid_holder=None, workers_list=[],
                        user_msg=None, root_id=sid)

    file_ref_resolver.set_tag_rules([{
        "tag": "ALL_TASKS__DONE",
        "_extension_id": "test.user-attention",
        "strip_wrapper": True,
        "marker": {"color": "#2563eb", "tooltip": "All tasks done"},
    }])
    try:
        # Same event dict, same order as `save_ws_callback`: prepare-for-
        # journal (mutates in place) runs BEFORE apply_event sees it.
        ev = _agent_message("marker-uuid", "<ALL_TASKS__DONE>done</ALL_TASKS__DONE>")
        strategy.prepare_provider_event_for_journal(app_session_id=sid, event=ev)
        strategy.apply_event(app_session_id=sid, msg=msg, event=ev,
                             ctx=ctx, source_is_provider_stream=True)

        marker = session_store._markers_for_session(sid).get("test.user-attention")
        if marker is None:
            print("  no marker persisted for session — live path lost the tag")
            return False
        if marker.get("tag") != "ALL_TASKS__DONE":
            print(f"  wrong marker tag persisted: {marker!r}")
            return False
        return True
    finally:
        file_ref_resolver.set_tag_rules([])


def test_worker_event_routes_to_existing_panel_owner() -> bool:
    sid, owner_msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    delegation_id = "del-worker-owner"

    strategy.apply_event(
        app_session_id=sid,
        msg=owner_msg,
        event={
            "type": "worker_start",
            "data": {
                "delegation_id": delegation_id,
                "worker_session_id": "worker-session",
                "worker_description": "worker",
            },
        },
        ctx=ctx,
        source_is_provider_stream=True,
    )

    later_msg = get_strategy("manager").build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, later_msg)
    strategy.apply_event(
        app_session_id=sid,
        msg=later_msg,
        event={
            "type": "worker_event",
            "data": {
                "delegation_id": delegation_id,
                "event": _agent_message("worker-inner", "worker output"),
            },
        },
        ctx=ctx,
        source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(sid)

    fresh = session_manager.get(sid)
    messages = fresh.get("messages") or []
    owner = next(m for m in messages if m.get("id") == owner_msg["id"])
    later = next(m for m in messages if m.get("id") == later_msg["id"])
    panel = next(
        w for w in owner.get("workers") or []
        if w.get("delegation_id") == delegation_id
    )
    rows, _, _ = event_ingester.read_events(sid)
    worker_row = next(
        (r for r in rows if r.get("type") == "worker_event"),
        None,
    )

    ok = (
        len(panel.get("events") or []) == 1
        and not (later.get("workers") or [])
        and worker_row is not None
        and worker_row.get("msg_id") == owner_msg["id"]
    )
    if not ok:
        print(
            f"  owner events={len(panel.get('events') or [])}; "
            f"later workers={len(later.get('workers') or [])}; "
            f"worker_row_msg={worker_row.get('msg_id') if worker_row else None}"
        )
    return ok


def test_hydration_recovers_legacy_worker_event_owner() -> bool:
    sid, owner_msg = _mk_session("manager")
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
    delegation_id = "del-legacy-owner"

    strategy.apply_event(
        app_session_id=sid,
        msg=owner_msg,
        event={
            "type": "worker_start",
            "data": {
                "delegation_id": delegation_id,
                "worker_session_id": "worker-session",
                "worker_description": "worker",
            },
        },
        ctx=ctx,
        source_is_provider_stream=False,
    )
    later_msg = get_strategy("manager").build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, later_msg)
    event_ingester.ingest(
        sid,
        sid,
        "worker_event",
        {
            "delegation_id": delegation_id,
            "event": _agent_message("legacy-worker-inner", "legacy output"),
        },
        source="provider_stream",
        msg_id=later_msg["id"],
    )

    root = session_manager._load_root(sid, hydrate_events=False)
    snapshot = session_manager._compute_messages_snapshot(sid, sid, root)
    owner = next(
        m for m in snapshot["messages"]
        if m.get("id") == owner_msg["id"]
    )
    panel = next(
        w for w in owner.get("workers") or []
        if w.get("delegation_id") == delegation_id
    )

    ok = len(panel.get("events") or []) == 1
    if not ok:
        print(f"  hydrated owner events={len(panel.get('events') or [])}")
    return ok


TESTS = [
    ("idempotent re-apply does not duplicate", test_idempotent_reapply_does_not_duplicate),
    ("idempotent re-apply repairs empty content", test_idempotent_reapply_repairs_empty_content),
    ("no-uuid wire markers skip msg.events", test_wire_markers_skip_msg_events),
    ("source_is_provider_stream=True tags events.jsonl with msg_id", test_live_true_writes_events_jsonl_with_msg_id),
    ("source_is_provider_stream=False does not write events.jsonl", test_live_false_skips_events_jsonl),
    ("source_is_provider_stream=True can skip journal write", test_live_true_can_skip_journal_write),
    ("manager _after_event pins session_id", test_manager_after_event_pins_session_id),
    ("native has no manager scope", test_native_has_no_manager_scope),
    ("manager_event shape normalizes to agent_message", test_shape_normalization_manager_event_unwraps),
    ("REST reconcile picks per-msg_id slice and is idempotent", test_reconcile_per_msg_id_slice),
    ("user_claude_uuid anchor wired from both shapes", test_user_claude_uuid_anchor_wired_from_both_shapes),
    ("non-render etypes skip msg.events", test_non_render_etypes_skip_msg_events_but_reach_events_jsonl),
    ("same-uuid streaming update: render replaces; jsonl appends",
        test_same_uuid_streaming_update_dual_surface_dedup),
    ("same-uuid lifecycle notice update: render/project latest only",
        test_lifecycle_notice_same_uuid_replaces_render_tree_and_projects_latest),
    ("reconcile after streaming preserves latest snapshot (no regression)",
        test_reconcile_after_streaming_preserves_latest_snapshot),
    ("reconcile fills partial finalized msg from orphan tail",
        test_reconcile_fills_partial_finalized_msg_from_orphan_tail),
    ("convergence: manager_event and agent_message write identical jsonl",
        test_convergence_manager_event_and_agent_message_write_identical_jsonl),
    ("live path sets attention marker from raw tag before strip",
        test_live_path_sets_attention_marker_from_raw_tag),
    ("worker_event routes to existing panel owner",
        test_worker_event_routes_to_existing_panel_owner),
    ("hydration recovers legacy worker_event owner",
        test_hydration_recovers_legacy_worker_event_owner),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
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
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
