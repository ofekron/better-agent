"""Regression test for sync event publishers waiting on writer ack.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_sync_publish.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-journal-sync-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import bus  # noqa: E402
from event_journal import (  # noqa: E402
    bind_event_journal_loop,
    event_journal_reader,
    event_journal_writer,
    publish_event_sync,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


async def _run() -> bool:
    loop = asyncio.get_running_loop()
    bind_event_journal_loop(loop)
    event_journal_writer.register(bus)

    written = await asyncio.to_thread(
        publish_event_sync,
        session_id="sync-session",
        event_type="agent_message",
        data={
            "uuid": "sync-event",
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "sync"}]},
        },
        source="sync-test",
        message_id="sync-message",
    )
    rows = event_journal_reader.read_message_events(
        "sync-session", "sync-message",
    )

    ok = True
    ok = _check(
        written.seq == 1
        and isinstance(written.event_id, str)
        and written.event_id != "sync-event",
        "sync publisher returns writer acknowledgement",
        str(written),
    ) and ok
    ok = _check(
        len(rows) == 1 and rows[0].get("msg_id") == "sync-message",
        "sync publisher returns only after durable append",
        str(rows),
    ) and ok
    update_a, update_b = await asyncio.gather(
        asyncio.to_thread(
            publish_event_sync,
            session_id="sync-session",
            event_type="agent_message",
            data={"uuid": "shared-provider-uuid", "value": "a"},
            source="sync-test",
            message_id="sync-message",
        ),
        asyncio.to_thread(
            publish_event_sync,
            session_id="sync-session",
            event_type="agent_message",
            data={"uuid": "shared-provider-uuid", "value": "b"},
            source="sync-test",
            message_id="sync-message",
        ),
    )
    ok = _check(
        update_a.event_id != update_b.event_id
        and {update_a.seq, update_b.seq} == {2, 3},
        "same provider uuid waits for distinct writer acknowledgements",
        f"{update_a} / {update_b}",
    ) and ok

    loop_written = publish_event_sync(
        session_id="sync-session",
        event_type="agent_message",
        data={"uuid": "loop-thread"},
        source="sync-test",
    )
    ok = _check(
        loop_written.seq == 4,
        "sync loop-thread publisher waits on writer thread without deadlock",
        str(loop_written),
    ) and ok

    bus.unsubscribe("event_journal_writer")
    direct_written = await asyncio.to_thread(
        publish_event_sync,
        session_id="sync-session",
        event_type="agent_message",
        data={"uuid": "without-bus-subscriber"},
        source="sync-test",
    )
    ok = _check(
        direct_written.seq == 5,
        "sync publisher receives writer acknowledgement without bus round-trip",
        str(direct_written),
    ) and ok
    return ok


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
