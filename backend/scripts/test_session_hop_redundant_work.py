"""Perf-regression tests for session-hop work.

Pins the post-fix invariants that make a session hop O(1) work in
steady state instead of O(N-events × N-panes):

  1. **WS subscribe builds messages_replay from session_manager only,
     and caps the payload at 50 messages even when `since_seq=0`** —
     no full-tree deepcopy, no full-history serialization on cold
     hop.

  2. **Subscribe stale-debug probes stay off the normal path**. Replay
     details are useful when DEBUG is enabled, but the normal reconnect
     path must not run extra session-manager probes or write INFO logs
     per subscribe.

The single-flight-reconcile-per-cold-hop, dirty-flag, delayed-progress-
badge, and loop-responsiveness-during-reconcile invariants this file used
to pin were retired with the async dirty-flag reconcile subsystem: the
write-time projection fold (`event_bus_subscribers.SessionProjectionDrainer`)
now folds every written journal row as it lands — single-flight admission,
capacity backpressure, and shutdown-bounded drain are pinned directly on
the drainer in `test_session_projection_drainer.py`; orphan-fold immediacy
(finalized vs. still-streaming) is pinned in
`test_tailer_routes_through_apply_event.py`.

Run with:
    cd backend && .venv/bin/python scripts/test_session_hop_redundant_work.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path
from unittest.mock import patch

import _test_home
import _test_installation

_TMP_HOME = _test_home.isolate("bc-test-hop-perf-")
_test_installation.activate(Path(_TMP_HOME), provider="claude")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
import event_ingester as ei_mod  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from event_bus_subscribers import (  # noqa: E402
    await_session_content_projection,
    register_default_subscribers,
    shutdown_session_content_projection,
)

event_ingester = ei_mod.event_ingester


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── Setup helpers ──────────────────────────────────────────────────


def _fresh_session(n_finalized_assistant: int = 3) -> str:
    """Create a session with `n_finalized_assistant` finalized assistant
    msgs interleaved with user msgs, AND one extra assistant msg with
    `events=[]` so the reconcile fast path falls through. Returns sid
    (also the root_id since this is a root)."""
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    for _ in range(n_finalized_assistant):
        u_id = str(uuid.uuid4())
        a_id = str(uuid.uuid4())
        session_manager.append_user_msg(sid, {
            "id": u_id, "role": "user", "content": "u",
            "events": [], "isStreaming": False,
        })
        session_manager.append_assistant_msg(sid, {
            "id": a_id, "role": "assistant", "content": "a",
            "events": [{
                "type": "assistant",
                "data": {"type": "assistant", "uuid": str(uuid.uuid4())},
            }],
            "isStreaming": False,
        })
    # Add one assistant msg with empty events so reconcile's fast path
    # falls through and we can observe `read_events` being called.
    bare_id = str(uuid.uuid4())
    session_manager.append_assistant_msg(sid, {
        "id": bare_id, "role": "assistant", "content": "",
        "events": [], "isStreaming": False,
    })
    return sid


def test_non_render_orphans_do_not_invalidate_snapshot() -> bool:
    sid = _fresh_session()
    _ = session_manager.get_root_tree_stubbed(sid)
    cached = session_manager._since_cache.get(sid)
    if cached is None:
        print("  expected warm _since_cache entry")
        return False
    before_key, before_snapshot = cached

    cases = [
        ("command_received", {
            "method": "POST",
            "path": f"/api/sessions/{sid}/seen",
            "uuid": str(uuid.uuid4()),
        }),
        ("run_state", {"uuid": str(uuid.uuid4()), "running": False}),
        ("user_message_received", {"uuid": str(uuid.uuid4())}),
        ("trace_step", {"uuid": str(uuid.uuid4()), "name": "x"}),
        ("agent_message", {"type": "ai-title", "sessionId": sid}),
    ]
    for event_type, data in cases:
        event_ingester.ingest(
            sid, sid=sid, event_type=event_type, data=data,
            source="test", msg_id=None,
        )

    if event_ingester.render_seq_for_sid(sid, sid) != 0:
        print("  non-render orphan advanced render_seq_for_sid")
        return False

    _ = session_manager.get_root_tree_stubbed(sid)
    after_key, after_snapshot = session_manager._since_cache.get(sid)
    if after_key != before_key:
        print(f"  cache key changed for audit row: {before_key} -> {after_key}")
        return False
    if after_snapshot is not before_snapshot:
        print("  snapshot rebuilt for audit row")
        return False
    return True


def test_worker_event_advances_render_watermark() -> bool:
    sid = _fresh_session()
    event_ingester.ingest(
        sid, sid=sid, event_type="worker_event",
        data={"event": {"type": "agent_message", "data": {"uuid": str(uuid.uuid4())}}},
        source="test", msg_id=None,
    )
    if event_ingester.render_seq_for_sid(sid, sid) <= 0:
        print("  worker_event did not advance render_seq_for_sid")
        return False
    return True


def test_non_render_orphan_does_not_rebuild_root_events_projection() -> bool:
    sid = _fresh_session()
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "root orphan"}],
            },
        },
        source="test", msg_id=None,
    )
    before = event_ingester.root_events_by_sid(sid)
    if not before.get(sid):
        print("  expected cached root event projection")
        return False
    cache_before = event_ingester._root_events_cache.get(sid)
    version_before = event_ingester._root_events_version.get(sid)

    event_ingester.ingest(
        sid, sid=sid, event_type="command_received",
        data={"method": "POST", "path": "/api/x", "uuid": str(uuid.uuid4())},
        source="rest", msg_id=None,
    )
    after = event_ingester.root_events_by_sid(sid)
    cache_after = event_ingester._root_events_cache.get(sid)
    version_after = event_ingester._root_events_version.get(sid)

    if version_after != version_before:
        print(f"  root event version changed: {version_before} -> {version_after}")
        return False
    if cache_after is not cache_before:
        print("  root event projection cache rebuilt for audit row")
        return False
    if after != before:
        print("  root event projection changed for audit row")
        return False
    return True


def test_render_orphan_updates_warm_root_events_projection() -> bool:
    sid = _fresh_session()
    first_uid = str(uuid.uuid4())
    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="agent_message",
        data={
            "type": "assistant",
            "uuid": first_uid,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "first orphan"}],
            },
        },
        source="test",
        msg_id=None,
    )
    event_ingester.root_events_by_sid(sid)
    original_read_all = event_ingester._read_all_events_locked
    try:
        event_ingester._read_all_events_locked = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("warm root-event projection rebuilt from disk")
        )
        uid = str(uuid.uuid4())
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={
                "type": "assistant",
                "uuid": uid,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "warm orphan"}],
                },
            },
            source="test",
            msg_id=None,
        )
        after = event_ingester.root_events_by_sid(sid)
    finally:
        event_ingester._read_all_events_locked = original_read_all
    if uid not in {
        ((event.get("data") or {}).get("uuid"))
        for event in after.get(sid, [])
        if isinstance(event, dict)
    }:
        print("  warm root event projection did not include appended orphan")
        return False
    return True


def test_stamped_event_updates_warm_root_events_projection() -> bool:
    sid = _fresh_session()
    uid = str(uuid.uuid4())
    event_ingester.ingest(
        sid,
        sid=sid,
        event_type="agent_message",
        data={
            "type": "assistant",
            "uuid": uid,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "orphan"}],
            },
        },
        source="test",
        msg_id=None,
    )
    before = event_ingester.root_events_by_sid(sid)
    if not before.get(sid):
        print("  expected orphan before stamped event")
        return False
    original_read_all = event_ingester._read_all_events_locked
    try:
        event_ingester._read_all_events_locked = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("warm root-event projection rebuilt from disk")
        )
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={
                "type": "assistant",
                "uuid": uid,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "owned"}],
                },
            },
            source="test",
            msg_id="asst-owned",
        )
        after = event_ingester.root_events_by_sid(sid)
    finally:
        event_ingester._read_all_events_locked = original_read_all
    if any(
        ((event.get("data") or {}).get("uuid")) == uid
        for event in after.get(sid, [])
        if isinstance(event, dict)
    ):
        print("  stamped event did not remove matching root orphan")
        return False
    return True


# ─── 9. WS replay cap (mirror handler's bounded build) ─────────────


def test_ws_replay_cap_at_msg_limit() -> bool:
    """Mirror of the WS subscribe handler's replay-build logic. Cold
    replay may include one extra turn initiator when the raw cap cuts
    through a user→assistant boundary, but it must stay bounded
    independent of `since_seq` or session length.

    A long session with `since_seq=0` must not ship full history on
    every cold hop.
    """
    sess = session_manager.create(
        name="replay-cap", model="glm-5.1", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    for i in range(200):
        session_manager.append_user_msg(sid, {
            "id": str(uuid.uuid4()), "role": "user", "content": f"u{i}",
            "events": [], "isStreaming": False,
        })
        session_manager.append_assistant_msg(sid, {
            "id": str(uuid.uuid4()), "role": "assistant", "content": f"a{i}",
            "events": [], "isStreaming": False,
        })

    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    delta = _build_messages_replay_delta(sid, 0, limit=50)
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    replay = delta["messages"]
    if len(replay) > 51:
        print(f"  replay payload {len(replay)} > 51 bounded cap")
        return False
    return True


def test_cold_ws_replay_keeps_turn_header_initiator() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    sess = session_manager.create(
        name="turn-boundary", model="glm-5.1", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    for i in range(2):
        session_manager.append_user_msg(sid, {
            "id": str(uuid.uuid4()), "role": "user", "content": f"u{i}",
            "events": [], "isStreaming": False,
        })
        session_manager.append_assistant_msg(sid, {
            "id": str(uuid.uuid4()), "role": "assistant", "content": f"a{i}",
            "events": [], "isStreaming": False,
        })
    delta = _build_messages_replay_delta(sid, 0, limit=1)
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    roles = [m.get("role") for m in delta["messages"]]
    if roles != ["user", "assistant"]:
        print(f"  cold replay split the turn boundary: {roles}")
        return False
    return True


def test_cold_ws_replay_does_not_invent_orphan_header() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    sess = session_manager.create(
        name="orphan-boundary", model="glm-5.1", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "u",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": str(uuid.uuid4()), "role": "assistant", "content": "a1",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": str(uuid.uuid4()), "role": "assistant", "content": "a2",
        "events": [], "isStreaming": False,
    })
    delta = _build_messages_replay_delta(sid, 0, limit=1)
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    roles = [m.get("role") for m in delta["messages"]]
    if roles != ["assistant"]:
        print(f"  orphan assistant got an invented header: {roles}")
        return False
    return True


def test_incremental_ws_replay_does_not_prepend_seen_initiator() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    sess = session_manager.create(
        name="incremental-boundary", model="glm-5.1", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "prior-u",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": str(uuid.uuid4()), "role": "assistant", "content": "prior-a",
        "events": [], "isStreaming": False,
    })
    user = session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "u",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": str(uuid.uuid4()), "role": "assistant", "content": "a",
        "events": [], "isStreaming": False,
    })
    delta = _build_messages_replay_delta(sid, int(user["seq"]), limit=1)
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    roles = [m.get("role") for m in delta["messages"]]
    if roles != ["assistant"]:
        print(f"  incremental replay prepended seen initiator: {roles}")
        return False
    return True


def test_incremental_ws_replay_reuses_identical_window() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    sess = session_manager.create(
        name="incremental-window-cache", model="glm-5.1", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "prior-u",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": str(uuid.uuid4()), "role": "assistant", "content": "prior-a",
        "events": [], "isStreaming": False,
    })
    user = session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "u",
        "events": [], "isStreaming": False,
    })
    assistant_id = str(uuid.uuid4())
    session_manager.append_assistant_msg(sid, {
        "id": assistant_id,
        "role": "assistant",
        "content": "a",
        "events": [],
        "isStreaming": False,
    })

    calls = 0
    original = session_manager._compute_messages_window

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    session_manager._compute_messages_window = counted
    try:
        first = _build_messages_replay_delta(sid, int(user["seq"]), limit=50)
        second = _build_messages_replay_delta(sid, int(user["seq"]), limit=50)
        if calls != 1:
            print(f"  identical replay rebuilt {calls} windows")
            return False
        if not first or not second:
            print("  replay delta unexpectedly None")
            return False
        first["messages"][0]["content"] = "mutated"
        third = _build_messages_replay_delta(sid, int(user["seq"]), limit=50)
        if not third or third["messages"][0].get("content") == "mutated":
            print("  cached replay returned shared mutable data")
            return False
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={"type": "assistant", "uuid": str(uuid.uuid4())},
            source="test",
            msg_id=assistant_id,
        )
        _build_messages_replay_delta(sid, int(user["seq"]), limit=50)
        if calls != 2:
            print(f"  render event did not invalidate window cache, calls={calls}")
            return False
    finally:
        session_manager._compute_messages_window = original
    return True


def _session_with_completed_replay_target(event_count: int = 80) -> tuple[str, str, int]:
    sess = session_manager.create(
        name="replay-target", model="glm-5.1", cwd="/tmp",
        orchestration_mode="native",
    )
    sid = sess["id"]
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "u",
        "events": [], "isStreaming": False,
    })
    msg_id = str(uuid.uuid4())
    msg = session_manager.append_assistant_msg(sid, {
        "id": msg_id,
        "role": "assistant",
        "content": "a",
        "events": [
            {
                "type": "assistant",
                "data": {"type": "assistant", "uuid": str(uuid.uuid4())},
            }
            for _ in range(event_count)
        ],
        "isStreaming": False,
    })
    assert msg is not None
    return sid, msg_id, int(msg.get("seq") or 0)


def test_ws_replay_survives_stale_historical_projection() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    projection = types.ModuleType("historical_children_projection")
    projection.ProjectionUnavailable = type("ProjectionUnavailable", (RuntimeError,), {})

    def unavailable(*_args, **_kwargs):
        raise projection.ProjectionUnavailable("not current")

    projection.root_manifest = unavailable

    sid, msg_id, _seen_seq = _session_with_completed_replay_target(event_count=2)
    with patch.dict(sys.modules, {"historical_children_projection": projection}):
        delta = _build_messages_replay_delta(sid, 0, limit=50)
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    replay = [m for m in delta["messages"] if m.get("id") == msg_id]
    if len(replay) != 1:
        print(f"  completed message missing/duplicated under stale projection: {len(replay)}")
        return False
    msg = replay[0]
    if msg.get("events"):
        print(f"  completed replay should stay stubbed, got events={msg.get('events')}")
        return False
    if not isinstance(msg.get("stub"), dict):
        print(f"  completed replay missing fallback stub: {msg}")
        return False
    return True


def test_ws_replay_excludes_completed_seen_message() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    sid, msg_id, seen_seq = _session_with_completed_replay_target()
    delta = _build_messages_replay_delta(sid, seen_seq, limit=50)
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    replay_ids = {m.get("id") for m in delta["messages"]}
    if msg_id in replay_ids:
        print("  replay resent completed seq N message")
        return False
    payload_size = len(json.dumps(delta["messages"]))
    if payload_size > 1000:
        print(f"  completed/no-in-flight replay serialized huge payload: {payload_size} bytes")
        return False
    return True


def test_ws_replay_keeps_preexisting_in_flight_message() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402
    from turn_manager import TurnManager  # noqa: E402

    sid, msg_id, seen_seq = _session_with_completed_replay_target(event_count=1)
    tm = TurnManager(coordinator=None)
    tm.current_assistant_msgs[sid] = {
        "id": msg_id,
        "role": "assistant",
        "content": "streaming",
        "events": [
            {"type": "assistant", "data": {"uuid": "live-1"}},
            {"type": "assistant", "data": {"uuid": "live-2"}},
        ],
        "isStreaming": True,
    }
    delta = _build_messages_replay_delta(
        sid,
        seen_seq,
        limit=50,
        get_in_flight=tm.get_in_flight_assistant_msg,
    )
    if delta is None:
        print("  replay delta unexpectedly None")
        return False
    replay = [m for m in delta["messages"] if m.get("id") == msg_id]
    if len(replay) != 1:
        print(f"  in-flight message missing/duplicated: {len(replay)}")
        return False
    msg = replay[0]
    if msg.get("content") != "streaming" or len(msg.get("events") or []) != 2:
        print(f"  in-flight replacement failed: {msg}")
        return False
    return True


def test_ws_replay_reruns_inclusive_when_in_flight_appears() -> bool:
    from session_detail_api import _build_messages_replay_delta  # noqa: E402

    sid = "sid-race"
    msg_id = "msg-race"
    calls: list[int] = []
    in_flight_holder = {"msg": None}

    def _get_in_flight(_sid: str):
        return in_flight_holder["msg"]

    def _get_messages_since(_sid: str, since_seq: int, *, limit: int):
        calls.append(since_seq)
        if len(calls) == 1:
            in_flight_holder["msg"] = {
                "id": msg_id, "role": "assistant",
                "events": [{"type": "assistant", "data": {"uuid": "live"}}],
                "isStreaming": True,
            }
            return {"messages": [], "next_seq": 8}
        return {
            "messages": [{
                "id": msg_id, "role": "assistant",
                "events": [], "isStreaming": False, "seq": 7,
            }],
            "next_seq": 8,
        }

    delta = _build_messages_replay_delta(
        sid,
        7,
        limit=50,
        get_messages_since=_get_messages_since,
        get_in_flight=_get_in_flight,
    )
    if calls != [8, 7]:
        print(f"  expected exclusive then inclusive calls, got {calls}")
        return False
    replay = delta["messages"] if delta else []
    if len(replay) != 1 or replay[0].get("id") != msg_id:
        print(f"  in-flight race replay wrong: {replay}")
        return False
    if not replay[0].get("isStreaming"):
        print("  in-flight race did not preserve streaming state")
        return False
    return True


def test_ws_event_cursor_uses_server_floor() -> bool:
    sid = _fresh_session()
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={"type": "assistant", "uuid": str(uuid.uuid4())},
        source="test", msg_id="historical-msg",
    )
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={"type": "assistant", "uuid": str(uuid.uuid4())},
        source="test", msg_id="historical-msg",
    )
    floor = event_ingester.max_seq_by_sid(sid).get(sid, 0)
    if floor <= 0:
        print(f"  expected positive event floor, got {floor}")
        return False
    from session_detail_api import _floor_events_from_seq  # noqa: E402

    cold_open = _floor_events_from_seq(sid, 0, cursor_known=False)
    if cold_open != floor:
        print(f"  cold open floor {cold_open} != server floor {floor}")
        return False
    known_zero = _floor_events_from_seq(sid, 0, cursor_known=True)
    if known_zero != 0:
        print(f"  known zero cursor should survive, got {known_zero}")
        return False
    stale_cached_cursor = _floor_events_from_seq(sid, floor - 1, cursor_known=True)
    if stale_cached_cursor != floor - 1:
        print(f"  positive client cursor behind floor should survive, got {stale_cached_cursor}")
        return False
    ahead = _floor_events_from_seq(sid, floor + 5, cursor_known=True)
    if ahead != floor + 5:
        print(f"  positive client cursor ahead should survive, got {ahead}")
        return False
    negative = _floor_events_from_seq("missing-session", -10, cursor_known=False)
    if negative != 0:
        print(f"  missing session negative cursor should clamp to 0, got {negative}")
        return False
    return True


def test_ws_replay_stale_debug_is_debug_gated() -> bool:
    """The replay debug probes (which used to include a reconcile-dirty
    peek — `session_manager.is_reconcile_dirty`/`_reconcile_gen`, retired
    with the async dirty-flag reconcile subsystem) stay behind the DEBUG
    gate, and no reconcile probe of any kind runs on the normal path."""
    source = Path(_BACKEND, "main.py").read_text(encoding="utf-8")
    start = source.index('await ws_callback({\n                                "type": "messages_replay"')
    end = source.index('                    except Exception:\n                        logger.exception("messages_replay on subscribe failed")', start)
    replay_tail = source[start:end]
    debug_gate = "if logger.isEnabledFor(logging.DEBUG):"
    if debug_gate not in replay_tail:
        print("  missing DEBUG gate around stale replay probes")
        return False
    gated = replay_tail[replay_tail.index(debug_gate):]
    if "logger.debug(" not in gated:
        print("  logger.debug( is not behind DEBUG gate")
        return False
    normal_path = replay_tail[:replay_tail.index(debug_gate)]
    for needle in (
        "session_manager.is_reconcile_dirty",
        "session_manager._reconcile_gen",
    ):
        if needle in replay_tail:
            print(f"  {needle} should no longer exist (reconcile subsystem retired)")
            return False
    if 'logger.info(\n                                    "WS replay' in normal_path:
        print("  WS replay timing log still runs on normal replay path")
        return False
    return True


async def test_stubbed_team_tree_skips_full_event_hydration() -> bool:
    register_default_subscribers()
    sess = session_manager.create(
        name="team-stub", model="glm-5.1", cwd="/tmp",
        orchestration_mode="team",
    )
    sid = sess["id"]
    user_id = str(uuid.uuid4())
    old_id = str(uuid.uuid4())
    latest_id = str(uuid.uuid4())
    session_manager.append_user_msg(sid, {
        "id": user_id, "role": "user", "content": "u",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": old_id, "role": "assistant", "content": "",
        "events": [], "isStreaming": False,
    })
    session_manager.append_assistant_msg(sid, {
        "id": latest_id, "role": "assistant", "content": "",
        "events": [], "isStreaming": True,
    })
    for i in range(5):
        await bus.publish(BusEvent(
            type="manager_event", root_id=sid, sid=sid, msg_id=old_id,
            payload={
                "event": {
                    "type": "agent_message",
                    "data": {
                        "uuid": f"old-{i}",
                        "type": "assistant",
                        "message": {"content": f"old-{i}"},
                    },
                },
            },
        ))
    for i in range(3):
        await bus.publish(BusEvent(
            type="manager_event", root_id=sid, sid=sid, msg_id=latest_id,
            payload={
                "event": {
                    "type": "agent_message",
                    "data": {
                        "uuid": f"latest-{i}",
                        "type": "assistant",
                        "message": {"content": f"latest-{i}"},
                    },
                },
            },
        ))

    await await_session_content_projection(sid)
    projected = session_manager.get_ref(sid) or {}
    projected_messages = projected.get("messages") or []
    projected_old = next(
        (m for m in projected_messages if m.get("id") == old_id), {},
    )
    projected_latest = next(
        (m for m in projected_messages if m.get("id") == latest_id), {},
    )
    projected_counts = (
        len(projected_old.get("events") or []),
        len(projected_latest.get("events") or []),
    )
    if projected_counts != (5, 3):
        print(f"  projected event counts={projected_counts}")
        return False

    calls = 0
    original = session_manager._hydrate_cached_root_events

    def counted_hydrate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    with patch.object(session_manager, "_hydrate_cached_root_events", counted_hydrate):
        tree = session_manager.get_root_tree_stubbed(sid, msg_limit=20)

    if calls:
        print(f"  stubbed team tree hydrated full events {calls} time(s)")
        return False
    messages = tree.get("messages") if tree else []
    old_msg = next((m for m in messages if m.get("id") == old_id), {})
    latest_msg = next((m for m in messages if m.get("id") == latest_id), {})
    old_stub = old_msg.get("stub") or {}
    latest_events = latest_msg.get("events") or []
    if old_stub.get("event_count") != 5:
        print(f"  old stub event_count={old_stub.get('event_count')}")
        return False
    if len(latest_events) != 3:
        print(f"  latest events={len(latest_events)}")
        return False
    return True


# ─── Runner ────────────────────────────────────────────────────────


async def _amain() -> int:
    sync_tests = [
        ("non-render orphans do not invalidate snapshot", test_non_render_orphans_do_not_invalidate_snapshot),
        ("worker_event advances render watermark", test_worker_event_advances_render_watermark),
        ("non-render orphan does not rebuild root events", test_non_render_orphan_does_not_rebuild_root_events_projection),
        ("render orphan updates warm root events", test_render_orphan_updates_warm_root_events_projection),
        ("stamped event updates warm root events", test_stamped_event_updates_warm_root_events_projection),
        ("ws replay cap at msg_limit", test_ws_replay_cap_at_msg_limit),
        ("cold ws replay keeps turn header initiator", test_cold_ws_replay_keeps_turn_header_initiator),
        ("cold ws replay does not invent orphan header", test_cold_ws_replay_does_not_invent_orphan_header),
        ("incremental ws replay does not prepend seen initiator", test_incremental_ws_replay_does_not_prepend_seen_initiator),
        ("incremental ws replay reuses identical window", test_incremental_ws_replay_reuses_identical_window),
        ("ws replay survives stale historical projection", test_ws_replay_survives_stale_historical_projection),
        ("ws replay excludes completed seen message", test_ws_replay_excludes_completed_seen_message),
        ("ws replay keeps preexisting in-flight message", test_ws_replay_keeps_preexisting_in_flight_message),
        ("ws replay reruns inclusive when in-flight appears", test_ws_replay_reruns_inclusive_when_in_flight_appears),
        ("ws event cursor uses server floor", test_ws_event_cursor_uses_server_floor),
        ("ws replay stale debug is DEBUG-gated", test_ws_replay_stale_debug_is_debug_gated),
    ]
    async_tests: list[tuple[str, object]] = [
        ("stubbed team tree skips full event hydration", test_stubbed_team_tree_skips_full_event_hydration),
    ]

    fails = 0
    for name, fn in sync_tests:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            print(f"  exception: {e!r}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            fails += 1

    for name, fn in async_tests:
        try:
            ok = await fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            fails += 1

    return fails


def main() -> int:
    try:
        fails = asyncio.run(_amain())
    finally:
        shutdown_session_content_projection()
        try:
            shutil.rmtree(_TMP_HOME, ignore_errors=True)
        except Exception:
            pass
    print()
    if fails == 0:
        print(f"{PASS}  all hop-perf invariants hold")
        return 0
    print(f"{FAIL}  {fails} hop-perf regression(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
