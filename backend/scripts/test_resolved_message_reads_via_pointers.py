"""Lock the perf invariant: a message with late-resolved (orphan) events
reads through the per-message byte-pointer path, NOT a full-file scan.

Before the pointer rewrite, any session with an `event_ownership_resolved`
fact forced `read_message_events` into `read_events(limit=999_999)` — an
O(file) scan per read. This proves that path is gone while the resolved-in
event is still returned correctly.

Run with:
    cd backend && .venv/bin/python scripts/test_resolved_message_reads_via_pointers.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-resolved-pointer-")

from event_bus import EventBus  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import (  # noqa: E402
    EventJournalReader,
    EventJournalWriter,
    bind_event_journal_loop,
    publish_event,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _uuids(rows: list[dict]) -> list[str]:
    return [
        (row.get("data") or {}).get("uuid")
        for row in rows
        if (row.get("data") or {}).get("uuid")
    ]


async def _run() -> bool:
    bind_event_journal_loop(asyncio.get_running_loop())
    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)
    sid = "session-1"

    async def publish(**kwargs):
        return await publish_event(
            session_id=sid, context_id=sid, source="test",
            bus_instance=bus, **kwargs,
        )

    await publish(
        event_type="turn_started",
        data={"turn_id": "t1", "message_id": "msg-1"},
        message_id="msg-1", turn_id="t1",
    )
    # Orphan: a sidechain child arrives before its parent fact — no owner
    # yet, parked as pending.
    unresolved = await publish(
        event_type="agent_message",
        data={
            "uuid": "orphan-child",
            "parentUuid": "orphan-parent",
            "isSidechain": True,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "child"}]},
        },
    )
    # Pad the file so an accidental full scan would be visibly large and a
    # tight pointer read provably skips it.
    for i in range(50):
        await publish(
            event_type="agent_message",
            data={"uuid": f"pad-{i}", "type": "assistant",
                  "message": {"content": []}},
            message_id="msg-pad",
        )
    # Parent fact arrives → resolves the orphan to msg-1 (write-time fact).
    await publish(
        event_type="agent_message",
        data={"uuid": "orphan-parent", "type": "assistant",
              "message": {"content": []}},
        message_id="msg-1",
    )

    reader = EventJournalReader(message_cache_size=20)

    read_events_calls: list[int] = []
    raw_range_calls: list[tuple[int, int]] = []
    orig_read_events = event_ingester.read_events
    orig_raw = reader._read_raw_range

    def spy_read_events(root_id, *a, **kw):
        read_events_calls.append(kw.get("limit", a[1] if len(a) > 1 else 0))
        return orig_read_events(root_id, *a, **kw)

    def spy_raw(session_id, byte_start, byte_end, **kw):
        raw_range_calls.append((byte_start, byte_end))
        return orig_raw(session_id, byte_start, byte_end, **kw)

    event_ingester.read_events = spy_read_events
    reader._read_raw_range = spy_raw
    try:
        rows = reader.read_message_events(sid, "msg-1")
    finally:
        event_ingester.read_events = orig_read_events
        reader._read_raw_range = orig_raw

    ok = True
    ok = _check(unresolved.msg_id is None, "orphan parked unresolved") and ok
    ok = _check(
        "orphan-child" in _uuids(rows) and "orphan-parent" in _uuids(rows),
        "resolved-in orphan + own row both returned",
        str(_uuids(rows)),
    ) and ok
    ok = _check(
        len(read_events_calls) == 0,
        "no full scan: event_ingester.read_events not called",
        f"calls={read_events_calls}",
    ) and ok
    ok = _check(
        bool(raw_range_calls),
        "pointer path used: _read_raw_range driven by byte range",
        str(raw_range_calls),
    ) and ok

    writer.close()
    return ok


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
