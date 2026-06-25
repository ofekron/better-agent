"""Regression: message frontend event conversion is cached."""

from __future__ import annotations

import os
import shutil
import sys
from unittest.mock import patch

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-message-frontend-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from event_journal import EventJournalReader  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _data(uid: str, text: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_message_frontend_events_cache() -> bool:
    root = "root-message-frontend-cache"
    sid = root
    msg_id = "msg-message-frontend-cache"
    reader = EventJournalReader()
    event_ingester.ingest(
        root,
        sid=sid,
        event_type="agent_message",
        data=_data("u1", "one"),
        source="test",
        msg_id=msg_id,
    )

    original = EventJournalReader._to_frontend_events
    calls = 0

    def counted(rows):
        nonlocal calls
        calls += 1
        return original(rows)

    with patch.object(EventJournalReader, "_to_frontend_events", side_effect=counted):
        first = reader.read_frontend_events(root, message_id=msg_id)
        second = reader.read_frontend_events(root, message_id=msg_id)
        if calls != 1:
            print(f"conversion calls before append: {calls}")
            return False
        if first != second or len(first) != 1:
            print(f"unexpected cached frontend events: first={first!r} second={second!r}")
            return False
        event_ingester.ingest(
            root,
            sid=sid,
            event_type="agent_message",
            data=_data("u2", "two"),
            source="test",
            msg_id=msg_id,
        )
        third = reader.read_frontend_events(root, message_id=msg_id)
        if calls != 2:
            print(f"conversion calls after append: {calls}")
            return False
        if len(third) != 2:
            print(f"appended frontend events missing: {third!r}")
            return False
    return True


def test_current_message_cache_skips_summary_recompute() -> bool:
    root = "root-message-summary-fast-hit"
    sid = root
    msg_id = "msg-message-summary-fast-hit"
    reader = EventJournalReader()
    event_ingester.ingest(
        root,
        sid=sid,
        event_type="agent_message",
        data=_data("summary-u1", "one"),
        source="test",
        msg_id=msg_id,
    )

    first = reader.read_frontend_events(root, message_id=msg_id)
    original = reader.message_event_summaries
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    reader.message_event_summaries = counted  # type: ignore[method-assign]
    second = reader.read_frontend_events(root, message_id=msg_id)
    if calls != 0:
        print(f"summary recompute calls on current cache hit: {calls}")
        return False
    if second != first:
        print(f"current cache hit changed events: first={first!r} second={second!r}")
        return False
    return True


def test_cold_reader_reuses_persisted_frontend_projection() -> bool:
    root = "root-message-frontend-projection"
    sid = root
    msg_id = "msg-message-frontend-projection"
    first_reader = EventJournalReader()
    event_ingester.ingest(
        root,
        sid=sid,
        event_type="agent_message",
        data=_data("projection-u1", "one"),
        source="test",
        msg_id=msg_id,
    )
    first = first_reader.read_frontend_events(root, message_id=msg_id)
    event_ingester.ingest(
        root,
        sid=sid,
        event_type="agent_message",
        data=_data("projection-unrelated", "other"),
        source="test",
        msg_id="other-message",
    )

    cold_reader = EventJournalReader()
    original = EventJournalReader._to_frontend_events
    calls = 0

    def counted(rows):
        nonlocal calls
        calls += 1
        return original(rows)

    with patch.object(EventJournalReader, "_to_frontend_events", side_effect=counted):
        second = cold_reader.read_frontend_events(root, message_id=msg_id)
        if calls != 0:
            print(f"cold projection conversion calls: {calls}")
            return False
        if second != first:
            print(f"cold projection changed events: first={first!r} second={second!r}")
            return False
    return True


def main() -> int:
    try:
        ok = test_message_frontend_events_cache()
        print(f"{PASS if ok else FAIL} message frontend events cache")
        fast_ok = test_current_message_cache_skips_summary_recompute()
        print(f"{PASS if fast_ok else FAIL} current message cache skips summary recompute")
        projection_ok = test_cold_reader_reuses_persisted_frontend_projection()
        print(f"{PASS if projection_ok else FAIL} cold reader reuses persisted frontend projection")
        ok = ok and fast_ok and projection_ok
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
