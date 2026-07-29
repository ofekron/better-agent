#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_IS_CRASH_CHILD = len(sys.argv) == 3 and sys.argv[1] == "--crash-child"
HOME = (
    os.environ["BETTER_AGENT_HOME"]
    if _IS_CRASH_CHILD
    else tempfile.mkdtemp(prefix="better-agent-queue-projection-")
)
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import session_queue_projection as projection  # noqa: E402
from session_queue_projection_runtime import (  # noqa: E402
    ProjectionRegistry,
    StorageContext,
)

STORAGE_IDENTITY = (Path(HOME) / "sessions").resolve()


def _session(index: int, *, queued: bool = False) -> dict:
    sid = f"session-{index:05d}"
    return {
        "id": sid,
        "model": "test",
        "cwd": "/tmp",
        "messages": [],
        "queued_prompts": [{"id": f"prompt-{index}", "content": "work"}] if queued else [],
        "forks": [],
    }


def _write_sessions(count: int) -> None:
    STORAGE_IDENTITY.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (STORAGE_IDENTITY / f"session-{index:05d}.json").write_text(
            json.dumps(_session(index, queued=index % 101 == 0)), encoding="utf-8",
        )


def _cold_reset() -> None:
    projection.shutdown(storage_identity=STORAGE_IDENTITY)


def test_thousands_of_sessions_rebuild_once_and_cold_load_exact() -> None:
    _write_sessions(2_500)
    started = time.perf_counter()
    assert projection.rebuild_from_disk(storage_identity=STORAGE_IDENTITY) == 2_500
    elapsed = time.perf_counter() - started
    assert elapsed < 15.0, f"transactional rebuild took {elapsed:.2f}s"
    expected = {
        f"session-{index:05d}": projection.project_session(
            _session(index, queued=index % 101 == 0)
        )
        for index in range(2_500)
    }
    _cold_reset()
    assert projection.get_many(expected, storage_identity=STORAGE_IDENTITY) == expected
    with sqlite3.connect(projection._database_path(STORAGE_IDENTITY)) as connection:
        assert connection.execute("SELECT count(*) FROM records").fetchone()[0] == 2_500
        assert connection.execute("SELECT count(*) FROM metadata WHERE key='sequence'").fetchone()[0] == 1


def test_concurrent_upsert_delete_and_queue_consumption_converge() -> None:
    scan_entered = threading.Event()
    release_scan = threading.Event()
    pause_scan = False

    def session_files() -> tuple[Path, ...]:
        if pause_scan:
            scan_entered.set()
            release_scan.wait()
        return tuple(STORAGE_IDENTITY.glob("*.json"))

    context = StorageContext.create(
        STORAGE_IDENTITY,
        session_files=session_files,
        enumerator_contract="queue-concurrency-test.v1",
        database_path=Path(HOME) / "queue-concurrency" / "projection.sqlite3",
    )
    registry = ProjectionRegistry()
    runtime = registry.acquire(context)
    runtime.rebuild(projection._project_root)
    pause_scan = True
    failures: list[BaseException] = []

    def rebuild() -> None:
        try:
            runtime.rebuild(projection._project_root)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=rebuild)
    thread.start()
    try:
        scan_entered.wait()
        runtime.upsert({"id": "during-scan", "queued_prompts": [{"id": "q"}]})
        runtime.delete(("session-00001",))
        consumed = _session(2, queued=True)
        consumed["messages"] = [{"role": "user", "client_id": "client", "id": "user"}]
        consumed["queued_prompts"][0]["client_id"] = "client"
        runtime.upsert(projection.project_session(consumed))
    finally:
        release_scan.set()
        thread.join()
    try:
        assert not failures
        expected = runtime.get_many(
            ["during-scan", "session-00001", "session-00002"]
        )
        assert expected["during-scan"]["queued_prompts"]
        assert "session-00001" not in expected
        assert expected["session-00002"]["queued_prompts"] == []
        registry.shutdown(context)
        runtime = registry.acquire(context)
        assert runtime.get_many(expected) == expected
    finally:
        registry.shutdown(context)


def test_slow_transaction_keeps_event_loop_heartbeat_alive() -> None:
    async def scenario() -> None:
        writer_entered = threading.Event()
        release_writer = threading.Event()
        loop_progressed = asyncio.Event()
        runtime = projection.runtime(STORAGE_IDENTITY)
        original_write_batch = runtime._write_batch

        def blocked_write_batch(batch) -> None:
            writer_entered.set()
            release_writer.wait()
            original_write_batch(batch)

        runtime._write_batch = blocked_write_batch
        records = [
            {"id": f"heartbeat-{index}", "queued_prompts": [], "payload": "x" * 1_000}
            for index in range(2_000)
        ]
        try:
            for record in records:
                projection.upsert_record_background(
                    record,
                    storage_identity=STORAGE_IDENTITY,
                )
            await asyncio.to_thread(writer_entered.wait)
            asyncio.get_running_loop().call_soon(loop_progressed.set)
            await loop_progressed.wait()
        finally:
            release_writer.set()
            runtime._write_batch = original_write_batch
        assert await asyncio.to_thread(
            projection.flush_pending_writes,
            storage_identity=STORAGE_IDENTITY,
        )

    asyncio.run(scenario())


def _crash_child(database: str) -> None:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT OR REPLACE INTO records(id, payload, sequence) VALUES(?, ?, ?)",
        ("crash-row", json.dumps({"id": "crash-row", "value": "new"}), 999),
    )
    print("READY", flush=True)
    sys.stdin.buffer.read(1)


def test_process_kill_mid_transaction_preserves_committed_snapshot() -> None:
    projection.upsert_record({"id": "crash-row", "value": "old"})
    process = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--crash-child",
            str(projection._database_path(STORAGE_IDENTITY)),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
    finally:
        process.kill()
        process.communicate()
    _cold_reset()
    assert projection.get(
        "crash-row",
        storage_identity=STORAGE_IDENTITY,
    )["value"] == "old"


def main() -> None:
    if _IS_CRASH_CHILD:
        _crash_child(sys.argv[2])
        return
    try:
        for test in (
            test_thousands_of_sessions_rebuild_once_and_cold_load_exact,
            test_concurrent_upsert_delete_and_queue_consumption_converge,
            test_slow_transaction_keeps_event_loop_heartbeat_alive,
            test_process_kill_mid_transaction_preserves_committed_snapshot,
        ):
            test()
            print(f"PASS {test.__name__}")
    finally:
        try:
            projection.shutdown(storage_identity=STORAGE_IDENTITY)
        finally:
            shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
