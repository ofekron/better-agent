"""Prove live WS journal delivery reads through EventJournalReader.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_ws_reader.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-journal-ws-reader-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import EventBus  # noqa: E402
from event_journal import EventJournalWriter, publish_event  # noqa: E402
from jsonl_tailer import BetterAgentJsonlTailer, _Subscriber  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _run() -> bool:
    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)
    received: list[dict] = []

    async def _receive(frame: dict) -> None:
        received.append(frame)

    tailer = BetterAgentJsonlTailer(
        events_jsonl_path=Path(_TMP_HOME) / "unused-events-path",
        root_id="root-1",
    )
    subscriber = _Subscriber(
        app_session_id="root-1",
        ws_callback=_receive,
        from_seq=0,
        root_id="root-1",
    )
    await tailer.add_subscriber(subscriber)
    task = asyncio.create_task(tailer.run())
    await publish_event(
        session_id="root-1",
        event_type="agent_message",
        data={"uuid": "ws-reader", "message": {"content": []}},
        source="test",
        message_id="msg-1",
        bus_instance=bus,
    )
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0.01)
    tailer.stop()
    await task
    writer.close()

    ok = (
        len(received) == 1
        and received[0].get("type") == "agent_message"
        and received[0].get("seq") == 1
    )
    print(
        f"{PASS if ok else FAIL} WS delivery is driven by EventJournalReader "
        f"-- {received}",
    )
    return ok


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
