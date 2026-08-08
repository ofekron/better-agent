"""Regression tests for Tier-1 lazy event fetch (message-granular).

Pins the read-side stubbing contract:

  1. `render_stub.build_stub` / `renderable_count` count the expanded
     manager/worker timeline filtered of lifecycle/non-render types
     ({complete, session_discovered, worker_prep_*}); `last_events` is
     the renderable tail.
  2. `latest_assistant_id` picks the most-recent assistant msg (max seq).
  3. `get_root_tree_stubbed` STUBS every completed assistant msg
     (empty events + `msg.stub`) and keeps streaming turns FULL.
  4. `get_message_full` returns the message WITH full events, and
     `stub.event_count` == the renderable count of those full events
     (single-source guarantee).
  5. The strip-before-deepcopy pop/restore does NOT corrupt the live
     cache (live events + no leftover `stub` key after the call).

Run with:
    cd backend && .venv/bin/python scripts/test_lazy_stub_fetch.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-lazy-stub-")

import render_stub  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _manager_event(uuid: str, text: str = "x") -> dict:
    return {
        "type": "manager_event",
        "data": {
            "uuid": uuid,
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


def _agent_event(uuid: str, text: str = "x") -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
    }


def _worker_event(delegation_id: str, uuid: str, text: str = "x") -> dict:
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


def _wait_for_summaries(sid: str, msg_ids: list[str], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summaries = event_journal_reader.message_event_summaries(sid)
        if all((summaries.get(msg_id) or {}).get("event_count", 0) > 0 for msg_id in msg_ids):
            return
        time.sleep(0.02)
    summaries = event_journal_reader.message_event_summaries(sid)
    raise AssertionError(f"journal summaries not ready for {msg_ids}: {summaries}")


def _mk_two_turn_session() -> tuple[str, str, str]:
    """Manager session with two completed turns. Turn 1 (asst1) has 3
    renderable events; turn 2 (asst2, the latest) has 2."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")

    def _turn(uid_prefix: str, uuids: list[str]) -> str:
        session_manager.append_user_msg(sid, {
            "id": f"user-{uid_prefix}", "role": "user",
            "content": uid_prefix, "events": [], "isStreaming": False,
        })
        asst = strategy.build_assistant_scaffold()
        session_manager.append_assistant_msg(sid, asst)
        msg = session_manager.get_ref(sid)["messages"][-1]
        ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=sid)
        for u in uuids:
            strategy.apply_event(app_session_id=sid, msg=msg,
                                 event=_manager_event(u), ctx=ctx, source_is_provider_stream=True)
        msg["isStreaming"] = False
        return msg["id"]

    asst1_id = _turn("q1", ["a1", "a2", "a3"])
    asst2_id = _turn("q2", ["b1", "b2"])
    _wait_for_summaries(sid, [asst1_id, asst2_id])
    return sid, asst1_id, asst2_id


def _mk_two_turn_session_with_worker() -> tuple[str, str, str]:
    sess = session_manager.create(
        name="tw", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")

    def _turn(uid_prefix: str, uuids: list[str], with_worker: bool = False) -> str:
        session_manager.append_user_msg(sid, {
            "id": f"user-worker-{uid_prefix}", "role": "user",
            "content": uid_prefix, "events": [], "isStreaming": False,
        })
        asst = strategy.build_assistant_scaffold()
        if with_worker:
            asst["workers"] = [{
                "delegation_id": "del_worker",
                "worker_session_id": "worker-session",
                "worker_description": "test worker",
                "is_new": False,
                "instructions_preview": "",
                "events": [],
                "jsonl_path": None,
                "new_byte_offset": None,
                "fork_agent_sid": None,
                "token_usage": None,
                "insert_at": 2,
            }]
        session_manager.append_assistant_msg(sid, asst)
        msg = session_manager.get_ref(sid)["messages"][-1]
        ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=sid)
        for u in uuids:
            strategy.apply_event(app_session_id=sid, msg=msg,
                                 event=_manager_event(u), ctx=ctx, source_is_provider_stream=True)
        if with_worker:
            strategy.apply_event(app_session_id=sid, msg=msg,
                                 event=_worker_event("del_worker", "w1"), ctx=ctx, source_is_provider_stream=True)
        msg["isStreaming"] = False
        return msg["id"]

    asst1_id = _turn("q1", ["a1", "a2", "a3"], with_worker=True)
    asst2_id = _turn("q2", ["b1", "b2"])
    _wait_for_summaries(sid, [asst1_id, asst2_id])
    return sid, asst1_id, asst2_id


def _native_event(uuid: str, text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _mk_native_two_turn_session() -> tuple[str, str, str]:
    sess = session_manager.create(
        name="native", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")

    def _turn(uid_prefix: str, uuids: list[str]) -> str:
        session_manager.append_user_msg(sid, {
            "id": f"user-native-{uid_prefix}", "role": "user",
            "content": uid_prefix, "events": [], "isStreaming": False,
        })
        asst = strategy.build_assistant_scaffold()
        session_manager.append_assistant_msg(sid, asst)
        ctx = ApplyEventCtx(root_id=sid, run_id=f"run-{uid_prefix}")
        msg = session_manager.get_ref(sid)["messages"][-1]
        for u in uuids:
            strategy.apply_event(
                app_session_id=sid, msg=msg,
                event=_native_event(u, f"text-{u}"), ctx=ctx, source_is_provider_stream=True,
            )
        msg["isStreaming"] = False
        return msg["id"]

    asst1_id = _turn("q1", ["n1", "n2", "n3"])
    asst2_id = _turn("q2", ["n4", "n5"])
    session_manager.flush_pending_persists()
    session_manager._roots.pop(sid, None)
    session_manager._event_hydrated_roots.discard(sid)
    return sid, asst1_id, asst2_id


# ─── unit ─────────────────────────────────────────────────────────

def test_build_stub_filters_lifecycle() -> None:
    msg = {"role": "assistant", "events": [
        {"type": "agent_message", "data": {"uuid": "x1"}},
        {"type": "session_discovered", "data": {}},
        {"type": "agent_message", "data": {"uuid": "x2"}},
        {"type": "worker_prep_event", "data": {}},
        {"type": "complete", "data": {}},
    ]}
    stub = render_stub.build_stub(msg)
    assert stub["event_count"] == 2, f"expected event_count 2, got {stub['event_count']}"
    assert [e.get("data", {}).get("uuid") for e in stub["last_events"]] == ["x1", "x2"], (
        f"last_events wrong: {stub['last_events']}"
    )
    # native primary list
    nat = {"role": "assistant", "events": [
        {"type": "agent_message"}, {"type": "worker_prep_start"},
    ]}
    assert render_stub.renderable_count(nat) == 1


def test_build_stub_includes_worker_timeline_tail() -> None:
    msg = {
        "role": "assistant",
        "events": [
            {"type": "agent_message", "data": {"uuid": "m1"}},
            {"type": "complete", "data": {"uuid": "ignored-manager"}},
            {"type": "agent_message", "data": {"uuid": "m2"}},
            {"type": "agent_message", "data": {"uuid": "m3"}},
        ],
        "workers": [
            {
                "delegation_id": "w1",
                "insert_at": 3,
                "events": [
                    {"type": "agent_message", "data": {"uuid": "w1"}},
                    {"type": "complete", "data": {"uuid": "ignored"}},
                ],
            },
            {
                "delegation_id": "w1",
                "insert_at": 3,
                "events": [
                    {"type": "agent_message", "data": {"uuid": "duplicate"}},
                ],
            }
        ],
    }
    stub = render_stub.build_stub(msg)
    uuids = [e.get("data", {}).get("uuid") for e in stub["last_events"]]
    assert uuids == ["m1", "m2", "w1", "m3"], f"expanded timeline tail wrong: {uuids}"
    assert stub["event_count"] == 4


def test_latest_assistant_id() -> None:
    msgs = [
        {"id": "u0", "role": "user", "seq": 0},
        {"id": "a0", "role": "assistant", "seq": 1},
        {"id": "u1", "role": "user", "seq": 2},
        {"id": "a1", "role": "assistant", "seq": 3},
    ]
    assert render_stub.latest_assistant_id(msgs) == "a1"


def test_stub_tail_truncates() -> None:
    events = [{"type": "agent_message", "data": {"uuid": str(i)}} for i in range(40)]
    msg = {"role": "assistant", "events": events}
    stub = render_stub.build_stub(msg, tail=render_stub.STUB_TAIL)
    assert stub["event_count"] == 40, f"count should be full 40, got {stub['event_count']}"
    assert len(stub["last_events"]) == render_stub.STUB_TAIL


def test_stub_tail_keeps_steer_prompts() -> None:
    steer = {
        "type": "steer_prompt",
        "data": {"uuid": "steer-1", "prompt": "keep visible while collapsed"},
    }
    events = [steer] + [
        {"type": "agent_message", "data": {"uuid": str(i)}} for i in range(40)
    ]
    msg = {"role": "assistant", "events": events}
    stub = render_stub.build_stub(msg, tail=render_stub.STUB_TAIL)
    prompts = [
        e.get("data", {}).get("prompt")
        for e in stub["last_events"]
        if e.get("type") == "steer_prompt"
    ]
    assert prompts == ["keep visible while collapsed"], (
        f"steer prompt missing from collapsed stub tail: {stub['last_events']}"
    )
    assert len(stub["last_events"]) == render_stub.STUB_TAIL + 1, (
        f"tail should keep steer plus normal tail, got {len(stub['last_events'])}"
    )
    explicit_stub = render_stub.build_stub_from_events(events, tail=render_stub.STUB_TAIL)
    explicit_prompts = [
        e.get("data", {}).get("prompt")
        for e in explicit_stub["last_events"]
        if e.get("type") == "steer_prompt"
    ]
    assert explicit_prompts == prompts, (
        f"explicit-events stub dropped steer prompt: {explicit_stub['last_events']}"
    )
    assert stub["event_count"] == 41
    assert explicit_stub["event_count"] == 41


# ─── integration ──────────────────────────────────────────────────

def test_stubbed_load_stubs_completed_latest() -> None:
    sid, asst1_id, asst2_id = _mk_two_turn_session()
    tree = session_manager.get_root_tree_stubbed(sid)
    msgs = {m["id"]: m for m in tree["messages"]}
    a1, a2 = msgs[asst1_id], msgs[asst2_id]
    assert a1.get("stub") is not None, "non-latest asst1 should be stubbed"
    assert not a1.get("events"), f"asst1 events should be empty, got {a1['events']}"
    assert a1["stub"]["event_count"] == 3, f"asst1 stub wrong: {a1['stub']}"
    assert len(a1["stub"]["last_events"]) == 3, f"asst1 stub wrong: {a1['stub']}"
    assert not a2.get("events"), f"completed latest asst2 events should be empty, got {a2.get('events')}"
    assert a2.get("stub", {}).get("event_count") == 2, (
        f"completed latest asst2 should be stubbed, got {a2.get('stub')}"
    )
    assert a2.get("event_ref"), "completed latest asst2 missing event_ref"


def test_stubbed_load_keeps_streaming_latest_full() -> None:
    sid, _, asst2_id = _mk_two_turn_session()
    live = session_manager.get_ref(sid)
    latest = next(m for m in live["messages"] if m["id"] == asst2_id)
    latest["isStreaming"] = True
    session_manager._tree_stub_cache.clear()
    session_manager._tree_stub_attached_cache.clear()

    tree = session_manager.get_root_tree_stubbed(sid)
    msg = {m["id"]: m for m in tree["messages"]}[asst2_id]
    assert msg.get("stub") is None, f"streaming latest must stay full, got stub={msg.get('stub')}"
    assert len(msg.get("events") or []) == 2, (
        f"streaming latest should keep full events, got {msg.get('events')}"
    )


def test_message_window_stubs_completed_latest() -> None:
    sid, _asst1_id, asst2_id = _mk_two_turn_session()
    delta = session_manager.get_messages_since(sid, since_seq=0, limit=50)
    assert delta, "get_messages_since returned no delta"
    msg = {m["id"]: m for m in delta["messages"]}[asst2_id]
    assert not msg.get("events"), f"completed latest delta events should be empty, got {msg.get('events')}"
    assert msg.get("stub", {}).get("event_count") == 2, (
        f"completed latest delta should be stubbed, got {msg.get('stub')}"
    )
    assert msg.get("event_ref"), "completed latest delta missing event_ref"


def test_manager_stubbed_cold_load_skips_hydrate_without_workers() -> None:
    sid, asst1_id, asst2_id = _mk_two_turn_session()
    session_manager.flush_pending_persists()
    session_manager._roots.pop(sid, None)
    session_manager._event_hydrated_roots.discard(sid)

    original = session_manager._hydrate_cached_root_events
    calls: list[str] = []

    def spy(rid, root):
        calls.append(rid)
        return original(rid, root)

    session_manager._hydrate_cached_root_events = spy
    try:
        tree = session_manager.get_root_tree_stubbed(sid, msg_limit=20)
    finally:
        session_manager._hydrate_cached_root_events = original

    msgs = {m["id"]: m for m in tree["messages"]}
    a1, a2 = msgs[asst1_id], msgs[asst2_id]
    a1_uuids = [
        e.get("data", {}).get("uuid")
        for e in a1.get("stub", {}).get("last_events") or []
    ]
    a2_uuids = [
        e.get("data", {}).get("uuid")
        for e in a2.get("events") or []
    ]
    assert not calls, f"manager stubbed cold load hydrated unexpectedly: {calls}"
    assert a1.get("stub", {}).get("event_count") == 3, f"manager journal stub wrong: {a1.get('stub')}"
    assert a1_uuids == ["a1", "a2", "a3"], f"manager journal stub wrong: {a1.get('stub')}"
    assert a2.get("stub", {}).get("event_count") == 2, (
        f"manager completed latest stub wrong: stub={a2.get('stub')} uuids={a2_uuids}"
    )
    assert not a2.get("events"), (
        f"manager completed latest stub wrong: stub={a2.get('stub')} uuids={a2_uuids}"
    )


def test_manager_stubbed_cold_load_skips_hydrate_with_workers() -> None:
    sid, asst1_id, _ = _mk_two_turn_session_with_worker()
    session_manager.flush_pending_persists()
    session_manager._roots.pop(sid, None)
    session_manager._event_hydrated_roots.discard(sid)

    original = session_manager._hydrate_cached_root_events
    calls: list[str] = []

    def spy(rid, root):
        calls.append(rid)
        return original(rid, root)

    session_manager._hydrate_cached_root_events = spy
    try:
        tree = session_manager.get_root_tree_stubbed(sid, msg_limit=20)
    finally:
        session_manager._hydrate_cached_root_events = original

    a1 = {m["id"]: m for m in tree["messages"]}[asst1_id]
    assert not calls, f"manager worker stubbed cold load hydrated unexpectedly: {calls}"
    assert not a1.get("events"), f"manager worker stub events should be empty, got {a1.get('events')}"
    assert a1.get("stub", {}).get("event_count") == 4, f"manager worker journal stub wrong: {a1.get('stub')}"


def test_get_message_full_count_matches_stub() -> None:
    sid, asst1_id, _ = _mk_two_turn_session()
    tree = session_manager.get_root_tree_stubbed(sid)
    stub = {m["id"]: m for m in tree["messages"]}[asst1_id]["stub"]
    full = session_manager.get_message_full(sid, asst1_id)
    assert full is not None, "get_message_full returned None"
    assert len(full.get("events") or []) == 3, f"full events expected 3, got {full.get('events')}"
    assert render_stub.renderable_count(full) == stub["event_count"], (
        f"count mismatch: full={render_stub.renderable_count(full)} stub={stub['event_count']}"
    )


def test_stub_summary_dedupes_streaming_uuid_updates() -> None:
    sess = session_manager.create(
        name="streaming-summary", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")

    session_manager.append_user_msg(sid, {
        "id": "user-streaming-1", "role": "user",
        "content": "q1", "events": [], "isStreaming": False,
    })
    asst = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst)
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=sid)
    strategy.apply_event(app_session_id=sid, msg=msg,
                         event=_agent_event("same", "partial"), ctx=ctx,
                         source_is_provider_stream=True)
    strategy.apply_event(app_session_id=sid, msg=msg,
                         event=_agent_event("same", "final"), ctx=ctx,
                         source_is_provider_stream=True)
    msg["isStreaming"] = False
    asst1_id = msg["id"]

    session_manager.append_user_msg(sid, {
        "id": "user-streaming-2", "role": "user",
        "content": "q2", "events": [], "isStreaming": False,
    })
    asst2 = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst2)
    latest = session_manager.get_ref(sid)["messages"][-1]
    strategy.apply_event(app_session_id=sid, msg=latest,
                         event=_agent_event("latest", "latest"), ctx=ctx,
                         source_is_provider_stream=True)
    latest["isStreaming"] = False

    _wait_for_summaries(sid, [asst1_id, latest["id"]])
    tree = session_manager.get_root_tree_stubbed(sid)
    stub = {m["id"]: m for m in tree["messages"]}[asst1_id]["stub"]
    full = session_manager.get_message_full(sid, asst1_id)
    assert full is not None, "get_message_full returned None"
    assert render_stub.renderable_count(full) == 1, f"full renderable count wrong: {full.get('events')}"
    assert stub["event_count"] == 1, f"stub should dedupe same uuid to 1, got {stub}"
    tail = stub["last_events"]
    text = (((tail[-1].get("data") or {}).get("message") or {})
            .get("content") or [{}])[0].get("text")
    assert text == "final", f"stub tail should carry latest mutation, got {tail}"


def test_journal_stubbed_load_keeps_steer_prompts() -> None:
    sess = session_manager.create(
        name="journal-steer-summary", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")

    session_manager.append_user_msg(sid, {
        "id": "user-journal-steer-1", "role": "user",
        "content": "q1", "events": [], "isStreaming": False,
    })
    asst = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst)
    first = session_manager.get_ref(sid)["messages"][-1]
    first["isStreaming"] = False
    first_id = first["id"]

    event_ingester.ingest(
        sid, sid, "steer_prompt",
        {"uuid": "steer-journal-1", "prompt": "visible from journal summary"},
        source="test", msg_id=first_id, cwd_override="/tmp",
    )
    for idx in range(40):
        event_ingester.ingest(
            sid, sid, "agent_message",
            {
                "uuid": f"journal-tail-{idx}",
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"tail {idx}"}],
                },
            },
            source="test", msg_id=first_id, cwd_override="/tmp",
        )

    session_manager.append_user_msg(sid, {
        "id": "user-journal-steer-2", "role": "user",
        "content": "q2", "events": [], "isStreaming": False,
    })
    latest = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, latest)
    latest_msg = session_manager.get_ref(sid)["messages"][-1]
    latest_msg["isStreaming"] = False
    event_ingester.ingest(
        sid, sid, "agent_message",
        {
            "uuid": "journal-latest",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "latest"}],
            },
        },
        source="test", msg_id=latest_msg["id"], cwd_override="/tmp",
    )

    _wait_for_summaries(sid, [first_id, latest_msg["id"]])
    tree = session_manager.get_root_tree_stubbed(sid)
    stub = {m["id"]: m for m in tree["messages"]}[first_id]["stub"]
    prompts = [
        e.get("data", {}).get("prompt")
        for e in stub.get("last_events") or []
        if e.get("type") == "steer_prompt"
    ]
    assert prompts == ["visible from journal summary"], (
        f"journal-backed collapsed stub dropped steer prompt: {stub}"
    )
    assert stub.get("event_count") == 41, f"journal-backed stub count wrong: {stub}"


def test_journal_summary_matches_render_tree_event_gate() -> None:
    root_id = "summary-render-gate"
    msg_id = "assistant-summary-render-gate"

    event_ingester.ingest(
        root_id, root_id, "manager_event",
        {
            "event": {
                "type": "agent_message",
                "uuid": "top-level",
                "data": {
                    "type": "assistant",
                    "message": {"content": "old top"},
                },
            },
        },
        source="test", msg_id=msg_id, cwd_override="/tmp",
    )
    event_ingester.ingest(
        root_id, root_id, "manager_event",
        {
            "event": {
                "type": "agent_message",
                "uuid": "top-level",
                "data": {
                    "type": "assistant",
                    "message": {"content": "new top"},
                },
            },
        },
        source="test", msg_id=msg_id, cwd_override="/tmp",
    )
    event_ingester.ingest(
        root_id, root_id, "worker_event",
        {
            "delegation_id": "worker-1",
            "event": {
                "type": "agent_message",
                "data": {
                    "uuid": "wrapped",
                    "type": "assistant",
                    "message": {"content": "old wrapped"},
                },
            },
        },
        source="test", msg_id=msg_id, cwd_override="/tmp",
    )
    event_ingester.ingest(
        root_id, root_id, "worker_event",
        {
            "delegation_id": "worker-1",
            "event": {
                "type": "agent_message",
                "data": {
                    "uuid": "wrapped",
                    "type": "assistant",
                    "message": {"content": "new wrapped"},
                },
            },
        },
        source="test", msg_id=msg_id, cwd_override="/tmp",
    )
    event_ingester.ingest(
        root_id, root_id, "agent_message",
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": "not in render tree",
        },
        source="test", msg_id=msg_id, cwd_override="/tmp",
    )

    summary = event_ingester.message_event_summaries(
        root_id, sid_filter=root_id, msg_ids={msg_id}, tail=25,
    ).get(msg_id)
    assert summary, "missing message summary"
    assert summary.get("event_count") == 2, f"summary should count two render-tree events, got {summary}"
    tail = summary.get("last_events") or []
    serialized = repr(tail)
    assert "old top" not in serialized and "old wrapped" not in serialized, (
        f"summary tail kept stale uuid snapshots: {tail}"
    )
    assert "new top" in serialized and "new wrapped" in serialized, (
        f"summary tail missing latest uuid snapshots: {tail}"
    )
    assert "not in render tree" not in serialized, (
        f"summary tail included uuid-less provider bookkeeping: {tail}"
    )


def test_stubbed_load_does_not_corrupt_cache() -> None:
    sid, asst1_id, _ = _mk_two_turn_session()
    session_manager.get_root_tree_stubbed(sid)  # strips + restores
    live = session_manager.get(sid)
    a1 = next(m for m in live["messages"] if m["id"] == asst1_id)
    assert len(a1.get("events") or []) == 3, f"live cache corrupted: asst1 events={a1.get('events')}"
    assert "stub" not in a1, "temp stub key leaked onto live cache"


def test_native_stubbed_load_keeps_cache_thin() -> None:
    sid, asst1_id, asst2_id = _mk_native_two_turn_session()
    tree = session_manager.get_root_tree_stubbed(sid)
    msgs = {m["id"]: m for m in tree["messages"]}
    a1, a2 = msgs[asst1_id], msgs[asst2_id]
    assert a1.get("events") == [], f"historical native events should be stubbed, got {a1.get('events')}"
    assert a1.get("stub", {}).get("event_count") == 3, f"native stub wrong: {a1.get('stub')}"
    assert a1.get("event_ref"), "native stub missing event_ref"
    assert not a2.get("events"), f"completed latest native message should be stubbed, got {a2}"
    assert a2.get("stub", {}).get("event_count") == 2, f"completed latest native stub wrong: {a2.get('stub')}"
    live_root = session_manager._roots.get(sid)
    live_a1 = next(m for m in live_root["messages"] if m["id"] == asst1_id)
    assert not live_a1.get("events"), "native cold cache should stay thin after stubbed load"
    full = session_manager.get_message_full(sid, asst1_id)
    assert len(full.get("events") or []) == 3, f"lazy full native events expected 3, got {full}"


def test_stubbed_snapshot_does_not_deepcopy_assistant_events() -> None:
    sid, _, asst2_id = _mk_two_turn_session()
    strategy = get_strategy("manager")
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=sid)
    for idx in range(render_stub.STUB_TAIL + 10):
        strategy.apply_event(app_session_id=sid, msg=msg,
                             event=_manager_event(f"big-{idx}"), ctx=ctx,
                             source_is_provider_stream=True)
    msg["isStreaming"] = False
    session_manager.flush_pending_persists()
    session_manager._since_cache.pop(sid, None)

    original = session_manager._compute_messages_snapshot.__globals__["copy"].deepcopy

    def guarded(value):
        if (
            isinstance(value, list)
            and len(value) > render_stub.STUB_TAIL
            and all(isinstance(item, dict) and item.get("type") for item in value)
        ):
            raise AssertionError("assistant event list was deep-copied")
        return original(value)

    with patch(
        "session_manager.copy.deepcopy",
        side_effect=guarded,
    ):
        tree = session_manager.get_root_tree_stubbed(sid)

    latest = render_stub.latest_assistant_id(tree.get("messages") or [])
    assert latest == asst2_id
    latest_msg = {m["id"]: m for m in tree["messages"]}[latest]
    assert latest_msg.get("events") == []
    assert latest_msg.get("stub", {}).get("event_count") == render_stub.STUB_TAIL + 12


TESTS = [
    ("build_stub filters lifecycle types", test_build_stub_filters_lifecycle),
    ("build_stub includes worker timeline tail",
        test_build_stub_includes_worker_timeline_tail),
    ("latest_assistant_id picks max-seq assistant", test_latest_assistant_id),
    ("stub tail truncates to STUB_TAIL, count is full", test_stub_tail_truncates),
    ("stub tail keeps steer prompts", test_stub_tail_keeps_steer_prompts),
    ("stubbed load stubs completed latest",
        test_stubbed_load_stubs_completed_latest),
    ("stubbed load keeps streaming latest full",
        test_stubbed_load_keeps_streaming_latest_full),
    ("message window stubs completed latest",
        test_message_window_stubs_completed_latest),
    ("manager stubbed cold load skips hydrate without workers",
        test_manager_stubbed_cold_load_skips_hydrate_without_workers),
    ("manager stubbed cold load skips hydrate with workers",
        test_manager_stubbed_cold_load_skips_hydrate_with_workers),
    ("get_message_full count matches stub.event_count",
        test_get_message_full_count_matches_stub),
    ("stub summary dedupes streaming uuid updates",
        test_stub_summary_dedupes_streaming_uuid_updates),
    ("journal-backed stubbed load keeps steer prompts",
        test_journal_stubbed_load_keeps_steer_prompts),
    ("journal summary matches render-tree event gate",
        test_journal_summary_matches_render_tree_event_gate),
    ("strip-before-deepcopy does not corrupt live cache",
        test_stubbed_load_does_not_corrupt_cache),
    ("native stubbed load keeps cache thin, expand reads jsonl",
        test_native_stubbed_load_keeps_cache_thin),
    ("stubbed snapshot avoids assistant event deepcopy",
        test_stubbed_snapshot_does_not_deepcopy_assistant_events),
]


def main_run() -> None:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"{FAIL}  {name}: {e}")
            except Exception:
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"{FAIL}  {name}")
            else:
                print(f"{PASS}  {name}")
        print()
        if failed:
            print(f"{failed} of {len(TESTS)} test(s) FAILED")
            raise SystemExit(1)
        print(f"all {len(TESTS)} tests passed")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main_run()
