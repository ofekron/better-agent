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


def test_message_frontend_events_cache() -> None:
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
        assert calls == 1, f"conversion calls before append: {calls}"
        assert first == second and len(first) == 1, (
            f"unexpected cached frontend events: first={first!r} second={second!r}"
        )
        event_ingester.ingest(
            root,
            sid=sid,
            event_type="agent_message",
            data=_data("u2", "two"),
            source="test",
            msg_id=msg_id,
        )
        third = reader.read_frontend_events(root, message_id=msg_id)
        assert calls == 2, f"conversion calls after append: {calls}"
        assert len(third) == 2, f"appended frontend events missing: {third!r}"


def test_current_message_cache_skips_summary_recompute() -> None:
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
    assert calls == 0, f"summary recompute calls on current cache hit: {calls}"
    assert second == first, (
        f"current cache hit changed events: first={first!r} second={second!r}"
    )


def test_cold_reader_reuses_persisted_frontend_projection() -> None:
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
        assert calls == 0, f"cold projection conversion calls: {calls}"
        assert second == first, (
            f"cold projection changed events: first={first!r} second={second!r}"
        )


def main() -> int:
    tests = [
        ("message frontend events cache", test_message_frontend_events_cache),
        ("current message cache skips summary recompute", test_current_message_cache_skips_summary_recompute),
        ("cold reader reuses persisted frontend projection", test_cold_reader_reuses_persisted_frontend_projection),
    ]
    failed = 0
    try:
        for label, runner in tests:
            try:
                runner()
            except AssertionError as exc:
                failed += 1
                print(f"{FAIL} {label}: {exc}")
            except Exception:
                failed += 1
                import traceback
                traceback.print_exc()
            else:
                print(f"{PASS} {label}")
        print(f"{len(tests) - failed}/{len(tests)} subtests passed")
        return 1 if failed else 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
