"""Regression tests for writer-thread ownership and reader message LRU.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_thread_and_reader_cache.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-journal-thread-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import EventBus  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import (  # noqa: E402
    EventJournalReader,
    EventJournalWriter,
    publish_event,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


async def _write(
    bus: EventBus,
    message_id: str,
    uid: str,
    text: str,
) -> None:
    await publish_event(
        session_id="session-1",
        context_id="session-1",
        message_id=message_id,
        event_type="agent_message",
        data={
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
        source="test",
        bus_instance=bus,
    )


async def _run() -> bool:
    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)
    reader = EventJournalReader(message_cache_size=20)

    ingest_threads: list[str] = []
    original_ingest = event_ingester.ingest

    def _recording_ingest(*args, **kwargs):
        ingest_threads.append(threading.current_thread().name)
        return original_ingest(*args, **kwargs)

    event_ingester.ingest = _recording_ingest
    try:
        await _write(bus, "msg-0", "uid-0", "zero")
    finally:
        event_ingester.ingest = original_ingest

    ok = True
    ok = _check(
        ingest_threads
        and all(
            name.startswith("ejw-") and name[-1].isdigit()
            for name in ingest_threads
        ),
        "journal append runs on dedicated writer thread",
        str(ingest_threads),
    ) and ok

    ranges: list[tuple[int, int]] = []
    original_read_range = reader._read_raw_range

    def _recording_read_range(session_id, byte_start, byte_end, **kwargs):
        ranges.append((byte_start, byte_end))
        return original_read_range(session_id, byte_start, byte_end, **kwargs)

    reader._read_raw_range = _recording_read_range
    first = reader.read_message_events("session-1", "msg-0")
    unchanged = reader.read_message_events("session-1", "msg-0")
    await _write(bus, "msg-0", "uid-0-update", "zero updated")
    extended = reader.read_message_events("session-1", "msg-0")
    ok = _check(
        len(first) == 1
        and len(unchanged) == 1
        and len(extended) == 2
        and len(ranges) == 2
        and ranges[1][0] == ranges[0][1],
        "cached current message reads only newly appended byte block",
        str(ranges),
    ) and ok

    for index in range(1, 22):
        await _write(
            bus,
            f"msg-{index}",
            f"uid-{index}",
            str(index),
        )
        reader.read_message_events("session-1", f"msg-{index}")
    ok = _check(
        len(reader._message_cache) == 20
        and ("session-1", "session-1", "msg-0") not in reader._message_cache,
        "expanded-message LRU is capped at 20",
        str(list(reader._message_cache)),
    ) and ok

    path = Path(_TMP_HOME) / "sessions" / "session-1" / "events.jsonl"
    complete_offset = path.stat().st_size
    with path.open("ab") as file:
        file.write(b'{"seq":999,"sid":"session-1"')
        file.flush()
    partial_entries, partial_offset = reader._read_appended_entries(
        "session-1", complete_offset,
    )
    with path.open("ab") as file:
        file.write(b',"type":"complete","data":{}}\n')
        file.flush()
    completed_entries, completed_offset = reader._read_appended_entries(
        "session-1", partial_offset,
    )
    ok = _check(
        not partial_entries
        and partial_offset == complete_offset
        and len(completed_entries) == 1
        and completed_offset > complete_offset,
        "live reader does not advance past a partial journal line",
        f"{partial_offset=} {completed_entries=}",
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
