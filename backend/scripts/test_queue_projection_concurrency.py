#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="better-agent-queue-projection-")
os.environ["BETTER_AGENT_HOME"] = HOME

import session_queue_projection as projection  # noqa: E402


def _record(session_id: str, value: int) -> dict:
    return {
        "id": session_id,
        "model": "test",
        "cwd": "/tmp",
        "queued_prompts": [{"id": f"q-{value}", "content": str(value)}],
        "user_message_acks": {},
        "user_lifecycle_msg_ids": [],
        "value": value,
    }


def _reset(*, loaded: bool) -> None:
    assert projection.flush_pending_writes(timeout=5)
    with projection._load_cv:
        projection._loaded = loaded
        projection._loading = False
        projection._load_merge_floor = None
        projection._records.clear()
        projection._record_generations.clear()
        projection._deleted_generations.clear()
        projection._load_cv.notify_all()
    with projection._write_cv:
        projection._pending_writes.clear()
        projection._active_write_generations.clear()
        projection._durable_generations.clear()
        projection._write_failures.clear()


def test_cold_load_merges_upsert_delete_without_partial_read() -> None:
    _reset(loaded=False)
    entered = threading.Event()
    release = threading.Event()
    reader_done = threading.Event()
    original_load = projection._load_candidate
    original_write = projection._write_record_locked
    original_delete = projection._delete_record_durable

    def blocked_load() -> dict[str, dict]:
        entered.set()
        assert release.wait(5)
        return {"keep": _record("keep", 1), "delete": _record("delete", 1)}

    projection._load_candidate = blocked_load
    projection._write_record_locked = lambda _record, generation=None: None
    projection._delete_record_durable = lambda _sid: None
    result: dict[str, dict] = {}

    def read() -> None:
        result.update(projection.get_many(["keep", "delete", "new"]))
        reader_done.set()

    loader = threading.Thread(target=read)
    loader.start()
    assert entered.wait(5)
    second_reader = threading.Thread(target=read)
    second_reader.start()
    time.sleep(0.05)
    assert not reader_done.is_set(), "a reader observed a partial cold snapshot"

    projection.upsert_record_background(_record("new", 2))
    projection.delete_record("delete")
    release.set()
    loader.join(5)
    second_reader.join(5)
    assert not loader.is_alive() and not second_reader.is_alive()
    assert projection.get("keep") == _record("keep", 1)
    assert projection.get("new") == _record("new", 2)
    assert projection.get("delete") is None

    projection._load_candidate = original_load
    projection._write_record_locked = original_write
    projection._delete_record_durable = original_delete


def test_overwrite_during_fsync_persists_latest_generation() -> None:
    _reset(loaded=True)
    entered = threading.Event()
    release = threading.Event()
    original_write = projection._write_record_locked

    def blocked_write(record: dict, generation=None) -> None:
        if record.get("value") == 1:
            entered.set()
            assert release.wait(5)
        original_write(record, generation)

    projection._write_record_locked = blocked_write
    projection.upsert_record_background(_record("race", 1))
    assert entered.wait(5)
    read_started = time.perf_counter()
    assert projection.get("race") == _record("race", 1)
    assert time.perf_counter() - read_started < 0.1
    projection.upsert_record_background(_record("race", 2))
    release.set()
    assert projection.flush_pending_writes(timeout=5)
    durable = json.loads(projection._record_path("race").read_text())
    assert durable["value"] == 2
    assert projection.get("race")["value"] == 2
    projection._write_record_locked = original_write


def test_failed_cold_load_retries_without_losing_concurrent_mutation() -> None:
    _reset(loaded=False)
    entered = threading.Event()
    release = threading.Event()
    original_load = projection._load_candidate
    original_write = projection._write_record_locked
    calls = 0

    def load() -> dict[str, dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            raise OSError("injected cold-load failure")
        return {"disk": _record("disk", 1)}

    projection._load_candidate = load
    projection._write_record_locked = lambda _record, generation=None: None
    errors: list[BaseException] = []

    def first_read() -> None:
        try:
            projection.get("disk")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=first_read)
    thread.start()
    assert entered.wait(5)
    projection.upsert_record_background(_record("concurrent", 2))
    release.set()
    thread.join(5)
    assert len(errors) == 1 and isinstance(errors[0], OSError)
    assert projection.get("disk") == _record("disk", 1)
    assert projection.get("concurrent") == _record("concurrent", 2)
    projection._load_candidate = original_load
    projection._write_record_locked = original_write


def test_late_older_enqueue_cannot_overwrite_newer_durable_generation() -> None:
    _reset(loaded=True)
    entered = threading.Event()
    release = threading.Event()
    original_enqueue = projection._enqueue_write

    def reordered_enqueue(session_id: str, generation: int, record) -> None:
        if record is not None and record.get("value") == 1:
            entered.set()
            assert release.wait(5)
        original_enqueue(session_id, generation, record)

    projection._enqueue_write = reordered_enqueue
    errors: list[BaseException] = []

    def older_sync_write() -> None:
        try:
            projection.upsert_record(_record("enqueue-race", 1))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=older_sync_write)
    thread.start()
    assert entered.wait(5)
    projection.upsert_record_background(_record("enqueue-race", 2))
    assert projection.flush_pending_writes(timeout=5)
    release.set()
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert json.loads(projection._record_path("enqueue-race").read_text())["value"] == 2
    projection._enqueue_write = original_enqueue


def test_failure_is_dirty_and_same_generation_retries() -> None:
    _reset(loaded=True)
    original_write = projection._write_record_locked
    calls = 0

    def fail_once(record: dict, generation=None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected fsync failure")
        original_write(record, generation)

    projection._write_record_locked = fail_once
    try:
        projection.upsert_record(_record("retry", 1))
        raise AssertionError("durability failure was not surfaced")
    except RuntimeError as exc:
        assert isinstance(exc.__cause__, OSError)
    assert projection._dirty_path().exists()
    projection.upsert_record(_record("retry", 1))
    assert json.loads(projection._record_path("retry").read_text())["value"] == 1
    projection._write_record_locked = original_write


def test_offline_ack_projection_preserves_authoritative_identity() -> None:
    session = {
        "id": "offline",
        "model": "test",
        "cwd": "/tmp",
        "messages": [{
            "id": "authoritative-message",
            "role": "user",
            "content": "large private prompt that must not be copied",
            "client_id": "offline-client",
            "lifecycle_msg_id": "lifecycle-1",
            "seq": 7,
            "timestamp": "2026-07-11T00:00:00",
        }],
        "queued_prompts": [
            {"id": "duplicate", "client_id": "offline-client"},
            {"id": "pending", "client_id": "another-client"},
        ],
    }
    record = projection.project_session(session)
    assert record is not None
    assert "user_messages" not in record and "user_client_ids" not in record
    assert record["user_message_acks"] == {
        "offline-client": {
            "id": "authoritative-message",
            "client_id": "offline-client",
            "lifecycle_msg_id": "lifecycle-1",
            "seq": 7,
            "timestamp": "2026-07-11T00:00:00",
        },
    }
    assert [prompt["id"] for prompt in record["queued_prompts"]] == ["pending"]


def test_recovery_phases_are_measured_separately() -> None:
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    for metric in (
        "startup.recovery.classification",
        "startup.recovery.integration",
        "startup.recovery.projection",
        "startup.recovery.re_enqueue",
    ):
        assert source.count(f'"{metric}"') == 1
    assert source.index('"startup.recovery.classification"') < source.index(
        "startup_recovery_gate.mark_recovery_done()"
    )
    assert source.index("startup_recovery_gate.mark_recovery_done()") < source.index(
        '"startup.recovery.integration"'
    )


def main() -> None:
    tests = [
        test_cold_load_merges_upsert_delete_without_partial_read,
        test_overwrite_during_fsync_persists_latest_generation,
        test_failed_cold_load_retries_without_losing_concurrent_mutation,
        test_late_older_enqueue_cannot_overwrite_newer_durable_generation,
        test_failure_is_dirty_and_same_generation_retries,
        test_offline_ack_projection_preserves_authoritative_identity,
        test_recovery_phases_are_measured_separately,
    ]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        projection.flush_pending_writes(timeout=5)
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
