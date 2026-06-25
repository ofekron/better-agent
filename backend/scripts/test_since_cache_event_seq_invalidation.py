"""Locks the _since_cache event-seq invalidation fix:

1. Create a native session, apply N events, compute snapshot (populates
   _since_cache). Verify the snapshot contains all N events.
2. Append LATE events to events.jsonl WITHOUT changing next_seq (no new
   messages). Compute snapshot again.
3. Verify the second snapshot contains ALL N+L events — the cache must
   have invalidated because the event journal max seq grew.

Run with:
    cd backend && .venv/bin/python scripts/test_since_cache_event_seq_invalidation.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-since-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _drain_journal(sid: str, expected_seq: int, timeout: float = 2.0) -> None:
    """Wait for fire-and-forget journal writes to land."""
    from event_ingester import event_ingester
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event_ingester.max_seq_for_sid(sid, sid) >= expected_seq:
            return
        time.sleep(0.01)
    raise TimeoutError(
        f"journal not drained after {timeout}s: "
        f"expected seq>={expected_seq}, got {event_ingester.max_seq_for_sid(sid, sid)}"
    )


def _native_event(uuid: str, text: str = "x") -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _mk_session_with_events(n: int) -> tuple[str, list[dict]]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-since",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    ctx = ApplyEventCtx(root_id=sid, run_id="run-1")
    msg = session_manager.get_ref(sid)["messages"][-1]
    raw_events = []
    for i in range(n):
        ev = _native_event(f"u-{i}", f"text-{i}")
        raw_events.append(ev)
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx,
            source_is_provider_stream=True,
        )
    return sid, raw_events


def _append_late_events_via_apply(
    sid: str, start_idx: int, count: int,
) -> list[dict]:
    """Append late events via apply_event (simulates post-finalization
    late events from the SDK callback / tailer)."""
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="run-1")
    msg = session_manager.get_ref(sid)["messages"][-1]
    late_events = []
    for i in range(count):
        ev = _native_event(f"u-late-{start_idx + i}", f"late-{i}")
        late_events.append(ev)
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx,
            source_is_provider_stream=True,
        )
    return late_events


def _with_reconcile_probe(sid: str, changes: list[dict], new_cursor: int) -> bool:
    from event_journal import event_journal_reader

    original_fn = session_manager._reconcile_fn
    original_current_seq = event_journal_reader.current_seq
    original_cursor = session_manager._reconcile_cursor.get(sid)
    original_gen = session_manager._reconcile_gen.get(sid)
    calls = []
    try:
        session_manager._reconcile_cursor[sid] = 0
        session_manager._reconcile_gen[sid] = 7

        def _probe(root_id: str, *, after_seq: int = 0) -> list[dict]:
            calls.append((root_id, after_seq))
            return changes

        session_manager._reconcile_fn = _probe
        event_journal_reader.current_seq = lambda root_id: new_cursor
        session_manager._sync_reconcile(sid)
        return (
            calls == [(sid, 0)]
            and session_manager._reconcile_cursor.get(sid) == new_cursor
            and session_manager._reconcile_gen.get(sid) == (
                8 if changes else 7
            )
        )
    finally:
        session_manager._reconcile_fn = original_fn
        event_journal_reader.current_seq = original_current_seq
        if original_cursor is None:
            session_manager._reconcile_cursor.pop(sid, None)
        else:
            session_manager._reconcile_cursor[sid] = original_cursor
        if original_gen is None:
            session_manager._reconcile_gen.pop(sid, None)
        else:
            session_manager._reconcile_gen[sid] = original_gen


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    N_INITIAL = 10
    N_LATE = 3

    sid, initial_events = _mk_session_with_events(N_INITIAL)
    initial_uuids = [e["data"]["uuid"] for e in initial_events]
    _drain_journal(sid, N_INITIAL)

    # 1) Compute first snapshot — populates _since_cache.
    tree1 = session_manager.get_root_tree_stubbed(sid, msg_limit=50)
    assert tree1 is not None
    from event_ingester import event_ingester
    render_seq = event_ingester.render_seq_for_sid(sid, sid)
    cache_key = session_manager._since_cache[sid][0]
    ok = cache_key[1] == render_seq
    results.append((
        "snapshot cache key uses render seq",
        ok,
        f"cache={cache_key[1]}, render_seq={render_seq}",
    ))
    msg1 = tree1["messages"][-1]
    events1 = msg1.get("events") or []
    ok = len(events1) == N_INITIAL
    results.append((
        f"first snapshot has {N_INITIAL} events",
        ok,
        f"got {len(events1)}",
    ))
    uuids1 = [e["data"]["uuid"] for e in events1 if isinstance(e.get("data"), dict)]
    ok = set(uuids1) == set(initial_uuids)
    results.append((
        "first snapshot uuids match initial events",
        ok,
        f"missing {set(initial_uuids) - set(uuids1)}",
    ))

    # 2) Append late events WITHOUT creating new messages (next_seq stays).
    pre_next_seq = session_manager.get_ref(sid).get("next_seq")
    late_events = _append_late_events_via_apply(sid, 0, N_LATE)
    _drain_journal(sid, N_INITIAL + N_LATE)
    late_uuids = [e["data"]["uuid"] for e in late_events]
    all_uuids = set(initial_uuids) | set(late_uuids)

    # Verify next_seq hasn't changed — late events don't create messages.
    root_after = session_manager.get_ref(sid)
    ok = root_after.get("next_seq") == pre_next_seq
    results.append((
        "next_seq unchanged after late events",
        ok,
        f"was {pre_next_seq}, now {root_after.get('next_seq')}",
    ))

    # 3) Compute second snapshot — cache MUST invalidate because
    #    event_max_seq grew.
    tree2 = session_manager.get_root_tree_stubbed(sid, msg_limit=50)
    assert tree2 is not None
    msg2 = tree2["messages"][-1]
    events2 = msg2.get("events") or []
    ok = len(events2) == N_INITIAL + N_LATE
    results.append((
        f"second snapshot has {N_INITIAL + N_LATE} events",
        ok,
        f"got {len(events2)} — cache staleness bug!",
    ))
    uuids2 = [e["data"]["uuid"] for e in events2 if isinstance(e.get("data"), dict)]
    ok = set(uuids2) == all_uuids
    results.append((
        "second snapshot includes late event uuids",
        ok,
        f"missing {all_uuids - set(uuids2)}",
    ))

    ok = _with_reconcile_probe(sid, [], 100)
    results.append((
        "no-op reconcile advances cursor without cache-gen bump",
        ok,
        "cursor-only reconcile invalidated snapshots",
    ))

    ok = _with_reconcile_probe(sid, [{"app_session_id": sid}], 101)
    results.append((
        "changed reconcile bumps cache generation",
        ok,
        "projection changes did not invalidate snapshots",
    ))

    # Report
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
