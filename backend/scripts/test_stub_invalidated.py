"""Regression test for Tier-1 F5: stub_invalidated detection.

When a reconcile (post-restart safety net) appends events to a
NON-latest (frontend-collapsed) historical assistant msg, its stub
went stale and the backend must fire `stub_invalidated`. This pins the
DETECTION inside `render_tree_hydrate.reconcile_msg_events_from_jsonl`:
the `on_historical_change` callback fires for a non-latest msg that
GAINS events on the pass, and NOT for the latest msg.

The reconcile fast-path skips the jsonl read when every finalized msg
already has events, so the realistic trigger is an ORPHAN (msg_id=None)
bracketed onto a non-latest msg WHILE the latest turn is still
streaming (which forces the jsonl-read + orphan-bracketing path).

Run with:
    cd backend && .venv/bin/python scripts/test_stub_invalidated.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import asyncio

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-stub-inval-")

import render_stub  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from render_tree_hydrate import reconcile_msg_events_from_jsonl  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _agent_message(uuid: str, text: str = "x") -> dict:
    """Inner agent_message event data (the shape apply_event stores)."""
    return {
        "event": {
            "type": "agent_message",
            "data": {
                "uuid": uuid,
                "type": "assistant",
                "message": {"content": text},
            },
        },
    }


def _agent_data(uuid: str, text: str = "x") -> dict:
    return {
        "uuid": uuid,
        "type": "assistant",
        "message": {"content": text},
    }


def _apply_and_ingest_agent_message(
    sid: str,
    msg: dict,
    ctx: ApplyEventCtx,
    uuid: str,
    text: str,
) -> None:
    get_strategy("manager").apply_event(
        app_session_id=sid, msg=msg,
        event={"type": "agent_message", "data": _agent_data(uuid, text)},
        ctx=ctx, source_is_provider_stream=False,
    )
    event_ingester.ingest(
        sid, sid, "agent_message", _agent_data(uuid, text),
        source="test", msg_id=msg["id"],
    )


def _mk_session_latest_streaming() -> tuple[str, str, str]:
    """Manager session: asst1 finalized with 3 events (the non-latest,
    collapsed historical turn), asst2 the LATEST and still STREAMING
    with zero events (forces reconcile's jsonl-read path and makes
    asst1's orphan-ceiling open so an orphan brackets onto asst1)."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)

    # Turn 1 — finalized, 3 events.
    session_manager.append_user_msg(sid, {
        "id": "user-q1", "role": "user", "content": "q1",
        "events": [], "isStreaming": False,
    })
    asst1 = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst1)
    for u in ["a1", "a2", "a3"]:
        _apply_and_ingest_agent_message(sid, asst1, ctx, u, "x")
    asst1["isStreaming"] = False

    # Turn 2 — latest, STREAMING, no events yet.
    session_manager.append_user_msg(sid, {
        "id": "user-q2", "role": "user", "content": "q2",
        "events": [], "isStreaming": False,
    })
    asst2 = strategy.build_assistant_scaffold()
    asst2["isStreaming"] = True
    session_manager.append_assistant_msg(sid, asst2)

    return sid, asst1["id"], asst2["id"]


def _mk_session_with_historical_events(event_count: int) -> tuple[str, str, str]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, workers_list=[],
                        user_msg=None, root_id=sid)

    session_manager.append_user_msg(sid, {
        "id": "user-q1", "role": "user", "content": "q1",
        "events": [], "isStreaming": False,
    })
    asst1 = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst1)
    for idx in range(event_count):
        _apply_and_ingest_agent_message(
            sid, asst1, ctx, f"same-{idx}", f"old-{idx}",
        )
    asst1["isStreaming"] = False

    session_manager.append_user_msg(sid, {
        "id": "user-q2", "role": "user", "content": "q2",
        "events": [], "isStreaming": False,
    })
    asst2 = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, asst2)
    _apply_and_ingest_agent_message(sid, asst2, ctx, "latest", "latest")
    asst2["isStreaming"] = False

    return sid, asst1["id"], asst2["id"]


def _collect(live: dict) -> list[tuple[str, str, int]]:
    collected: list[tuple[str, str, int]] = []

    def _on_change(s: str, mid: str, m: dict) -> None:
        collected.append((s, mid, render_stub.build_stub(m)["event_count"]))

    session_manager.flush_pending_persists()
    reconcile_msg_events_from_jsonl(live, on_historical_change=_on_change)
    return collected


def _collect_stubs(live: dict) -> list[tuple[str, str, dict]]:
    collected: list[tuple[str, str, dict]] = []

    def _on_change(s: str, mid: str, m: dict) -> None:
        collected.append((s, mid, render_stub.build_stub(m)))

    session_manager.flush_pending_persists()
    reconcile_msg_events_from_jsonl(live, on_historical_change=_on_change)
    return collected


def test_orphan_on_non_latest_fires_invalidation() -> bool:
    sid, asst1_id, asst2_id = _mk_session_latest_streaming()
    # Orphan (msg_id=None) ingested AFTER asst1's events → higher seq.
    # asst2 has no named events, so asst1's orphan ceiling is open and
    # the orphan brackets onto asst1.
    event_ingester.ingest(
        sid, sid, "manager_event", _agent_message("orphan1"),
        source="test", msg_id=None,
    )
    live = session_manager.get_ref(sid)
    collected = _collect(live)
    if len(collected) != 1:
        print(f"  expected exactly 1 invalidation, got {collected}")
        return False
    s, mid, count = collected[0]
    if mid != asst1_id:
        print(f"  invalidation should target non-latest asst1, got {mid}")
        return False
    if count != 4:
        print(f"  asst1 count should be 3->4, got {count}")
        return False
    # asst2 (latest, streaming) must never be collected.
    if any(mid == asst2_id for _, mid, _ in collected):
        print("  latest streaming asst2 must NOT be invalidated")
        return False
    return True


def test_clean_reconcile_no_false_fire() -> bool:
    sid, _asst1_id, _asst2_id = _mk_session_latest_streaming()
    # No new jsonl rows beyond what apply_event already wrote.
    live = session_manager.get_ref(sid)
    collected = _collect(live)
    if collected:
        print(f"  clean reconcile must not fire, got {collected}")
        return False
    return True


def test_same_uuid_tail_replacement_fires_invalidation() -> bool:
    sid, asst1_id, _asst2_id = _mk_session_with_historical_events(1)
    event_ingester.ingest(
        sid, sid, "agent_message", _agent_data("same-0", "new-tail"),
        source="test", msg_id=asst1_id,
    )

    collected = _collect_stubs(session_manager.get_ref(sid))
    if len(collected) != 1:
        print(f"  expected exactly 1 invalidation, got {collected}")
        return False
    _s, mid, stub = collected[0]
    if mid != asst1_id:
        print(f"  invalidation should target historical asst1, got {mid}")
        return False
    text = (
        stub["last_events"][-1]
        .get("data", {})
        .get("message", {})
        .get("content")
    )
    if text != "new-tail":
        print(f"  stub tail should contain new-tail, got {stub}")
        return False
    again = _collect_stubs(session_manager.get_ref(sid))
    if again:
        print(f"  second reconcile should not invalidate again, got {again}")
        return False
    return True


def test_same_uuid_replacement_outside_tail_invalidates_without_tail_bloat() -> bool:
    sid, asst1_id, _asst2_id = _mk_session_with_historical_events(
        render_stub.STUB_TAIL + 1,
    )
    event_ingester.ingest(
        sid, sid, "agent_message", _agent_data("same-0", "new-outside-tail"),
        source="test", msg_id=asst1_id,
    )

    collected = _collect_stubs(session_manager.get_ref(sid))
    if len(collected) != 1:
        print(f"  replacement outside stub tail must invalidate once, got {collected}")
        return False
    _s, mid, stub = collected[0]
    if mid != asst1_id:
        print(f"  invalidation should target historical asst1, got {mid}")
        return False
    tail_text = str(stub.get("last_events") or [])
    if "new-outside-tail" in tail_text:
        print(f"  outside-tail replacement should not bloat stub tail, got {stub}")
        return False
    again = _collect_stubs(session_manager.get_ref(sid))
    if again:
        print(f"  second reconcile should not invalidate again, got {again}")
        return False
    return True


def test_emit_stub_invalidated_batches_changes() -> bool:
    import main

    calls: list[tuple[str, dict]] = []
    original = main.coordinator.broadcast_global
    original_delay = main._STUB_INVALIDATED_COALESCE_SECONDS

    async def fake_broadcast(event_type: str, data: dict) -> None:
        calls.append((event_type, data))

    main.coordinator.broadcast_global = fake_broadcast  # type: ignore[method-assign]
    main._STUB_INVALIDATED_COALESCE_SECONDS = 0.001
    handle_after_flush = None
    scheduled_after_flush = False
    try:
        async def _run() -> None:
            main._emit_stub_invalidated([])
            main._emit_stub_invalidated([
                {"app_session_id": "s1", "msg_id": "m1", "stub": {"event_count": 1, "last_events": []}},
            ])
            main._emit_stub_invalidated([
                {"app_session_id": "s1", "msg_id": "m2", "stub": {"event_count": 2, "last_events": []}},
                {"app_session_id": "s2", "msg_id": "m3", "stub": {"event_count": 3, "last_events": []}},
            ])
            await asyncio.sleep(0.02)
            main._emit_stub_invalidated([
                {"app_session_id": "s3", "msg_id": "m4", "stub": {"event_count": 4, "last_events": []}},
            ])
            await asyncio.sleep(0.02)

        asyncio.run(_run())
        handle_after_flush = main._stub_invalidated_flush_handle
        scheduled_after_flush = main._stub_invalidated_flush_scheduled
    finally:
        handle = main._stub_invalidated_flush_handle
        if handle is not None:
            handle.cancel()
        main._stub_invalidated_pending.clear()
        main._stub_invalidated_flush_handle = None
        main._stub_invalidated_flush_scheduled = False
        main.coordinator.broadcast_global = original  # type: ignore[method-assign]
        main._STUB_INVALIDATED_COALESCE_SECONDS = original_delay

    if handle_after_flush is not None:
        print("  flush handle should be cleared after flush")
        return False
    if scheduled_after_flush:
        print("  flush scheduled flag should be cleared after flush")
        return False
    if len(calls) != 2:
        print(f"  expected two coalesced broadcasts, got {calls}")
        return False
    expected_msg_ids = [["m1", "m2", "m3"], ["m4"]]
    for index, ((event_type, data), msg_ids) in enumerate(zip(calls, expected_msg_ids)):
        if event_type != "stub_invalidated":
            print(f"  wrong event type: {event_type}")
            return False
        changes = data.get("changes")
        actual_msg_ids = [
            change.get("msg_id")
            for change in changes
            if isinstance(change, dict)
        ] if isinstance(changes, list) else []
        if actual_msg_ids != msg_ids:
            print(f"  wrong batch {index}: {data}")
            return False
    return True


TESTS = [
    ("orphan bracketed onto non-latest fires stub_invalidated",
        test_orphan_on_non_latest_fires_invalidation),
    ("clean reconcile fires no false invalidation",
        test_clean_reconcile_no_false_fire),
    ("same-uuid tail replacement fires stub_invalidated",
        test_same_uuid_tail_replacement_fires_invalidation),
    ("same-uuid replacement outside tail invalidates without tail bloat",
        test_same_uuid_replacement_outside_tail_invalidates_without_tail_bloat),
    ("stub_invalidated emitter batches reconcile changes",
        test_emit_stub_invalidated_batches_changes),
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
        session_manager.flush_pending_persists()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
