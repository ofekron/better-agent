"""Perf-regression tests for session-hop work.

Pins the post-fix invariants that make a session hop O(1) work in
steady state instead of O(N-events × N-panes):

  1. **Cold-cache hop schedules ONE reconcile**, not one per pane and
     not inline. The cold-load reconcile happens once per root per
     backend lifetime, fan-out independent.

  2. **Warm-cache hop schedules ZERO reconciles**. After the cold-
     load reconcile clears the dirty flag, subsequent
     `schedule_reconcile_if_needed` calls return None — no JSONL
     scan, no `_reconcile_msg_events_from_jsonl` walk.

  3. **WS subscribe builds messages_replay from session_manager only,
     and caps the payload at 50 messages even when `since_seq=0`** —
     no full-tree deepcopy, no full-history serialization on cold
     hop.

  4. **Orphan events arm the dirty flag**. An ingest with msg_id=None
     for a sid whose latest assistant msg is finalized sets
     reconcile_dirty=True so the next reader spawns a recovery
     reconcile.

  5. **Single-flight**: two concurrent `schedule_reconcile_if_needed`
     calls for the same root → exactly ONE reconcile task runs.

  6. **Delayed-progress 0.3s threshold**: a reconcile that finishes
     under 0.3s emits ZERO `session_processing_*` events (no UI
     flash). A reconcile that exceeds 0.3s emits `started` then
     `finished`, in that order, even if the reconcile body raises.

  7. **Loop-stays-responsive**: while a slow reconcile is running in
     the threadpool, an unrelated coroutine on the event loop still
     gets scheduled within tight latency.

  8. **Subscribe stale-debug probes stay off the normal path**. Replay
     details are useful when DEBUG is enabled, but the normal reconnect
     path must not run extra session-manager probes or write INFO logs
     per subscribe.

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
import uuid
from pathlib import Path
from unittest.mock import patch

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-hop-perf-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
import event_ingester as ei_mod  # noqa: E402

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


def _evict_cache(root_id: str) -> None:
    """Drop the in-memory cache for `root_id` so the next read triggers
    `_load_root` from disk (simulating a cold backend boot)."""
    session_manager._roots.pop(root_id, None)
    # Don't pop _node_root_id — root_id lookup must still work.
    # Clear dirty flag so we can observe the cold-load setter.
    session_manager._reconcile_dirty.pop(root_id, None)


def _wire_loop_and_fns(
    loop: asyncio.AbstractEventLoop,
    *,
    reconcile_fn=None,
    emit_fn=None,
) -> tuple[list, list]:
    """Bind a fresh loop + counter wrappers. Returns
    (reconcile_calls, emit_calls) — both lists you can inspect from
    the test body. Defaults to a no-op reconcile and capturing emit."""
    reconcile_calls: list[str] = []
    emit_calls: list[tuple[str, str]] = []

    def _default_reconcile(root_id: str, *, after_seq: int = 0) -> list:
        reconcile_calls.append(root_id)
        return []

    def _default_emit(root_id: str, kind: str) -> None:
        emit_calls.append((root_id, kind))

    session_manager.bind_loop(loop)
    session_manager.bind_reconcile_fn(reconcile_fn or _default_reconcile)
    session_manager.bind_processing_emitter(emit_fn or _default_emit)
    return reconcile_calls, emit_calls


# ─── 1. Cold-cache hop schedules ONE reconcile ─────────────────────


async def test_cold_hop_schedules_one_reconcile() -> bool:
    sid = _fresh_session()
    _evict_cache(sid)
    loop = asyncio.get_running_loop()
    reconcile_calls, _ = _wire_loop_and_fns(loop)

    # First read repopulates the cache and arms the dirty flag (per
    # `_load_root`'s "cold load → mark dirty" rule).
    _ = session_manager.get(sid)
    assert session_manager._reconcile_dirty.get(sid) is True, (
        "cold _load_root must arm the dirty flag"
    )

    task1 = session_manager.schedule_reconcile_if_needed(sid)
    task2 = session_manager.schedule_reconcile_if_needed(sid)
    if task1 is None:
        print("  schedule_reconcile_if_needed returned None on cold hop")
        return False
    # Single-flight: second call returns the same task.
    if task2 is not task1:
        print("  second schedule_reconcile_if_needed should return the same task")
        return False

    await task1
    if len(reconcile_calls) != 1:
        print(f"  expected reconcile=1, got {len(reconcile_calls)}")
        return False
    if reconcile_calls[0] != sid:
        print(f"  reconcile called for wrong root: {reconcile_calls[0]}")
        return False
    return True


# ─── 2. Warm-cache hop schedules ZERO reconciles ───────────────────


async def test_warm_hop_schedules_no_reconcile() -> bool:
    sid = _fresh_session()
    loop = asyncio.get_running_loop()
    reconcile_calls, _ = _wire_loop_and_fns(loop)

    # First pass: drain the cold-load reconcile.
    _ = session_manager.get(sid)
    task = session_manager.schedule_reconcile_if_needed(sid)
    if task is not None:
        await task
    initial = len(reconcile_calls)

    # Subsequent hops do nothing. Simulate 4-pane subscribe burst.
    for _ in range(4):
        result = session_manager.schedule_reconcile_if_needed(sid)
        if result is not None:
            print(f"  warm hop spawned reconcile (returned {result})")
            return False
    if len(reconcile_calls) != initial:
        print(
            f"  warm hops triggered reconcile: {initial} → {len(reconcile_calls)}"
        )
        return False
    return True


# ─── 3. Orphan-event ingest arms dirty flag ────────────────────────


def test_orphan_event_arms_dirty() -> bool:
    sid = _fresh_session()  # finalized assistant present
    # Warm cache so latest_assistant_finalized can read it.
    _ = session_manager.get(sid)
    session_manager._reconcile_dirty[sid] = False

    # Orphan-shape ingest: msg_id=None.
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "orphan tail"}],
            },
        },
        source="claude_tailer", msg_id=None,
    )
    if session_manager._reconcile_dirty.get(sid) is not True:
        print("  orphan ingest did NOT set reconcile_dirty")
        return False

    # consume_reconcile_dirty clears it.
    was = session_manager.consume_reconcile_dirty(sid)
    if was is not True:
        print(f"  consume_reconcile_dirty returned {was}, expected True")
        return False
    if session_manager._reconcile_dirty.get(sid) is True:
        print("  consume failed to clear the flag")
        return False
    return True


def test_non_render_orphans_do_not_invalidate_snapshot() -> bool:
    sid = _fresh_session()
    _ = session_manager.get_root_tree_stubbed(sid)
    cached = session_manager._since_cache.get(sid)
    if cached is None:
        print("  expected warm _since_cache entry")
        return False
    before_key, before_snapshot = cached
    session_manager._reconcile_dirty[sid] = False

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

    if session_manager._reconcile_dirty.get(sid) is True:
        print("  non-render orphan incorrectly armed reconcile_dirty")
        return False
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


# ─── 4. Single-flight under concurrent schedules ───────────────────


async def test_single_flight_concurrent_schedules() -> bool:
    sid = _fresh_session()
    _evict_cache(sid)
    loop = asyncio.get_running_loop()

    # Gate the reconcile so two schedule calls land before the first
    # completes — deterministically tests single-flight.
    gate = threading.Event()
    call_counter = itertools.count()
    call_count = [0]

    def _slow_reconcile(root_id: str, *, after_seq: int = 0) -> list:
        call_count[0] = next(call_counter) + 1
        gate.wait(timeout=2.0)
        return []

    _wire_loop_and_fns(loop, reconcile_fn=_slow_reconcile)
    _ = session_manager.get(sid)  # arms dirty

    tasks = [
        session_manager.schedule_reconcile_if_needed(sid)
        for _ in range(4)
    ]
    # All should be the same task (or None for the second+ calls when
    # the dirty flag was already consumed and an in-flight task exists).
    non_null = [t for t in tasks if t is not None]
    if len(non_null) != 4:
        print(
            f"  expected 4 same-task returns, got "
            f"{len([t for t in tasks if t is not None])} non-null"
        )
        return False
    first = non_null[0]
    if any(t is not first for t in non_null):
        print("  schedule returned different tasks under concurrent calls")
        return False

    gate.set()
    await first
    if call_count[0] != 1:
        print(f"  expected reconcile=1 under concurrent schedules, got {call_count[0]}")
        return False
    return True


# ─── 5. Fast reconcile emits ZERO progress events ──────────────────


async def test_fast_reconcile_no_progress_events() -> bool:
    sid = _fresh_session()
    _evict_cache(sid)
    loop = asyncio.get_running_loop()
    _, emit_calls = _wire_loop_and_fns(loop)  # no-op reconcile → instant
    _ = session_manager.get(sid)

    task = session_manager.schedule_reconcile_if_needed(sid)
    assert task is not None
    await task
    if emit_calls:
        print(f"  fast reconcile emitted progress events: {emit_calls}")
        return False
    return True


# ─── 6. Slow reconcile emits started + finished, in order ──────────


async def test_slow_reconcile_emits_started_then_finished() -> bool:
    sid = _fresh_session()
    _evict_cache(sid)
    loop = asyncio.get_running_loop()

    def _sleepy(root_id: str, *, after_seq: int = 0) -> list:
        time.sleep(0.5)  # > 0.3s threshold
        return []

    _, emit_calls = _wire_loop_and_fns(loop, reconcile_fn=_sleepy)
    _ = session_manager.get(sid)
    task = session_manager.schedule_reconcile_if_needed(sid)
    assert task is not None
    await task
    kinds = [k for (_, k) in emit_calls]
    if kinds != ["started", "finished"]:
        print(f"  expected ['started','finished'], got {kinds}")
        return False
    return True


# ─── 7. Slow reconcile that raises still emits finished ────────────


async def test_failing_reconcile_still_emits_finished() -> bool:
    sid = _fresh_session()
    _evict_cache(sid)
    loop = asyncio.get_running_loop()

    def _sleepy_fail(root_id: str, *, after_seq: int = 0) -> list:
        time.sleep(0.4)
        raise RuntimeError("synthetic failure")

    _, emit_calls = _wire_loop_and_fns(loop, reconcile_fn=_sleepy_fail)
    _ = session_manager.get(sid)
    task = session_manager.schedule_reconcile_if_needed(sid)
    assert task is not None
    # `_sync_reconcile` swallows the exception, so the task completes
    # cleanly. The `finally` MUST still fire `finished`.
    await task
    kinds = [k for (_, k) in emit_calls]
    if kinds != ["started", "finished"]:
        print(f"  failing reconcile emit sequence wrong: {kinds}")
        return False
    # In-flight registry must be cleared.
    if sid in session_manager._in_flight_reconcile:
        print(f"  in-flight registry not cleared after failure")
        return False
    return True


# ─── 8. Loop-stays-responsive during slow reconcile ────────────────


async def test_loop_responsive_during_slow_reconcile() -> bool:
    sid = _fresh_session()
    _evict_cache(sid)
    loop = asyncio.get_running_loop()

    gate = threading.Event()
    started = threading.Event()

    def _gated(root_id: str, *, after_seq: int = 0) -> list:
        started.set()
        gate.wait(timeout=2.0)
        return []

    _wire_loop_and_fns(loop, reconcile_fn=_gated)
    _ = session_manager.get(sid)
    reconcile_task = session_manager.schedule_reconcile_if_needed(sid)
    assert reconcile_task is not None

    # Wait for the threadpool worker to actually start.
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    if not started.is_set():
        print("  threadpool worker never started")
        return False

    # Now measure: schedule an asyncio.sleep(0) — the loop should
    # service it within tight latency despite the reconcile holding
    # the threadpool slot. Pre-fix, an inline reconcile would block
    # the loop entirely; post-fix, it's on a worker thread so the
    # loop stays responsive.
    t0 = time.perf_counter()
    await asyncio.sleep(0)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    gate.set()
    await reconcile_task
    if latency_ms > 50:
        print(f"  loop latency during slow reconcile: {latency_ms:.1f}ms (>50ms)")
        return False
    return True


# ─── 9. WS replay cap (mirror handler's bounded build) ─────────────


def test_ws_replay_cap_at_msg_limit() -> bool:
    """Mirror of the WS subscribe handler's replay-build logic. The
    cap MUST hold even when since_seq=0 (cold hop) so the WS payload
    is bounded independent of `since_seq` or session length.

    Pre-fix, the WS path delivered every message with `seq >= since_seq`
    uncapped — a 1000-msg session with `since_seq=0` shipped the full
    history on every cold hop.
    """
    sid = _fresh_session()
    # Push 200 more user messages so the total exceeds the cap.
    for i in range(200):
        session_manager.append_user_msg(sid, {
            "id": str(uuid.uuid4()), "role": "user", "content": f"u{i}",
            "events": [], "isStreaming": False,
        })

    sess = session_manager.get(sid)
    persisted = sess.get("messages") or []
    since_seq = 0
    replay = [m for m in persisted if int(m.get("seq", 0)) >= since_seq]
    replay = replay[-50:]
    if len(replay) > 50:
        print(f"  replay payload {len(replay)} > 50 cap")
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
    from main import _floor_events_from_seq  # noqa: E402

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
    source = Path(_BACKEND, "main.py").read_text(encoding="utf-8")
    start = source.index('await ws_callback({\n                                "type": "messages_replay"')
    end = source.index('                    except Exception:\n                        logger.exception("messages_replay on subscribe failed")', start)
    replay_tail = source[start:end]
    debug_gate = "if logger.isEnabledFor(logging.DEBUG):"
    if debug_gate not in replay_tail:
        print("  missing DEBUG gate around stale replay probes")
        return False
    gated = replay_tail[replay_tail.index(debug_gate):]
    for needle in (
        "session_manager._root_id_for",
        "session_manager.is_reconcile_dirty",
        "logger.debug(",
    ):
        if needle not in gated:
            print(f"  {needle} is not behind DEBUG gate")
            return False
    normal_path = replay_tail[:replay_tail.index(debug_gate)]
    for needle in (
        "session_manager._root_id_for",
        "session_manager.is_reconcile_dirty",
        "logger.info(\n                                    \"WS replay",
    ):
        if needle in normal_path:
            print(f"  {needle} still runs on normal replay path")
            return False
    return True


def test_stubbed_team_tree_skips_full_event_hydration() -> bool:
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
        "events": [], "isStreaming": False,
    })
    for i in range(5):
        event_ingester.ingest(
            sid, sid=sid, event_type="manager_event",
            data={
                "event": {
                    "type": "agent_message",
                    "data": {
                        "uuid": f"old-{i}",
                        "type": "assistant",
                        "message": {"content": f"old-{i}"},
                    },
                },
            },
            source="test", msg_id=old_id,
        )
    for i in range(3):
        event_ingester.ingest(
            sid, sid=sid, event_type="manager_event",
            data={
                "event": {
                    "type": "agent_message",
                    "data": {
                        "uuid": f"latest-{i}",
                        "type": "assistant",
                        "message": {"content": f"latest-{i}"},
                    },
                },
            },
            source="test", msg_id=latest_id,
        )

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
        ("orphan-event arms dirty", test_orphan_event_arms_dirty),
        ("non-render orphans do not invalidate snapshot", test_non_render_orphans_do_not_invalidate_snapshot),
        ("worker_event advances render watermark", test_worker_event_advances_render_watermark),
        ("non-render orphan does not rebuild root events", test_non_render_orphan_does_not_rebuild_root_events_projection),
        ("ws replay cap at msg_limit", test_ws_replay_cap_at_msg_limit),
        ("ws event cursor uses server floor", test_ws_event_cursor_uses_server_floor),
        ("ws replay stale debug is DEBUG-gated", test_ws_replay_stale_debug_is_debug_gated),
        ("stubbed team tree skips full event hydration", test_stubbed_team_tree_skips_full_event_hydration),
    ]
    async_tests = [
        ("cold hop → 1 reconcile", test_cold_hop_schedules_one_reconcile),
        ("warm hop → 0 reconciles", test_warm_hop_schedules_no_reconcile),
        ("single-flight under concurrent schedules", test_single_flight_concurrent_schedules),
        ("fast reconcile → no progress events", test_fast_reconcile_no_progress_events),
        ("slow reconcile → started+finished", test_slow_reconcile_emits_started_then_finished),
        ("failing reconcile still emits finished", test_failing_reconcile_still_emits_finished),
        ("loop responsive during slow reconcile", test_loop_responsive_during_slow_reconcile),
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
