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
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_HOME = Path(tempfile.mkdtemp(prefix="better-agent-jsonl-store-"))
os.environ["BETTER_AGENT_HOME"] = str(STATE_HOME)
sys.path.insert(0, str(ROOT / "backend"))

from chat_projection_store import ChatProjectionStoreError, ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_jsonl import JsonlChatProjectionStore, _record_line
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
    duplicate = store.commit(request(sequence=2))
    assert duplicate.duplicate and path.read_bytes().count(b"\n") == lines_before
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

    index_path = path.with_name(f"{path.name}.index.sqlite3")
    index_path.unlink()
    rebuilt = JsonlChatProjectionStore(path)
    assert rebuilt.projection_cursor("root", 0) == 1
    rebuilt.close()

    crash_path = STATE_HOME / "chat" / "crash-window.jsonl"
    crash = JsonlChatProjectionStore(crash_path)
    crash.select_generation("root", 0)
    crash.commit(request())
    crash.close()
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
    for _ in range(2):
        recovered = JsonlChatProjectionStore(crash_path)
        assert recovered.projection_cursor("root", 0) == 2
        assert recovered.read_projection("root", 0, second_request.event_id).render_node["text"] == "answer-2"
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
    assert_error("storage_corrupt", lambda: JsonlChatProjectionStore(path))


def main() -> None:
    try:
        test_commit_duplicate_mutation_restart_and_delete()
        print("PASS test_commit_duplicate_mutation_restart_and_delete")
        test_partial_tail_index_rebuild_corruption_and_concurrency()
        print("PASS test_partial_tail_index_rebuild_corruption_and_concurrency")
    finally:
        shutil.rmtree(STATE_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
