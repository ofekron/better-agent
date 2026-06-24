"""Regression test for production event-bus → EventJournal wiring.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_bus_integration.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-event-journal-bus-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import BusEvent, bus  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from event_bus_subscribers import register_default_subscribers  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

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
    return ok


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
