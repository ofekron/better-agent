"""Regression tests for event attribution.

1. test_fork_rows_never_graft_on_parent — a worker-fork tailer's backup
   rows (written with the PARENT's app_session_id on the tailer) must
   never attach to the parent render tree: not via write-time msg_id
   stamping, not via the journal writer's ownership inference, and not
   via cold-load hydrate. The rows stay durable in events.jsonl.

2. test_warm_reconcile_brackets_by_journal_seq — the warm reconcile
   path (`main._reconcile_root_by_id`, bound as the reconcile body for
   `session_manager._sync_reconcile` / `reconcile_through`) must
   bracket orphan rows by their journal seq within the owning sid, not
   pile every orphan onto the last finalized message root-wide.

Run with:
    cd backend && .venv/bin/python scripts/test_event_attribution.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

# State-isolation rule: set BETTER_CLAUDE_HOME BEFORE importing any
# backend module so every store, runs root, traces dir lands in a
# throwaway tempdir.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-attribution-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from event_bus import bus  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer, publish_event_sync  # noqa: E402
from jsonl_tailer import OwnedClaudeJsonlTailer  # noqa: E402
from orchs import get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── helpers ──────────────────────────────────────────────────────

def _mk_session_with_streaming_msg(
    *, primary_agent_sid: str,
) -> tuple[str, str, str]:
    """Manager-mode session with `primary_agent_sid` as its primary
    agent and one streaming assistant msg. Returns (sid, root_id, msg_id)."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    session_manager.set_agent_sid(sid, "manager", primary_agent_sid)
    scaffold = get_strategy("manager").build_assistant_scaffold()
    scaffold["isStreaming"] = True
    session_manager.append_assistant_msg(sid, scaffold)
    root_id = session_manager._root_id_for(sid)
    return sid, root_id, scaffold["id"]


def _append_finalized_msg(sid: str) -> str:
    scaffold = get_strategy("manager").build_assistant_scaffold()
    scaffold["isStreaming"] = True
    session_manager.append_assistant_msg(sid, scaffold)
    session_manager.set_streaming(sid, scaffold["id"], False)
    return scaffold["id"]


def _line(uuid: str, text: str, *, timestamp: bool = False) -> dict:
    d = {"uuid": uuid, "type": "assistant", "message": {"content": text}}
    if timestamp:
        d["timestamp"] = datetime.now().isoformat()
    return d


def _msg_event_uuids(sid: str, msg_id: str) -> set[str]:
    """All event uuids attached to a msg's render-tree events list —
    both the strategy list and the flat `events` key (defensive: graft
    must show up regardless of which list the strategy uses)."""
    strategy = get_strategy("manager")
    sess = session_manager.get(sid) or {}
    out: set[str] = set()
    for m in sess.get("messages") or []:
        if m.get("id") != msg_id:
            continue
        lists = [strategy._events_list(m), m.get("events") or []]
        mgr = m.get("manager")
        if isinstance(mgr, dict):
            lists.append(mgr.get("events") or [])
        for evs in lists:
            for ev in evs:
                d = ev.get("data") if isinstance(ev, dict) else None
                if isinstance(d, dict) and d.get("uuid"):
                    out.add(d["uuid"])
    return out


def _journal_uuids(root_id: str) -> set[str]:
    rows, _, _ = event_ingester.read_events(root_id, limit=10_000)
    return {
        (r.get("data") or {}).get("uuid")
        for r in rows
        if isinstance(r.get("data"), dict)
    }


# ─── tests ────────────────────────────────────────────────────────

async def test_fork_rows_never_graft_on_parent() -> bool:
    """dim5-F2 end-to-end chain: fork-tailer `_dispatch` with the
    PARENT app_session_id (once while the parent msg streams — the
    explicit-stamp channel; once after finalize with a provider
    timestamp — the write-time ownership-inference channel), then a
    cold-load hydrate. The parent msg.events must contain no fork raw
    line; events.jsonl must retain both fork rows."""
    primary_sid = "agent-primary-attr"
    fork_sid = "agent-worker-fork-attr"
    sid, root_id, msg_id = _mk_session_with_streaming_msg(
        primary_agent_sid=primary_sid,
    )

    tailer = OwnedClaudeJsonlTailer(
        root_id=root_id,
        app_session_id=sid,  # PARENT sid, as native_files_manager constructs it
        agent_sid=fork_sid,
        jsonl_path=Path("/tmp/unused-fork.jsonl"),
        start_offset=0,
    )

    # Channel 1: parent msg is streaming → write-time explicit stamp.
    await tailer._dispatch(_line("fork-uuid-1", "worker raw 1", timestamp=True))
    event_journal_writer.barrier_sync(root_id)

    # Channel 2: parent finalized → source-ts ownership inference.
    session_manager.set_streaming(sid, msg_id, False)
    await tailer._dispatch(_line("fork-uuid-2", "worker raw 2", timestamp=True))
    event_journal_writer.barrier_sync(root_id)

    # Cold-load hydrate over the live tree (the `_load_root` body).
    from render_tree_hydrate import hydrate_msg_events_from_jsonl
    tree = session_manager.get_ref(root_id)
    hydrate_msg_events_from_jsonl(tree)

    fork_uuids = {"fork-uuid-1", "fork-uuid-2"}
    grafted = fork_uuids & _msg_event_uuids(sid, msg_id)
    retained = fork_uuids - _journal_uuids(root_id)

    ok = not grafted and not retained
    print(f"{PASS if ok else FAIL} test_fork_rows_never_graft_on_parent: "
          f"grafted-on-parent={sorted(grafted)} (must be empty); "
          f"missing-from-events.jsonl={sorted(retained)} (must be empty)")
    return ok


async def test_legacy_claude_tailer_rows_still_render() -> bool:
    """Before the fork-identity discriminator, the PRIMARY tailer also
    stamped rows `source="claude_tailer"` (+msg_id). Real disks hold
    such legacy rows as the SOLE copy of primary content. They must
    keep their old render semantics: hydrate attaches them to the
    stamped msg and message reads return them. The fork discriminator
    must therefore be a NEW source value, never "claude_tailer"."""
    primary_sid = "agent-primary-legacy"
    sid, root_id, msg_id = _mk_session_with_streaming_msg(
        primary_agent_sid=primary_sid,
    )
    session_manager.set_streaming(sid, msg_id, False)

    # Legacy-style row, written as the ONLY copy of this event.
    event_ingester.ingest(
        root_id, sid=sid, event_type="agent_message",
        data=_line("legacy-uuid-1", "legacy primary backup line"),
        source="claude_tailer", msg_id=msg_id,
    )

    from render_tree_hydrate import hydrate_msg_events_from_jsonl
    hydrate_msg_events_from_jsonl(session_manager.get_ref(root_id))
    rendered = "legacy-uuid-1" in _msg_event_uuids(sid, msg_id)

    from event_journal import event_journal_reader
    read_rows = event_journal_reader.read_message_events(root_id, msg_id)
    readable = any(
        (r.get("data") or {}).get("uuid") == "legacy-uuid-1"
        for r in read_rows
    )

    ok = rendered and readable
    print(f"{PASS if ok else FAIL} test_legacy_claude_tailer_rows_still_render: "
          f"hydrated-on-msg={rendered}; message-read-returns-row={readable}")
    return ok


async def test_warm_reconcile_brackets_by_journal_seq() -> bool:
    """dim6-H1: seed events.jsonl with two finalized msgs' named rows
    (seqs 1-2 and 5-6), an orphan between them (seq 3), and a
    foreign-sid orphan (seq 4). The warm reconcile must attach the
    seq-3 orphan to msg1 (seq-bracketed) and never attach the
    foreign-sid orphan to this node."""
    primary_sid = "agent-primary-warm"
    sid, root_id, m1 = _mk_session_with_streaming_msg(
        primary_agent_sid=primary_sid,
    )
    session_manager.set_streaming(sid, m1, False)
    m2 = _append_finalized_msg(sid)

    def _publish(uuid: str, text: str, *, message_id=None, context_id=None):
        publish_event_sync(
            session_id=root_id,
            context_id=context_id or sid,
            event_type="agent_message",
            data=_line(uuid, text),
            source="apply_event",
            message_id=message_id,
        )

    _publish("m1-e1", "a1", message_id=m1)   # seq 1
    _publish("m1-e2", "a2", message_id=m1)   # seq 2
    _publish("orphan-mid", "o")              # seq 3 — between m1 and m2
    _publish("orphan-foreign", "f", context_id="foreign-sid-warm")  # seq 4
    _publish("m2-e1", "b1", message_id=m2)   # seq 5
    _publish("m2-e2", "b2", message_id=m2)   # seq 6
    event_journal_writer.barrier_sync(root_id)

    from main import _reconcile_root_by_id
    _reconcile_root_by_id(root_id, after_seq=0)

    m1_uuids = _msg_event_uuids(sid, m1)
    m2_uuids = _msg_event_uuids(sid, m2)

    on_correct_msg = "orphan-mid" in m1_uuids
    not_on_last = "orphan-mid" not in m2_uuids
    foreign_excluded = "orphan-foreign" not in (m1_uuids | m2_uuids)

    ok = on_correct_msg and not_on_last and foreign_excluded
    print(f"{PASS if ok else FAIL} test_warm_reconcile_brackets_by_journal_seq: "
          f"orphan-on-msg1={on_correct_msg}; not-on-last-msg={not_on_last}; "
          f"foreign-sid-excluded={foreign_excluded}")
    return ok


# ─── runner ───────────────────────────────────────────────────────

def _bracket(assistant_msgs, by_msg_id, orphan_raw):
    from render_tree_hydrate import _bracket_orphan_rows
    return _bracket_orphan_rows(assistant_msgs, by_msg_id, orphan_raw)


def test_orphan_not_swallowed_across_empty_neighbor_into_later_turn() -> bool:
    # A(named@10)  B(empty — its events are still transient orphans mid
    # resolution)  C(named@100). An orphan at seq 150 belongs to C. The
    # old unbounded ceil (neighbor B had no named rows) made A swallow
    # every later orphan, rendering 150 under the wrong (earlier) turn.
    assistant_msgs = [(0, {"id": "A"}), (1, {"id": "B"}), (2, {"id": "C"})]
    by_msg_id = {"A": [{"seq": 10}], "C": [{"seq": 100}]}
    out = _bracket(assistant_msgs, by_msg_id, [{"seq": 50}, {"seq": 150}])
    if 150 in {r["seq"] for r in out.get("A", [])}:
        print(f"  orphan 150 wrongly attributed to earlier turn A: {out}")
        return False
    if 150 not in {r["seq"] for r in out.get("C", [])}:
        print(f"  orphan 150 not attributed to its owning turn C: {out}")
        return False
    print("PASS  orphan not swallowed across empty neighbor into later turn")
    return True


def test_orphan_assigned_to_exactly_one_turn() -> bool:
    # No orphan may render under two turns (single-owner attribution).
    assistant_msgs = [(0, {"id": "A"}), (1, {"id": "B"}), (2, {"id": "C"})]
    by_msg_id = {"A": [{"seq": 10}], "C": [{"seq": 100}]}
    out = _bracket(assistant_msgs, by_msg_id, [{"seq": 50}, {"seq": 150}])
    assigned = [r["seq"] for rows in out.values() for r in rows]
    if len(assigned) != len(set(assigned)):
        print(f"  orphan double-assigned across turns: {out}")
        return False
    print("PASS  orphan assigned to exactly one turn")
    return True


def test_normal_bracketing_unchanged() -> bool:
    # Every message has named rows: attribution must be unchanged.
    assistant_msgs = [(0, {"id": "A"}), (1, {"id": "B"})]
    by_msg_id = {"A": [{"seq": 10}], "B": [{"seq": 100}]}
    out = _bracket(assistant_msgs, by_msg_id, [{"seq": 50}, {"seq": 150}])
    if {r["seq"] for r in out.get("A", [])} != {50} or {r["seq"] for r in out.get("B", [])} != {150}:
        print(f"  normal bracketing changed: {out}")
        return False
    print("PASS  normal bracketing unchanged")
    return True


async def _run() -> int:
    event_journal_writer.register(bus)
    results = [
        await test_fork_rows_never_graft_on_parent(),
        await test_legacy_claude_tailer_rows_still_render(),
        await test_warm_reconcile_brackets_by_journal_seq(),
        test_orphan_not_swallowed_across_empty_neighbor_into_later_turn(),
        test_orphan_assigned_to_exactly_one_turn(),
        test_normal_bracketing_unchanged(),
    ]
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} tests passed")
    return 0 if passed == total else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
