"""Regression tests for deterministic event-journal ownership resolution.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_ownership.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-journal-ownership-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import EventBus  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import (  # noqa: E402
    Event,
    EventJournalReader,
    EventJournalWriter,
    publish_event,
)
import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _crash_cut_facts(root_id: str) -> list[Event]:
    common = {"root_id": root_id, "sid": root_id, "source": "crash-cut-test"}
    return [
        Event(
            **common,
            event_type="turn_started",
            data={
                "turn_id": "turn-1",
                "message_id": "msg-1",
                "source_ts": "2026-06-06T10:00:00Z",
            },
            message_id="msg-1",
            turn_id="turn-1",
            event_id="fact-turn-1",
        ),
        Event(
            **common,
            event_type="turn_started",
            data={
                "turn_id": "turn-2",
                "message_id": "msg-2",
                "source_ts": "2026-06-06T11:00:00Z",
            },
            message_id="msg-2",
            turn_id="turn-2",
            event_id="fact-turn-2",
        ),
        Event(
            **common,
            event_type="agent_message",
            data={
                "uuid": "late-child",
                "parentUuid": "late-parent",
                "isSidechain": True,
                "timestamp": "2026-06-06T11:10:00Z",
            },
            event_id="fact-late-child",
        ),
        Event(
            **common,
            event_type="agent_message",
            data={"uuid": "late-parent"},
            message_id="msg-1",
            event_id="fact-late-parent",
        ),
        Event(
            **common,
            event_type="agent_message",
            data={"uuid": "old-ts", "timestamp": "2026-06-06T10:30:00Z"},
            event_id="fact-old-ts",
        ),
        Event(
            **common,
            event_type="agent_message",
            data={
                "uuid": "top-level",
                "parentUuid": "late-parent",
                "isSidechain": False,
                "timestamp": "2026-06-06T11:20:00Z",
            },
            event_id="fact-top-level",
        ),
    ]


def _effective_ownership(root_id: str) -> dict[str, str | None]:
    rows, _, _ = EventJournalReader().read_events(root_id, limit=999_999)
    return {
        str((row.get("data") or {}).get("uuid")): row.get("msg_id")
        for row in rows
        if (row.get("data") or {}).get("uuid")
    }


def _run_crash_cut(cut: int, facts_count: int) -> dict[str, str | None]:
    root_id = f"crash-cut-{cut}"
    facts = _crash_cut_facts(root_id)
    first_writer = EventJournalWriter()
    for fact in facts[:cut]:
        first_writer.submit_event_sync(fact)
    first_writer.close()
    event_ingester.close_all()

    second_writer = EventJournalWriter()
    for fact in facts[cut:]:
        second_writer.submit_event_sync(fact)
    second_writer.close()
    event_ingester.close_all()
    assert len(facts) == facts_count
    return _effective_ownership(root_id)


async def _publish(bus: EventBus, **kwargs):
    return await publish_event(
        session_id="root-1",
        context_id="root-1",
        source="ownership-test",
        bus_instance=bus,
        **kwargs,
    )


async def _turn(
    bus: EventBus, turn_id: str, message_id: str, source_ts: str,
) -> None:
    await _publish(
        bus,
        event_type="turn_started",
        data={
            "turn_id": turn_id,
            "message_id": message_id,
            "source_ts": source_ts,
        },
        message_id=message_id,
        turn_id=turn_id,
        run_id=turn_id,
    )


async def _run() -> bool:
    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)

    await _turn(bus, "turn-1", "msg-1", "2026-06-06T10:00:00Z")
    first = await _publish(
        bus,
        event_type="agent_message",
        data={"uuid": "first", "timestamp": "2026-06-06T10:05:00Z"},
    )
    parent = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "parent",
            "timestamp": "2026-06-06T10:06:00Z",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Agent",
                    "input": {},
                }],
            },
        },
    )
    await _turn(bus, "turn-2", "msg-2", "2026-06-06T11:00:00Z")
    late = await _publish(
        bus,
        event_type="agent_message",
        data={"uuid": "late", "timestamp": "2026-06-06T10:30:00Z"},
    )
    equal = await _publish(
        bus,
        event_type="agent_message",
        data={"uuid": "equal", "timestamp": "2026-06-06T11:00:00Z"},
    )
    subagent = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "subagent",
            "parent_tool_use_id": "tool-1",
            "timestamp": "2026-06-06T11:10:00Z",
        },
    )
    tool_result = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "tool-result",
            "type": "user",
            "timestamp": "2026-06-06T11:10:30Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "done",
                }],
            },
        },
    )
    explicit_result = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "explicit-tool-result",
            "type": "user",
            "timestamp": "2026-06-06T11:10:31Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "result-only-id",
                    "content": "done",
                }],
            },
        },
        message_id="msg-1",
    )
    tool_result_child = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "tool-result-child",
            "parent_tool_use_id": "result-only-id",
            "timestamp": "2026-06-06T11:10:32Z",
        },
    )
    child = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "child",
            "parentUuid": "parent",
            "isSidechain": True,
            "timestamp": "2026-06-06T11:11:00Z",
        },
    )
    top_level_child = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "top-level-child",
            "parentUuid": "parent",
            "isSidechain": False,
            "timestamp": "2026-06-06T11:12:00Z",
        },
    )
    explicit = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "explicit",
            "parentUuid": "parent",
            "isSidechain": True,
            "timestamp": "2026-06-06T10:30:00Z",
        },
        message_id="msg-explicit",
    )
    unresolved = await _publish(
        bus,
        event_type="agent_message",
        data={
            "uuid": "unresolved-child",
            "parentUuid": "future-parent",
            "isSidechain": True,
            "timestamp": "2026-06-06T11:30:00Z",
        },
    )
    await _publish(
        bus,
        event_type="agent_message",
        data={"uuid": "future-parent"},
        message_id="msg-1",
    )
    duplicate_data = {"uuid": "duplicate-late-owner"}
    duplicate_orphan = await _publish(
        bus,
        event_type="agent_message",
        data=duplicate_data,
    )
    duplicate_owned = await _publish(
        bus,
        event_type="agent_message",
        data=duplicate_data,
        message_id="msg-2",
    )
    resolved_rows = EventJournalReader().read_message_events("root-1", "msg-1")
    duplicate_rows = EventJournalReader().read_message_events("root-1", "msg-2")

    ok = True
    ok = _check(
        first.msg_id == parent.msg_id == late.msg_id == "msg-1",
        "source timestamps resolve the correct turn interval",
        f"{first} / {parent} / {late}",
    ) and ok
    ok = _check(
        equal.msg_id == "msg-2",
        "event at boundary timestamp belongs to the new turn",
        str(equal),
    ) and ok
    ok = _check(
        subagent.msg_id == child.msg_id == "msg-1",
        "causal tool and parent facts outrank source timestamp",
        f"{subagent} / {child}",
    ) and ok
    ok = _check(
        tool_result.msg_id == "msg-1",
        "tool_result block resolves to the owning tool_use message",
        str(tool_result),
    ) and ok
    ok = _check(
        explicit_result.msg_id == "msg-1" and tool_result_child.msg_id is None,
        "tool_result ids are consumer keys, not causal donor facts",
        f"{explicit_result} / {tool_result_child}",
    ) and ok
    ok = _check(
        top_level_child.msg_id == "msg-2",
        "top-level provider parentUuid does not cross turn ownership",
        str(top_level_child),
    ) and ok
    ok = _check(
        explicit.msg_id == "msg-explicit",
        "explicit message ownership outranks causal and timestamp facts",
        str(explicit),
    ) and ok
    ok = _check(
        unresolved.msg_id is None
        and any(
            (row.get("data") or {}).get("uuid") == "unresolved-child"
            for row in resolved_rows
        ),
        "late causal fact appends ownership resolution applied by reader",
        str(resolved_rows),
    ) and ok
    ok = _check(
        duplicate_orphan.msg_id is None
        and duplicate_owned.msg_id == "msg-2"
        and any(
            (row.get("data") or {}).get("uuid") == "duplicate-late-owner"
            for row in duplicate_rows
        ),
        "stronger ownership fact survives render-row deduplication",
        str(duplicate_rows),
    ) and ok

    repeated_a = {"uuid": "repeated-provider-uuid", "marker": "a"}
    repeated_b = {"uuid": "repeated-provider-uuid", "marker": "b"}
    await _publish(bus, event_type="agent_message", data=repeated_a)
    await _publish(bus, event_type="agent_message", data=repeated_b)
    await _publish(
        bus,
        event_type="agent_message",
        data=repeated_b,
        message_id="msg-2",
    )
    repeated_rows, _, _ = EventJournalReader().read_events(
        "root-1", limit=999_999,
    )
    repeated_owners = {
        (row.get("data") or {}).get("marker"): row.get("msg_id")
        for row in repeated_rows
        if (row.get("data") or {}).get("uuid") == "repeated-provider-uuid"
    }
    ok = _check(
        repeated_owners == {"a": None, "b": "msg-2"},
        "ownership resolution targets one journal seq, not repeated provider UUID",
        str(repeated_owners),
    ) and ok

    writer.close()
    event_ingester.close_all()

    restart_bus = EventBus()
    restarted_writer = EventJournalWriter()
    restarted_writer.register(restart_bus)
    restored_subagent = await _publish(
        restart_bus,
        event_type="agent_message",
        data={
            "uuid": "restored-subagent",
            "parent_tool_use_id": "tool-1",
            "timestamp": "2026-06-06T11:20:00Z",
        },
    )
    restored_late = await _publish(
        restart_bus,
        event_type="agent_message",
        data={"uuid": "restored-late", "timestamp": "2026-06-06T10:40:00Z"},
    )
    ok = _check(
        restored_subagent.msg_id == restored_late.msg_id == "msg-1",
        "restart rebuilds causal and turn ownership from durable facts",
        f"{restored_subagent} / {restored_late}",
    ) and ok
    restarted_writer.close()

    facts_count = len(_crash_cut_facts("count-only"))
    expected = _run_crash_cut(facts_count, facts_count)
    cuts = [_run_crash_cut(cut, facts_count) for cut in range(facts_count)]
    ok = _check(
        all(result == expected for result in cuts),
        "every writer crash cut converges to identical effective ownership",
        f"{expected=} {cuts=}",
    ) and ok

    snapshot = session_store.create_session(
        id="snapshot-boundary-root",
        orchestration_mode="native",
    )
    local_now = datetime.now().astimezone().replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    snapshot["messages"] = [
        {
            "id": "snapshot-msg-1",
            "role": "assistant",
            "timestamp": local_now.replace(tzinfo=None).isoformat(),
            "content": "",
            "seq": 0,
        },
        {
            "id": "snapshot-msg-2",
            "role": "assistant",
            "timestamp": (local_now + timedelta(hours=1)).replace(
                tzinfo=None,
            ).isoformat(),
            "content": "",
            "seq": 1,
        },
    ]
    snapshot["next_seq"] = 2
    session_store.write_session_full(snapshot, bump_updated_at=False)
    snapshot_writer = EventJournalWriter()
    snapshot_owned = snapshot_writer.submit_event_sync(Event(
        root_id=snapshot["id"],
        sid=snapshot["id"],
        event_type="agent_message",
        data={
            "uuid": "snapshot-owned",
            "timestamp": (local_now + timedelta(minutes=30)).replace(
                tzinfo=None,
            ).isoformat(),
        },
        source="snapshot-boundary-test",
    ))
    snapshot_before = snapshot_writer.submit_event_sync(Event(
        root_id=snapshot["id"],
        sid=snapshot["id"],
        event_type="agent_message",
        data={
            "uuid": "snapshot-before",
            "timestamp": (local_now - timedelta(minutes=30)).replace(
                tzinfo=None,
            ).isoformat(),
        },
        source="snapshot-boundary-test",
    ))
    snapshot_writer.close()
    ok = _check(
        snapshot_owned.msg_id == "snapshot-msg-1"
        and snapshot_before.msg_id is None,
        "snapshot turn boundaries own restored local timestamps safely",
        f"{snapshot_owned=} {snapshot_before=}",
    ) and ok
    return ok


def _run_pending_resolution_scan_gate() -> bool:
    writer = EventJournalWriter()
    calls: list[str] = []
    original_resolve = writer._resolve_pending_events

    def counted(root_id: str) -> int:
        calls.append(root_id)
        return original_resolve(root_id)

    writer._resolve_pending_events = counted  # type: ignore[method-assign]
    common = {"root_id": "gated-root", "sid": "gated-root", "source": "test"}
    writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data={"uuid": "pending-child", "parent_tool_use_id": "tool-1"},
    ))
    writer.submit_event_sync(Event(
        **common,
        event_type="trace_step",
        data={"kind": "irrelevant"},
    ))
    no_fact_calls = len(calls)
    writer.submit_event_sync(Event(
        **common,
        event_type="turn_started",
        data={
            "turn_id": "turn-1",
            "message_id": "msg-1",
            "source_ts": "2026-06-06T10:00:00Z",
        },
        message_id="msg-1",
        turn_id="turn-1",
    ))
    turn_fact_calls = len(calls)
    writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data={
            "uuid": "tool-owner",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "Read"},
                ],
            },
        },
        message_id="msg-1",
    ))
    causal_fact_calls = len(calls)
    writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data={"uuid": "duplicate-donor-child", "parent_tool_use_id": "tool-dup"},
    ))
    duplicate_donor = {
        "uuid": "duplicate-donor",
        "message": {
            "content": [
                {"type": "tool_use", "id": "tool-dup", "name": "Read"},
            ],
        },
    }
    writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data=duplicate_donor,
    ))
    before_duplicate_resolution_calls = len(calls)
    writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data=duplicate_donor,
        message_id="msg-1",
    ))
    after_duplicate_resolution_calls = len(calls)
    duplicate_child_rows = EventJournalReader().read_message_events(
        "gated-root", "msg-1",
    )
    writer.close()
    event_ingester.close_all()

    ok = True
    ok = _check(
        no_fact_calls == 0,
        "pending scan skipped for pending/unrelated writes",
        str(calls),
    ) and ok
    ok = _check(
        turn_fact_calls == 1,
        "pending scan runs when turn boundary fact is recorded",
        str(calls),
    ) and ok
    ok = _check(
        causal_fact_calls == 2,
        "pending scan runs when causal owner fact is recorded",
        str(calls),
    ) and ok
    ok = _check(
        after_duplicate_resolution_calls == before_duplicate_resolution_calls + 1,
        "pending scan runs when duplicate donor records causal facts",
        str(calls),
    ) and ok
    ok = _check(
        any(
            (row.get("data") or {}).get("uuid") == "duplicate-donor-child"
            for row in duplicate_child_rows
        ),
        "duplicate donor resolves pending child immediately",
        str(duplicate_child_rows),
    ) and ok
    return ok


def _run_hydration_fact_scan_gate() -> bool:
    root_id = "hydration-gated-root"
    common = {"root_id": root_id, "sid": root_id, "source": "test"}
    first_writer = EventJournalWriter()
    first_writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data={"uuid": "hydrated-child", "parent_tool_use_id": "tool-hydrated"},
    ))
    duplicate_donor = {
        "uuid": "hydrated-donor",
        "message": {
            "content": [
                {"type": "tool_use", "id": "tool-hydrated", "name": "Read"},
            ],
        },
    }
    first_writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data=duplicate_donor,
    ))
    first_writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data=duplicate_donor,
        message_id="msg-hydrated",
    ))
    first_writer.close()
    event_ingester.close_all()

    restarted_writer = EventJournalWriter()
    calls: list[str] = []
    original_resolve = restarted_writer._resolve_pending_events

    def counted(root: str) -> int:
        calls.append(root)
        return original_resolve(root)

    restarted_writer._resolve_pending_events = counted  # type: ignore[method-assign]
    restarted_writer.submit_event_sync(Event(
        **common,
        event_type="trace_step",
        data={"kind": "unrelated-after-restart"},
    ))
    rows = EventJournalReader().read_message_events(root_id, "msg-hydrated")
    restarted_writer.close()
    event_ingester.close_all()
    ok = True
    ok = _check(
        calls == [root_id],
        "hydration-created facts flush pending once",
        str(calls),
    ) and ok
    ok = _check(
        any(
            (row.get("data") or {}).get("uuid") == "hydrated-child"
            for row in rows
        ),
        "hydration-created duplicate donor resolves pending child",
        str(rows),
    ) and ok
    return ok


def _run_snapshot_boundary_hydration_scan_gate() -> bool:
    root = session_store.create_session(
        id="snapshot-hydration-gated-root",
        orchestration_mode="native",
    )
    local_now = datetime.now().astimezone().replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    root["messages"] = [{
        "id": "snapshot-hydration-msg",
        "role": "assistant",
        "timestamp": local_now.replace(tzinfo=None).isoformat(),
        "content": "",
        "seq": 0,
    }]
    root["next_seq"] = 1
    session_store.write_session_full(root, bump_updated_at=False)
    root_id = root["id"]
    common = {"root_id": root_id, "sid": root_id, "source": "test"}
    first_writer = EventJournalWriter()
    first_writer.submit_event_sync(Event(
        **common,
        event_type="agent_message",
        data={
            "uuid": "snapshot-hydrated-event",
            "timestamp": (local_now + timedelta(minutes=5)).replace(
                tzinfo=None,
            ).isoformat(),
        },
    ))
    first_writer.close()
    event_ingester.close_all()

    restarted_writer = EventJournalWriter()
    calls: list[str] = []
    original_resolve = restarted_writer._resolve_pending_events

    def counted(root_for_call: str) -> int:
        calls.append(root_for_call)
        return original_resolve(root_for_call)

    restarted_writer._resolve_pending_events = counted  # type: ignore[method-assign]
    restarted_writer.submit_event_sync(Event(
        **common,
        event_type="trace_step",
        data={"kind": "unrelated-after-snapshot-hydration"},
    ))
    rows = EventJournalReader().read_message_events(
        root_id, "snapshot-hydration-msg",
    )
    restarted_writer.close()
    event_ingester.close_all()
    ok = True
    ok = _check(
        calls == [root_id],
        "snapshot boundary hydration flushes pending once",
        str(calls),
    ) and ok
    ok = _check(
        any(
            (row.get("data") or {}).get("uuid") == "snapshot-hydrated-event"
            for row in rows
        ),
        "snapshot boundary hydration resolves timestamp-owned pending row",
        str(rows),
    ) and ok
    return ok


def main() -> int:
    try:
        ok = asyncio.run(_run())
        ok = _run_pending_resolution_scan_gate() and ok
        ok = _run_hydration_fact_scan_gate() and ok
        ok = _run_snapshot_boundary_hydration_scan_gate() and ok
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
