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


def fact(*, root="root", stream="provider:one", event="e1", order=1, text="a"):
    return CanonicalFact.create(
        root_id=root,
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
    assert [row.canonical_seq for row in store.read("root")] == [1, 2]
    store.close()


def test_same_source_order_different_content_fails_closed():
    store = CanonicalEventStore(Path(HOME) / "conflict.sqlite")
    store.submit(fact(text="one"))
    try:
        store.submit(fact(text="two"))
        raise AssertionError("expected source conflict")
    except SourceConflictError:
        pass
    assert len(store.read("root")) == 1
    store.close()


def test_barrier_linearizes_prior_acceptance():
    store = CanonicalEventStore(Path(HOME) / "barrier.sqlite")
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(store.submit(fact(event=f"e{i}", order=i)))) for i in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    barrier = store.barrier("root")
    assert barrier.committed_ticket >= max(result.acceptance_ticket for result in results)
    assert barrier.canonical_through_seq == 5
    store.close()


if __name__ == "__main__":
    test_commit_ack_and_idempotency()
    test_same_source_order_different_content_fails_closed()
    test_barrier_linearizes_prior_acceptance()
    print("canonical event store tests passed")
