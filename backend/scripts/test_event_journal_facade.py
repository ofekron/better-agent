"""Regression tests for the EventJournal writer/reader facade.

This keeps the first consolidation step honest:
  - producers publish event_journal.event BusEvents.
  - EventJournalWriter resolves message/root/metadata ownership internally.
  - writer emits event_journal.written ack events after durable append.
  - reader projections delegate through the same facade callers will use.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_facade.py
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
_TMP_HOME = _test_home.isolate("bc-test-event-journal-")

from event_bus import BusEvent, EventBus  # noqa: E402
from event_journal import (  # noqa: E402
    EVENT_JOURNAL_EVENT,
    EVENT_JOURNAL_TURN_FINISHED,
    EVENT_JOURNAL_TURN_MESSAGE_SET,
    EVENT_JOURNAL_WRITE_FAILED,
    EVENT_JOURNAL_WRITTEN,
    RENDER_EVENT_TYPES,
    EventJournalWriter,
    event_journal_reader,
    publish_event,
)
from event_bus_subscribers import (  # noqa: E402
    _SESSION_PROJECTION_DISPATCHER,
    _refresh_session_content_projection,
)
from session_manager import manager as session_manager  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _agent_data(uid: str, text: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


async def _duplicate_ack_is_not_projected_twice() -> None:
    import session_search_projection

    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _refresh_session_content_projection,
        name="duplicate-content-projection",
    )
    acks: list[BusEvent] = []
    subscriber_failures: list[BusEvent] = []

    async def record_ack(event: BusEvent) -> None:
        acks.append(event)

    async def record_subscriber_failure(event: BusEvent) -> None:
        subscriber_failures.append(event)

    bus.subscribe(EVENT_JOURNAL_WRITTEN, record_ack, name="duplicate-ack-recorder")
    bus.subscribe("subscriber_failed", record_subscriber_failure, name="duplicate-failure-recorder")
    session = session_manager.create(
        name="duplicate-ack", cwd="/tmp", orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.append_assistant_msg(
        sid, {"id": "msg-dup", "role": "assistant", "content": "", "events": []},
    )
    indexed: list[dict] = []
    dirty: list[str] = []
    original_index = session_search_projection.note_event_written
    original_dirty = session_manager.mark_reconcile_dirty
    session_search_projection.note_event_written = lambda _root_id, entry: indexed.append(entry)
    session_manager.mark_reconcile_dirty = lambda root_id: dirty.append(root_id)
    try:
        data = _agent_data("same-uid", "one durable event")
        first = await publish_event(
            session_id=sid,
            event_type="agent_message",
            data=data,
            source="test",
            message_id="msg-dup",
            bus_instance=bus,
        )
        duplicate = await publish_event(
            session_id=sid,
            event_type="agent_message",
            data=data,
            source="test",
            message_id="msg-dup",
            bus_instance=bus,
        )
        await _SESSION_PROJECTION_DISPATCHER.barrier(sid)
        rows = event_journal_reader.read_message_events(sid, "msg-dup")
        msg = session_manager.get_message_full(sid, "msg-dup") or {}
        assert first.seq > 0
        assert duplicate.seq == -1
        assert [event.payload.get("appended") for event in acks] == [True, False]
        assert len(rows) == 1
        assert len(msg.get("events") or []) == 1
        assert len(indexed) == 1
        assert dirty == []
        assert subscriber_failures == []
    finally:
        session_search_projection.note_event_written = original_index
        session_manager.mark_reconcile_dirty = original_dirty
        writer.close()


def test_duplicate_ack_is_not_projected_twice() -> None:
    asyncio.run(_duplicate_ack_is_not_projected_twice())


def _agent_blocks(uid: str, blocks: list[dict]) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": blocks},
    }


async def _publish(
    bus: EventBus,
    sid: str,
    *,
    event_type: str,
    data: dict,
    source: str = "test",
    message_id: str | None = None,
    turn_id: str | None = None,
    event_id: str | None = None,
) -> None:
    payload = {
        "event_type": event_type,
        "data": data,
        "source": source,
    }
    if message_id:
        payload["message_id"] = message_id
    if turn_id:
        payload["turn_id"] = turn_id
    if event_id:
        payload["event_id"] = event_id
    await bus.publish(BusEvent(
        type=EVENT_JOURNAL_EVENT,
        root_id=sid,
        sid=sid,
        payload=payload,
    ))


async def _publish_turn_message_set(
    bus: EventBus,
    sid: str,
    *,
    turn_id: str,
    message_id: str,
) -> None:
    await bus.publish(BusEvent(
        type=EVENT_JOURNAL_TURN_MESSAGE_SET,
        root_id=sid,
        sid=sid,
        payload={"turn_id": turn_id, "message_id": message_id},
        persist=False,
    ))


async def _publish_turn_finished(
    bus: EventBus,
    sid: str,
    *,
    turn_id: str,
) -> None:
    await bus.publish(BusEvent(
        type=EVENT_JOURNAL_TURN_FINISHED,
        root_id=sid,
        sid=sid,
        payload={"turn_id": turn_id},
        persist=False,
    ))


async def _run() -> bool:
    sess = session_manager.create(
        name="journal", model="sonnet", cwd="/tmp/journal",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    session_manager.append_assistant_msg(
        sid, {"id": "msg-1", "role": "assistant", "content": "", "events": []},
    )
    session_manager.append_assistant_msg(
        sid, {"id": "msg-2", "role": "assistant", "content": "", "events": []},
    )

    ok = True

    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)
    acks: list[BusEvent] = []
    failures: list[BusEvent] = []

    async def _record_ack(ev: BusEvent) -> None:
        acks.append(ev)

    async def _record_failure(ev: BusEvent) -> None:
        failures.append(ev)

    async def _project_session_content(ev: BusEvent) -> None:
        if not ev.msg_id:
            return
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        if payload.get("event_type") not in RENDER_EVENT_TYPES:
            return
        session_manager.apply_written_journal_event(
            ev.root_id,
            ev.sid,
            ev.msg_id,
            str(payload.get("event_type") or "unknown"),
            payload.get("data") if isinstance(payload.get("data"), dict) else {},
            int(payload.get("seq") or 0),
        )

    bus.subscribe(EVENT_JOURNAL_WRITTEN, _record_ack, name="ack_recorder")
    bus.subscribe(EVENT_JOURNAL_WRITE_FAILED, _record_failure, name="fail_recorder")
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _project_session_content,
        name="session_content_projection",
    )

    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data=_agent_data("owned", "owned text"),
        message_id="msg-1",
        event_id="e-owned",
    )
    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data=_agent_blocks("owned-final", [
            {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
            {"type": "text", "text": "final answer"},
        ]),
        message_id="msg-1",
        event_id="e-owned-final",
    )
    await _publish_turn_message_set(
        bus,
        sid,
        turn_id="turn-2",
        message_id="msg-2",
    )
    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data=_agent_data("turn-owned", "turn owned text"),
        turn_id="turn-2",
        event_id="e-turn-owned",
    )
    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data=_agent_data("root", "root text"),
        event_id="e-root",
    )
    await _publish(
        bus,
        sid,
        event_type="command_received",
        data={"uuid": "meta", "method": "TEST"},
    )
    await _publish_turn_finished(bus, sid, turn_id="turn-2")
    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data=_agent_data("post-finish", "post finish text"),
        turn_id="turn-2",
        event_id="e-post-finish",
    )

    ok = _check(
        [a.payload.get("seq") for a in acks] == [1, 2, 3, 4, 5, 6],
        "writer emits ordered EventWritten acks",
        str([a.payload for a in acks]),
    ) and ok
    ok = _check(
        [a.payload.get("event_id") for a in acks]
        == [
            "e-owned", "e-owned-final", "e-turn-owned", "e-root",
            "meta", "e-post-finish",
        ],
        "EventWritten acks echo event_id",
        str([a.payload for a in acks]),
    ) and ok

    rows, total, has_more = event_journal_reader.read_events(sid, limit=10)
    msg_ids = [(r.get("data") or {}).get("uuid") + ":" + str(r.get("msg_id")) for r in rows]
    ok = _check(total == 6 and not has_more, "reader returns all rows", str(total)) and ok
    ok = _check("owned:msg-1" in msg_ids, "owned row keeps msg_id", str(msg_ids)) and ok
    ok = _check(
        "owned-final:msg-1" in msg_ids,
        "second owned row keeps msg_id",
        str(msg_ids),
    ) and ok
    ok = _check(
        "turn-owned:msg-2" in msg_ids,
        "writer resolves turn_id to message_id",
        str(msg_ids),
    ) and ok
    ok = _check("root:None" in msg_ids, "root row is explicitly unattached", str(msg_ids)) and ok
    ok = _check(
        "post-finish:msg-2" in msg_ids,
        "turn_finished preserves ownership for late provider events",
        str(msg_ids),
    ) and ok
    ok = _check("meta:None" in msg_ids, "metadata row is session-scoped", str(msg_ids)) and ok

    domain_rows, domain_total, _ = event_journal_reader.read_session_events(
        sid, limit=10,
    )
    ok = _check(
        domain_total == 6 and len(domain_rows) == 6,
        "domain reader returns session events",
        str(domain_rows),
    ) and ok

    owned_rows = event_journal_reader.read_message_events(sid, "msg-1")
    ok = _check(
        len(owned_rows) == 2
        and [(r.get("data") or {}).get("uuid") for r in owned_rows]
        == ["owned", "owned-final"],
        "domain reader message filter works",
        str(owned_rows),
    ) and ok
    msg1 = session_manager.get_message_full(sid, "msg-1") or {}
    ok = _check(
        msg1.get("content") == "final answer",
        "session projection refreshes collapsed assistant content",
        str(msg1),
    ) and ok
    ok = _check(
        [(e.get("data") or {}).get("uuid") for e in msg1.get("events") or []]
        == ["owned", "owned-final"],
        "session projection routes written rows through apply_event",
        str(msg1),
    ) and ok
    await _refresh_session_content_projection(BusEvent(
        type=EVENT_JOURNAL_WRITTEN,
        root_id=sid,
        sid=sid,
        msg_id="msg-1",
        payload={
            "event_type": "agent_message",
            "seq": 999,
            "source": "provider_stream",
            "data": _agent_data("provider-stream-skip", "skip"),
        },
        persist=False,
    ))
    msg1_after_provider_stream = session_manager.get_message_full(sid, "msg-1") or {}
    ok = _check(
        [(e.get("data") or {}).get("uuid")
         for e in msg1_after_provider_stream.get("events") or []]
        == ["owned", "owned-final"],
        "generic journal projection skips provider_stream rows",
        str(msg1_after_provider_stream),
    ) and ok

    live_msg = {
        "id": "msg-live",
        "role": "assistant",
        "content": "",
        "events": [],
    }
    session_manager.append_assistant_msg(sid, live_msg)
    live_event = {
        "type": "agent_message",
        "data": _agent_data("live-provider", "live provider text"),
    }
    await TurnManager._publish_provider_stream_event(
        None,
        app_session_id=sid,
        event_dict=live_event,
        assistant_msg=live_msg,
        run_id="run-live",
    )
    live_rows, live_total, _ = event_journal_reader.read_events(sid, limit=20)
    provider_rows = [
        r for r in live_rows
        if (r.get("data") or {}).get("uuid") == "live-provider"
    ]
    ok = _check(
        live_total == 7
        and len(provider_rows) == 1
        and provider_rows[0].get("source") == "provider_stream"
        and provider_rows[0].get("msg_id") == "msg-live",
        "provider stream publish writes exactly one message-owned journal row",
        str(provider_rows),
    ) and ok
    root_ref_before_apply = session_manager.get_ref(sid) or {}
    msg_live_before_apply = next(
        (
            m for m in root_ref_before_apply.get("messages") or []
            if m.get("id") == "msg-live"
        ),
        {},
    )
    ok = _check(
        msg_live_before_apply.get("events") == [],
        "provider stream publish does not mutate render tree before live apply",
        str(msg_live_before_apply),
    ) and ok
    replay_before_apply = session_manager.get_messages_since(
        sid, since_seq=0, limit=10,
    ) or {}
    replay_live_msg = next(
        (
            m for m in replay_before_apply.get("messages") or []
            if m.get("id") == "msg-live"
        ),
        {},
    )
    ok = _check(
        [(e.get("data") or {}).get("uuid")
         for e in replay_live_msg.get("events") or []]
        == ["live-provider"]
        and replay_live_msg.get("content") == "live provider text",
        "message replay recovers provider_stream row before live apply",
        str(replay_live_msg),
    ) and ok
    with session_manager.batch(sid):
        TurnManager._apply_event_to_assistant_msg(
            None,
            sid,
            live_event,
            live_msg,
            {},
            [],
            run_id="run-live",
            write_journal=False,
        )
    live_rows_after_apply, live_total_after_apply, _ = (
        event_journal_reader.read_events(sid, limit=20)
    )
    provider_rows_after_apply = [
        r for r in live_rows_after_apply
        if (r.get("data") or {}).get("uuid") == "live-provider"
    ]
    msg_live_after_apply = session_manager.get_message_full(sid, "msg-live") or {}
    ok = _check(
        live_total_after_apply == 7 and len(provider_rows_after_apply) == 1,
        "live apply with write_journal false does not duplicate provider row",
        str(provider_rows_after_apply),
    ) and ok
    ok = _check(
        [(e.get("data") or {}).get("uuid")
         for e in msg_live_after_apply.get("events") or []]
        == ["live-provider"],
        "provider stream live apply mutates render tree once",
        str(msg_live_after_apply),
    ) and ok

    orphans = event_journal_reader.read_unattached_events(sid)
    render_orphans = event_journal_reader.read_unattached_events(
        sid, render_only=True,
    )
    render_orphan_ids = {
        (e.get("data") or {}).get("uuid") for e in render_orphans
    }
    ok = _check(
        render_orphan_ids == {"root"},
        "domain reader unattached render projection works",
        str(orphans),
    ) and ok

    frontend_events = event_journal_reader.read_frontend_events(
        sid, message_id="msg-1",
    )
    ok = _check(
        [(e.get("data") or {}).get("uuid") for e in frontend_events]
        == ["owned", "owned-final"],
        "domain reader frontend projection works",
        str(frontend_events),
    ) and ok

    # Framing/control rows are stamped with a msg_id in events.jsonl for
    # ownership + recovery, but MUST NOT surface as renderable frontend
    # events — otherwise a native reload renders e.g. "unknown event:
    # event.turn_started" and diverges from the live render tree. The
    # frontend gate is `_to_frontend_events`; lock it directly so a future
    # refactor can't reintroduce the leak.
    mixed_rows = [
        {"type": "turn_started", "data": {"turn_id": "t-1"}},
        {"type": "turn_start", "data": {"manager_session_id": "m"}},
        {"type": "trace_step", "data": {"trace_id": "tr"}},
        {"type": "turn_complete", "data": {"success": True}},
        {"type": "agent_message", "data": _agent_data("a1", "hi")},
        {
            "type": "manager_event",
            "data": {"event": {"type": "agent_message",
                               "data": _agent_data("m1", "mgr")}},
        },
    ]
    projected = event_journal_reader._to_frontend_events(mixed_rows)
    ok = _check(
        [(e.get("data") or {}).get("uuid") for e in projected] == ["a1", "m1"]
        and all(
            e.get("type") not in (
                "turn_started", "turn_start", "trace_step", "turn_complete",
            )
            for e in projected
        ),
        "frontend projection drops framing/control rows",
        str(projected),
    ) and ok

    try:
        event_journal_reader.read_session_events(
            sid, fork_id="fork-a", worker_id="worker-b",
        )
        ok = _check(False, "domain reader rejects ambiguous context") and ok
    except ValueError:
        ok = _check(True, "domain reader rejects ambiguous context") and ok

    await bus.publish(BusEvent(
        type=EVENT_JOURNAL_EVENT,
        root_id=sid,
        sid=sid,
        payload={
            "event_type": "agent_message",
            "source": "test",
            "event_id": "e-bad",
        },
    ))
    ok = _check(
        len(failures) == 1
        and failures[0].payload.get("event_id") == "e-bad"
        and failures[0].payload.get("error_class") == "ValueError",
        "writer emits EventWriteFailed ack for invalid payload",
        str([f.payload for f in failures]),
    ) and ok
    rows_after_failure, total_after_failure, _ = event_journal_reader.read_events(
        sid, limit=10,
    )
    ok = _check(
        total_after_failure == 7 and len(rows_after_failure) == 7,
        "failed journal event does not append a row",
        str(rows_after_failure),
    ) and ok
    from event_ingester import event_ingester  # noqa: E402

    page_start_seq = total_after_failure
    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data={
            **_agent_data("late-child", "late child"),
            "parentUuid": "late-parent",
            "isSidechain": True,
        },
        event_id="e-late-child",
    )
    for i in range(12):
        await _publish(
            bus,
            sid,
            event_type="agent_message",
            data=_agent_data(f"pad-{i}", f"pad {i}"),
            message_id="msg-pad",
            event_id=f"e-pad-{i}",
        )
    await _publish(
        bus,
        sid,
        event_type="agent_message",
        data=_agent_data("late-parent", "late parent"),
        message_id="msg-late",
        event_id="e-late-parent",
    )
    late_reader = type(event_journal_reader)(message_cache_size=20)
    paged_calls: list[dict] = []
    original_read_events = event_ingester.read_events

    def spy_read_events(root_id, *args, **kwargs):
        paged_calls.append(dict(kwargs))
        return original_read_events(root_id, *args, **kwargs)

    event_ingester.read_events = spy_read_events
    try:
        paged_rows, paged_total, paged_more = late_reader.read_events(
            sid,
            after_seq=page_start_seq,
            limit=5,
            sid_filter=sid,
        )
    finally:
        event_ingester.read_events = original_read_events
    ok = _check(
        paged_calls
        and paged_calls[-1].get("limit") == 6
        and paged_calls[-1].get("after_seq") == page_start_seq
        and paged_calls[-1].get("sid_filter") == sid,
        "resolved reader keeps non-message reads paged",
        str(paged_calls),
    ) and ok
    ok = _check(
        len(paged_rows) == 5 and paged_total == 6 and paged_more,
        "resolved reader returns one capped page",
        f"len={len(paged_rows)} total={paged_total} more={paged_more}",
    ) and ok
    ok = _check(
        any(
            (row.get("data") or {}).get("uuid") == "late-child"
            and row.get("msg_id") == "msg-late"
            for row in paged_rows
        ),
        "paged resolved reader stamps late-owned rows",
        str([(row.get("data") or {}).get("uuid") + ":" + str(row.get("msg_id"))
             for row in paged_rows]),
    ) and ok
    return ok


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
