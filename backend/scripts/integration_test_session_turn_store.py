from __future__ import annotations

import os
import multiprocessing
import sqlite3
import sys
import threading
import time
from pathlib import Path


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home


_IMPORT_HOME = _test_home.TestHome.acquire("ba-test-session-turn-store-import-")

from stores.session_turn_store import (  # noqa: E402
    AggregateVersionConflict,
    IdempotencyConflict,
    MAX_DOCUMENT_BYTES,
    MAX_ERROR_TEXT_CHARS,
    OutboxLeaseConflict,
    SCHEMA_VERSION,
    SchemaVersionError,
    SessionTurnStore,
)


def _apply(store: SessionTurnStore, **overrides):
    values = {
        "root_id": "root-1",
        "sid": "session-1",
        "turn_id": "turn-1",
        "expected_version": 0,
        "event_type": "turn.accepted",
        "payload": {"message_id": "user-1"},
        "new_state": {"status": "accepted", "messages": ["user-1"]},
        "idempotency_key": "accept-user-1",
        "outbox_topic": "session.turn.changed",
        "event_id": "event-1",
    }
    values.update(overrides)
    return store.apply_command(**values)


def test_atomic_owner_event_and_outbox() -> None:
    store = SessionTurnStore()
    result = _apply(store)
    assert result.appended is True
    assert result.aggregate_version == 1
    assert store.get_turn("root-1", "session-1", "turn-1") == {
        "root_id": "root-1",
        "sid": "session-1",
        "turn_id": "turn-1",
        "aggregate_version": 1,
        "state": {"messages": ["user-1"], "status": "accepted"},
    }
    outbox = store.pending_outbox(limit=10)
    assert len(outbox) == 1
    assert outbox[0]["event_id"] == "event-1"
    assert outbox[0]["payload"]["aggregate_version"] == 1


def test_idempotency_and_compare_and_swap() -> None:
    store = SessionTurnStore()
    first = _apply(store)
    duplicate = _apply(store)
    assert duplicate.appended is False
    assert duplicate.commit_seq == first.commit_seq
    assert duplicate.outbox_id == first.outbox_id

    try:
        _apply(store, payload={"message_id": "changed"})
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("changed command reused an idempotency key")

    try:
        _apply(
            store,
            idempotency_key="second-command",
            event_id="event-2",
            payload={"message_id": "user-2"},
        )
    except AggregateVersionConflict:
        pass
    else:
        raise AssertionError("stale aggregate version committed")


def test_constraint_failure_rolls_back_owner_state() -> None:
    store = SessionTurnStore()
    _apply(store)
    try:
        _apply(
            store,
            expected_version=1,
            idempotency_key="complete-turn",
            event_id="event-1",
            event_type="turn.completed",
            payload={"message_id": "assistant-1"},
            new_state={"status": "completed", "messages": ["user-1", "assistant-1"]},
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate event id unexpectedly committed")
    turn = store.get_turn("root-1", "session-1", "turn-1")
    assert turn is not None
    assert turn["aggregate_version"] == 1
    assert turn["state"]["status"] == "accepted"
    assert len(store.pending_outbox(limit=10)) == 1


def test_restart_and_schema_mismatch_fail_closed() -> None:
    store = SessionTurnStore()
    _apply(store)
    reopened = SessionTurnStore()
    turn = reopened.get_turn("root-1", "session-1", "turn-1")
    assert turn is not None
    assert turn["aggregate_version"] == 1
    assert reopened.pending_outbox(limit=10)[0]["event_id"] == "event-1"

    conn = sqlite3.connect(store.path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    try:
        SessionTurnStore()
    except SchemaVersionError:
        pass
    else:
        raise AssertionError("unsupported schema version was accepted")


def test_matching_version_with_corrupt_shape_fails_closed() -> None:
    path = Path(os.environ["BETTER_AGENT_HOME"]) / "corrupt.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE turn_aggregates (
            root_id INTEGER, sid TEXT, turn_id TEXT, aggregate_version INTEGER,
            state_json TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE domain_events (
            commit_seq INTEGER, event_id TEXT, root_id TEXT, sid TEXT, turn_id TEXT,
            aggregate_version INTEGER, event_type TEXT, schema_version INTEGER,
            payload_json TEXT, causation_id TEXT, correlation_id TEXT,
            idempotency_key TEXT, command_hash TEXT, created_at REAL
        );
        CREATE TABLE outbox (
            outbox_id INTEGER, event_id TEXT, topic TEXT, payload_json TEXT,
            created_at REAL, dispatched_at REAL, claimed_by TEXT, claim_epoch INTEGER,
            lease_expires_at REAL, attempts INTEGER, last_error TEXT
        );
        CREATE INDEX domain_events_aggregate
            ON domain_events(root_id, sid, turn_id, aggregate_version);
        CREATE INDEX outbox_pending ON outbox(outbox_id) WHERE dispatched_at IS NULL;
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    try:
        SessionTurnStore(path)
    except SchemaVersionError:
        pass
    else:
        raise AssertionError("matching version with corrupt schema was accepted")

    tampered_path = Path(os.environ["BETTER_AGENT_HOME"]) / "tampered.sqlite3"
    SessionTurnStore(tampered_path)
    conn = sqlite3.connect(tampered_path)
    conn.execute(
        "CREATE TRIGGER rogue_trigger AFTER INSERT ON turn_aggregates "
        "BEGIN UPDATE turn_aggregates SET state_json='{}'; END"
    )
    conn.commit()
    conn.close()
    try:
        SessionTurnStore(tampered_path)
    except SchemaVersionError:
        pass
    else:
        raise AssertionError("canonical schema plus a rogue object was accepted")


def test_concurrent_initialization_and_compare_and_swap() -> None:
    path = Path(os.environ["BETTER_AGENT_HOME"]) / "turns.sqlite3"
    barrier = threading.Barrier(2)
    init_errors: list[Exception] = []

    def initialize() -> None:
        try:
            barrier.wait()
            SessionTurnStore(path)
        except Exception as exc:
            init_errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert init_errors == []

    store = SessionTurnStore(path)
    write_barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def write(command: str) -> None:
        try:
            write_barrier.wait()
            _apply(store, idempotency_key=command, event_id=command)
            outcomes.append("committed")
        except AggregateVersionConflict:
            outcomes.append("conflict")

    writers = [threading.Thread(target=write, args=(f"event-{index}",)) for index in range(2)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert sorted(outcomes) == ["committed", "conflict"]


def test_sid_identity_and_boundary_validation() -> None:
    store = SessionTurnStore()
    _apply(store)
    other = _apply(store, sid="session-2", idempotency_key="accept-other", event_id="event-other")
    assert other.aggregate_version == 1
    assert store.get_turn("root-1", "session-1", "turn-1") is not None
    assert store.get_turn("root-1", "session-2", "turn-1") is not None
    for call in (
        lambda: store.get_turn("", "session-1", "turn-1"),
        lambda: store.pending_outbox(limit=True),
        lambda: _apply(store, expected_version=True),
        lambda: _apply(store, event_schema_version=True),
        lambda: _apply(store, payload={"large": "x" * (MAX_DOCUMENT_BYTES + 1)}),
    ):
        try:
            call()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid boundary input was accepted")


def test_outbox_claim_fencing_ack_failure_and_expiry() -> None:
    store = SessionTurnStore()
    _apply(store)
    first = store.claim_outbox(consumer_id="consumer-a", limit=1, lease_seconds=0.01)
    assert len(first) == 1
    assert store.claim_outbox(consumer_id="consumer-b", limit=1, lease_seconds=1) == []
    time.sleep(0.02)
    for operation in (
        lambda: store.acknowledge_outbox(
            outbox_id=first[0]["outbox_id"],
            consumer_id="consumer-a",
            claim_epoch=first[0]["claim_epoch"],
        ),
        lambda: store.fail_outbox(
            outbox_id=first[0]["outbox_id"],
            consumer_id="consumer-a",
            claim_epoch=first[0]["claim_epoch"],
            error="expired",
        ),
    ):
        try:
            operation()
        except OutboxLeaseConflict:
            pass
        else:
            raise AssertionError("expired outbox lease retained authority")
    reclaimed = store.claim_outbox(consumer_id="consumer-b", limit=1, lease_seconds=1)
    assert reclaimed[0]["claim_epoch"] == first[0]["claim_epoch"] + 1
    try:
        store.acknowledge_outbox(
            outbox_id=first[0]["outbox_id"],
            consumer_id="consumer-a",
            claim_epoch=first[0]["claim_epoch"],
        )
    except OutboxLeaseConflict:
        pass
    else:
        raise AssertionError("stale outbox lease acknowledged")
    store.fail_outbox(
        outbox_id=reclaimed[0]["outbox_id"],
        consumer_id="consumer-b",
        claim_epoch=reclaimed[0]["claim_epoch"],
        error="delivery failed: " + "x" * (MAX_ERROR_TEXT_CHARS * 2),
    )
    conn = sqlite3.connect(store.path)
    stored_error = conn.execute(
        "SELECT last_error FROM outbox WHERE outbox_id=?",
        (reclaimed[0]["outbox_id"],),
    ).fetchone()[0]
    conn.close()
    assert len(stored_error) == MAX_ERROR_TEXT_CHARS
    assert stored_error.startswith("delivery failed: ")
    retry = store.claim_outbox(consumer_id="consumer-c", limit=1, lease_seconds=1)
    assert retry[0]["attempts"] == 3
    store.acknowledge_outbox(
        outbox_id=retry[0]["outbox_id"],
        consumer_id="consumer-c",
        claim_epoch=retry[0]["claim_epoch"],
    )
    assert store.pending_outbox(limit=10) == []

    for invalid_lease in (float("nan"), float("inf"), float("-inf")):
        try:
            store.claim_outbox(
                consumer_id="consumer-invalid",
                limit=1,
                lease_seconds=invalid_lease,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite lease was accepted")

    try:
        _apply(
            SessionTurnStore(),
            root_id="nan-root",
            turn_id="nan-turn",
            event_id="nan-event",
            idempotency_key="nan-command",
            payload={"value": float("nan")},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite JSON number was accepted")


def _crash_before_commit(path: str) -> None:
    _IMPORT_HOME.release()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO turn_aggregates VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("crash-root", "crash-sid", "crash-turn", 1, "{}", time.time(), time.time()),
    )
    os._exit(0)


def _commit_then_crash(path: str) -> None:
    _IMPORT_HOME.release()
    store = SessionTurnStore(Path(path))
    store.apply_command(
        root_id="crash-root",
        sid="crash-sid",
        turn_id="crash-turn",
        expected_version=0,
        event_type="turn.accepted",
        payload={},
        new_state={"status": "accepted"},
        idempotency_key="crash-command",
        outbox_topic="session.turn.changed",
        event_id="crash-event",
    )
    os._exit(0)


def test_process_crash_boundaries_and_wal_recovery() -> None:
    path = SessionTurnStore().path
    context = multiprocessing.get_context("spawn")
    before = context.Process(target=_crash_before_commit, args=(str(path),))
    before.start()
    before.join(10)
    assert before.exitcode == 0
    reopened = SessionTurnStore(path)
    assert reopened.get_turn("crash-root", "crash-sid", "crash-turn") is None

    after = context.Process(target=_commit_then_crash, args=(str(path),))
    after.start()
    after.join(10)
    assert after.exitcode == 0
    recovered = SessionTurnStore(path)
    assert recovered.get_turn("crash-root", "crash-sid", "crash-turn")["state"] == {
        "status": "accepted"
    }
    assert recovered.pending_outbox(limit=10)[0]["event_id"] == "crash-event"


def main() -> None:
    tests = [
        test_atomic_owner_event_and_outbox,
        test_idempotency_and_compare_and_swap,
        test_constraint_failure_rolls_back_owner_state,
        test_restart_and_schema_mismatch_fail_closed,
        test_matching_version_with_corrupt_shape_fails_closed,
        test_concurrent_initialization_and_compare_and_swap,
        test_sid_identity_and_boundary_validation,
        test_outbox_claim_fencing_ack_failure_and_expiry,
        test_process_crash_boundaries_and_wal_recovery,
    ]
    _IMPORT_HOME.release()
    for test in tests:
        home = _test_home.TestHome.acquire("ba-test-session-turn-store-")
        try:
            test()
            print(f"PASS {test.__name__}")
        finally:
            home.release()


if __name__ == "__main__":
    main()
