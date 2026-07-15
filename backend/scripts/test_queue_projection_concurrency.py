#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="better-agent-queue-projection-")
os.environ["BETTER_AGENT_HOME"] = HOME

import session_queue_projection as projection  # noqa: E402


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
    directory = Path(HOME) / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"session-{index:05d}.json").write_text(
            json.dumps(_session(index, queued=index % 101 == 0)), encoding="utf-8",
        )


def _cold_reset() -> None:
    assert projection.flush_pending_writes(timeout=10)
    with projection._load_cv:
        projection._loaded = False
        projection._loading = False
        projection._records.clear()
        projection._load_cv.notify_all()


def test_thousands_of_sessions_rebuild_once_and_cold_load_exact() -> None:
    _write_sessions(2_500)
    started = time.perf_counter()
    assert projection.rebuild_from_disk() == 2_500
    elapsed = time.perf_counter() - started
    assert elapsed < 15.0, f"transactional rebuild took {elapsed:.2f}s"
    expected = {f"session-{index:05d}": projection.project_session(_session(index, queued=index % 101 == 0)) for index in range(2_500)}
    _cold_reset()
    assert projection.get_many(expected) == expected
    with sqlite3.connect(projection._database_path()) as connection:
        assert connection.execute("SELECT count(*) FROM records").fetchone()[0] == 2_500
        assert connection.execute("SELECT count(*) FROM metadata WHERE key='sequence'").fetchone()[0] == 1


def test_concurrent_upsert_delete_and_queue_consumption_converge() -> None:
    scan_entered = threading.Event()
    release_scan = threading.Event()
    original_scan = projection._scan_complete_snapshot

    def paused_scan():
        result = original_scan()
        scan_entered.set()
        assert release_scan.wait(10)
        return result

    projection._scan_complete_snapshot = paused_scan
    thread = threading.Thread(target=projection.rebuild_from_disk)
    thread.start()
    assert scan_entered.wait(10)
    projection.upsert_record({"id": "during-scan", "queued_prompts": [{"id": "q"}]})
    projection.delete_record("session-00001")
    consumed = _session(2, queued=True)
    consumed["messages"] = [{"role": "user", "client_id": "client", "id": "user"}]
    consumed["queued_prompts"][0]["client_id"] = "client"
    projection.upsert_record(projection.project_session(consumed))
    release_scan.set()
    thread.join(20)
    projection._scan_complete_snapshot = original_scan
    assert not thread.is_alive()
    assert projection.flush_pending_writes(timeout=10)
    expected = projection.get_many(["during-scan", "session-00001", "session-00002"])
    assert expected["during-scan"]["queued_prompts"]
    assert "session-00001" not in expected
    assert expected["session-00002"]["queued_prompts"] == []
    _cold_reset()
    assert projection.get_many(expected) == expected


def test_slow_transaction_keeps_event_loop_heartbeat_alive() -> None:
    async def scenario() -> None:
        ticks = 0
        stop = False

        async def heartbeat() -> None:
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(heartbeat())
        records = [
            {"id": f"heartbeat-{index}", "queued_prompts": [], "payload": "x" * 1_000}
            for index in range(2_000)
        ]
        await asyncio.to_thread(lambda: [projection.upsert_record_background(record) for record in records])
        assert await asyncio.to_thread(projection.flush_pending_writes, 15)
        stop = True
        await task
        assert ticks >= 2, f"event loop starved; ticks={ticks}"

    asyncio.run(scenario())


def _crash_child(database: str, marker: str) -> None:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT OR REPLACE INTO records(id, payload, sequence) VALUES(?, ?, ?)",
        ("crash-row", json.dumps({"id": "crash-row", "value": "new"}), 999),
    )
    Path(marker).write_text("ready", encoding="utf-8")
    time.sleep(60)


def test_sigkill_mid_transaction_preserves_committed_snapshot() -> None:
    projection.upsert_record({"id": "crash-row", "value": "old"})
    marker = Path(HOME) / "crash-ready"
    process = subprocess.Popen([
        sys.executable, __file__, "--crash-child", str(projection._database_path()), str(marker),
    ])
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    _cold_reset()
    assert projection.get("crash-row")["value"] == "old"


def test_stale_lower_queue_revision_never_regresses_projection() -> None:
    sid = "revision-guard"
    stale = {
        "id": sid, "messages": [], "forks": [],
        "queued_prompts": [{"id": "q-stale"}], "queue_revision": 1,
    }
    newer = {
        "id": sid, "messages": [], "forks": [],
        "queued_prompts": [], "queue_revision": 2,
    }
    projection.upsert_record(projection.project_session(newer))
    projection.note_persisted_tree(stale)
    record = projection.get(sid)
    assert record["queued_prompts"] == [], f"note_persisted_tree regressed: {record}"
    assert record["queue_revision"] == 2, f"note_persisted_tree regressed: {record}"
    projection.upsert_record_background(projection.project_session(stale))
    record = projection.get(sid)
    assert record["queued_prompts"] == [], f"background upsert regressed: {record}"
    assert record["queue_revision"] == 2, f"background upsert regressed: {record}"
    # Equal-or-newer revisions still apply.
    projection.note_persisted_tree({
        "id": sid, "messages": [], "forks": [],
        "queued_prompts": [{"id": "q-new"}], "queue_revision": 3,
    })
    record = projection.get(sid)
    assert record["queue_revision"] == 3, f"newer revision refused: {record}"
    assert [p["id"] for p in record["queued_prompts"]] == ["q-new"], record


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--crash-child":
        _crash_child(sys.argv[2], sys.argv[3])
        return
    try:
        for test in (
            test_thousands_of_sessions_rebuild_once_and_cold_load_exact,
            test_concurrent_upsert_delete_and_queue_consumption_converge,
            test_slow_transaction_keeps_event_loop_heartbeat_alive,
            test_sigkill_mid_transaction_preserves_committed_snapshot,
            test_stale_lower_queue_revision_never_regresses_projection,
        ):
            test()
            print(f"PASS {test.__name__}")
    finally:
        projection.flush_pending_writes(timeout=10)
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
