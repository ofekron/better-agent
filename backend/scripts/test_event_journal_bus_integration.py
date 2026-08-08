"""Regression test for production event-bus → EventJournal wiring.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_bus_integration.py
"""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-journal-bus-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import BusEvent, bus  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from event_bus_subscribers import (  # noqa: E402
    await_session_content_projection,
    register_default_subscribers,
    shutdown_session_content_projection,
)
from session_manager import manager as session_manager  # noqa: E402
from render_tree_hydrate import _hydrate_msg_events_from_jsonl  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


async def _run() -> bool:
    sess = session_manager.create(
        name="journal-bus", model="sonnet", cwd="/tmp/journal-bus",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    msg_id = "msg-bus"
    session_manager.append_assistant_msg(
        sid, {"id": msg_id, "role": "assistant", "content": "", "events": []},
    )

    # Combined suites can exercise application shutdown before rebinding.
    # Registration must reopen the singleton projection drainer.
    shutdown_session_content_projection()
    register_default_subscribers()

    await bus.publish(BusEvent(
        type="agent_message",
        root_id=sid,
        sid=sid,
        msg_id=msg_id,
        payload={
            "uuid": "bus-owned",
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "bus projected text"},
                ],
            },
        },
    ))
    await await_session_content_projection(sid)

    rows = event_journal_reader.read_message_events(sid, msg_id)
    lite = session_manager.get_lite(sid) or {}
    msg = next(
        (m for m in (lite.get("messages") or []) if m.get("id") == msg_id),
        {},
    )

    ok = True
    ok = _check(
        len(rows) == 1
        and rows[0].get("type") == "agent_message"
        and rows[0].get("msg_id") == msg_id,
        "bus persistence writes through event journal",
        str(rows),
    ) and ok
    ok = _check(
        msg.get("content") == "bus projected text",
        "session projection updates from journal written ack",
        str(msg),
    ) and ok
    await bus.publish(BusEvent(
        type="agent_message",
        root_id=sid,
        sid=sid,
        msg_id=msg_id,
        payload={
            "uuid": "bus-owned-second",
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "second"}]},
        },
    ))
    await await_session_content_projection(sid)
    ordered = session_manager.get(sid) or {}
    ordered_msg = next(
        (m for m in (ordered.get("messages") or []) if m.get("id") == msg_id),
        {},
    )
    event_uuids = [
        (event.get("data") or {}).get("uuid")
        for event in ordered_msg.get("events") or []
    ]
    ok = _check(
        event_uuids == ["bus-owned", "bus-owned-second"],
        "projection barrier preserves journal order",
        str(event_uuids),
    ) and ok

    worker_changes: list[dict] = []
    worker_deltas_ready = asyncio.Event()

    async def capture_worker_delta(event: BusEvent) -> None:
        if event.sid != sid:
            return
        worker_changes.append(event.payload)
        if len(worker_changes) == 3:
            worker_deltas_ready.set()

    delta_subscriber = f"test-worker-deltas-{sid}"
    session_manager.bind_loop(asyncio.get_running_loop())
    bus.subscribe(
        "session.journal_event_projected",
        capture_worker_delta,
        name=delta_subscriber,
        bind_current_loop=True,
    )
    worker_id = "codex-child"
    worker_events = [
        BusEvent(
            type="worker_start", root_id=sid, sid=sid, msg_id=msg_id,
            payload={
                "delegation_id": worker_id,
                "worker_session_id": "codex-thread-child",
                "worker_description": "Codex child",
                "run_mode": "codex_subagent",
            },
        ),
        BusEvent(
            type="worker_event", root_id=sid, sid=sid, msg_id=msg_id,
            payload={
                "delegation_id": worker_id,
                "event": {
                    "type": "agent_message",
                    "data": {
                        "uuid": "codex-child-output",
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "child result"}],
                        },
                    },
                },
            },
        ),
        BusEvent(
            type="worker_complete", root_id=sid, sid=sid, msg_id=msg_id,
            payload={"delegation_id": worker_id, "success": True},
        ),
    ]
    try:
        for worker_event in worker_events:
            await bus.publish(worker_event)
        await await_session_content_projection(sid)
        await asyncio.wait_for(worker_deltas_ready.wait(), timeout=2)
    finally:
        bus.unsubscribe(delta_subscriber)

    projected_worker_deltas = [
        change for change in worker_changes
        if change.get("kind") == "journal_event_projected"
        and (change.get("delta") or {}).get("workers")
    ]
    current = session_manager.get(sid) or {}
    current_msg = next(
        message for message in current.get("messages") or []
        if message.get("id") == msg_id
    )
    current_panel = next(
        panel for panel in current_msg.get("workers") or []
        if panel.get("delegation_id") == worker_id
    )
    ok = _check(
        len(projected_worker_deltas) == 3
        and len(current_panel.get("events") or []) == 1
        and current_panel.get("success") is True,
        "worker rows project through journal into canonical deltas",
        str(projected_worker_deltas),
    ) and ok

    session_manager.set_streaming(sid, msg_id, True)
    root = session_manager._load_root(sid, hydrate_events=False)
    snapshot = session_manager._compute_messages_snapshot(sid, sid, root) or {}
    snapshot_msg = next(
        message for message in snapshot.get("messages") or []
        if message.get("id") == msg_id
    )
    snapshot_panel = next(
        panel for panel in snapshot_msg.get("workers") or []
        if panel.get("delegation_id") == worker_id
    )
    ok = _check(
        not any(
            event.get("type") in ("worker_start", "worker_complete")
            for event in snapshot_msg.get("events") or []
        )
        and len(snapshot_panel.get("events") or []) == 1
        and snapshot_panel.get("success") is True,
        "streaming snapshot routes every worker lifecycle row to its panel",
        str(snapshot_msg),
    ) and ok

    streaming_snapshot = copy.deepcopy(current)
    streaming_msg = next(
        message for message in streaming_snapshot.get("messages") or []
        if message.get("id") == msg_id
    )
    streaming_msg["isStreaming"] = True
    streaming_msg["workers"] = []
    streaming_msg["events"] = []
    _hydrate_msg_events_from_jsonl(
        streaming_snapshot,
        caller_owns_live_tree=True,
    )
    hydrated_panel = next(
        (
            panel for panel in streaming_msg.get("workers") or []
            if panel.get("delegation_id") == worker_id
        ),
        None,
    )
    ok = _check(
        hydrated_panel is not None
        and len(hydrated_panel.get("events") or []) == 1
        and hydrated_panel.get("success") is True,
        "streaming reconnect snapshot rebuilds worker panels before watermark",
        str(streaming_msg),
    ) and ok

    ok = await _check_sync_write_then_barrier(sid) and ok
    return ok


async def _check_sync_write_then_barrier(sid: str) -> bool:
    """A synchronous journal write followed by the sync barrier must find
    its row already folded — the case `native_import.import_session` runs.

    `submit_event_sync` acks as soon as the writer thread commits the row,
    then hands the `EVENT_JOURNAL_WRITTEN` publish to the bound loop
    fire-and-forget. So the drainer command reliably lands AFTER the
    writing thread has already called the barrier. Barriering the drainer
    alone found no state for the root and returned having waited for
    nothing, leaving `msg.events` empty — which `import_session` reads as
    "no importable events" and tears the session down. Asserted with no
    polling or sleeps on purpose: the barrier must be deterministic, not
    merely likely.
    """
    from event_bus_subscribers import await_session_content_projection_sync
    from event_journal import publish_event_sync

    msg_id = "msg-sync-barrier"
    session_manager.append_assistant_msg(
        sid, {"id": msg_id, "role": "assistant", "content": "", "events": []},
    )

    def _journal_then_barrier() -> None:
        publish_event_sync(
            session_id=sid,
            context_id=sid,
            event_type="agent_message",
            data={
                "uuid": "sync-barrier-row",
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "sync"}]},
            },
            source="native_import",
            message_id=msg_id,
        )
        await_session_content_projection_sync(sid)

    # Off the loop: the barrier blocks, and the fold it waits on can only
    # be driven by the loop this coroutine is running on.
    await asyncio.to_thread(_journal_then_barrier)

    folded = session_manager.get(sid) or {}
    folded_msg = next(
        (m for m in folded.get("messages") or [] if m.get("id") == msg_id),
        {},
    )
    uuids = [
        (event.get("data") or {}).get("uuid")
        for event in folded_msg.get("events") or []
    ]
    return _check(
        uuids == ["sync-barrier-row"],
        "sync journal write is folded before the sync barrier returns",
        str(folded_msg),
    )


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_event_journal_bus_integration() -> None:
    assert asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
