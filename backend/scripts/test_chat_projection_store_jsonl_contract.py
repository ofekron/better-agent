#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_HOME = Path(tempfile.mkdtemp(prefix="better-agent-jsonl-store-"))
os.environ["BETTER_AGENT_HOME"] = str(STATE_HOME)
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_store import ChatProjectionStoreError, ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_jsonl import (
    INTEGRITY_MODULUS_A, INTEGRITY_MODULUS_B, JsonlChatProjectionStore, MAX_JSONL_ROW_BYTES,
    _JsonlOwnerStore, _record_line,
)
from chat_projection_store_sqlite import SQLiteChatProjectionStore, canonical_json


FIXTURE = ROOT / "test-contracts" / "chat-panel" / "v1" / "canonical-session.json"


def event() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["events"][1]


def request(*, version: int = 1, sequence: int = 1) -> ProjectionCommit:
    fact = event()
    fact["content_version"] = version
    fact["data"]["text"] = f"answer-{version}"
    digest = hashlib.sha256(canonical_json(fact).encode()).hexdigest()
    return ProjectionCommit(
        root_id="root", root_generation=0, event_id=fact["event_id"], content_hash=digest,
        canonical_fact=fact, render_node={"type": "Explanation", "text": f"answer-{version}"},
        turn_id=fact["turn_id"], message_id=fact["message_id"],
        parent_event_id=fact["parent_event_id"], owner_scope="turn:turn-1",
        manifest=TurnManifest(fact["turn_id"], sequence, 1),
        visible_delta={"replace": fact["event_id"]},
        historical_revision={"event_id": fact["event_id"], "content_version": version},
        watermark=SourceWatermark("provider-neutral", 0, sequence),
    )


def growing_request(sequence: int, payload_size: int = 40 * 1024) -> ProjectionCommit:
    fact = event()
    fact["event_id"] = f"event-{sequence}"
    fact["content_version"] = sequence
    fact["data"]["text"] = f"{sequence}:" + "x" * payload_size
    digest = hashlib.sha256(canonical_json(fact).encode()).hexdigest()
    return ProjectionCommit(
        root_id="root", root_generation=0, event_id=fact["event_id"], content_hash=digest,
        canonical_fact=fact, render_node={"type": "Explanation", "text": fact["data"]["text"]},
        turn_id=fact["turn_id"], message_id=fact["message_id"],
        parent_event_id=fact["parent_event_id"], owner_scope="turn:turn-1",
        manifest=TurnManifest(fact["turn_id"], sequence, sequence),
        visible_delta={"append": fact["event_id"]},
        historical_revision={"event_id": fact["event_id"], "content_version": sequence},
        watermark=SourceWatermark("provider-neutral", 0, sequence),
    )


def register_integrity_functions(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "jsonl_row_a", -1,
        lambda table, *values: _JsonlOwnerStore._row_integrity(0, table, *values),
        deterministic=True,
    )
    connection.create_function(
        "jsonl_row_b", -1,
        lambda table, *values: _JsonlOwnerStore._row_integrity(1, table, *values),
        deterministic=True,
    )
    connection.create_function(
        "jsonl_accumulate_a", 3,
        lambda current, remove, add: (current - remove + add) % INTEGRITY_MODULUS_A,
        deterministic=True,
    )
    connection.create_function(
        "jsonl_accumulate_b", 3,
        lambda current, remove, add: (current - remove + add) % INTEGRITY_MODULUS_B,
        deterministic=True,
    )


def assert_error(code: str, callback) -> None:
    try:
        callback()
    except ChatProjectionStoreError as exc:
        assert exc.code == code
        return
    raise AssertionError(f"expected {code}")


def test_commit_duplicate_mutation_restart_and_delete() -> None:
    path = STATE_HOME / "chat" / "canonical.jsonl"
    store = JsonlChatProjectionStore(path)
    store.select_generation("root", 0)
    first = store.commit(request())
    assert (first.fact_sequence, first.revision, first.projection_cursor) == (1, 1, 1)
    lines_before = path.read_bytes().count(b"\n")
    duplicate = store.commit(request(sequence=1))
    assert duplicate.duplicate and path.read_bytes().count(b"\n") == lines_before
    advanced = store.commit(request(sequence=2))
    assert advanced.duplicate and path.read_bytes().count(b"\n") == lines_before + 1
    assert store.source_watermark("root", 0, "provider-neutral").sequence == 2
    second = store.commit(request(version=2, sequence=2))
    assert (second.fact_sequence, second.revision) == (2, 2)
    assert store.read_projection("root", 0, request().event_id).render_node["text"] == "answer-2"
    store.close()

    reopened = JsonlChatProjectionStore(path)
    assert [item.fact_sequence for item in reopened.read_facts("root", 0)] == [1, 2]
    assert reopened.projection_cursor("root", 0) == 2
    reopened.select_generation("root", 1)
    reopened.delete_generation("root", 0)
    assert reopened.read_facts("root", 0) == []
    reopened.close()
    reopened = JsonlChatProjectionStore(path)
    assert reopened.read_facts("root", 0) == []
    reopened.close()


def test_partial_tail_index_rebuild_corruption_and_concurrency() -> None:
    path = STATE_HOME / "chat" / "recovery.jsonl"
    store = JsonlChatProjectionStore(path)
    store.select_generation("root", 0)
    store.commit(request())
    store.close()
    with path.open("ab") as journal:
        journal.write(b'{"incomplete"')
        journal.flush()
        os.fsync(journal.fileno())
    reopened = JsonlChatProjectionStore(path)
    assert reopened.projection_cursor("root", 0) == 1
    reopened.close()
    assert path.read_bytes().endswith(b"\n")

    newest_index = max(path.parent.glob(f"{path.name}.index.*.sqlite3"), key=lambda item: item.stat().st_mtime_ns)
    newest_index.write_bytes(b"corrupt disposable epoch")
    newest_index.chmod(0o600)
    rebuilt = JsonlChatProjectionStore(path)
    assert rebuilt.projection_cursor("root", 0) == 1
    rebuilt.close()
    assert newest_index.read_bytes() == b"corrupt disposable epoch"

    reusable_path = STATE_HOME / "chat" / "reusable.jsonl"
    reusable = JsonlChatProjectionStore(reusable_path)
    reusable.select_generation("root", 0)
    reusable.commit(request())
    reusable.close()
    slots_before = sorted(reusable_path.parent.glob(f"{reusable_path.name}.index.*.sqlite3"))
    for _ in range(3):
        repeated = JsonlChatProjectionStore(reusable_path)
        assert repeated.projection_cursor("root", 0) == 1
        assert repeated.startup_read_bytes() == 0
        repeated.close()
    assert sorted(reusable_path.parent.glob(f"{reusable_path.name}.index.*.sqlite3")) == slots_before
    fallback_slot = reusable_path.with_name(f"{reusable_path.name}.index.1.sqlite3")
    shutil.copyfile(slots_before[0], fallback_slot)
    fallback_slot.chmod(0o600)
    slots_before[0].write_bytes(b"corrupt newest epoch")
    slots_before[0].chmod(0o600)
    fallback = JsonlChatProjectionStore(reusable_path)
    assert fallback.projection_cursor("root", 0) == 1
    assert fallback.startup_read_bytes() == 0
    fallback.close()
    assert slots_before[0].read_bytes() == b"corrupt newest epoch"

    eof_path = STATE_HOME / "chat" / "valid-eof.jsonl"
    eof = JsonlChatProjectionStore(eof_path)
    eof.select_generation("root", 0)
    eof.close()
    eof_bytes = eof_path.read_bytes()
    eof_path.write_bytes(eof_bytes[:-1])
    retained = JsonlChatProjectionStore(eof_path)
    retained.close()
    assert eof_path.read_bytes() == eof_bytes

    oversized_path = STATE_HOME / "chat" / "oversized-eof.jsonl"
    oversized_path.write_bytes(b"x" * (MAX_JSONL_ROW_BYTES + 1))
    oversized_path.chmod(0o600)
    oversized_size = oversized_path.stat().st_size
    assert_error("storage_corrupt", lambda: JsonlChatProjectionStore(oversized_path))
    assert oversized_path.stat().st_size == oversized_size

    crash_path = STATE_HOME / "chat" / "crash-window.jsonl"
    crash = JsonlChatProjectionStore(crash_path)
    crash.select_generation("root", 0)
    crash.commit(request())
    crash.close()
    crash_slot = next(crash_path.parent.glob(f"{crash_path.name}.index.*.sqlite3"))
    crash_connection = sqlite3.connect(crash_slot)
    checkpoint_sequence = crash_connection.execute(
        "SELECT record_sequence FROM jsonl_checkpoint"
    ).fetchone()[0]
    crash_connection.close()
    last = json.loads(crash_path.read_bytes().splitlines()[-1])
    second_request = request(version=2, sequence=2)
    line, _ = _record_line(
        last["sequence"] + 1, last["checksum"], "commit",
        {"request": SQLiteChatProjectionStore._commit_to_dict(second_request)},
    )
    with crash_path.open("ab") as journal:
        journal.write(line)
        journal.flush()
        os.fsync(journal.fileno())
    crash_connection = sqlite3.connect(crash_slot)
    assert crash_connection.execute(
        "SELECT record_sequence FROM jsonl_checkpoint"
    ).fetchone()[0] == checkpoint_sequence
    crash_connection.close()
    for _ in range(2):
        recovered = JsonlChatProjectionStore(crash_path)
        assert recovered.projection_cursor("root", 0) == 2
        assert recovered.read_projection("root", 0, second_request.event_id).render_node["text"] == "answer-2"
        recovered.close()

    injected_path = STATE_HOME / "chat" / "injected-crash-window.jsonl"
    initialized = JsonlChatProjectionStore(injected_path)
    initialized.select_generation("root", 0)
    initialized.close()
    injected_slot = next(injected_path.parent.glob(f"{injected_path.name}.index.*.sqlite3"))
    connection = sqlite3.connect(injected_slot)
    before_sequence = connection.execute("SELECT record_sequence FROM jsonl_checkpoint").fetchone()[0]
    connection.close()
    injected = JsonlChatProjectionStore(
        injected_path, _test_owner_fault="post_append_failure",
    )
    assert_error("storage_write_failed", lambda: injected.commit(request()))
    injected.close()
    connection = sqlite3.connect(injected_slot)
    assert connection.execute("SELECT record_sequence FROM jsonl_checkpoint").fetchone()[0] == before_sequence
    connection.close()
    recovered = JsonlChatProjectionStore(injected_path)
    assert recovered.projection_cursor("root", 0) == 1
    assert recovered.read_projection("root", 0, request().event_id).render_node["text"] == "answer-1"
    recovered.close()

    concurrent_path = STATE_HOME / "chat" / "concurrent.jsonl"
    concurrent = JsonlChatProjectionStore(concurrent_path)
    concurrent.select_generation("root", 0)
    outcomes = []
    threads = [threading.Thread(target=lambda version=version: outcomes.append(
        concurrent.commit(request(version=version, sequence=5))
    )) for version in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 5 and concurrent.projection_cursor("root", 0) == 5
    concurrent.close()

    corrupt = bytearray(path.read_bytes())
    corrupt[10] ^= 1
    path.write_bytes(corrupt)
    fast = JsonlChatProjectionStore(path)
    assert_error("rebuild_required", fast.audit_prefix)
    fast.close()


def test_growing_projection_has_bounded_checkpoint_work() -> None:
    path = STATE_HOME / "chat" / "growing.jsonl"
    store = JsonlChatProjectionStore(path)
    store.select_generation("root", 0)
    table_count = len(SQLiteChatProjectionStore._TABLES)
    for sequence in range(1, 426):
        before = store.checkpoint_rows_read()
        result = store.commit(growing_request(sequence))
        assert result.projection_cursor == sequence
        assert store.checkpoint_rows_read() - before == table_count
        if sequence % 100 == 0:
            store.close()
            store = JsonlChatProjectionStore(path)
            assert store.startup_read_bytes() == 0
            assert store.projection_cursor("root", 0) == sequence
    assert path.stat().st_size > 16 * 1024 * 1024
    store.close()
    reopened = JsonlChatProjectionStore(path)
    assert reopened.startup_read_bytes() == 0
    assert reopened.projection_cursor("root", 0) == 425
    reopened.close()
    slot = next(path.parent.glob(f"{path.name}.index.*.sqlite3"))
    connection = sqlite3.connect(slot)
    assert connection.execute("SELECT COUNT(*) FROM canonical_facts").fetchone()[0] == 425
    connection.close()

def test_invalid_requests_do_not_append_and_writer_is_exclusive() -> None:
    path = STATE_HOME / "chat" / "validation.jsonl"
    store = JsonlChatProjectionStore(path)
    store.select_generation("root", 0)
    size = path.stat().st_size
    assert_error("hash_mismatch", lambda: store.commit(replace(request(), content_hash="0" * 64)))
    assert path.stat().st_size == size
    assert_error("missing_generation", lambda: store.delete_generation("root", 7))
    assert path.stat().st_size == size
    assert_error("writer_active", lambda: JsonlChatProjectionStore(path))
    assert path.stat().st_size == size
    store.close()
    reopened = JsonlChatProjectionStore(path)
    assert reopened.projection_cursor("root", 0) == 0
    reopened.close()


def test_hundred_thousand_record_checkpoint_is_tail_only() -> None:
    path = STATE_HOME / "chat" / "large-checkpoint.jsonl"
    store = JsonlChatProjectionStore(path)
    store.select_generation("root", 0)
    store.close()
    slot = next(path.parent.glob(f"{path.name}.index.*.sqlite3"))
    connection = sqlite3.connect(slot)
    checkpoint = connection.execute("SELECT * FROM jsonl_checkpoint").fetchone()
    sequence, chain, prefix = checkpoint[4], checkpoint[5], checkpoint[6]
    with path.open("ab", buffering=1024 * 1024) as journal:
        for _ in range(100_000):
            line, chain = _record_line(
                sequence + 1, chain, "select_generation",
                {"root_id": "root", "root_generation": 0},
            )
            journal.write(line)
            prefix = hashlib.sha256(bytes.fromhex(prefix) + line).hexdigest()
            sequence += 1
        journal.flush()
        os.fsync(journal.fileno())
    connection.execute(
        "UPDATE jsonl_checkpoint SET slot_generation=?,byte_offset=?,record_sequence=?,chain_head=?,prefix_digest=?",
        (sequence, path.stat().st_size, sequence, chain, prefix),
    )
    connection.commit()
    connection.close()
    fast = JsonlChatProjectionStore(path)
    assert fast.startup_read_bytes() == 0
    started = time.monotonic()
    assert fast.projection_cursor("root", 0) == 0
    assert time.monotonic() - started < 1.0
    fast.audit_prefix()
    fast.close()


def test_epoch_reuse_tamper_fallback_and_automatic_quarantine() -> None:
    crash_path = STATE_HOME / "chat" / "repeated-crash.jsonl"
    initial = JsonlChatProjectionStore(crash_path)
    initial.select_generation("root", 0)
    initial.commit(request())
    initial.close()
    slots = sorted(crash_path.parent.glob(f"{crash_path.name}.index.*.sqlite3"))
    for _ in range(4):
        crashed = JsonlChatProjectionStore(crash_path)
        crashed._owner.process.kill()
        crashed._owner.process.wait()
        assert_error("owner_unavailable", crashed.close)
    assert sorted(crash_path.parent.glob(f"{crash_path.name}.index.*.sqlite3")) == slots

    tamper_path = STATE_HOME / "chat" / "row-tamper.jsonl"
    tamper = JsonlChatProjectionStore(tamper_path)
    tamper.select_generation("root", 0)
    tamper.commit(request())
    tamper.close()
    tampered_slot = next(tamper_path.parent.glob(f"{tamper_path.name}.index.*.sqlite3"))
    connection = sqlite3.connect(tampered_slot)
    register_integrity_functions(connection)
    connection.execute("UPDATE render_nodes SET node_json='{}'")
    connection.commit()
    connection.close()
    recovered = JsonlChatProjectionStore(tamper_path)
    assert recovered.read_projection("root", 0, request().event_id).render_node["text"] == "answer-1"
    recovered.close()
    tampered_connection = sqlite3.connect(tampered_slot)
    assert tampered_connection.execute("SELECT node_json FROM render_nodes").fetchone()[0] == "{}"
    tampered_connection.close()

    trigger_path = STATE_HOME / "chat" / "trigger-tamper.jsonl"
    trigger_store = JsonlChatProjectionStore(trigger_path)
    trigger_store.select_generation("root", 0)
    trigger_store.commit(request())
    trigger_store.close()
    trigger_slot = next(trigger_path.parent.glob(f"{trigger_path.name}.index.*.sqlite3"))
    trigger_connection = sqlite3.connect(trigger_slot)
    trigger_name = trigger_connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name LIMIT 1"
    ).fetchone()[0]
    trigger_connection.execute(f'DROP TRIGGER "{trigger_name}"')
    trigger_connection.commit()
    trigger_connection.close()
    trigger_recovered = JsonlChatProjectionStore(trigger_path)
    assert trigger_recovered.projection_cursor("root", 0) == 1
    trigger_recovered.close()
    trigger_connection = sqlite3.connect(trigger_slot)
    assert trigger_connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,),
    ).fetchone() is None
    trigger_connection.close()

    digest_path = STATE_HOME / "chat" / "digest-tamper.jsonl"
    digest_store = JsonlChatProjectionStore(digest_path)
    digest_store.select_generation("root", 0)
    digest_store.close()
    digest_slot = next(digest_path.parent.glob(f"{digest_path.name}.index.*.sqlite3"))
    digest_connection = sqlite3.connect(digest_slot)
    digest_connection.execute("UPDATE jsonl_checkpoint SET integrity_json=?", ("forged",))
    digest_connection.commit()
    digest_connection.close()
    digest_recovered = JsonlChatProjectionStore(digest_path)
    assert digest_recovered.projection_cursor("root", 0) == 0
    digest_recovered.close()

    prefix_path = STATE_HOME / "chat" / "automatic-audit.jsonl"
    prefix = JsonlChatProjectionStore(prefix_path)
    prefix.select_generation("root", 0)
    prefix.commit(request())
    prefix.close()
    mutated = bytearray(prefix_path.read_bytes())
    mutated[10] ^= 1
    prefix_path.write_bytes(mutated)
    quarantined = JsonlChatProjectionStore(prefix_path)
    statuses = []
    for _ in range(100_000):
        status = quarantined.audit_status()
        statuses.append(status)
        if status == "failed":
            break
    assert statuses and statuses[-1] == "failed"
    assert_error("rebuild_required", lambda: quarantined.projection_cursor("root", 0))
    assert_error("owner_unavailable", lambda: quarantined.commit(request(version=2, sequence=2)))
    quarantined.close()


def test_exact_schema_attestation_falls_back_before_selection() -> None:
    def initialized_path(name: str) -> tuple[Path, Path]:
        path = STATE_HOME / "chat" / f"schema-{name}.jsonl"
        store = JsonlChatProjectionStore(path)
        store.select_generation("root", 0)
        store.commit(request())
        store.close()
        slot = next(path.parent.glob(f"{path.name}.index.*.sqlite3"))
        return path, slot

    valid_path, valid_slot = initialized_path("valid")
    valid = JsonlChatProjectionStore(valid_path)
    assert valid.startup_read_bytes() == 0
    assert valid.projection_cursor("root", 0) == 1
    valid.close()
    assert len(list(valid_path.parent.glob(f"{valid_path.name}.index.*.sqlite3"))) == 1
    assert valid_slot.exists()

    view_path, view_slot = initialized_path("view")
    connection = sqlite3.connect(view_slot)
    connection.execute("CREATE VIEW unexpected_projection_view AS SELECT root_id FROM selected_roots")
    connection.commit()
    connection.close()
    rebuilt = JsonlChatProjectionStore(view_path)
    assert rebuilt.startup_read_bytes() == view_path.stat().st_size
    assert rebuilt.projection_cursor("root", 0) == 1
    rebuilt.close()
    connection = sqlite3.connect(view_slot)
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='unexpected_projection_view'"
    ).fetchone()
    connection.close()

    index_path, index_slot = initialized_path("index")
    connection = sqlite3.connect(index_slot)
    connection.execute("CREATE INDEX unexpected_projection_index ON render_nodes(event_id)")
    connection.commit()
    connection.close()
    rebuilt = JsonlChatProjectionStore(index_path)
    assert rebuilt.startup_read_bytes() == index_path.stat().st_size
    assert rebuilt.projection_cursor("root", 0) == 1
    rebuilt.close()
    connection = sqlite3.connect(index_slot)
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='unexpected_projection_index'"
    ).fetchone()
    connection.close()

    for name, original, replacement in (
        (
            "pk", "PRIMARY KEY(root_id,root_generation,fact_sequence)",
            "PRIMARY KEY(root_id,fact_sequence,root_generation)",
        ),
        (
            "unique", "UNIQUE(root_id,root_generation,event_id,content_hash)",
            "UNIQUE(root_id,root_generation,event_id,fact_sequence)",
        ),
    ):
        path, slot = initialized_path(name)
        connection = sqlite3.connect(slot)
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='canonical_facts'"
        ).fetchone()[0]
        assert original in ddl
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='canonical_facts'",
            (ddl.replace(original, replacement),),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
        connection.close()
        rebuilt = JsonlChatProjectionStore(path)
        assert rebuilt.startup_read_bytes() == path.stat().st_size
        assert rebuilt.projection_cursor("root", 0) == 1
        rebuilt.close()
        connection = sqlite3.connect(slot)
        assert replacement in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='canonical_facts'"
        ).fetchone()[0]
        connection.close()


def main() -> None:
    try:
        test_commit_duplicate_mutation_restart_and_delete()
        print("PASS test_commit_duplicate_mutation_restart_and_delete")
        test_partial_tail_index_rebuild_corruption_and_concurrency()
        print("PASS test_partial_tail_index_rebuild_corruption_and_concurrency")
        test_growing_projection_has_bounded_checkpoint_work()
        print("PASS test_growing_projection_has_bounded_checkpoint_work")
        test_invalid_requests_do_not_append_and_writer_is_exclusive()
        print("PASS test_invalid_requests_do_not_append_and_writer_is_exclusive")
        test_hundred_thousand_record_checkpoint_is_tail_only()
        print("PASS test_hundred_thousand_record_checkpoint_is_tail_only")
        test_epoch_reuse_tamper_fallback_and_automatic_quarantine()
        print("PASS test_epoch_reuse_tamper_fallback_and_automatic_quarantine")
        test_exact_schema_attestation_falls_back_before_selection()
        print("PASS test_exact_schema_attestation_falls_back_before_selection")
    finally:
        shutil.rmtree(STATE_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
