from __future__ import annotations

import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_journal import EventJournalReader, _MessageCacheEntry  # noqa: E402


def test_frontend_conversion_runs_outside_message_cache_lock() -> None:
    reader = EventJournalReader()
    lock = threading.Lock()
    reader._message_cache_lock = lock  # type: ignore[assignment]
    cached = _MessageCacheEntry(
        events=[{"type": "agent_message", "data": {"uuid": "u1"}}],
        frontend_events=None,
        byte_start=0,
        byte_end=1,
        seq_end=1,
        res_version=0,
    )
    reader._ensure_message_cache = lambda *_args, **_kwargs: cached  # type: ignore[method-assign]

    def convert(events: list[dict]) -> list[dict]:
        acquired = lock.acquire(blocking=False)
        if not acquired:
            raise AssertionError("frontend conversion ran under message cache lock")
        lock.release()
        return [{"type": "agent_message", "data": dict(events[0]["data"])}]

    reader._to_frontend_events = convert  # type: ignore[method-assign]
    got = reader.read_message_frontend_events("root", "msg")
    assert got == [{"type": "agent_message", "data": {"uuid": "u1"}}]
    assert cached.frontend_events == got


if __name__ == "__main__":
    test_frontend_conversion_runs_outside_message_cache_lock()
    print("ok")
