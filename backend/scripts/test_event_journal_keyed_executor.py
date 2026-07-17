from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import event_journal
from event_journal import EventJournalWriter


def test_read_lane_does_not_queue_behind_write_lane_for_same_root() -> None:
    """This is the exact starvation the read/write lane split fixes:
    ensure_canonical_authority_sync (read lane) must not sit behind a
    slow/backlogged write for the same root."""
    writer = EventJournalWriter()
    release = threading.Event()
    started = threading.Event()
    try:
        slow_write = writer._executor.submit(
            "root", lambda: (started.set(), release.wait())[1], lane="write",
        )
        assert started.wait(1)
        fast_read = writer._executor.submit(
            "root", lambda: "read-durable", lane="read",
        )
        assert fast_read.result(timeout=1) == "read-durable"
        assert not slow_write.done()
    finally:
        release.set()
        writer.close()


def test_write_lane_does_not_queue_behind_read_lane_for_same_root() -> None:
    writer = EventJournalWriter()
    release = threading.Event()
    started = threading.Event()
    try:
        slow_read = writer._executor.submit(
            "root", lambda: (started.set(), release.wait())[1], lane="read",
        )
        assert started.wait(1)
        fast_write = writer._executor.submit(
            "root", lambda: "write-durable", lane="write",
        )
        assert fast_write.result(timeout=1) == "write-durable"
        assert not slow_read.done()
    finally:
        release.set()
        writer.close()


def test_barrier_records_enqueue_to_start_wait() -> None:
    writer = EventJournalWriter()
    recorded: list[tuple[str, float]] = []
    original_record = event_journal.perf.record
    original_cursor = event_journal.event_ingester.cursor
    event_journal.perf.record = lambda name, value: recorded.append((name, value))
    event_journal.event_ingester.cursor = lambda root_id: 7
    try:
        assert writer.barrier_sync("root") == 7
    finally:
        event_journal.perf.record = original_record
        event_journal.event_ingester.cursor = original_cursor
        writer.close()
    samples = [value for name, value in recorded if name == "event_journal.barrier.queue_wait"]
    assert len(samples) == 1
    assert samples[0] >= 0


def test_ensure_canonical_authority_sync_uses_read_lane() -> None:
    writer = EventJournalWriter()
    seen_lanes: list[str] = []
    original_submit = writer._executor.submit

    def _tracking_submit(key, fn, /, *args, lane="default", **kwargs):
        seen_lanes.append(lane)
        return original_submit(key, fn, *args, lane=lane, **kwargs)

    writer._executor.submit = _tracking_submit
    try:
        writer._ensure_canonical_authority = lambda root_id: 42
        assert writer.ensure_canonical_authority_sync("root") == 42
    finally:
        writer.close()
    assert seen_lanes == ["read"]


if __name__ == "__main__":
    test_read_lane_does_not_queue_behind_write_lane_for_same_root()
    test_write_lane_does_not_queue_behind_read_lane_for_same_root()
    test_barrier_records_enqueue_to_start_wait()
    test_ensure_canonical_authority_sync_uses_read_lane()
    print("event journal keyed executor tests passed")
