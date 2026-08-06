"""Regression: orphan rows (msg_id=None, from externally-driven sessions
Better Agent never spawned — native terminal sessions that never fire a BA
`turn_complete` fact) must fold into the render tree AND get attention-marker
detection, via the session-projection drainer's fold pass
(`event_bus_subscribers._apply_session_projection_row`).

Two bugs this locks down:

  1. Marker gap: attention-marker detection requires the RAW (pre-strip)
     assistant text, which never survives to the persisted journal row —
     `event_ingester`'s canonical-storage rewrite strips `<TAG>` wrappers
     at write time for every source. The live path detects markers before
     that strip (`orchs.base.apply_event`, gated
     `source_is_provider_stream=True`); orphan rows never take that branch,
     so before this fix `orchs.base.ingest_orphan` never detected markers
     at all and natively-imported/externally-driven sessions' attention
     markers NEVER fired. `test_marker_fires_on_immediately_folded_orphan`
     locks down the fix (detection added directly in `ingest_orphan`, on
     the raw event, before the ingester's write-time strip).

  2. Buffer-then-flush: an orphan row arriving while the sid's latest
     assistant message is still streaming must NOT be applied immediately
     (no in-flight message it could safely bracket onto yet) — it must be
     buffered and only applied, in order, once finalization is observed on
     a later fold. `test_buffered_orphan_flushes_in_order_on_finalize`
     locks this down.

Run with:
    cd backend && .venv/bin/python scripts/test_orphan_fold_bracket_and_markers.py
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-orphan-fold-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import event_bus_subscribers  # noqa: E402
import file_ref_resolver  # noqa: E402
import session_store  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_TAG = "NEEDS_USER_DECISION"


def _register_marker_rule() -> None:
    file_ref_resolver.set_tag_rules([
        {
            "tag": _TAG,
            "marker": {"color": "#ff8c00", "tooltip": "needs a decision"},
            "_extension_id": "test-orphan-fold",
        },
    ])


@pytest.fixture(autouse=True)
def _marker_rule_registered():
    """Marker detection reads the global tag-rule registry, which `main()`
    seeds via `_register_marker_rule()`. Under pytest `main()` never runs, so
    register here (save/restore to avoid cross-module rule pollution)."""
    saved_rules = file_ref_resolver._tag_rules
    saved_re = file_ref_resolver._tag_scan_re
    _register_marker_rule()
    try:
        yield
    finally:
        file_ref_resolver._tag_rules = saved_rules
        file_ref_resolver._tag_scan_re = saved_re


def _mk_session(msg_id: str, *, is_streaming: bool) -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/orphan-fold",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = msg_id
    scaffold["isStreaming"] = is_streaming
    session_manager.append_assistant_msg(sid, scaffold)
    return sid


def _orphan_agent_message(uuid: str, text: str) -> dict:
    return {
        "uuid": uuid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _ingest_orphan_row(root_id: str, sid: str, uuid: str, text: str) -> None:
    # The real production path: `OwnedClaudeJsonlTailer` calls
    # `strategy.ingest_orphan`, which detects attention markers on the RAW
    # event BEFORE handing off to the ingester (which strips `<TAG>`
    # wrappers at write time). A direct `event_ingester.ingest(...)` call
    # would bypass that detection entirely and can't exercise the fix.
    get_strategy("native").ingest_orphan(
        app_session_id=sid,
        event={"type": "agent_message", "data": _orphan_agent_message(uuid, text)},
        ctx=ApplyEventCtx(root_id=root_id),
        source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(root_id)


def _rows_after(root_id: str, after_seq: int) -> list[dict]:
    rows, _, _ = event_ingester.read_events(root_id, limit=999_999)
    return [r for r in rows if int(r.get("seq") or 0) > after_seq]


def _msg_events(sid: str, msg_id: str) -> list:
    sess = session_manager.get(sid)
    msg = next(m for m in sess["messages"] if m["id"] == msg_id)
    return msg.get("events") or []


def _marker_color(sid: str) -> str:
    markers = session_store._markers_for_session(sid)
    return markers.get("test-orphan-fold", {}).get("color", "")


def test_marker_fires_on_immediately_folded_orphan() -> None:
    """Orphan row for a sid whose latest assistant message is ALREADY
    finalized (the steady state for externally-driven sessions) folds
    immediately AND gets attention-marker detection — closing the gap
    where orphan rows never passed through apply_event's
    source_is_provider_stream=True branch."""
    sid = _mk_session("msg-1", is_streaming=False)
    root_id = sid
    _ingest_orphan_row(
        root_id, sid, "u1",
        f"Please advise.\n<{_TAG}>Pick A or B</{_TAG}>",
    )
    for row in _rows_after(root_id, 0):
        event_bus_subscribers._apply_session_projection_row(root_id, row, 0)

    events = _msg_events(sid, "msg-1")
    landed = any(
        (e.get("data") or {}).get("uuid") == "u1" for e in events
    )
    color = _marker_color(sid)
    assert landed, f"orphan row did not land on msg.events (color={color!r})"
    assert color == "#ff8c00", f"marker did not fire (landed={landed}, color={color!r})"
    print(
        f"  {PASS} immediately-folded orphan lands on "
        f"msg.events and fires marker (landed={landed}, color={color!r})",
    )


def test_buffered_orphan_flushes_in_order_on_finalize() -> None:
    """Orphan rows arriving while the latest assistant message is still
    streaming must buffer (not apply) until finalization is observed on a
    later fold, then flush in seq order — push-driven, not polled."""
    sid = _mk_session("msg-2", is_streaming=True)
    root_id = sid
    key = (root_id, sid)

    _ingest_orphan_row(root_id, sid, "o1", "first buffered orphan")
    _ingest_orphan_row(
        root_id, sid, "o2", f"<{_TAG}>second buffered orphan</{_TAG}>",
    )
    orphan_rows = _rows_after(root_id, 0)
    assert len(orphan_rows) == 2, orphan_rows
    for row in orphan_rows:
        event_bus_subscribers._apply_session_projection_row(root_id, row, 0)

    events_before = _msg_events(sid, "msg-2")
    buffered = list(event_bus_subscribers._PENDING_ORPHAN_ROWS.get(key) or [])
    # Marker detection runs at `ingest_orphan` time (on the raw event,
    # before the ingester's write-time strip) — it's independent of
    # whether the row has folded into the render tree yet, so it's
    # already visible here. What must NOT have happened yet is the
    # render-tree fold itself (msg.events), which needs a bracket target.
    not_applied_yet = (
        not any((e.get("data") or {}).get("uuid") in ("o1", "o2") for e in events_before)
        and [r.get("data", {}).get("uuid") for r in buffered] == ["o1", "o2"]
        and _marker_color(sid) == "#ff8c00"
    )
    assert not_applied_yet, (
        "orphan rows were not buffered (or folded too early) while streaming "
        f"(buffered={len(buffered)}, applied_early={not not_applied_yet})"
    )
    print(
        f"  {PASS} orphan rows buffered "
        f"(not yet folded into msg.events) while still streaming, "
        f"marker already fired at ingest time "
        f"(buffered={len(buffered)})",
    )

    # Finalize msg-2 (mirrors orchestrator's own turn_complete flip) then
    # fold ONE more real, msg_id-owned row — this is what must push-drive
    # the buffered-orphan flush, in the SAME call, without any poll/timer.
    session_manager.set_streaming(sid, "msg-2", False)
    event_ingester.ingest(
        root_id, sid, "agent_message",
        {"uuid": "final-1", "type": "assistant",
         "message": {"content": [{"type": "text", "text": "wrap up"}]}},
        source="apply_event", msg_id="msg-2",
    )
    final_row = _rows_after(root_id, 2)
    assert len(final_row) == 1, final_row
    event_bus_subscribers._apply_session_projection_row(root_id, final_row[0], 0)

    events_after = _msg_events(sid, "msg-2")
    uuids_present = {
        (e.get("data") or {}).get("uuid")
        for e in events_after
        if (e.get("data") or {}).get("uuid") in ("o1", "o2", "final-1")
    }
    uuids_in_order = [
        (e.get("data") or {}).get("uuid")
        for e in events_after
        if (e.get("data") or {}).get("uuid") in ("o1", "o2")
    ]
    buffer_drained = not event_bus_subscribers._PENDING_ORPHAN_ROWS.get(key)
    # All three land, the two buffered rows keep their relative (seq)
    # order among themselves — the flush-vs-triggering-row interleave
    # itself isn't part of this contract, only that nothing is lost, out
    # of order relative to its buffer siblings, or silently dropped.
    flushed_ok = (
        uuids_present == {"o1", "o2", "final-1"}
        and uuids_in_order == ["o1", "o2"]
        and buffer_drained
        and _marker_color(sid) == "#ff8c00"
    )
    assert flushed_ok, (
        "buffered orphans did not flush in order on finalize "
        f"(present={uuids_present}, buffered_order={uuids_in_order}, "
        f"buffer_drained={buffer_drained}, color={_marker_color(sid)!r})"
    )
    print(
        f"  {PASS} buffered orphans flush in "
        f"order on finalize, push-driven (present={uuids_present}, "
        f"buffered_order={uuids_in_order}, buffer_drained={buffer_drained}, "
        f"color={_marker_color(sid)!r})",
    )


def main() -> int:
    try:
        _register_marker_rule()
        tests = [
            test_marker_fires_on_immediately_folded_orphan,
            test_buffered_orphan_flushes_in_order_on_finalize,
        ]
        failed = 0
        for t in tests:
            try:
                t()
            except Exception:
                import traceback
                traceback.print_exc()
                print(f"  {FAIL} {t.__name__}")
                failed += 1
                continue
            print(f"  {PASS} {t.__name__}")
        print("\n" + PASS + " all passed" if not failed else "\n" + FAIL + f" {failed} failed")
        return 1 if failed else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
