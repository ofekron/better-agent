"""Regression tests for Tier-1 lazy event fetch (message-granular).

Pins the read-side stubbing contract:

  1. `render_stub.build_stub` / `renderable_count` count the expanded
     manager/worker timeline filtered of lifecycle/non-render types
     ({complete, session_discovered, worker_prep_*}); `last_events` is
     the renderable tail.
  2. `latest_assistant_id` picks the most-recent assistant msg (max seq).
  3. `get_root_tree_stubbed` STUBS every completed non-latest assistant
     msg (empty events + `msg.stub`) and keeps the latest turn FULL.
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
import tempfile
import time
from unittest.mock import patch

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-lazy-stub-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

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
        ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                            user_msg=None, root_id=sid)
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
        ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                            user_msg=None, root_id=sid)
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

def test_build_stub_filters_lifecycle() -> bool:
    msg = {"role": "assistant", "events": [
        {"type": "agent_message", "data": {"uuid": "x1"}},
        {"type": "session_discovered", "data": {}},
        {"type": "agent_message", "data": {"uuid": "x2"}},
        {"type": "worker_prep_event", "data": {}},
        {"type": "complete", "data": {}},
    ]}
    stub = render_stub.build_stub(msg)
    if stub["event_count"] != 2:
        print(f"  expected event_count 2, got {stub['event_count']}")
        return False
    if [e.get("data", {}).get("uuid") for e in stub["last_events"]] != ["x1", "x2"]:
        print(f"  last_events wrong: {stub['last_events']}")
        return False
    # native primary list
    nat = {"role": "assistant", "events": [
        {"type": "agent_message"}, {"type": "worker_prep_start"},
    ]}
    return render_stub.renderable_count(nat) == 1


def test_build_stub_includes_worker_timeline_tail() -> bool:
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
    if uuids != ["m1", "m2", "w1", "m3"]:
        print(f"  expanded timeline tail wrong: {uuids}")
        return False
    return stub["event_count"] == 4


def test_latest_assistant_id() -> bool:
    msgs = [
        {"id": "u0", "role": "user", "seq": 0},
        {"id": "a0", "role": "assistant", "seq": 1},
        {"id": "u1", "role": "user", "seq": 2},
        {"id": "a1", "role": "assistant", "seq": 3},
    ]
    return render_stub.latest_assistant_id(msgs) == "a1"


def test_stub_tail_truncates() -> bool:
    events = [{"type": "agent_message", "data": {"uuid": str(i)}} for i in range(40)]
    msg = {"role": "assistant", "events": events}
    stub = render_stub.build_stub(msg, tail=render_stub.STUB_TAIL)
    if stub["event_count"] != 40:
        print(f"  count should be full 40, got {stub['event_count']}")
        return False
    return len(stub["last_events"]) == render_stub.STUB_TAIL


def test_stub_tail_keeps_steer_prompts() -> bool:
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
    if prompts != ["keep visible while collapsed"]:
        print(f"  steer prompt missing from collapsed stub tail: {stub['last_events']}")
        return False
    if len(stub["last_events"]) != render_stub.STUB_TAIL + 1:
        print(f"  tail should keep steer plus normal tail, got {len(stub['last_events'])}")
        return False
    explicit_stub = render_stub.build_stub_from_events(events, tail=render_stub.STUB_TAIL)
    explicit_prompts = [
        e.get("data", {}).get("prompt")
        for e in explicit_stub["last_events"]
        if e.get("type") == "steer_prompt"
    ]
    if explicit_prompts != prompts:
        print(f"  explicit-events stub dropped steer prompt: {explicit_stub['last_events']}")
        return False
    return stub["event_count"] == 41 and explicit_stub["event_count"] == 41


# ─── integration ──────────────────────────────────────────────────

def test_stubbed_load_stubs_non_latest_keeps_latest_full() -> bool:
    sid, asst1_id, asst2_id = _mk_two_turn_session()
    tree = session_manager.get_root_tree_stubbed(sid)
    msgs = {m["id"]: m for m in tree["messages"]}
    a1, a2 = msgs[asst1_id], msgs[asst2_id]
    if a1.get("stub") is None:
        print("  non-latest asst1 should be stubbed")
        return False
    if a1.get("events"):
        print(f"  asst1 events should be empty, got {a1['events']}")
        return False
    if a1["stub"]["event_count"] != 3 or len(a1["stub"]["last_events"]) != 3:
        print(f"  asst1 stub wrong: {a1['stub']}")
        return False
    if a2.get("stub") is not None:
        print("  latest asst2 must NOT be stubbed")
        return False
    if len(a2.get("events") or []) != 2:
        print(f"  asst2 should keep 2 full events, got {a2.get('events')}")
        return False
    return True


def test_manager_stubbed_cold_load_skips_hydrate_without_workers() -> bool:
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
    if calls:
        print(f"  manager stubbed cold load hydrated unexpectedly: {calls}")
        return False
    if a1.get("stub", {}).get("event_count") != 3 or a1_uuids != ["a1", "a2", "a3"]:
        print(f"  manager journal stub wrong: {a1.get('stub')}")
        return False
    if a2.get("stub") is not None or a2_uuids != ["b1", "b2"]:
        print(f"  manager latest events wrong: stub={a2.get('stub')} uuids={a2_uuids}")
        return False
    return True


def test_manager_stubbed_cold_load_skips_hydrate_with_workers() -> bool:
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
    if calls:
        print(f"  manager worker stubbed cold load hydrated unexpectedly: {calls}")
        return False
    if a1.get("events"):
        print(f"  manager worker stub events should be empty, got {a1.get('events')}")
        return False
    if a1.get("stub", {}).get("event_count") != 4:
        print(f"  manager worker journal stub wrong: {a1.get('stub')}")
        return False
    return True


def test_get_message_full_count_matches_stub() -> bool:
    sid, asst1_id, _ = _mk_two_turn_session()
    tree = session_manager.get_root_tree_stubbed(sid)
    stub = {m["id"]: m for m in tree["messages"]}[asst1_id]["stub"]
    full = session_manager.get_message_full(sid, asst1_id)
    if full is None:
        print("  get_message_full returned None")
        return False
    if len(full.get("events") or []) != 3:
        print(f"  full events expected 3, got {full.get('events')}")
        return False
    if render_stub.renderable_count(full) != stub["event_count"]:
        print(f"  count mismatch: full={render_stub.renderable_count(full)} "
              f"stub={stub['event_count']}")
        return False
    return True


def test_stub_summary_dedupes_streaming_uuid_updates() -> bool:
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
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
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
    if full is None:
        print("  get_message_full returned None")
        return False
    if render_stub.renderable_count(full) != 1:
        print(f"  full renderable count wrong: {full.get('events')}")
        return False
    if stub["event_count"] != 1:
        print(f"  stub should dedupe same uuid to 1, got {stub}")
        return False
    tail = stub["last_events"]
    text = (((tail[-1].get("data") or {}).get("message") or {})
            .get("content") or [{}])[0].get("text")
    if text != "final":
        print(f"  stub tail should carry latest mutation, got {tail}")
        return False
    return True


def test_journal_summary_matches_render_tree_event_gate() -> bool:
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
    if not summary:
        print("  missing message summary")
        return False
    if summary.get("event_count") != 2:
        print(f"  summary should count two render-tree events, got {summary}")
        return False
    tail = summary.get("last_events") or []
    serialized = repr(tail)
    if "old top" in serialized or "old wrapped" in serialized:
        print(f"  summary tail kept stale uuid snapshots: {tail}")
        return False
    if "new top" not in serialized or "new wrapped" not in serialized:
        print(f"  summary tail missing latest uuid snapshots: {tail}")
        return False
    if "not in render tree" in serialized:
        print(f"  summary tail included uuid-less provider bookkeeping: {tail}")
        return False
    return True


def test_stubbed_load_does_not_corrupt_cache() -> bool:
    sid, asst1_id, _ = _mk_two_turn_session()
    session_manager.get_root_tree_stubbed(sid)  # strips + restores
    live = session_manager.get(sid)
    a1 = next(m for m in live["messages"] if m["id"] == asst1_id)
    if len(a1.get("events") or []) != 3:
        print(f"  live cache corrupted: asst1 events={a1.get('events')}")
        return False
    if "stub" in a1:
        print("  temp stub key leaked onto live cache")
        return False
    return True


def test_native_stubbed_load_keeps_cache_thin() -> bool:
    sid, asst1_id, asst2_id = _mk_native_two_turn_session()
    tree = session_manager.get_root_tree_stubbed(sid)
    msgs = {m["id"]: m for m in tree["messages"]}
    a1, a2 = msgs[asst1_id], msgs[asst2_id]
    if a1.get("events") != []:
        print(f"  historical native events should be stubbed, got {a1.get('events')}")
        return False
    if a1.get("stub", {}).get("event_count") != 3:
        print(f"  native stub wrong: {a1.get('stub')}")
        return False
    if not a1.get("event_ref"):
        print("  native stub missing event_ref")
        return False
    if len(a2.get("events") or []) != 2:
        print(f"  latest native message should be hydrated in response, got {a2}")
        return False
    live_root = session_manager._roots.get(sid)
    live_a1 = next(m for m in live_root["messages"] if m["id"] == asst1_id)
    if live_a1.get("events"):
        print("  native cold cache should stay thin after stubbed load")
        return False
    full = session_manager.get_message_full(sid, asst1_id)
    if len(full.get("events") or []) != 3:
        print(f"  lazy full native events expected 3, got {full}")
        return False
    return True


def test_stubbed_snapshot_does_not_deepcopy_assistant_events() -> bool:
    sid, _, asst2_id = _mk_two_turn_session()
    strategy = get_strategy("manager")
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)
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
    if latest != asst2_id:
        return False
    latest_msg = {m["id"]: m for m in tree["messages"]}[latest]
    return len(latest_msg.get("events") or []) == render_stub.STUB_TAIL + 12


TESTS = [
    ("build_stub filters lifecycle types", test_build_stub_filters_lifecycle),
    ("build_stub includes worker timeline tail",
        test_build_stub_includes_worker_timeline_tail),
    ("latest_assistant_id picks max-seq assistant", test_latest_assistant_id),
    ("stub tail truncates to STUB_TAIL, count is full", test_stub_tail_truncates),
    ("stub tail keeps steer prompts", test_stub_tail_keeps_steer_prompts),
    ("stubbed load stubs non-latest, keeps latest full",
        test_stubbed_load_stubs_non_latest_keeps_latest_full),
    ("manager stubbed cold load skips hydrate without workers",
        test_manager_stubbed_cold_load_skips_hydrate_without_workers),
    ("manager stubbed cold load skips hydrate with workers",
        test_manager_stubbed_cold_load_skips_hydrate_with_workers),
    ("get_message_full count matches stub.event_count",
        test_get_message_full_count_matches_stub),
    ("stub summary dedupes streaming uuid updates",
        test_stub_summary_dedupes_streaming_uuid_updates),
    ("journal summary matches render-tree event gate",
        test_journal_summary_matches_render_tree_event_gate),
    ("strip-before-deepcopy does not corrupt live cache",
        test_stubbed_load_does_not_corrupt_cache),
    ("native stubbed load keeps cache thin, expand reads jsonl",
        test_native_stubbed_load_keeps_cache_thin),
    ("stubbed snapshot avoids assistant event deepcopy",
        test_stubbed_snapshot_does_not_deepcopy_assistant_events),
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
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
