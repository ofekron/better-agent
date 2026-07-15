#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_HOME = Path(tempfile.mkdtemp(prefix="better-agent-chat-store-"))
os.environ["BETTER_AGENT_HOME"] = str(STATE_HOME)
os.environ["BETTER_AGENT_TEST_MODE"] = "1"
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_store import ChatProjectionStoreError, ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_sqlite import MAX_READ_LIMIT, SQLiteChatProjectionStore, canonical_json


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


def _path(name: str) -> Path:
    return STATE_HOME / "chat-tests" / f"{name}.sqlite3"


def _assert_error(code: str, callback) -> None:
    try:
        callback()
    except ChatProjectionStoreError as exc:
        assert exc.code == code
        return
    raise AssertionError(f"expected ChatProjectionStoreError({code})")


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
    for table in (
        "canonical_facts", "render_nodes", "ownership", "turn_manifests", "revisions",
        "source_watermarks", "root_generation_heads", "selected_roots",
    ):
        assert restarted._connection.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE root_id=?', ("root-1",),
        ).fetchone()[0] == 0
    restarted.close()


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
