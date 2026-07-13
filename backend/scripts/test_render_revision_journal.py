#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="better-agent-render-revision-")
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import render_revision_store
import session_store
from event_ingester import event_ingester
from session_manager import SessionManager


def _session(sid: str) -> dict:
    return {
        "version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "messages": [],
        "forks": [],
    }


def _manager(sid: str) -> SessionManager:
    session_store.write_session_full(_session(sid), bump_updated_at=False)
    manager = SessionManager()
    assert manager.get_lite(sid)
    return manager


def test_snapshot_subscribe_boundary() -> None:
    sid = "boundary"
    manager = _manager(sid)
    fence = render_revision_store.fence(sid)
    barrier = threading.Barrier(2)

    def mutate() -> None:
        barrier.wait()
        manager.append_user_msg(sid, {"id": "u1", "role": "user", "content": "x"})

    thread = threading.Thread(target=mutate)
    thread.start()
    barrier.wait()
    thread.join()
    replay = manager.replay_render_deltas(
        sid,
        incarnation=fence["incarnation"],
        after_revision=fence["render_revision"],
    )
    assert replay["status"] == "ok"
    assert [entry["revision"] for entry in replay["entries"]] == [1]
    assert replay["entries"][0]["delta"]["op"] == "replace_turn"


def test_atomic_compact_page_fence_and_older_page() -> None:
    sid = "compact-page"
    manager = _manager(sid)
    for index in range(3):
        manager.append_user_msg(
            sid, {"id": f"u{index}", "role": "user", "content": str(index)},
        )
        manager.append_assistant_msg(
            sid, {"id": f"a{index}", "role": "assistant", "content": str(index)},
        )
    latest = manager.get_compact_turn_page(sid, turn_limit=2)
    assert latest is not None
    assert [turn["prompt"]["id"] for turn in latest["turns"]] == ["u1", "u2"]
    older = manager.get_compact_turn_page(
        sid,
        turn_limit=2,
        before_seq=latest["page_cursor"]["before_seq"],
    )
    assert older is not None
    assert [turn["prompt"]["id"] for turn in older["turns"]] == ["u0"]
    manager.truncate_messages(sid, 4)
    replay = manager.replay_render_deltas(
        sid,
        incarnation=latest["incarnation"],
        after_revision=latest["render_revision"],
    )
    assert replay["status"] == "ok"
    assert len(replay["entries"]) == 1
    assert replay["entries"][0]["delta"]["op"] == "truncate_after_seq"


def test_live_event_after_rest_page_is_above_snapshot_watermark() -> None:
    sid = "event-watermark-gap"
    manager = _manager(sid)
    page = manager.get_compact_turn_page(sid, turn_limit=1)
    assert page is not None
    watermark = page["events_watermark"]
    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="agent_message",
        data={"type": "assistant", "uuid": "after-rest"},
        source="test",
        msg_id=None,
    )
    assert event_ingester.max_seq_by_sid(sid)[sid] > watermark


def test_delete_tombstone_and_truncate() -> None:
    sid = "delete"
    manager = _manager(sid)
    manager.append_assistant_msg(sid, {"id": "a1", "role": "assistant", "content": "a"})
    manager.remove_assistant_msg(sid, "a1")
    manager.truncate_messages(sid, 0)
    fence = render_revision_store.fence(sid)
    replay = manager.replay_render_deltas(
        sid, incarnation=fence["incarnation"], after_revision=0,
    )
    assert [entry["delta"]["op"] for entry in replay["entries"]] == [
        "replace_turn", "delete_turn", "truncate_after_seq",
    ]
    assert replay["entries"][1]["delta"]["turn_id"]


def test_gap_and_incarnation_fail_closed() -> None:
    sid = "gap"
    manager = _manager(sid)
    manager.append_user_msg(sid, {"id": "u1", "role": "user", "content": "x"})
    manager.append_assistant_msg(sid, {"id": "a1", "role": "assistant", "content": "a"})
    fence = render_revision_store.fence(sid)
    render_revision_store._states[sid]["entries"] = [
        render_revision_store._states[sid]["entries"][1]
    ]
    gap = manager.replay_render_deltas(
        sid, incarnation=fence["incarnation"], after_revision=0,
    )
    assert gap["status"] == "resnapshot_required"
    wrong = manager.replay_render_deltas(
        sid, incarnation="wrong", after_revision=1,
    )
    assert wrong["status"] == "resnapshot_required"


def test_restart_requires_resnapshot() -> None:
    sid = "restart"
    manager = _manager(sid)
    manager.append_user_msg(sid, {"id": "u1", "role": "user", "content": "x"})
    before = render_revision_store.fence(sid)
    restarted_store = importlib.reload(render_revision_store)
    after = restarted_store.fence(sid)
    assert after["incarnation"] != before["incarnation"]
    assert after["render_revision"] == 0
    replay = restarted_store.replay(
        sid, incarnation=before["incarnation"], after_revision=0,
    )
    assert replay["status"] == "resnapshot_required"


def test_internal_changes_do_not_advance_revision() -> None:
    sid = "internal"
    manager = _manager(sid)
    before = render_revision_store.fence(sid)
    manager._fire(sid, {"kind": "processed_lines_advanced", "value": 10})
    after = render_revision_store.fence(sid)
    assert after == before


def test_live_turn_changes_do_not_advance_revision() -> None:
    sid = "live"
    manager = _manager(sid)
    before = render_revision_store.fence(sid)
    manager._fire(sid, {
        "kind": "running_content_updated",
        "msg_id": "a1",
        "content": "token",
    })
    manager._fire(sid, {
        "kind": "journal_event_projected",
        "msg_id": "a1",
        "delta": {"id": "a1", "content": "tool"},
    })
    after = render_revision_store.fence(sid)
    assert after == before


def test_retention_overflow_fails_closed() -> None:
    sid = "overflow"
    manager = _manager(sid)
    original_limit = render_revision_store._MAX_ENTRIES
    render_revision_store._MAX_ENTRIES = 2
    try:
        fence = render_revision_store.fence(sid)
        for index in range(3):
            manager.append_user_msg(
                sid,
                {"id": f"u{index}", "role": "user", "content": str(index)},
            )
        replay = manager.replay_render_deltas(
            sid, incarnation=fence["incarnation"], after_revision=0,
        )
        assert replay["status"] == "resnapshot_required"
    finally:
        render_revision_store._MAX_ENTRIES = original_limit


def _apply_turn_delta(turns: list[dict], delta: dict) -> list[dict]:
    op = delta["op"]
    if op == "replace_turn":
        return [
            delta["turn"] if turn["id"] == delta["turn_id"] else turn
            for turn in turns
            if turn["id"] != delta["turn_id"] or delta.get("turn")
        ] + (
            [delta["turn"]]
            if not any(turn["id"] == delta["turn_id"] for turn in turns)
            else []
        )
    if op == "delete_turn":
        return [turn for turn in turns if turn["id"] != delta["turn_id"]]
    if op == "truncate_after_seq":
        boundary = delta.get("after_seq")
        return [] if boundary is None else [
            turn for turn in turns if (turn.get("end_seq") or 0) <= boundary
        ]
    return turns


def test_rest_page_plus_turn_deltas_converges_exactly() -> None:
    sid = "turn-convergence"
    manager = _manager(sid)
    for index in range(3):
        manager.append_user_msg(
            sid, {"id": f"u{index}", "role": "user", "content": str(index)},
        )
        manager.append_assistant_msg(
            sid, {"id": f"a{index}", "role": "assistant", "content": str(index)},
        )
    snapshot = manager.get_compact_turn_page(sid, turn_limit=10)
    assert snapshot is not None
    turns = snapshot["turns"]
    manager.set_msg_ask_result(sid, "a2", {
        "reasoning": "Choose", "session_ids": ["target"],
    })
    manager.set_completed_at(sid, "a2", "2026-01-01T00:00:01")
    replay = manager.replay_render_deltas(
        sid,
        incarnation=snapshot["incarnation"],
        after_revision=snapshot["render_revision"],
    )
    assert replay["status"] == "ok"
    for entry in replay["entries"]:
        turns = _apply_turn_delta(turns, entry["delta"])
    completed = manager.get_compact_turn_page(sid, turn_limit=10)
    assert completed is not None
    assert turns == completed["turns"]
    manager.truncate_messages(sid, 4)
    manager.remove_assistant_msg(sid, "a1")
    replay = manager.replay_render_deltas(
        sid,
        incarnation=completed["incarnation"],
        after_revision=completed["render_revision"],
    )
    assert replay["status"] == "ok"
    for entry in replay["entries"]:
        turns = _apply_turn_delta(turns, entry["delta"])
    fresh = manager.get_compact_turn_page(sid, turn_limit=10)
    assert fresh is not None
    assert turns == fresh["turns"]


if __name__ == "__main__":
    tests = [
        test_snapshot_subscribe_boundary,
        test_atomic_compact_page_fence_and_older_page,
        test_live_event_after_rest_page_is_above_snapshot_watermark,
        test_delete_tombstone_and_truncate,
        test_gap_and_incarnation_fail_closed,
        test_restart_requires_resnapshot,
        test_internal_changes_do_not_advance_revision,
        test_live_turn_changes_do_not_advance_revision,
        test_retention_overflow_fails_closed,
        test_rest_page_plus_turn_deltas_converges_exactly,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
