from __future__ import annotations

import multiprocessing
import os
import sqlite3
import sys
from pathlib import Path


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home


_IMPORT_HOME = _test_home.TestHome.acquire("ba-test-requirement-store-import-")

from stores.requirement_store import (  # noqa: E402
    MAX_TEXT_CHARS,
    PurgeIncomplete,
    RequirementIdempotencyConflict,
    RequirementNotFound,
    RequirementStateError,
    RequirementStore,
    RevisionConflict,
    SENSITIVITIES,
)
from stores.sqlite_truth_base import SchemaVersionError  # noqa: E402


ALL_SENSITIVITIES = frozenset(SENSITIVITIES)


def _register(store: RequirementStore, **overrides):
    fields = {
        "requirement_id": "req-1",
        "text": "keep responses short and executive-summary style",
        "kind": "communication",
        "authority": "user_stated",
        "sensitivity": "normal",
        "source_session_id": "session-1",
        "source_message_id": "message-1",
        "span_start": 10,
        "span_end": 58,
    }
    fields.update(overrides)
    return store.register(**fields)


def _dump_requirements(store: RequirementStore) -> list[tuple]:
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(
            "SELECT * FROM requirements ORDER BY requirement_id"
        ).fetchall()
    finally:
        conn.close()


def _db_bytes(store: RequirementStore) -> bytes:
    blob = b""
    for path in (
        store.path,
        Path(str(store.path) + "-wal"),
        Path(str(store.path) + "-shm"),
        store._index_path,
        Path(str(store._index_path) + "-wal"),
        Path(str(store._index_path) + "-shm"),
    ):
        if path.exists():
            blob += path.read_bytes()
    return blob


def test_register_and_exact_citation_retrieval() -> None:
    store = RequirementStore()
    result = _register(store)
    assert result.appended and result.revision == 1

    hits = store.retrieve("executive-summary", authorized_sensitivities=ALL_SENSITIVITIES)
    assert len(hits) == 1
    citation = hits[0]
    assert citation["requirement_id"] == "req-1"
    assert citation["revision"] == 1
    assert citation["status"] == "active"
    assert citation["authority"] == "user_stated"
    assert citation["source"] == {
        "session_id": "session-1",
        "message_id": "message-1",
        "span_start": 10,
        "span_end": 58,
        "sha256": __import__("hashlib").sha256(citation["text"].encode("utf-8")).hexdigest(),
    }

    duplicate = _register(store)
    assert not duplicate.appended and duplicate.commit_seq == result.commit_seq
    try:
        _register(store, text="a different requirement text entirely")
    except RequirementIdempotencyConflict:
        pass
    else:
        raise AssertionError("conflicting re-registration was accepted")


def test_authorization_filters_and_fails_closed() -> None:
    store = RequirementStore()
    _register(store)
    _register(
        store,
        requirement_id="req-secret",
        text="the deploy token lives in the secret vault path",
        sensitivity="secret",
        source_message_id="message-2",
        span_start=0,
        span_end=48,
    )

    for query, allowed, expected_ids in (
        ("secret vault", frozenset({"normal"}), []),
        ("secret vault", frozenset({"secret"}), ["req-secret"]),
        ("responses", frozenset({"normal", "secret"}), ["req-1"]),
    ):
        hits = store.retrieve(query, authorized_sensitivities=allowed)
        assert [h["requirement_id"] for h in hits] == expected_ids

    assert store.get("req-secret", authorized_sensitivities=frozenset({"normal"})) is None
    assert store.get("req-secret", authorized_sensitivities=frozenset({"secret"})) is not None

    for invalid in ((), ("classified",), "normal", ("normal", "bogus")):
        try:
            store.retrieve("anything", authorized_sensitivities=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid authorization {invalid!r} was accepted")

    try:
        store.retrieve('vault" OR requirement_id:*', authorized_sensitivities=frozenset({"normal"}))
    except ValueError:
        raise AssertionError("quoted token query must not raise")


def test_authorized_match_never_starved_by_unauthorized_rank() -> None:
    store = RequirementStore()
    for index in range(6):
        _register(
            store,
            requirement_id=f"req-secret-{index}",
            text=f"deploy deploy deploy secret runbook number {index}",
            sensitivity="secret",
            source_message_id=f"message-s{index}",
            span_start=0,
            span_end=40,
        )
    _register(
        store,
        requirement_id="req-normal",
        text="the deploy checklist must run the smoke suite",
        source_message_id="message-n",
        span_start=0,
        span_end=46,
    )
    hits = store.retrieve("deploy", authorized_sensitivities=frozenset({"normal"}), limit=1)
    assert [h["requirement_id"] for h in hits] == ["req-normal"]


def test_busy_checkpoint_defers_purge_and_recovers_on_reopen() -> None:
    store = RequirementStore()
    secret_text = "DEFERRED-PURGE-9d4e residue held open by a reader"
    _register(store, requirement_id="req-deferred", text=secret_text, span_end=49)
    store.retrieve("DEFERRED", authorized_sensitivities=ALL_SENSITIVITIES)

    reader = sqlite3.connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM requirements").fetchall()
    try:
        try:
            store.delete("req-deferred", expected_revision=1)
        except PurgeIncomplete:
            pass
        else:
            raise AssertionError("blocked WAL truncation reported a completed purge")
    finally:
        reader.close()

    conn = sqlite3.connect(store.path)
    markers = conn.execute("SELECT COUNT(*) FROM purge_markers").fetchone()[0]
    conn.close()
    assert markers == 1
    # The logical delete is committed even though the purge is pending.
    tombstone = store.get("req-deferred", authorized_sensitivities=frozenset({"normal"}))
    assert tombstone["status"] == "deleted"

    reopened = RequirementStore(store.path)
    assert b"DEFERRED-PURGE-9d4e" not in _db_bytes(reopened)
    conn = sqlite3.connect(reopened.path)
    assert conn.execute("SELECT COUNT(*) FROM purge_markers").fetchone()[0] == 0
    conn.close()


def test_supersession_chain_and_revision_cas() -> None:
    store = RequirementStore()
    _register(store)
    replacement = {
        "requirement_id": "req-2",
        "text": "keep responses short, elaborate only on demand",
        "kind": "communication",
        "authority": "user_stated",
        "sensitivity": "normal",
        "source_session_id": "session-2",
        "source_message_id": "message-9",
        "span_start": 4,
        "span_end": 51,
    }
    try:
        store.supersede("req-1", expected_revision=7, replacement=replacement)
    except RevisionConflict:
        pass
    else:
        raise AssertionError("stale revision superseded")
    result = store.supersede("req-1", expected_revision=1, replacement=replacement)
    assert result.old_revision == 2 and result.new_requirement_id == "req-2"

    active = store.retrieve("responses", authorized_sensitivities=ALL_SENSITIVITIES)
    assert [h["requirement_id"] for h in active] == ["req-2"]
    everything = store.retrieve(
        "responses", authorized_sensitivities=ALL_SENSITIVITIES, include_superseded=True
    )
    assert sorted(h["requirement_id"] for h in everything) == ["req-1", "req-2"]
    old = store.get("req-1", authorized_sensitivities=ALL_SENSITIVITIES)
    assert old["status"] == "superseded" and old["superseded_by"] == "req-2"

    try:
        store.supersede("req-1", expected_revision=2, replacement=dict(replacement, requirement_id="req-3"))
    except RequirementStateError:
        pass
    else:
        raise AssertionError("superseded requirement superseded again")
    try:
        store.supersede("missing", expected_revision=1, replacement=dict(replacement, requirement_id="req-3"))
    except RequirementNotFound:
        pass
    else:
        raise AssertionError("supersede of a missing requirement was accepted")


def test_deletion_purges_every_byte_and_leaves_tombstone() -> None:
    store = RequirementStore()
    secret_text = "PURGE-ME-7f3a9c the launch codes requirement"
    _register(store, requirement_id="req-purge", text=secret_text, span_end=44)
    assert store.retrieve("PURGE", authorized_sensitivities=ALL_SENSITIVITIES)
    assert b"PURGE-ME-7f3a9c" in _db_bytes(store)

    try:
        store.delete("req-purge", expected_revision=3)
    except RevisionConflict:
        pass
    else:
        raise AssertionError("stale revision deleted")
    store.delete("req-purge", expected_revision=1)

    assert b"PURGE-ME-7f3a9c" not in _db_bytes(store)
    assert store.retrieve("PURGE", authorized_sensitivities=ALL_SENSITIVITIES) == []
    tombstone = store.get("req-purge", authorized_sensitivities=frozenset({"normal"}))
    assert tombstone["status"] == "deleted" and tombstone["revision"] == 2
    assert set(tombstone) == {"requirement_id", "status", "revision", "deleted_at"}

    try:
        store.delete("req-purge", expected_revision=2)
    except RequirementStateError:
        pass
    else:
        raise AssertionError("double delete was accepted")
    try:
        _register(store, requirement_id="req-purge", text=secret_text, span_end=44)
    except RequirementIdempotencyConflict:
        pass
    else:
        raise AssertionError("tombstoned id was re-registered")


def test_deterministic_rebuild_of_projection_and_index() -> None:
    store = RequirementStore()
    _register(store)
    _register(
        store,
        requirement_id="req-secret",
        text="the deploy token lives in the secret vault path",
        sensitivity="secret",
        source_message_id="message-2",
        span_start=0,
        span_end=48,
    )
    store.supersede(
        "req-1",
        expected_revision=1,
        replacement={
            "requirement_id": "req-2",
            "text": "keep responses short, elaborate only on demand",
            "kind": "communication",
            "authority": "user_stated",
            "sensitivity": "normal",
            "source_session_id": "session-2",
            "source_message_id": "message-9",
            "span_start": 4,
            "span_end": 51,
        },
    )
    store.delete("req-secret", expected_revision=1)

    before_rows = _dump_requirements(store)
    before_hits = store.retrieve(
        "responses", authorized_sensitivities=ALL_SENSITIVITIES, include_superseded=True
    )
    assert store.rebuild_projection() == 3
    assert _dump_requirements(store) == before_rows

    store._destroy_index()
    conn = sqlite3.connect(store._index_path)
    conn.execute("CREATE TABLE junk (x)")
    conn.commit()
    conn.close()
    after_hits = store.retrieve(
        "responses", authorized_sensitivities=ALL_SENSITIVITIES, include_superseded=True
    )
    assert after_hits == before_hits


def test_truth_schema_fails_closed() -> None:
    path = Path(os.environ["BETTER_AGENT_HOME"]) / "tampered-requirements.sqlite3"
    RequirementStore(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE rogue (x)")
    conn.commit()
    conn.close()
    try:
        RequirementStore(path)
    except SchemaVersionError:
        pass
    else:
        raise AssertionError("rogue object in requirement truth db was accepted")


def test_boundary_validation() -> None:
    store = RequirementStore()
    for overrides in (
        {"text": ""},
        {"text": "x" * (MAX_TEXT_CHARS + 1)},
        {"authority": "model_says_so"},
        {"sensitivity": "classified"},
        {"span_start": -1},
        {"span_start": True},
        {"span_end": 10, "span_start": 10},
        {"requirement_id": ""},
        {"source_session_id": "s" * 513},
    ):
        try:
            _register(store, **overrides)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid registration {overrides!r} was accepted")
    for bad_limit in (0, 201, True):
        try:
            store.retrieve("x", authorized_sensitivities=ALL_SENSITIVITIES, limit=bad_limit)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid limit was accepted")
    for bad_query in ("", "   ", "q" * 2000, 7):
        try:
            store.retrieve(bad_query, authorized_sensitivities=ALL_SENSITIVITIES)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid query was accepted")


def _delete_then_crash_before_finalize(path: str) -> None:
    _IMPORT_HOME.release()
    store = RequirementStore(Path(path))
    store._finalize_purges = lambda: os._exit(0)  # crash exactly at the purge-finalize boundary
    store.delete("req-crash", expected_revision=1)
    os._exit(1)


def test_crash_between_delete_and_purge_finalize_recovers() -> None:
    store = RequirementStore()
    secret_text = "CRASH-PURGE-51c2 residue that must not survive restart"
    _register(store, requirement_id="req-crash", text=secret_text, span_end=55)
    store.retrieve("CRASH", authorized_sensitivities=ALL_SENSITIVITIES)

    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_delete_then_crash_before_finalize, args=(str(store.path),))
    child.start()
    child.join(10)
    assert child.exitcode == 0

    reopened = RequirementStore(store.path)
    assert b"CRASH-PURGE-51c2" not in _db_bytes(reopened)
    assert reopened.retrieve("CRASH", authorized_sensitivities=ALL_SENSITIVITIES) == []
    tombstone = reopened.get("req-crash", authorized_sensitivities=frozenset({"normal"}))
    assert tombstone["status"] == "deleted"
    conn = sqlite3.connect(reopened.path)
    assert conn.execute("SELECT COUNT(*) FROM purge_markers").fetchone()[0] == 0
    conn.close()


def main() -> None:
    tests = [
        test_register_and_exact_citation_retrieval,
        test_authorization_filters_and_fails_closed,
        test_authorized_match_never_starved_by_unauthorized_rank,
        test_busy_checkpoint_defers_purge_and_recovers_on_reopen,
        test_supersession_chain_and_revision_cas,
        test_deletion_purges_every_byte_and_leaves_tombstone,
        test_deterministic_rebuild_of_projection_and_index,
        test_truth_schema_fails_closed,
        test_boundary_validation,
        test_crash_between_delete_and_purge_finalize_recovers,
    ]
    _IMPORT_HOME.release()
    for test in tests:
        home = _test_home.TestHome.acquire("ba-test-requirement-store-")
        try:
            test()
            print(f"PASS {test.__name__}")
        finally:
            home.release()


if __name__ == "__main__":
    main()
