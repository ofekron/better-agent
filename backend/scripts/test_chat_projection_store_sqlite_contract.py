#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
from unittest import mock
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_HOME = Path(tempfile.mkdtemp(prefix="better-agent-chat-store-"))
os.environ["BETTER_AGENT_HOME"] = str(STATE_HOME)
os.environ["BETTER_AGENT_TEST_MODE"] = "1"
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_store import ChatProjectionStoreError, ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_sqlite import (
    MAX_COMMIT_BYTES, MAX_IPC_BYTES, MAX_IPC_TIMEOUT_SECONDS, MAX_JSON_DEPTH, MAX_JSON_LIST_ITEMS,
    MAX_JSON_NODES, MAX_JSON_OBJECT_ITEMS, MAX_READ_LIMIT, MAX_RESPONSE_BYTES,
    MAX_SQLITE_INTEGER, MAX_TEXT_BYTES, MIN_IPC_TIMEOUT_SECONDS,
    SQLiteChatProjectionStore, _encode_json_bounded, canonical_json,
)
from chat_projection_store_owner import encode_frame, receive_frame, send_frame, serve_owner
import chat_projection_store_owner as owner_transport


FIXTURE = ROOT / "test-contracts" / "chat-panel" / "v1" / "canonical-session.json"


def _fixture_event(index: int = 1) -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["events"][index]


def _request(event: dict, *, generation: int = 0, watermark: int | None = None) -> ProjectionCommit:
    fact = json.loads(json.dumps(event))
    digest = __import__("hashlib").sha256(canonical_json(fact).encode("utf-8")).hexdigest()
    return ProjectionCommit(
        root_id="root-1", root_generation=generation, event_id=event["event_id"],
        content_hash=digest, canonical_fact=fact,
        render_node={"type": "Explanation", "text": event["data"].get("text", "")},
        turn_id=event["turn_id"], message_id=event["message_id"],
        parent_event_id=event["parent_event_id"], owner_scope="turn:turn-1",
        manifest=TurnManifest(event["turn_id"], event["journal_seq"], 1),
        visible_delta={"replace": event["event_id"]},
        historical_revision={"event_id": event["event_id"], "content_version": event["content_version"]},
        watermark=SourceWatermark("provider-neutral", 0, watermark or event["journal_seq"]),
    )


def _with_event_id(request: ProjectionCommit, event_id: str) -> ProjectionCommit:
    fact = dict(request.canonical_fact)
    fact["event_id"] = event_id
    digest = __import__("hashlib").sha256(canonical_json(fact).encode("utf-8")).hexdigest()
    return replace(request, event_id=event_id, canonical_fact=fact, content_hash=digest)


def _path(name: str) -> Path:
    return STATE_HOME / "chat-tests" / f"{name}.sqlite3"


def _assert_error(code: str, callback) -> None:
    try:
        callback()
    except ChatProjectionStoreError as exc:
        assert exc.code == code
        return
    raise AssertionError(f"expected ChatProjectionStoreError({code})")


def _fd_is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return True
    return False


def test_atomic_commit_duplicate_mutation_and_projection_surfaces() -> None:
    path = _path("projection")
    store = SQLiteChatProjectionStore(path)
    store.select_generation("root-1", 0)
    request = _request(_fixture_event())
    first = store.commit(request)
    assert (first.duplicate, first.fact_sequence, first.revision, first.projection_cursor) == (False, 1, 1, 1)
    duplicate = store.commit(replace(request, watermark=SourceWatermark("provider-neutral", 0, 9)))
    assert (duplicate.duplicate, duplicate.revision, duplicate.projection_cursor) == (True, 1, 1)
    assert store.source_watermark("root-1", 0, "provider-neutral").sequence == 9
    mutated_fact = json.loads(json.dumps(request.canonical_fact))
    mutated_fact["content_version"] = 2
    mutated_fact["data"]["text"] = "Mutated answer"
    digest = __import__("hashlib").sha256(canonical_json(mutated_fact).encode()).hexdigest()
    mutated = replace(
        request, canonical_fact=mutated_fact, content_hash=digest,
        render_node={"type": "Explanation", "text": "Mutated answer"},
        visible_delta={"replace": request.event_id, "text": "Mutated answer"},
        historical_revision={"event_id": request.event_id, "content_version": 2},
        watermark=SourceWatermark("provider-neutral", 0, 10),
    )
    second = store.commit(mutated)
    assert (second.fact_sequence, second.revision, second.projection_cursor) == (2, 2, 2)
    assert len(store.read_facts("root-1", 0)) == 2
    revisions = store.read_revisions("root-1", 0)
    assert [revision.revision for revision in revisions] == [1, 2]
    assert revisions[0].visible_delta == {"replace": request.event_id}
    projection = store.read_projection("root-1", 0, request.event_id)
    assert projection.render_node["text"] == "Mutated answer"
    assert projection.manifest.direct_child_count == 1
    old_duplicate = store.commit(replace(
        request, watermark=SourceWatermark("provider-neutral", 0, 11),
    ))
    assert (old_duplicate.duplicate, old_duplicate.fact_sequence) == (True, 1)
    assert (old_duplicate.revision, old_duplicate.projection_cursor) == (2, 2)
    store.close()
    reopened = SQLiteChatProjectionStore(path)
    assert reopened.projection_cursor("root-1", 0) == 2
    assert len(reopened.read_revisions("root-1", 0)) == 2
    reopened.close()


def test_wal_full_and_crash_boundaries() -> None:
    path = _path("crash-before")
    def fail_before() -> None:
        raise RuntimeError("simulated crash before commit")
    store = SQLiteChatProjectionStore(path, before_commit=fail_before)
    store.select_generation("root-1", 0)
    try:
        store.commit(_request(_fixture_event()))
        raise AssertionError("before-commit crash did not fire")
    except RuntimeError:
        pass
    assert store.read_facts("root-1", 0) == []
    store.close()
    reopened = SQLiteChatProjectionStore(path)
    assert reopened.read_revisions("root-1", 0) == []
    reopened.close()

    durable_path = _path("crash-after")
    def fail_after() -> None:
        raise RuntimeError("simulated crash after commit")
    durable = SQLiteChatProjectionStore(durable_path, after_commit=fail_after)
    durable.select_generation("root-1", 0)
    try:
        durable.commit(_request(_fixture_event()))
        raise AssertionError("after-commit crash did not fire")
    except RuntimeError:
        pass
    durable.close()
    reopened = SQLiteChatProjectionStore(durable_path)
    assert len(reopened.read_facts("root-1", 0)) == 1
    reopened.close()
    connection = sqlite3.connect(durable_path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    connection.close()


def test_generation_fencing_schema_paths_and_bounded_reads() -> None:
    path = _path("fencing")
    store = SQLiteChatProjectionStore(path)
    store.select_generation("root-1", 0)
    old = _request(_fixture_event(), generation=0)
    store.commit(old)
    assert store.projection_cursor("root-1", 0) == 1
    store.select_generation("root-1", 1)
    _assert_error("stale_generation", lambda: store.commit(old))
    current = _request(_fixture_event(), generation=1)
    assert store.commit(current).fact_sequence == 1
    assert len(store.read_facts("root-1", 0)) == 1
    assert store.projection_cursor("root-1", 0) == 1
    _assert_error("invalid_cursor", lambda: store.read_facts("root-1", 1, after=-1))
    _assert_error("invalid_limit", lambda: store.read_facts("root-1", 1, limit=MAX_READ_LIMIT + 1))
    store.close()
    reopened = SQLiteChatProjectionStore(path)
    assert reopened.projection_cursor("root-1", 0) == 1
    assert reopened.projection_cursor("root-1", 1) == 1
    reopened.close()
    _assert_error("invalid_path", lambda: SQLiteChatProjectionStore(Path("relative.sqlite3")))
    _assert_error("path_escape", lambda: SQLiteChatProjectionStore(STATE_HOME.parent / "escape.sqlite3"))
    if hasattr(os, "symlink"):
        outside = STATE_HOME.parent / "outside-chat-store.sqlite3"
        symlink = STATE_HOME / "chat-tests" / "symlink.sqlite3"
        symlink.symlink_to(outside)
        _assert_error("path_escape", lambda: SQLiteChatProjectionStore(symlink))
        symlink.unlink()

    bad = _path("bad-schema")
    bad.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(bad)
    connection.execute("CREATE TABLE root_heads(root_id TEXT)")
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()
    _assert_error("unsupported_schema", lambda: SQLiteChatProjectionStore(bad))


def test_strict_json_and_same_version_schema_validation() -> None:
    for invalid in (float("nan"), float("inf"), float("-inf")):
        _assert_error("invalid_json", lambda invalid=invalid: canonical_json({"value": invalid}))
    _assert_error("invalid_json", lambda: canonical_json({"nested": [{1: "not a JSON object"}]}))
    _assert_error("invalid_json", lambda: canonical_json({"not_an_array": (1, 2)}))
    shared = {"ok": True}
    assert canonical_json({"left": shared, "right": shared})
    cycle = []
    cycle.append(cycle)
    _assert_error("json_cycle", lambda: canonical_json({"cycle": cycle}))
    exact_depth = True
    for _ in range(MAX_JSON_DEPTH):
        exact_depth = {"nested": exact_depth}
    assert canonical_json(exact_depth)
    _assert_error("json_depth_limit", lambda: canonical_json({"nested": exact_depth}))
    assert canonical_json({"items": [None] * MAX_JSON_LIST_ITEMS})
    _assert_error(
        "json_list_limit",
        lambda: canonical_json({"items": [None] * (MAX_JSON_LIST_ITEMS + 1)}),
    )
    assert canonical_json({str(index): None for index in range(MAX_JSON_OBJECT_ITEMS)})
    _assert_error(
        "json_object_limit",
        lambda: canonical_json({str(index): None for index in range(MAX_JSON_OBJECT_ITEMS + 1)}),
    )
    exact_nodes = {
        "left": [None] * (MAX_JSON_LIST_ITEMS - 1),
        "right": [None] * (MAX_JSON_LIST_ITEMS - 2),
    }
    assert canonical_json(exact_nodes)
    exact_nodes["right"].append(None)
    _assert_error("json_node_limit", lambda: canonical_json(exact_nodes))

    malformed_column = _path("malformed-column")
    valid = SQLiteChatProjectionStore(malformed_column)
    valid.close()
    connection = sqlite3.connect(malformed_column)
    connection.execute("ALTER TABLE selected_roots ADD COLUMN unexpected TEXT")
    connection.commit()
    connection.close()
    _assert_error("unsupported_schema", lambda: SQLiteChatProjectionStore(malformed_column))

    malformed_index = _path("malformed-index")
    valid = SQLiteChatProjectionStore(malformed_index)
    valid.close()
    connection = sqlite3.connect(malformed_index)
    connection.execute("CREATE UNIQUE INDEX unexpected_fact_index ON canonical_facts(event_id)")
    connection.commit()
    connection.close()
    _assert_error("unsupported_schema", lambda: SQLiteChatProjectionStore(malformed_index))

    unexpected_objects = _path("unexpected-objects")
    valid = SQLiteChatProjectionStore(unexpected_objects)
    valid.close()
    connection = sqlite3.connect(unexpected_objects)
    connection.execute("CREATE VIEW unexpected_view AS SELECT root_id FROM selected_roots")
    connection.execute("CREATE TRIGGER unexpected_trigger AFTER INSERT ON selected_roots BEGIN SELECT 1; END")
    connection.commit()
    connection.close()
    _assert_error("unsupported_schema", lambda: SQLiteChatProjectionStore(unexpected_objects))

    modified_sql = _path("modified-sql")
    valid = SQLiteChatProjectionStore(modified_sql)
    valid.close()
    connection = sqlite3.connect(modified_sql)
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(
        "UPDATE sqlite_master SET sql=replace(sql, 'root_id TEXT PRIMARY KEY', "
        "'root_id TEXT COLLATE NOCASE PRIMARY KEY') WHERE name='selected_roots'"
    )
    connection.execute("PRAGMA writable_schema=OFF")
    connection.commit()
    connection.close()
    _assert_error("unsupported_schema", lambda: SQLiteChatProjectionStore(modified_sql))

    unexpected_fk = _path("unexpected-fk")
    valid = SQLiteChatProjectionStore(unexpected_fk)
    valid.close()
    connection = sqlite3.connect(unexpected_fk)
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(
        "UPDATE sqlite_master SET sql=substr(sql,1,length(sql)-1) || "
        "', FOREIGN KEY(root_id) REFERENCES selected_roots(root_id))' WHERE name='ownership'"
    )
    connection.execute("PRAGMA writable_schema=OFF")
    connection.commit()
    connection.close()
    _assert_error("unsupported_schema", lambda: SQLiteChatProjectionStore(unexpected_fk))


def test_concurrent_generation_selection_never_regresses() -> None:
    path = _path("concurrent-generation")
    stores = [SQLiteChatProjectionStore(path) for _ in range(8)]
    barrier = threading.Barrier(len(stores))
    failures = []

    def select(store, generation: int) -> None:
        barrier.wait()
        try:
            store.select_generation("race-root", generation)
        except ChatProjectionStoreError as exc:
            if exc.code != "stale_generation":
                failures.append(exc.code)

    threads = [threading.Thread(target=select, args=(store, generation)) for generation, store in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    _assert_error("stale_generation", lambda: stores[0].select_generation("race-root", 6))
    request = replace(_request(_fixture_event(), generation=7), root_id="race-root")
    assert stores[0].commit(request).fact_sequence == 1
    for store in stores:
        store.close()
    reopened = SQLiteChatProjectionStore(path)
    _assert_error("stale_generation", lambda: reopened.select_generation("race-root", 0))
    reopened.close()


def test_text_aggregate_and_sqlite_error_boundaries() -> None:
    path = _path("text-limits")
    store = SQLiteChatProjectionStore(path)
    exact_root = "é" * (MAX_TEXT_BYTES // 2)
    store.select_generation(exact_root, 0)
    _assert_error("text_too_large", lambda: store.select_generation(exact_root + "é", 0))
    store.select_generation("root-1", 0)
    request = _request(_fixture_event())
    exact_event = _with_event_id(request, "é" * (MAX_TEXT_BYTES // 2))
    assert store.commit(exact_event).fact_sequence == 1
    oversized = "é" * (MAX_TEXT_BYTES // 2 + 1)
    cases = [
        replace(request, root_id=oversized), _with_event_id(request, oversized),
        replace(request, turn_id=oversized), replace(request, owner_scope=oversized),
        replace(request, message_id=oversized), replace(request, parent_event_id=oversized),
        replace(request, manifest=replace(request.manifest, turn_id=oversized)),
        replace(request, watermark=replace(request.watermark, stream_id=oversized)),
    ]
    for invalid in cases:
        _assert_error("text_too_large", lambda invalid=invalid: store.commit(invalid))
    other_bytes = sum(len(value.encode("utf-8")) for value in (
        request.root_id, request.event_id, request.content_hash, request.turn_id,
        request.message_id or "", request.parent_event_id or "", request.owner_scope,
        request.manifest.turn_id, request.watermark.stream_id,
        canonical_json(request.canonical_fact), canonical_json(request.render_node),
        canonical_json(request.visible_delta),
    ))
    padding = MAX_COMMIT_BYTES - other_bytes - len('{"pad":""}'.encode("utf-8"))
    exact_commit = replace(request, historical_revision={"pad": "x" * padding})
    assert store.commit(exact_commit).fact_sequence == 2
    _assert_error(
        "commit_too_large",
        lambda: store.commit(replace(exact_commit, historical_revision={"pad": "x" * (padding + 1)})),
    )
    store._process.kill()
    store._process.wait()
    _assert_error("owner_unavailable", lambda: store.read_facts("root-1", 0))
    store.close()

    write_failure = SQLiteChatProjectionStore(_path("write-failure"))
    write_failure._process.kill()
    write_failure._process.wait()
    _assert_error("owner_unavailable", lambda: write_failure.select_generation("root-1", 0))
    write_failure.close()

    corrupt = _path("corrupt")
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not sqlite")
    _assert_error("storage_init_failed", lambda: SQLiteChatProjectionStore(corrupt))


def test_generation_and_root_deletion_are_atomic_and_durable() -> None:
    path = _path("deletion")
    store = SQLiteChatProjectionStore(path)
    store.select_generation("root-1", 0)
    store.commit(_request(_fixture_event(), generation=0))
    store.select_generation("root-1", 1)
    store.commit(_request(_fixture_event(), generation=1))
    _assert_error("current_generation", lambda: store.delete_generation("root-1", 1))
    _assert_error("missing_generation", lambda: store.delete_generation("root-1", 99))
    store.delete_generation("root-1", 0)
    assert store.read_facts("root-1", 0) == []
    assert store.read_revisions("root-1", 0) == []
    assert store.source_watermark("root-1", 0, "provider-neutral") is None
    assert len(store.read_facts("root-1", 1)) == 1
    store.close()
    reopened = SQLiteChatProjectionStore(path)
    assert reopened.read_facts("root-1", 0) == []
    assert len(reopened.read_facts("root-1", 1)) == 1
    reopened.close()

    rollback_path = _path("deletion-rollback")
    setup = SQLiteChatProjectionStore(rollback_path)
    setup.select_generation("root-1", 0)
    setup.commit(_request(_fixture_event(), generation=0))
    setup.select_generation("root-1", 1)
    setup.close()

    def fail_before() -> None:
        raise RuntimeError("simulated crash before delete commit")

    rollback = SQLiteChatProjectionStore(rollback_path, before_commit=fail_before)
    try:
        rollback.delete_generation("root-1", 0)
        raise AssertionError("delete rollback hook did not fire")
    except RuntimeError:
        pass
    assert len(rollback.read_facts("root-1", 0)) == 1
    try:
        rollback.delete_root("root-1")
        raise AssertionError("root delete rollback hook did not fire")
    except RuntimeError:
        pass
    assert len(rollback.read_facts("root-1", 0)) == 1
    rollback.close()

    durable = SQLiteChatProjectionStore(rollback_path)
    durable.delete_root("root-1")
    _assert_error("missing_root", lambda: durable.delete_root("root-1"))
    durable.close()
    restarted = SQLiteChatProjectionStore(rollback_path)
    restarted.close()
    connection = sqlite3.connect(rollback_path)
    for table in (
        "canonical_facts", "render_nodes", "ownership", "turn_manifests", "revisions",
        "source_watermarks", "root_generation_heads", "selected_roots",
    ):
        assert connection.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE root_id=?', ("root-1",),
        ).fetchone()[0] == 0
    connection.close()


def test_sqlite_integer_boundaries_unicode_and_persisted_corruption() -> None:
    path = _path("integer-boundaries")
    store = SQLiteChatProjectionStore(path)
    store.select_generation("max-root", MAX_SQLITE_INTEGER)
    request = replace(
        _request(_fixture_event(), generation=MAX_SQLITE_INTEGER), root_id="max-root",
        manifest=TurnManifest(_fixture_event()["turn_id"], MAX_SQLITE_INTEGER, MAX_SQLITE_INTEGER),
        watermark=SourceWatermark("provider-neutral", MAX_SQLITE_INTEGER, MAX_SQLITE_INTEGER),
    )
    assert store.commit(request).fact_sequence == 1
    assert store.read_facts("max-root", MAX_SQLITE_INTEGER, after=MAX_SQLITE_INTEGER) == []
    for invalid in (MAX_SQLITE_INTEGER + 1, True):
        _assert_error("invalid_input", lambda invalid=invalid: store.select_generation("overflow", invalid))
        _assert_error(
            "invalid_input",
            lambda invalid=invalid: store.commit(replace(request, manifest=replace(request.manifest, event_count=invalid))),
        )
        _assert_error(
            "invalid_input",
            lambda invalid=invalid: store.commit(replace(request, watermark=replace(request.watermark, sequence=invalid))),
        )
    _assert_error(
        "invalid_cursor",
        lambda: store.read_facts("max-root", MAX_SQLITE_INTEGER, after=MAX_SQLITE_INTEGER + 1),
    )
    _assert_error("invalid_cursor", lambda: store.read_facts("max-root", MAX_SQLITE_INTEGER, after=True))
    _assert_error("invalid_limit", lambda: store.read_facts("max-root", MAX_SQLITE_INTEGER, limit=True))
    _assert_error("invalid_input", lambda: store.select_generation("bad\ud800", 0))
    _assert_error("invalid_input", lambda: canonical_json({"bad": "\ud800"}))
    store.close()

    def corrupt(name: str, statement: str, callback) -> None:
        corrupt_path = _path(f"corrupt-{name}")
        target = SQLiteChatProjectionStore(corrupt_path)
        target.select_generation("root-1", 0)
        target.commit(_request(_fixture_event()))
        connection = sqlite3.connect(corrupt_path)
        connection.execute(statement)
        connection.commit()
        connection.close()
        _assert_error("storage_corrupt", lambda: callback(target))
        target.close()

    corrupt("fact-json", "UPDATE canonical_facts SET fact_json='[]'", lambda target: target.read_facts("root-1", 0))
    corrupt("fact-nan", "UPDATE canonical_facts SET fact_json='{\"value\":NaN}'", lambda target: target.read_facts("root-1", 0))
    corrupt("fact-hash", "UPDATE canonical_facts SET content_hash=printf('%064d',0)", lambda target: target.read_facts("root-1", 0))
    corrupt("fact-seq", "UPDATE canonical_facts SET fact_sequence='bad'", lambda target: target.read_facts("root-1", 0))
    corrupt("revision-json", "UPDATE revisions SET visible_delta_json='bad'", lambda target: target.read_revisions("root-1", 0))
    corrupt("revision-int", "UPDATE revisions SET revision='bad'", lambda target: target.read_revisions("root-1", 0))
    corrupt("revision-missing-fact", "DELETE FROM canonical_facts", lambda target: target.read_revisions("root-1", 0))
    corrupt("cursor", "UPDATE root_generation_heads SET projection_cursor='bad'", lambda target: target.projection_cursor("root-1", 0))
    corrupt("cursor-behind", "UPDATE root_generation_heads SET projection_cursor=0", lambda target: target.read_revisions("root-1", 0))
    corrupt("projection-json", "UPDATE render_nodes SET node_json='[]'", lambda target: target.read_projection("root-1", 0, _fixture_event()["event_id"]))
    corrupt("projection-count", "UPDATE turn_manifests SET event_count='bad'", lambda target: target.read_projection("root-1", 0, _fixture_event()["event_id"]))
    corrupt("watermark", "UPDATE source_watermarks SET source_sequence='bad'", lambda target: target.source_watermark("root-1", 0, "provider-neutral"))

    rollback_path = _path("corrupt-transaction")
    rollback = SQLiteChatProjectionStore(rollback_path)
    rollback.select_generation("root-1", 0)
    original = _request(_fixture_event())
    rollback.commit(original)
    connection = sqlite3.connect(rollback_path)
    connection.execute("UPDATE root_generation_heads SET fact_sequence='bad'")
    connection.commit()
    connection.close()
    changed_fact = dict(original.canonical_fact)
    changed_fact["content_version"] = 99
    digest = __import__("hashlib").sha256(canonical_json(changed_fact).encode()).hexdigest()
    changed = replace(original, canonical_fact=changed_fact, content_hash=digest,
                      watermark=replace(original.watermark, sequence=original.watermark.sequence + 1))
    _assert_error("storage_corrupt", lambda: rollback.commit(changed))
    connection = sqlite3.connect(rollback_path)
    assert connection.execute("SELECT COUNT(*) FROM canonical_facts").fetchone()[0] == 1
    assert connection.execute("SELECT source_sequence FROM source_watermarks").fetchone()[0] == original.watermark.sequence
    connection.close()
    rollback.close()


def test_owner_anchors_database_wal_and_lifecycle_through_path_swaps() -> None:
    path = _path("owner-race")
    store = SQLiteChatProjectionStore(path)
    process = store._process
    store.select_generation("root-1", 0)
    base = _request(_fixture_event())
    store.commit(base)
    anchored_parent = store._path.parent
    held_parent = anchored_parent.with_name(f"{anchored_parent.name}-held")
    outside = STATE_HOME / "outside-owner-race"
    outside.mkdir()
    os.rename(anchored_parent, held_parent)
    os.symlink(outside, anchored_parent)
    try:
        for version in range(2, 22):
            fact = json.loads(json.dumps(base.canonical_fact))
            fact["content_version"] = version
            digest = __import__("hashlib").sha256(canonical_json(fact).encode()).hexdigest()
            store.commit(replace(
                base, canonical_fact=fact, content_hash=digest,
                historical_revision={"content_version": version},
                watermark=replace(base.watermark, sequence=base.watermark.sequence + version),
            ))
        assert list(outside.iterdir()) == []
    finally:
        anchored_parent.unlink()
        os.rename(held_parent, anchored_parent)
    store.close()
    assert process.poll() is not None
    reopened = SQLiteChatProjectionStore(path)
    assert len(reopened.read_facts("root-1", 0)) == 21
    reopened.close()

    os.rename(anchored_parent, held_parent)
    os.symlink(outside, anchored_parent)
    try:
        _assert_error("path_escape", lambda: SQLiteChatProjectionStore(path))
        assert list(outside.iterdir()) == []
    finally:
        anchored_parent.unlink()
        os.rename(held_parent, anchored_parent)

    file_store = SQLiteChatProjectionStore(path)
    database = file_store._path
    moved_database = database.with_name(f"{database.name}.held")
    outside_database = outside / database.name
    outside_database.write_bytes(b"outside sentinel")
    os.rename(database, moved_database)
    os.symlink(outside_database, database)
    try:
        assert len(file_store.read_facts("root-1", 0)) == 21
        assert outside_database.read_bytes() == b"outside sentinel"
    finally:
        database.unlink()
        os.rename(moved_database, database)
    file_store.close()
    assert not any(item.name.endswith(("-wal", "-shm")) for item in outside.iterdir())


def test_secure_file_metadata_and_hardlink_checkpoints() -> None:
    parent = STATE_HOME / "chat-tests" / "metadata"
    parent.mkdir(parents=True)

    permissive = parent / "permissive.sqlite3"
    permissive.touch(mode=0o644)
    permissive.chmod(0o644)
    _assert_error("insecure_store_file", lambda: SQLiteChatProjectionStore(permissive))

    fifo = parent / "fifo.sqlite3"
    os.mkfifo(fifo, 0o600)
    _assert_error("insecure_store_file", lambda: SQLiteChatProjectionStore(fifo))

    hardlinked = parent / "hardlinked.sqlite3"
    hardlinked.touch(mode=0o600)
    hardlink_peer = parent / "hardlinked-peer.sqlite3"
    os.link(hardlinked, hardlink_peer)
    _assert_error("insecure_store_file", lambda: SQLiteChatProjectionStore(hardlinked))

    checkpoint_path = parent / "checkpoint.sqlite3"
    store = SQLiteChatProjectionStore(checkpoint_path)
    store.select_generation("root-1", 0)
    checkpoint_peer = parent / "checkpoint-peer.sqlite3"
    os.link(checkpoint_path, checkpoint_peer)
    _assert_error("insecure_store_file", lambda: store.commit(_request(_fixture_event())))
    checkpoint_peer.unlink()
    connection = sqlite3.connect(checkpoint_path)
    assert connection.execute("SELECT COUNT(*) FROM canonical_facts").fetchone()[0] == 0
    connection.close()
    _assert_error("owner_unavailable", lambda: store.commit(_request(_fixture_event())))
    store.close()
    restarted = SQLiteChatProjectionStore(checkpoint_path)
    assert restarted.commit(_request(_fixture_event())).fact_sequence == 1
    restarted.close()


def test_owner_timeout_ambiguity_protocol_poison_and_idempotent_close() -> None:
    stopped_path = _path("owner-stopped")
    stopped = SQLiteChatProjectionStore(stopped_path, _ipc_timeout_seconds=0.1)
    stopped_process = stopped._process
    os.kill(stopped_process.pid, signal.SIGSTOP)
    _assert_error("owner_unavailable", lambda: stopped.read_facts("root-1", 0))
    assert stopped_process.poll() is not None
    _assert_error("owner_unavailable", lambda: stopped.read_facts("root-1", 0))
    stopped.close()
    stopped.close()

    ambiguous_path = _path("owner-ambiguous-commit")
    ambiguous = SQLiteChatProjectionStore(
        ambiguous_path, _ipc_timeout_seconds=0.1, _test_owner_fault="post_commit_stop",
    )
    ambiguous.select_generation("root-1", 0)
    ambiguous_process = ambiguous._process
    _assert_error("commit_outcome_unknown", lambda: ambiguous.commit(_request(_fixture_event())))
    assert ambiguous_process.poll() is not None
    _assert_error("owner_unavailable", lambda: ambiguous.read_facts("root-1", 0))
    ambiguous.close()
    ambiguous.close()
    restarted = SQLiteChatProjectionStore(ambiguous_path)
    assert len(restarted.read_facts("root-1", 0)) == 1
    restarted.close()

    malformed = SQLiteChatProjectionStore(
        _path("owner-malformed"), _test_owner_fault="malformed_response",
    )
    malformed_process = malformed._process
    _assert_error("owner_protocol_error", lambda: malformed.select_generation("root-1", 0))
    assert malformed_process.poll() is not None
    _assert_error("owner_unavailable", lambda: malformed.select_generation("root-1", 1))
    malformed.close()
    malformed.close()

    mismatch = SQLiteChatProjectionStore(
        _path("owner-semantic-mismatch"), _test_owner_fault="semantic_mismatch",
    )
    mismatch.select_generation("root-1", 0)
    _assert_error("owner_protocol_error", lambda: mismatch.projection_cursor("root-1", 0))
    _assert_error("owner_unavailable", lambda: mismatch.projection_cursor("root-1", 0))
    mismatch.close()

    concurrent = SQLiteChatProjectionStore(
        _path("owner-concurrent-close"), _ipc_timeout_seconds=0.1,
    )
    os.kill(concurrent._process.pid, signal.SIGSTOP)
    failures = []
    def invoke(callback) -> None:
        try:
            callback()
        except ChatProjectionStoreError as exc:
            failures.append(exc.code)
    readers = [
        threading.Thread(target=invoke, args=(lambda: concurrent.read_facts("root-1", 0),)),
        threading.Thread(target=invoke, args=(concurrent.close,)),
    ]
    for thread in readers:
        thread.start()
    for thread in readers:
        thread.join()
    assert failures and set(failures) == {"owner_unavailable"}
    reused = [os.open(os.devnull, os.O_RDONLY) for _ in range(8)]
    concurrent.close()
    for descriptor in reused:
        os.fstat(descriptor)
        os.close(descriptor)


def test_response_page_budget_timeout_admission_and_close_failure() -> None:
    for failure_name in ("socketpair", "settimeout"):
        failure_path = _path(f"init-{failure_name}-failure")
        descriptors_before = len(os.listdir("/dev/fd"))
        real_socketpair = socket.socketpair
        sockets = []
        def failing_socketpair():
            if failure_name == "socketpair":
                raise OSError("injected socketpair failure")
            parent, child = real_socketpair()
            sockets.extend((parent, child))
            class FailingTimeoutSocket:
                def settimeout(self, _value) -> None:
                    raise OSError("injected settimeout failure")
                def close(self) -> None:
                    parent.close()
            return FailingTimeoutSocket(), child
        with (
            mock.patch.object(owner_transport.socket, "socketpair", side_effect=failing_socketpair),
            mock.patch.object(owner_transport.subprocess, "Popen") as launch,
        ):
            _assert_error("owner_start_failed", lambda: SQLiteChatProjectionStore(failure_path))
            launch.assert_not_called()
        assert not failure_path.exists()
        assert all(item.fileno() == -1 for item in sockets)
        assert len(os.listdir("/dev/fd")) == descriptors_before

    existing_path = _path("init-failure-existing")
    existing = SQLiteChatProjectionStore(existing_path)
    existing.close()
    with mock.patch.object(owner_transport.socket, "socketpair", side_effect=OSError("injected")):
        _assert_error("owner_start_failed", lambda: SQLiteChatProjectionStore(existing_path))
    assert existing_path.exists()

    missing_path = _path("init-missing-script")
    missing_path.parent.mkdir(parents=True, exist_ok=True)
    missing_path.write_bytes(b"")
    missing_path.chmod(0o600)
    sentinels = [missing_path.with_name(f"{missing_path.name}{suffix}") for suffix in ("-wal", "-shm")]
    for sentinel in sentinels:
        sentinel.write_bytes(f"sentinel:{sentinel.name}".encode())
        sentinel.chmod(0o600)
    missing_processes = []
    real_popen = owner_transport.subprocess.Popen
    def capture_missing(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        missing_processes.append(process)
        return process
    with mock.patch.object(owner_transport.subprocess, "Popen", side_effect=capture_missing):
        _assert_error(
            "owner_start_failed",
            lambda: owner_transport.OwnerClient(
                root_path=STATE_HOME, path=missing_path,
                owner_script=STATE_HOME / "missing-owner.py", owner_arguments=(),
                validate_result=lambda _operation, result, _arguments: result,
            ),
        )
    assert missing_processes and all(process.poll() is not None for process in missing_processes)
    assert missing_path.exists()
    assert all(sentinel.read_bytes() == f"sentinel:{sentinel.name}".encode() for sentinel in sentinels)

    orphan_path = _path("init-orphan-sidecars")
    orphan_wal = orphan_path.with_name(f"{orphan_path.name}-wal")
    orphan_wal.write_bytes(b"orphan sentinel")
    orphan_wal.chmod(0o600)
    _assert_error("orphan_sidecars", lambda: SQLiteChatProjectionStore(orphan_path))
    assert not orphan_path.exists()
    assert orphan_wal.read_bytes() == b"orphan sentinel"

    post_popen_path = _path("init-post-popen-failure")
    real_socketpair = socket.socketpair
    post_popen_processes = []
    def post_popen_socketpair():
        parent, child = real_socketpair()
        class FailingCloseSocket:
            def fileno(self) -> int:
                return child.fileno()
            def close(self) -> None:
                child.close()
                for suffix in ("-wal", "-shm"):
                    sidecar = post_popen_path.with_name(f"{post_popen_path.name}{suffix}")
                    sidecar.write_bytes(b"new owner sidecar")
                    sidecar.chmod(0o600)
                raise OSError("injected post-Popen failure")
        return parent, FailingCloseSocket()
    def capture_post_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        post_popen_processes.append(process)
        return process
    with (
        mock.patch.object(owner_transport.socket, "socketpair", side_effect=post_popen_socketpair),
        mock.patch.object(owner_transport.subprocess, "Popen", side_effect=capture_post_popen),
    ):
        _assert_error("owner_start_failed", lambda: SQLiteChatProjectionStore(post_popen_path))
    assert post_popen_processes and all(process.poll() is not None for process in post_popen_processes)
    assert not post_popen_path.exists()
    post_sidecars = [
        post_popen_path.with_name(f"{post_popen_path.name}{suffix}") for suffix in ("-wal", "-shm")
    ]
    assert all(sidecar.read_bytes() == b"new owner sidecar" for sidecar in post_sidecars)
    _assert_error("orphan_sidecars", lambda: SQLiteChatProjectionStore(post_popen_path))
    assert all(sidecar.read_bytes() == b"new owner sidecar" for sidecar in post_sidecars)

    replacement_path = _path("init-sidecar-replacement")
    replacement_path.parent.mkdir(parents=True, exist_ok=True)
    replacement_path.write_bytes(b"")
    replacement_path.chmod(0o600)
    replacement_wal = replacement_path.with_name(f"{replacement_path.name}-wal")
    replacement_wal.write_bytes(b"original sentinel")
    replacement_wal.chmod(0o600)
    def replacement_socketpair():
        parent, child = real_socketpair()
        class ReplacingCloseSocket:
            def fileno(self) -> int:
                return child.fileno()
            def close(self) -> None:
                child.close()
                replacement_wal.unlink()
                replacement_wal.write_bytes(b"replacement sentinel")
                replacement_wal.chmod(0o600)
                raise OSError("injected replacement failure")
        return parent, ReplacingCloseSocket()
    with mock.patch.object(owner_transport.socket, "socketpair", side_effect=replacement_socketpair):
        _assert_error(
            "owner_start_failed",
            lambda: owner_transport.OwnerClient(
                root_path=STATE_HOME, path=replacement_path,
                owner_script=STATE_HOME / "missing-owner.py", owner_arguments=(),
                validate_result=lambda _operation, result, _arguments: result,
            ),
        )
    assert replacement_path.exists()
    assert replacement_wal.read_bytes() == b"replacement sentinel"

    timeout_path = _path("invalid-timeout")
    for invalid in (float("nan"), float("inf"), float("-inf"), 0.01, 301, True):
        _assert_error(
            "invalid_input",
            lambda invalid=invalid: SQLiteChatProjectionStore(
                timeout_path, _ipc_timeout_seconds=invalid,
            ),
        )
        assert not timeout_path.exists()
    for boundary in (MIN_IPC_TIMEOUT_SECONDS, MAX_IPC_TIMEOUT_SECONDS):
        boundary_store = SQLiteChatProjectionStore(
            _path(f"timeout-{boundary}"), _ipc_timeout_seconds=boundary,
        )
        boundary_store.close()

    startup_path = _path("startup-timeout")
    _assert_error(
        "owner_start_failed",
        lambda: SQLiteChatProjectionStore(
            startup_path, _startup_timeout_seconds=0.05, _test_owner_fault="startup_stop",
        ),
    )
    assert not startup_path.exists()
    startup_sidecars = [
        startup_path.with_name(f"{startup_path.name}{suffix}") for suffix in ("-wal", "-shm")
    ]
    assert all(sidecar.read_bytes() == b"startup sidecar sentinel" for sidecar in startup_sidecars)
    _assert_error("orphan_sidecars", lambda: SQLiteChatProjectionStore(startup_path))
    for invalid in (float("nan"), float("inf"), 0.01, 301, True):
        _assert_error(
            "invalid_input",
            lambda invalid=invalid: SQLiteChatProjectionStore(
                startup_path, _startup_timeout_seconds=invalid,
            ),
        )
        assert not startup_path.exists()

    page_path = _path("response-page-budget")
    store = SQLiteChatProjectionStore(page_path)
    store.select_generation("root-1", 0)
    base = _request(_fixture_event())
    blob = "x" * (MAX_RESPONSE_BYTES // 4)
    for version in range(1, 6):
        fact = json.loads(json.dumps(base.canonical_fact))
        fact["content_version"] = version
        fact["data"]["text"] = blob
        digest = __import__("hashlib").sha256(canonical_json(fact).encode()).hexdigest()
        store.commit(replace(
            base, canonical_fact=fact, content_hash=digest,
            historical_revision={"blob": blob, "version": version},
            watermark=replace(base.watermark, sequence=base.watermark.sequence + version),
        ))
    _assert_error("response_too_large", lambda: store.read_facts("root-1", 0, limit=5))
    first_facts = store.read_facts("root-1", 0, limit=3)
    assert [item.fact_sequence for item in first_facts] == [1, 2, 3]
    assert [item.fact_sequence for item in store.read_facts("root-1", 0, after=3, limit=2)] == [4, 5]
    _assert_error("response_too_large", lambda: store.read_revisions("root-1", 0, limit=5))
    assert [item.revision for item in store.read_revisions("root-1", 0, limit=3)] == [1, 2, 3]
    store.close()

    close_path = _path("close-checkpoint-failure")
    closing = SQLiteChatProjectionStore(close_path)
    closing.select_generation("root-1", 0)
    close_peer = close_path.with_name("close-checkpoint-peer.sqlite3")
    os.link(close_path, close_peer)
    _assert_error("insecure_store_file", closing.close)
    close_peer.unlink()
    closing.close()


def test_exact_correlated_response_cap_and_commit_protocol_uncertainty() -> None:
    assert bytes(encode_frame({"operation": "probe"})) == b'{"operation":"probe"}'
    for malformed in (b'{"key":1,"key":2}', b'{"value":NaN}'):
        sender, receiver = socket.socketpair()
        try:
            sender.sendall(struct.pack("!I", len(malformed)) + malformed)
            _assert_error("owner_protocol_error", lambda: receive_frame(receiver))
        finally:
            sender.close()
            receiver.close()

    generic_parent, generic_child = socket.socketpair()
    generic_directory = os.open(STATE_HOME, os.O_RDONLY | os.O_DIRECTORY)
    generic_file_path = STATE_HOME / "generic-owner.file"
    generic_file = os.open(generic_file_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    child_pid = os.fork()
    if child_pid == 0:
        generic_parent.close()
        channel_fd = generic_child.detach()
        class GenericStore:
            pass
        try:
            serve_owner(
                channel_fd, generic_directory, generic_file, generic_file_path.name,
                lambda *_: GenericStore(),
                lambda _store, operation, arguments, _request_id: (
                    None if operation == "close" and not arguments else
                    (_ for _ in ()).throw(ChatProjectionStoreError("owner_protocol_error", "unexpected"))
                ),
                lambda _store: (_ for _ in ()).throw(RuntimeError("close failed")),
                lambda _channel, _request_id, _operation, result: (result, False),
                MAX_RESPONSE_BYTES,
            )
            assert all(_fd_is_closed(fd) for fd in (channel_fd, generic_file, generic_directory))
            os._exit(0)
        except BaseException:
            os._exit(1)
    generic_child.close()
    assert receive_frame(generic_parent) == {"ready": True}
    send_frame(generic_parent, {"request_id": 1, "operation": "close", "arguments": {}})
    assert receive_frame(generic_parent) == {"request_id": 1, "operation": "close", "result": None}
    generic_parent.close()
    os.close(generic_file)
    os.close(generic_directory)
    assert os.waitpid(child_pid, 0)[1] == 0

    validator = SQLiteChatProjectionStore(_path("validator-runtime-error"))
    validator.select_generation("root-1", 0)
    validator_process = validator._process
    validator._owner._validate_result = lambda *_: (_ for _ in ()).throw(RuntimeError("bad validator"))
    _assert_error("owner_protocol_error", lambda: validator.projection_cursor("root-1", 0))
    assert validator_process.poll() is not None
    _assert_error("owner_unavailable", lambda: validator.projection_cursor("root-1", 0))
    validator.close()

    domain_validator = SQLiteChatProjectionStore(_path("validator-domain-error"))
    domain_validator.select_generation("root-1", 0)
    domain_validator._owner._validate_result = lambda *_: (_ for _ in ()).throw(
        ChatProjectionStoreError("validator_domain", "validator rejected result")
    )
    _assert_error("validator_domain", lambda: domain_validator.projection_cursor("root-1", 0))
    _assert_error("owner_unavailable", lambda: domain_validator.projection_cursor("root-1", 0))
    domain_validator.close()

    def install_fact(path: Path, extra_bytes: int) -> None:
        setup = SQLiteChatProjectionStore(path)
        setup.select_generation("root-1", 0)
        setup.close()
        fact = {"event_id": "event-exact", "data": {"nested": {"text": ""}}}
        digest = __import__("hashlib").sha256(canonical_json(fact).encode()).hexdigest()
        wire_row = {
            "fact_sequence": 1, "event_id": "event-exact", "content_hash": digest,
            "canonical_fact": fact, "root_id": "root-1", "root_generation": 0,
        }
        envelope = {
            "request_id": 1, "operation": "read_facts", "result": {
                "root_id": "root-1", "root_generation": 0, "after": 0,
                "projection_cursor": 1, "rows": [wire_row],
            },
        }
        base_size = len(_encode_json_bounded(envelope, MAX_IPC_BYTES))
        fact["data"]["nested"]["text"] = "x" * (MAX_RESPONSE_BYTES - base_size + extra_bytes)
        digest = __import__("hashlib").sha256(canonical_json(fact).encode()).hexdigest()
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO canonical_facts VALUES(?,?,?,?,?,?)",
            ("root-1", 0, 1, "event-exact", digest, canonical_json(fact)),
        )
        connection.execute(
            "UPDATE root_generation_heads SET fact_sequence=1,revision=1,projection_cursor=1 "
            "WHERE root_id='root-1' AND root_generation=0"
        )
        connection.commit()
        connection.close()

    exact_path = _path("exact-response-cap")
    install_fact(exact_path, 0)
    exact = SQLiteChatProjectionStore(exact_path)
    assert len(exact.read_facts("root-1", 0, limit=1)) == 1
    exact.close()

    over_path = _path("over-response-cap")
    install_fact(over_path, 1)
    over = SQLiteChatProjectionStore(over_path)
    _assert_error("response_too_large", lambda: over.read_facts("root-1", 0, limit=1))
    over.close()

    ambiguous_path = _path("malformed-commit-response")
    ambiguous = SQLiteChatProjectionStore(
        ambiguous_path, _test_owner_fault="malformed_commit_response",
    )
    ambiguous.select_generation("root-1", 0)
    _assert_error("commit_outcome_unknown", lambda: ambiguous.commit(_request(_fixture_event())))
    _assert_error("owner_unavailable", lambda: ambiguous.read_facts("root-1", 0))
    ambiguous.close()
    restarted = SQLiteChatProjectionStore(ambiguous_path)
    assert len(restarted.read_facts("root-1", 0)) == 1
    restarted.close()


def test_revision_fact_pairing_and_delta_identity_are_atomic() -> None:
    def two_fact_store(path: Path, *, fault: str | None = None) -> SQLiteChatProjectionStore:
        store = SQLiteChatProjectionStore(path, _test_owner_fault=fault)
        store.select_generation("root-1", 0)
        base = _request(_fixture_event())
        store.commit(base)
        fact = json.loads(json.dumps(base.canonical_fact))
        fact["content_version"] = 2
        fact["data"]["text"] = "second revision"
        digest = __import__("hashlib").sha256(canonical_json(fact).encode()).hexdigest()
        store.commit(replace(
            base, canonical_fact=fact, content_hash=digest,
            historical_revision={"event_id": base.event_id, "content_version": 2},
            watermark=replace(base.watermark, sequence=base.watermark.sequence + 1),
        ))
        return store

    repointed_path = _path("revision-repointed")
    repointed = two_fact_store(repointed_path)
    connection = sqlite3.connect(repointed_path)
    connection.execute("UPDATE revisions SET fact_sequence=2 WHERE revision=1")
    connection.commit()
    connection.close()
    _assert_error("storage_corrupt", lambda: repointed.read_revisions("root-1", 0))
    repointed.close()

    identity_path = _path("revision-identity-mismatch")
    identity = two_fact_store(identity_path)
    connection = sqlite3.connect(identity_path)
    connection.execute(
        "UPDATE revisions SET historical_json='{\"event_id\":\"wrong-event\"}' WHERE revision=1"
    )
    connection.commit()
    connection.close()
    _assert_error("storage_corrupt", lambda: identity.read_revisions("root-1", 0))
    identity.close()

    malformed = two_fact_store(
        _path("revision-wire-mismatch"), fault="revision_pair_mismatch",
    )
    _assert_error("owner_protocol_error", lambda: malformed.read_revisions("root-1", 0))
    _assert_error("owner_unavailable", lambda: malformed.read_revisions("root-1", 0))
    malformed.close()


def main() -> None:
    try:
        tests = [value for name, value in globals().items() if name.startswith("test_")]
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        shutil.rmtree(STATE_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
