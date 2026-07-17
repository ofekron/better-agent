import os
import sys
import tempfile
import threading
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="ba-canonical-store-")
os.environ["BETTER_AGENT_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event import CanonicalFact, SourceOrder
from canonical_event_store import CanonicalEventStore, SourceConflictError


def fact(*, root="root", generation=0, stream="provider:one", event="e1", order=1, text="a"):
    return CanonicalFact.create(
        root_id=root,
        root_generation=generation,
        sid=root,
        source="claude",
        source_stream_id=stream,
        source_event_id=event,
        source_order=SourceOrder(sequence=order),
        payload_type="assistant_output",
        payload={"text": text},
        update_semantics="snapshot",
    )


def test_commit_ack_and_idempotency():
    store = CanonicalEventStore(Path(HOME) / "events-v1.sqlite", queue_capacity=8)
    first = store.submit(fact())
    duplicate = store.submit(fact())
    changed = store.submit(fact(order=2, text="ab"))
    assert first.committed and first.canonical_seq == 1
    assert duplicate.duplicate and duplicate.canonical_seq == 1
    assert changed.canonical_seq == 2
    assert [row.canonical_seq for row in store.read("root", 0)] == [1, 2]
    store.close()


def test_same_source_order_different_content_fails_closed():
    store = CanonicalEventStore(Path(HOME) / "conflict.sqlite")
    store.submit(fact(text="one"))
    try:
        store.submit(fact(text="two"))
        raise AssertionError("expected source conflict")
    except SourceConflictError:
        pass
    assert len(store.read("root", 0)) == 1
    store.close()


def test_barrier_linearizes_prior_acceptance():
    store = CanonicalEventStore(Path(HOME) / "barrier.sqlite")
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(store.submit(fact(event=f"e{i}", order=i)))) for i in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    barrier = store.barrier("root", 0)
    assert barrier.committed_ticket >= max(result.acceptance_ticket for result in results)
    assert barrier.canonical_through_seq == 5
    store.close()


def test_root_generation_scopes_identity_and_sequence():
    store = CanonicalEventStore(Path(HOME) / "generation.sqlite")
    first = store.submit(fact(generation=1))
    reused = store.submit(fact(generation=2))
    assert first.canonical_seq == 1
    assert reused.canonical_seq == 1
    assert [(row.fact.root_generation, row.canonical_seq) for row in store.read("root", 1)] == [(1, 1)]
    assert [(row.fact.root_generation, row.canonical_seq) for row in store.read("root", 2)] == [(2, 1)]
    store.close()


def test_bulk_commit_is_atomic_and_generation_scoped():
    store = CanonicalEventStore(Path(HOME) / "bulk.sqlite")
    rows = [fact(event=f"e{index}", order=index) for index in range(1, 501)]
    acks = store.submit_many(rows, max_batch_size=128)
    assert len(acks) == 500
    assert [row.canonical_seq for row in store.read("root", 0)] == list(range(1, 501))
    assert store.barrier("root", 0).canonical_through_seq == 500
    store.close()


def test_upsert_rewrite_flags_ack_and_logs():
    import logging

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    handler = _Capture()
    store_logger = logging.getLogger("canonical_event_store")
    store_logger.addHandler(handler)
    store = CanonicalEventStore(Path(HOME) / "upsert.sqlite")
    try:
        first = store.submit(fact(text="one"))
        assert first.rewritten is False
        same = store.submit_many([fact(text="one")], upsert=True)[0]
        assert same.duplicate and same.rewritten is False
        rewritten = store.submit_many([fact(text="two")], upsert=True)[0]
        assert rewritten.committed and rewritten.rewritten is True
        assert rewritten.canonical_seq == first.canonical_seq
        rows = store.read("root", 0)
        assert len(rows) == 1 and rows[0].fact.payload == {"text": "two"}
        rewrite_logs = [
            record for record in handler.records
            if "rewrote fact in place" in record.getMessage()
        ]
        assert len(rewrite_logs) == 1
        message = rewrite_logs[0].getMessage()
        assert "root_id=root" in message and "canonical_seq=1" in message
        assert "payload_type=assistant_output" in message
    finally:
        store_logger.removeHandler(handler)
        store.close()


if __name__ == "__main__":
    test_commit_ack_and_idempotency()
    test_same_source_order_different_content_fails_closed()
    test_barrier_linearizes_prior_acceptance()
    test_root_generation_scopes_identity_and_sequence()
    test_bulk_commit_is_atomic_and_generation_scoped()
    test_upsert_rewrite_flags_ack_and_logs()
    print("canonical event store tests passed")
