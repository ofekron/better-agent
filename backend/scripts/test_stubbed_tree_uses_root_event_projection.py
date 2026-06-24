from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-root-event-projection-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import event_journal  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _agent_msg(uid: str, text: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _uuids(events: list[dict]) -> set[str]:
    return {
        (event.get("data") or {}).get("uuid")
        for event in events
        if (event.get("data") or {}).get("uuid")
    }


def main() -> int:
    original_read_events = event_journal.event_journal_reader.read_events
    try:
        sess = session_manager.create(
            name="root-event-projection",
            cwd="/tmp/root-event-projection",
            orchestration_mode="native",
        )
        sid = sess["id"]
        session_manager.append_assistant_msg(
            sid,
            {
                "id": "asst-1",
                "role": "assistant",
                "content": "",
                "events": [],
                "isStreaming": False,
                "seq": 1,
            },
        )
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data=_agent_msg("owned", "owned"),
            source="test",
            msg_id="asst-1",
        )
        duplicate_seq = event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data=_agent_msg("owned", "owned duplicate"),
            source="test",
            msg_id=None,
        )
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data=_agent_msg("visible", "visible"),
            source="test",
            msg_id=None,
        )
        resolved_seq = event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data=_agent_msg("resolved", "resolved"),
            source="test",
            msg_id=None,
        )
        assert duplicate_seq > 0
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="event_ownership_resolved",
            data={"event_seq": resolved_seq, "message_id": "asst-1"},
            source="test",
            msg_id="asst-1",
        )

        calls = 0

        def counted_read_events(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("session load must not call journal read_events")

        event_journal.event_journal_reader.read_events = counted_read_events
        tree = session_manager.get_root_tree_stubbed(sid, msg_limit=10)
        root_events = (tree or {}).get("root_events") or []
        uuids = _uuids(root_events)

        checks = [
            ("visible orphan surfaces", "visible" in uuids),
            ("owned duplicate is deduped", "owned" not in uuids),
            ("resolved orphan is not detached", "resolved" not in uuids),
            ("journal reader was not called", calls == 0),
            ("only visible orphan remains", len(root_events) == 1),
        ]
        ok = True
        for label, passed in checks:
            print(f"{PASS if passed else FAIL} {label}")
            ok = ok and passed
        return 0 if ok else 1
    finally:
        event_journal.event_journal_reader.read_events = original_read_events
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
