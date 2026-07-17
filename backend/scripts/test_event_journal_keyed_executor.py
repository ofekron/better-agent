from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import event_journal
from event_journal import Event, EventJournalWriter


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


def test_concurrent_roots_do_not_race_shared_ownership_dicts() -> None:
    """`_turn_messages`/`_turn_boundaries`/`_event_messages`/`_tool_messages`/
    `_delegate_messages`/`_pending_events` are all shared across every root
    (the first five flat, keyed by (root_id, ...); `_pending_events` nested,
    root_id -> sub_key) -- but KeyedLaneExecutor gives each root its own
    thread, so without `_ownership_state_lock` a `_clear_ownership_state` or
    `_write_ownership_checkpoint` iterating one of the flat dicts for one
    root can hit `RuntimeError: dictionary changed size during iteration`
    when a different root's thread inserts a brand-new key concurrently.
    Hammer many distinct roots concurrently through insert, iterate-and-
    clear, snapshot, and pending-event paths and assert nothing raises."""
    writer = EventJournalWriter()
    root_count = 24
    iterations = 60
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def churn(root_index: int) -> None:
        root_id = f"race-root-{root_index}"
        try:
            for i in range(iterations):
                timestamp = datetime.fromtimestamp(
                    1_700_000_000 + root_index * 1000 + i, tz=timezone.utc,
                ).isoformat()
                event = Event(
                    root_id=root_id,
                    sid=f"sid-{root_index}",
                    event_type="turn_start",
                    data={"turn_id": f"turn-{i}", "timestamp": timestamp},
                    source="test",
                    turn_id=f"turn-{i}",
                )
                writer._record_turn_started(event, f"msg-{root_index}-{i}")
                # Also churns `_pending_events`, the sixth root-keyed
                # shared dict (nested: root_id -> journal_seq -> Event).
                pending_event = Event(
                    root_id=root_id,
                    sid=f"sid-{root_index}",
                    event_type="agent_message",
                    data={"uuid": f"pending-{root_index}-{i}"},
                    source="test",
                )
                writer._record_resolved_event(pending_event, None, journal_seq=i + 1)
                writer._resolve_pending_events(root_id)
                if i % 5 == 0:
                    writer._write_ownership_checkpoint(root_id, i)
                if i % 7 == 0:
                    writer._clear_ownership_state(root_id)
        except BaseException as exc:  # noqa: BLE001 - surface to main thread
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=churn, args=(index,)) for index in range(root_count)
    ]
    # `dictionary changed size during iteration` needs a context switch to
    # land mid-iteration -- a dict comprehension over a handful of keys
    # finishes far faster than the default ~5ms GIL switch interval, so
    # this race barely ever fires without forcing much more frequent
    # switches. Confirmed this reliably reproduces the RuntimeError within
    # a couple of iterations against the pre-fix (unlocked) code.
    original_switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(0.00005)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "ownership churn thread hung"
    finally:
        sys.setswitchinterval(original_switch_interval)
        writer.close()
    assert not errors, errors


if __name__ == "__main__":
    test_read_lane_does_not_queue_behind_write_lane_for_same_root()
    test_write_lane_does_not_queue_behind_read_lane_for_same_root()
    test_barrier_records_enqueue_to_start_wait()
    test_ensure_canonical_authority_sync_uses_read_lane()
    test_concurrent_roots_do_not_race_shared_ownership_dicts()
    print("event journal keyed executor tests passed")
