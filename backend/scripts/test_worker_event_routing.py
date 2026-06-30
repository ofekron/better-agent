"""Regression test for `apply_event` routing of `worker_event` frames.

Pre-existing bug since commit c3a8536 (May 20): `apply_event` extracts
the inner agent_message uuid from worker_event via `_event_uuid` and
appends the OUTER worker_event wrapper to `msg.events`. The
correct destination is the matching worker panel's events list, looked
up by `delegation_id` in `msg.workers`.

This test locks the fix:

  A. live worker_event with matching panel → panel.events grew by 1;
     msg.events is EMPTY; events.jsonl has 1 row of type
     "worker_event" with source "apply_event".
  B. same worker_event again → idempotent; panel.events length unchanged.
  C. same UUID, mutated data → entry replaced in-place.
  D. worker_event with no matching panel → graceful no-op; parent msg.events
     still EMPTY; no crash.
  E. worker_event with source_is_provider_stream=False → panel mutated; NO events.jsonl row;
     `file_ref_resolver.rewrite_event_data` NOT called (monkeypatch spy).
  F. panel.events[-1] is deep-equal to the INNER agent_message dict
     (the unwrap shape), not the outer worker_event wrapper.

Run with:
    cd backend && .venv/bin/python scripts/test_worker_event_routing.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-worker-event-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
import config_store  # noqa: E402
import render_stub  # noqa: E402
import session_store  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from turn_manager import TurnManager  # noqa: E402


config_store.get_default_provider = lambda: {
    "id": "test-claude",
    "kind": "claude",
    "default_model": "sonnet",
}


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── helpers ──────────────────────────────────────────────────────

def _mk_session_with_panel(delegation_id: str) -> tuple[str, str, str, dict]:
    """Create a manager-mode session, append a streaming assistant_msg
    with one worker panel matching `delegation_id`. Returns
    `(sid, root_id, msg_id, panel_ref)`."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["isStreaming"] = True
    panel = {
        "delegation_id": delegation_id,
        "worker_session_id": "ws_test",
        "worker_description": "test worker",
        "is_new": False,
        "instructions_preview": "",
        "events": [],
        "jsonl_path": None,
        "new_byte_offset": None,
        "fork_agent_sid": None,
        "token_usage": None,
    }
    scaffold["workers"] = [panel]
    session_manager.append_assistant_msg(sid, scaffold)
    root_id = session_manager._root_id_for(sid)
    # Re-fetch the live panel ref so assertions see persisted state.
    fresh = session_manager.get(sid) or {}
    msg = next(m for m in fresh["messages"] if m.get("id") == scaffold["id"])
    live_panel = msg["workers"][0]
    return sid, root_id, scaffold["id"], live_panel


def _worker_event(delegation_id: str, uuid: str, text: str) -> dict:
    """Mimic the shape sent by `_delegation.py:604`."""
    return {
        "type": "worker_event",
        "data": {
            "delegation_id": delegation_id,
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


def _mgr_events(sid: str, msg_id: str) -> list:
    sess = session_manager.get(sid) or {}
    for m in sess.get("messages") or []:
        if m.get("id") == msg_id:
            return (m.get("events")) or []
    return []


def _panel_events(sid: str, msg_id: str, delegation_id: str) -> list:
    sess = session_manager.get(sid) or {}
    for m in sess.get("messages") or []:
        if m.get("id") == msg_id:
            for p in m.get("workers") or []:
                if p.get("delegation_id") == delegation_id:
                    return p.get("events") or []
    return []


def _events_jsonl_for(root_id: str, sid: str) -> list[dict]:
    raw, _, _ = event_ingester.read_events(
        root_id, limit=10_000, sid_filter=sid,
    )
    return raw


def _apply(
    sid: str,
    msg_id: str,
    root_id: str,
    event: dict,
    *,
    source_is_provider_stream: bool,
) -> None:
    """Re-fetch the msg ref (apply_event needs `msg.id`) and apply."""
    sess = session_manager.get(sid) or {}
    msg = next(m for m in sess["messages"] if m.get("id") == msg_id)
    ctx = ApplyEventCtx(root_id=root_id)
    get_strategy("manager").apply_event(
        app_session_id=sid, msg=msg, event=event, ctx=ctx,
        source_is_provider_stream=source_is_provider_stream,
    )
    if source_is_provider_stream:
        event_journal_writer.barrier_sync(root_id)


# ─── subtests ─────────────────────────────────────────────────────

def test_a_routes_to_panel_not_manager() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_A")
    rows_before = len(_events_jsonl_for(root_id, sid))

    _apply(sid, msg_id, root_id, _worker_event("del_A", "uuid-A", "hi"), source_is_provider_stream=True)

    panel_evs = _panel_events(sid, msg_id, "del_A")
    mgr_evs = _mgr_events(sid, msg_id)
    rows_after = _events_jsonl_for(root_id, sid)
    new_rows = [r for r in rows_after if (r.get("data") or {}).get("delegation_id") == "del_A"]

    ok = (
        len(panel_evs) == 1
        and len(mgr_evs) == 0
        and len(rows_after) == rows_before + 1
        and len(new_rows) == 1
        and new_rows[0].get("type") == "worker_event"
        and new_rows[0].get("source") == "apply_event"
    )
    print(f"{PASS if ok else FAIL} A: routes to panel — panel.events={len(panel_evs)} "
          f"parent msg.events={len(mgr_evs)} events.jsonl_new={len(new_rows)}")
    return ok


def test_b_idempotent_same_uuid_same_data() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_B")
    ev = _worker_event("del_B", "uuid-B", "first")
    _apply(sid, msg_id, root_id, ev, source_is_provider_stream=True)
    _apply(sid, msg_id, root_id, ev, source_is_provider_stream=True)  # idempotent re-apply

    panel_evs = _panel_events(sid, msg_id, "del_B")
    mgr_evs = _mgr_events(sid, msg_id)
    ok = len(panel_evs) == 1 and len(mgr_evs) == 0
    print(f"{PASS if ok else FAIL} B: idempotent — panel.events={len(panel_evs)} "
          f"parent msg.events={len(mgr_evs)}")
    return ok


def test_c_replace_on_mutated_data() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_C")
    _apply(sid, msg_id, root_id, _worker_event("del_C", "uuid-C", "v1"), source_is_provider_stream=True)
    _apply(sid, msg_id, root_id, _worker_event("del_C", "uuid-C", "v2"), source_is_provider_stream=True)

    panel_evs = _panel_events(sid, msg_id, "del_C")
    mgr_evs = _mgr_events(sid, msg_id)
    last_content = (
        (panel_evs[-1].get("data") or {}).get("message", {}).get("content")
        if panel_evs else None
    )
    ok = (
        len(panel_evs) == 1
        and last_content == "v2"
        and len(mgr_evs) == 0
    )
    print(f"{PASS if ok else FAIL} C: replace — panel.events={len(panel_evs)} "
          f"last_content={last_content!r}")
    return ok


def test_d_no_matching_panel_is_noop() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_D")
    # Send worker_event for a delegation_id NOT on any panel.
    _apply(sid, msg_id, root_id, _worker_event("ghost", "uuid-D", "stray"), source_is_provider_stream=True)

    panel_evs = _panel_events(sid, msg_id, "del_D")
    mgr_evs = _mgr_events(sid, msg_id)
    ok = len(panel_evs) == 0 and len(mgr_evs) == 0
    print(f"{PASS if ok else FAIL} D: ghost delegation_id no-op — "
          f"panel.events={len(panel_evs)} parent msg.events={len(mgr_evs)}")
    return ok


def test_e_live_false_skips_ingest_and_rewrite() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_E")
    rows_before = len(_events_jsonl_for(root_id, sid))

    # Spy on rewrite_event_data.
    import file_ref_resolver
    orig = file_ref_resolver.rewrite_event_data
    call_count = {"n": 0}
    def spy(*args, **kwargs):
        call_count["n"] += 1
        return orig(*args, **kwargs)
    file_ref_resolver.rewrite_event_data = spy
    try:
        _apply(sid, msg_id, root_id, _worker_event("del_E", "uuid-E", "x"), source_is_provider_stream=False)
    finally:
        file_ref_resolver.rewrite_event_data = orig

    panel_evs = _panel_events(sid, msg_id, "del_E")
    mgr_evs = _mgr_events(sid, msg_id)
    rows_after = _events_jsonl_for(root_id, sid)
    ok = (
        len(panel_evs) == 1
        and len(mgr_evs) == 0
        and len(rows_after) == rows_before  # no events.jsonl write
        and call_count["n"] == 0  # no file-ref rewrite
    )
    print(f"{PASS if ok else FAIL} E: source_is_provider_stream=False — panel.events={len(panel_evs)} "
          f"rows_delta={len(rows_after) - rows_before} rewrite_calls={call_count['n']}")
    return ok


def test_g_reconcile_roundtrip_rehydrates_panel_events() -> bool:
    """Round-trip: live worker_event writes events.jsonl row;
    `_reconcile_msg_events_from_jsonl` re-reads it and re-applies via
    `apply_event(source_is_provider_stream=False)` → must land back in panel.events. Locks
    the CLAUDE.md convergence invariant for worker_event events.

    Sub-case G2: when the panel is missing from msg.workers at
    reconcile time, the new branch silently no-ops (event drops from
    the render tree) — verify msg.events DOES NOT absorb it
    via the generic ev_uuid fallthrough."""
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_G")
    _apply(sid, msg_id, root_id, _worker_event("del_G", "uuid-G", "src"), source_is_provider_stream=True)

    # Simulate fresh load: blow away panel.events but keep the panel
    # entry on msg.workers. Reconcile should refill from events.jsonl.
    def _clear_panel_events(s: dict) -> None:
        m = next((mm for mm in s.get("messages") or []
                  if mm.get("id") == msg_id), None)
        if m:
            for p in m.get("workers") or []:
                if p.get("delegation_id") == "del_G":
                    p["events"] = []
                    # External mutators of an events list MUST
                    # invalidate the `_uid_idx` cache that
                    # `apply_event` / `apply_worker_panel_event`
                    # maintain. The post-A' lazy build-only path
                    # trusts the cache; a stale dict would index
                    # past the now-empty list (IndexError).
                    p.pop("_uid_idx", None)
    session_manager._run(
        sid, _clear_panel_events,
        {"kind": "test_clear_panel_events"},
    )

    # Finalize the msg so reconcile's "skip streaming" guard doesn't
    # bail (reconcile only re-applies events for FINALIZED msgs).
    session_manager.set_streaming(sid, msg_id, False)
    session_manager.mark_reconcile_dirty(root_id)

    # Run reconcile against the root tree.
    from main import _reconcile_msg_events_from_jsonl
    tree = session_manager.get_root_tree(sid) or session_manager.get(sid)
    _reconcile_msg_events_from_jsonl(tree)

    panel_evs = _panel_events(sid, msg_id, "del_G")
    mgr_evs = _mgr_events(sid, msg_id)
    g1_ok = len(panel_evs) == 1 and len(mgr_evs) == 0

    # G2: panel missing entirely → reconcile no-ops; parent msg.events
    # MUST NOT absorb the worker_event via generic fallthrough.
    sid2, root_id2, msg_id2, _ = _mk_session_with_panel("del_G2")
    _apply(sid2, msg_id2, root_id2,
           _worker_event("del_G2", "uuid-G2", "x"), source_is_provider_stream=True)

    def _drop_panel(s: dict) -> None:
        m = next((mm for mm in s.get("messages") or []
                  if mm.get("id") == msg_id2), None)
        if m:
            m["workers"] = []
    session_manager._run(
        sid2, _drop_panel,
        {"kind": "test_drop_panel"},
    )
    session_manager.set_streaming(sid2, msg_id2, False)
    session_manager.mark_reconcile_dirty(root_id2)

    tree2 = session_manager.get_root_tree(sid2) or session_manager.get(sid2)
    _reconcile_msg_events_from_jsonl(tree2)

    mgr_evs2 = _mgr_events(sid2, msg_id2)
    # Look up by id directly since panel is gone.
    sess2 = session_manager.get(sid2) or {}
    msg2 = next(m for m in sess2["messages"] if m.get("id") == msg_id2)
    workers2 = msg2.get("workers") or []
    g2_ok = len(mgr_evs2) == 0 and len(workers2) == 0

    ok = g1_ok and g2_ok
    print(f"{PASS if ok else FAIL} G: reconcile round-trip — "
          f"G1(panel_present)={g1_ok} (panel_evs={len(panel_evs)} mgr_evs={len(mgr_evs)}); "
          f"G2(panel_missing)={g2_ok} (mgr_evs2={len(mgr_evs2)} workers2={len(workers2)})")
    return ok


def test_g3_worker_event_updates_raw_session_content() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_G3")
    _apply(
        sid,
        msg_id,
        root_id,
        _worker_event("del_G3", "uuid-G3", "worker final text"),
        source_is_provider_stream=True,
    )
    session_manager.set_streaming(sid, msg_id, False)
    session_manager.flush_pending_persists()

    raw = session_store.get_session(sid) or {}
    msg = next((m for m in raw.get("messages") or [] if m.get("id") == msg_id), {})
    workers = msg.get("workers") or []
    worker_events_present = any("events" in w for w in workers if isinstance(w, dict))
    ok = (
        msg.get("content") == "worker final text"
        and "events" not in msg
        and not worker_events_present
    )
    print(
        f"{PASS if ok else FAIL} G3: raw session content follows worker timeline — "
        f"content={msg.get('content')!r} worker_events_present={worker_events_present}",
    )
    return ok


def test_h_multi_panel_routes_correctly() -> bool:
    """N>1 panels on the same msg. A worker_event for `del_Y` must
    land on the Y panel ONLY. Locks against wrong-panel routing /
    accidental shared list-ref bugs in `apply_worker_panel_event`."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["isStreaming"] = True
    def _mk_p(did: str) -> dict:
        return {
            "delegation_id": did,
            "worker_session_id": f"ws_{did}",
            "worker_description": f"worker {did}",
            "is_new": False,
            "instructions_preview": "",
            "events": [],
            "jsonl_path": None,
            "new_byte_offset": None,
            "fork_agent_sid": None,
            "token_usage": None,
        }
    scaffold["workers"] = [_mk_p("del_X"), _mk_p("del_Y"), _mk_p("del_Z")]
    session_manager.append_assistant_msg(sid, scaffold)
    root_id = session_manager._root_id_for(sid)
    msg_id = scaffold["id"]

    _apply(sid, msg_id, root_id, _worker_event("del_Y", "uuid-H", "for-Y"), source_is_provider_stream=True)

    ex = _panel_events(sid, msg_id, "del_X")
    ey = _panel_events(sid, msg_id, "del_Y")
    ez = _panel_events(sid, msg_id, "del_Z")
    mgr = _mgr_events(sid, msg_id)
    ok = len(ex) == 0 and len(ey) == 1 and len(ez) == 0 and len(mgr) == 0
    print(f"{PASS if ok else FAIL} H: multi-panel routing — "
          f"X={len(ex)} Y={len(ey)} Z={len(ez)} mgr={len(mgr)}")
    return ok


def test_i_worker_event_does_not_need_snapshot_workers() -> bool:
    """Worker-event routing mutates the matching panel directly."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["isStreaming"] = True
    # `panel` is the SAME dict that ends up in the cache's
    # `m["workers"][0]` because `append_assistant_msg` does
    # `s["messages"].append(msg)` (no deepcopy).
    panel = {
        "delegation_id": "del_I",
        "worker_session_id": "ws_I",
        "worker_description": "test worker I",
        "is_new": False,
        "instructions_preview": "",
        "events": [],
        "jsonl_path": None,
        "new_byte_offset": None,
        "fork_agent_sid": None,
        "token_usage": None,
    }
    scaffold["workers"] = [panel]
    session_manager.append_assistant_msg(sid, scaffold)
    root_id = session_manager._root_id_for(sid)
    msg_id = scaffold["id"]

    # Mirror production wiring: workers_list shares panel refs with
    # m["workers"]. `get_ref` returns the live cache (no deepcopy) so
    # `msg` is the exact dict the strategy will mutate.
    workers_list = [panel]
    sess_ref = session_manager.get_ref(sid) or {}
    msg = next(m for m in sess_ref["messages"] if m.get("id") == msg_id)
    ctx = ApplyEventCtx(root_id=root_id, workers_list=workers_list)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_worker_event("del_I", "uuid-I", "with-snapshot"),
        ctx=ctx, source_is_provider_stream=True,
    )

    panel_evs = _panel_events(sid, msg_id, "del_I")
    mgr_evs = _mgr_events(sid, msg_id)
    ok = len(panel_evs) == 1 and len(mgr_evs) == 0
    print(f"{PASS if ok else FAIL} I: worker_event updates panel directly — "
          f"panel.events={len(panel_evs)} mgr={len(mgr_evs)}")
    return ok


def test_i2_worker_event_skips_cold_event_hydration() -> bool:
    """Worker-panel routing must not hydrate full root event history."""
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_I2")
    original = session_manager._hydrate_cached_root_events
    calls = 0

    def counted_hydrate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    session_manager._event_hydrated_roots.discard(root_id)
    session_manager._hydrate_cached_root_events = counted_hydrate
    try:
        _apply(
            sid,
            msg_id,
            root_id,
            _worker_event("del_I2", "uuid-I2", "without-hydrate"),
            source_is_provider_stream=True,
        )
    finally:
        session_manager._hydrate_cached_root_events = original

    panel_evs = _panel_events(sid, msg_id, "del_I2")
    ok = calls == 0 and len(panel_evs) == 1
    print(f"{PASS if ok else FAIL} I2: worker_event skips cold hydration — "
          f"hydrate_calls={calls} panel.events={len(panel_evs)}")
    return ok


def test_j_malformed_inner_no_crash_no_pollute() -> bool:
    """Corner cases on the worker_event payload shape:
       J1: data.event = None → inner becomes {} (falsy) → branch's
           `if delegation_id and inner:` skips the mutator call.
       J2: data.event = {} → same as above.
       J3: missing delegation_id → branch skips the mutator call.
    All three must: NOT crash, NOT pollute msg.events, AND
    still write the outer wrapper to events.jsonl for forensic recovery."""
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_J")
    rows_before = len(_events_jsonl_for(root_id, sid))

    j1 = {"type": "worker_event",
          "data": {"delegation_id": "del_J", "event": None}}
    j2 = {"type": "worker_event",
          "data": {"delegation_id": "del_J", "event": {}}}
    j3 = {"type": "worker_event",
          "data": {"event": {"type": "agent_message",
                              "data": {"uuid": "uuid-J3"}}}}

    for ev in (j1, j2, j3):
        try:
            _apply(sid, msg_id, root_id, ev, source_is_provider_stream=True)
        except Exception as e:
            print(f"{FAIL} J: crash on payload {ev} — {e}")
            return False

    panel_evs = _panel_events(sid, msg_id, "del_J")
    mgr_evs = _mgr_events(sid, msg_id)
    rows_after = _events_jsonl_for(root_id, sid)
    # All three should ingest the outer wrapper to events.jsonl.
    rows_added = len(rows_after) - rows_before
    ok = (
        len(panel_evs) == 0
        and len(mgr_evs) == 0
        and rows_added == 3
    )
    print(f"{PASS if ok else FAIL} J: malformed payloads no-op gracefully — "
          f"panel={len(panel_evs)} mgr={len(mgr_evs)} jsonl_rows_added={rows_added}")
    return ok


def test_k_worker_start_creates_panel_before_worker_event() -> bool:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["isStreaming"] = True
    session_manager.append_assistant_msg(sid, scaffold)
    msg_id = scaffold["id"]

    _apply(sid, msg_id, root_id, {
        "type": "worker_start",
        "data": {
            "delegation_id": "codex_subagent_child",
            "worker_session_id": "child",
            "worker_description": "Codex subagent child",
            "run_mode": "codex_subagent",
        },
    }, source_is_provider_stream=True)
    _apply(sid, msg_id, root_id, _worker_event(
        "codex_subagent_child", "uuid-K", "child event",
    ), source_is_provider_stream=True)

    panel_evs = _panel_events(sid, msg_id, "codex_subagent_child")
    mgr_evs = _mgr_events(sid, msg_id)
    rows = _events_jsonl_for(root_id, sid)
    types = [r.get("type") for r in rows[-2:]]
    ok = (
        len(panel_evs) == 1
        and len(mgr_evs) == 0
        and types == ["worker_start", "worker_event"]
    )
    print(f"{PASS if ok else FAIL} K: worker_start creates panel before event — "
          f"panel={len(panel_evs)} mgr={len(mgr_evs)} types={types}")
    return ok


def test_l_post_trigger_insert_at_counts_after_current_event() -> bool:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    tm = TurnManager(None)
    tm.current_assistant_msgs[sid] = {
        "id": "msg",
        "events": [
            {"type": "agent_message", "data": {"uuid": "before-trigger"}},
        ],
    }
    before = tm.in_flight_event_count(sid)
    after = tm.in_flight_event_count_after_current_event(sid)
    ok = before == 1 and after == 2
    print(f"{PASS if ok else FAIL} L: post-trigger insert_at — "
          f"before={before} after={after}")
    return ok


def test_m_session_panel_emitters_stamp_after_pending_trigger() -> bool:
    async def _run() -> bool:
        sess = session_manager.create(
            name="t", model="sonnet", cwd="/tmp",
            orchestration_mode="native", source="cli",
        )
        sid = sess["id"]
        tm = TurnManager(None)
        saved: list[dict] = []

        async def save(event: dict) -> None:
            saved.append(event)

        tm._turn_save_callbacks[sid] = save
        tm.current_turn_workers[sid] = []
        tm.current_assistant_msgs[sid] = {
            "id": "msg",
            "events": [
                {"type": "agent_message", "data": {"uuid": "before-trigger"}},
            ],
        }
        fake = SimpleNamespace(turn_manager=tm)

        panel = await Coordinator._start_team_message_panel(
            fake,
            sender_session_id=sid,
            target_session_id="target-session",
            target={"name": "Target", "kind": "sub_session"},
            message="hello",
            queue_item_id="queue-1",
            run_mode="team_message",
        )
        created = await Coordinator.emit_session_created_panel(
            fake,
            sender_session_id=sid,
            target_session={
                "id": "created-session",
                "name": "Created",
                "kind": "sub_session",
            },
        )
        ok = (
            panel is not None
            and panel.get("insert_at") == 2
            and created is not None
            and created.get("insert_at") == 2
            and [event["data"]["insert_at"] for event in saved] == [2, 2]
        )
        if not ok:
            print(f"  panel={panel!r} created={created!r} saved={saved!r}")
        return ok

    ok = asyncio.run(_run())
    print(f"{PASS if ok else FAIL} M: session panel emitters insert after pending trigger")
    return ok


def test_f_panel_stores_inner_not_outer_wrapper() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_F")
    inner = {
        "type": "agent_message",
        "data": {
            "uuid": "uuid-F",
            "type": "assistant",
            "message": {"content": "shape-check"},
        },
    }
    outer = {
        "type": "worker_event",
        "data": {"delegation_id": "del_F", "event": inner},
    }
    _apply(sid, msg_id, root_id, outer, source_is_provider_stream=True)

    panel_evs = _panel_events(sid, msg_id, "del_F")
    stored = panel_evs[-1] if panel_evs else None
    ok = stored == inner
    print(f"{PASS if ok else FAIL} F: panel stores INNER — "
          f"shape_matches_inner={ok}")
    return ok


def test_n_snapshot_routes_journal_worker_event_to_panel_once() -> bool:
    sid, root_id, msg_id, _ = _mk_session_with_panel("del_N")
    _apply(
        sid,
        msg_id,
        root_id,
        _worker_event("del_N", "uuid-N", "partial"),
        source_is_provider_stream=True,
    )
    _apply(
        sid,
        msg_id,
        root_id,
        _worker_event("del_N", "uuid-N", "final"),
        source_is_provider_stream=True,
    )

    session_manager._since_cache.pop(sid, None)
    replay = session_manager.get_messages_since(sid, since_seq=0, limit=50) or {}
    msg = next(
        (m for m in replay.get("messages") or [] if m.get("id") == msg_id),
        {},
    )
    panel = next(
        (
            p for p in msg.get("workers") or []
            if p.get("delegation_id") == "del_N"
        ),
        {},
    )
    parent_worker_events = [
        e for e in msg.get("events") or [] if e.get("type") == "worker_event"
    ]
    panel_events = panel.get("events") or []
    last_content = (
        ((panel_events[-1].get("data") or {}).get("message") or {}).get("content")
        if panel_events else None
    )
    ok = (
        parent_worker_events == []
        and len(panel_events) == 1
        and last_content == "final"
        and render_stub.renderable_count(msg) == 1
    )
    print(
        f"{PASS if ok else FAIL} N: snapshot routes journal worker_event once — "
        f"parent_worker_events={len(parent_worker_events)} "
        f"panel_events={len(panel_events)} last_content={last_content!r} "
        f"renderable={render_stub.renderable_count(msg)}",
    )
    return ok


# ─── runner ───────────────────────────────────────────────────────

def main() -> int:
    try:
        results = [
            test_a_routes_to_panel_not_manager(),
            test_b_idempotent_same_uuid_same_data(),
            test_c_replace_on_mutated_data(),
            test_d_no_matching_panel_is_noop(),
            test_e_live_false_skips_ingest_and_rewrite(),
            test_f_panel_stores_inner_not_outer_wrapper(),
            test_g_reconcile_roundtrip_rehydrates_panel_events(),
            test_g3_worker_event_updates_raw_session_content(),
            test_h_multi_panel_routes_correctly(),
            test_i_worker_event_does_not_need_snapshot_workers(),
            test_i2_worker_event_skips_cold_event_hydration(),
            test_j_malformed_inner_no_crash_no_pollute(),
            test_k_worker_start_creates_panel_before_worker_event(),
            test_l_post_trigger_insert_at_counts_after_current_event(),
            test_m_session_panel_emitters_stamp_after_pending_trigger(),
            test_n_snapshot_routes_journal_worker_event_to_panel_once(),
        ]
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} subtests passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
