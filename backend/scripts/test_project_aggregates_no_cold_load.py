"""Perf regression: the session-list aggregation endpoints must NOT
deep-hydrate (cold-load) sessions on the request path.

Pins the fix for the ~44s `ingest.read_events` burst. `GET /api/projects`
(`_project_aggregates`) and `GET /api/sessions` (`get_sessions`) enriched
every session with `session_manager.is_running` + `get_unread_count`,
both of which cold-load the full tree via `_load_root` (~2 `events.jsonl`
scans each: `hydrate_msg_events_from_jsonl` + `_derive_current_todos`).
On a cold cache the first call deep-hydrated ALL sessions (~423 × 2 ≈
792 `read_events` ≈ 44s), blocking the loop while a turn was queued.

Asserts:
  1. SETUP IS GENUINELY COLD: the OLD blocking enrichers
     (`is_running` + `get_unread_count`) over the cold sessions DO scan
     `events.jsonl` (>0 read_events) — proves the sessions would scan.
  2. THE FIX: `_project_aggregates()` over the SAME cold sessions
     triggers ZERO `read_events` (coordinator.is_running +
     peek_unread_count, both load-free).
  3. WARM CORRECTNESS: `warm_unread(sid)` hydrates the count off the hot
     path, fires `unread_changed`, and makes `peek_unread_count` return
     the correct non-zero value so project aggregates converge instead
     of staying undercounted.

Run with:
    cd backend && .venv/bin/python scripts/test_project_aggregates_no_cold_load.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
import warnings

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-proj-agg-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import event_ingester as ei_mod  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402

event_ingester = ei_mod.event_ingester

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── read_events call counter ───────────────────────────────────────

_read_events_calls = {"n": 0}
_orig_read_events = event_ingester.read_events


def _counting_read_events(*a, **k):
    _read_events_calls["n"] += 1
    return _orig_read_events(*a, **k)


event_ingester.read_events = _counting_read_events


def _reset_counter() -> None:
    _read_events_calls["n"] = 0


def _drain_journal(sids: list[str]) -> None:
    for sid in sids:
        event_journal_writer.barrier_sync(sid)


# ─── Setup helpers ──────────────────────────────────────────────────


def _mk_session_with_events(n_events: int = 2) -> str:
    """Create a native session with one assistant msg carrying
    `n_events` LIVE-ingested events (so they land in events.jsonl).
    `last_seen_event_uid` is left None → every event is unread."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-proj-agg",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    ctx = ApplyEventCtx(root_id=sid)
    for _ in range(n_events):
        strategy.apply_event(
            app_session_id=sid, msg=scaffold,
            event={
                "type": "agent_message",
                "data": {"uuid": str(uuid.uuid4()), "type": "assistant",
                         "message": {"content": "x"}},
            },
            ctx=ctx, source_is_provider_stream=True,
        )
    return sid


def _make_cold(sid: str) -> None:
    """Drop every trace of `sid`'s root from the in-memory caches so the
    next access cold-loads from disk (simulating a fresh backend boot)."""
    rid = session_manager._root_id_for(sid)
    session_manager._roots.pop(rid, None)
    session_manager._reconcile_dirty.pop(rid, None)
    session_manager._unread_hydrated.discard(sid)
    session_manager._unread_counts.pop(sid, None)


def _capture_fires():
    events: list[dict] = []

    def listener(sid: str, change: dict) -> None:
        events.append({"sid": sid, **change})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(listener)
    return events


# ─── Tests ──────────────────────────────────────────────────────────


def test_cold_setup_blocking_path_scans() -> bool:
    """Sanity: the OLD blocking enrichers DO cold-load (scan) the cold
    sessions — proves the regression test's fixtures would have driven
    the burst. Without this, test 2's `== 0` could pass on a setup that
    never had anything to scan."""
    sids = [_mk_session_with_events() for _ in range(3)]
    _drain_journal(sids)
    for sid in sids:
        _make_cold(sid)
    _reset_counter()
    for sid in sids:
        session_manager.is_running(sid)
        session_manager.get_unread_count(sid)
    scanned = _read_events_calls["n"]
    ok = scanned > 0
    print(f"{PASS if ok else FAIL} blocking enrichers scan cold sessions "
          f"(read_events={scanned}, expected >0)")
    return ok


def test_project_aggregates_zero_cold_load() -> bool:
    """The fix: `_project_aggregates()` enriches via load-free sources
    only → ZERO read_events even when every session is cold."""
    from main import _project_aggregates

    sids = [_mk_session_with_events() for _ in range(3)]
    _drain_journal(sids)
    for sid in sids:
        _make_cold(sid)
    _reset_counter()
    _project_aggregates()
    scanned = _read_events_calls["n"]
    ok = scanned == 0
    print(f"{PASS if ok else FAIL} _project_aggregates triggers 0 read_events "
          f"on cold sessions (got {scanned})")
    return ok


def test_warm_unread_hydrates_and_fires() -> bool:
    """`warm_unread` fills the unread cache off the hot path so the
    project total converges, and fires `unread_changed`."""
    sid = _mk_session_with_events(n_events=2)
    _drain_journal([sid])
    _make_cold(sid)

    # Cold: peek must NOT know the count yet (so aggregates would show 0).
    assert session_manager.peek_unread_count(sid) is None, (
        "expected peek=None on a cold/un-hydrated session"
    )

    fires = _capture_fires()
    session_manager.warm_unread(sid)

    peeked = session_manager.peek_unread_count(sid)
    unread_fires = [f for f in fires
                    if f.get("sid") == sid and f.get("kind") == "unread_changed"]
    # 2 events on ONE assistant msg → 1 unread msg_id.
    ok = (peeked == 1 and len(unread_fires) == 1
          and unread_fires[0].get("unread_count") == 1)
    print(f"{PASS if ok else FAIL} warm_unread hydrates count "
          f"(peek={peeked}, fires={len(unread_fires)})")

    # Idempotent: a second warm is a no-op (already hydrated, no new fire).
    fires.clear()
    session_manager.warm_unread(sid)
    ok = ok and len(fires) == 0
    print(f"{PASS if ok else FAIL} second warm_unread is a no-op "
          f"(fires={len(fires)})")
    return ok


def test_seen_journal_head_fast_clean_skips_cold_load() -> bool:
    sid = _mk_session_with_events(n_events=2)
    _drain_journal([sid])
    sess = session_manager.get(sid) or {}
    latest_uid = None
    for msg in sess.get("messages") or []:
        for event in msg.get("events") or []:
            data = event.get("data") if isinstance(event, dict) else None
            uid = data.get("uuid") if isinstance(data, dict) else None
            if uid:
                latest_uid = uid
    assert latest_uid, "fixture must have a latest event uid"
    session_manager.mark_seen(sid, latest_uid)
    _make_cold(sid)
    rid = session_manager._root_id_for(sid)
    _reset_counter()

    cleaned = session_manager.mark_unread_clean_if_journal_seen(sid, latest_uid)
    root_loaded = rid in session_manager._roots
    peeked = session_manager.peek_unread_count(sid)
    scanned = _read_events_calls["n"]
    ok = cleaned and not root_loaded and peeked == 0 and scanned == 0
    print(f"{PASS if ok else FAIL} seen journal-head fast-clean "
          f"(cleaned={cleaned}, root_loaded={root_loaded}, "
          f"peek={peeked}, read_events={scanned})")
    return ok


def test_seen_fast_clean_rejects_later_render_before_non_render_tail() -> bool:
    sess = session_manager.create(
        name="tail-order", model="sonnet", cwd="/tmp/test-proj-agg",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    first = strategy.build_assistant_scaffold()
    second = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, first)
    session_manager.append_assistant_msg(sid, second)
    event_ingester.ingest(
        sid, sid, "agent_message",
        {"uuid": "seen-u1", "type": "assistant", "message": {"content": "a"}},
        source="test", msg_id=first["id"], cwd_override="",
    )
    event_ingester.ingest(
        sid, sid, "agent_message",
        {"uuid": "unread-u2", "type": "assistant", "message": {"content": "b"}},
        source="test", msg_id=second["id"], cwd_override="",
    )
    event_ingester.ingest(
        sid, sid, "complete", {"ok": True},
        source="test", msg_id=first["id"], cwd_override="",
    )
    session_manager.mark_seen(sid, "seen-u1")
    _make_cold(sid)

    cleaned = session_manager.mark_unread_clean_if_journal_seen(sid, "seen-u1")
    ok = cleaned is False
    print(f"{PASS if ok else FAIL} seen fast-clean rejects later render "
          f"(cleaned={cleaned})")
    return ok


def main() -> int:
    results = [
        test_cold_setup_blocking_path_scans(),
        test_project_aggregates_zero_cold_load(),
        test_warm_unread_hydrates_and_fires(),
        test_seen_journal_head_fast_clean_skips_cold_load(),
        test_seen_fast_clean_rejects_later_render_before_non_render_tail(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
