from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-session-list-no-scan-")
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402


def _record(sid: str) -> dict:
    return {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "model": "gpt-5.5",
        "cwd": "/tmp/session-list-no-scan",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [],
        "next_seq": 0,
        "created_at": "2026-07-20T00:00:00+00:00",
        "updated_at": "2026-07-20T00:00:00+00:00",
        "source": "cli",
        "user_initiated": True,
    }


def _run() -> None:
    sid = "session-list-hot-path"
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{sid}.json").write_text(json.dumps(_record(sid)), encoding="utf-8")
    session_store._ensure_summary_index(blocking=True)

    original_session_files = session_store._session_json_files
    scan_attempts = 0
    scan_lock = threading.Lock()

    def fail_if_scanned():
        nonlocal scan_attempts
        with scan_lock:
            scan_attempts += 1
        raise AssertionError("session-list projection read touched the filesystem")
        yield

    def read_projection() -> None:
        listed = session_store.list_sessions()
        ordered = session_store.ordered_session_summary_ids("updated_at")
        selected = session_store.get_session_summaries_by_ids([sid])
        indexed = session_store.get_indexed_session_summary(sid)
        indexed_many = session_store.get_indexed_session_summaries_by_ids([sid])
        assert [item["id"] for item in listed] == [sid]
        assert ordered == [sid]
        assert [item["id"] for item in selected] == [sid]
        assert indexed is not None and indexed["id"] == sid
        assert [item["id"] for item in indexed_many] == [sid]

    session_store._session_json_files = fail_if_scanned
    started = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(read_projection) for _ in range(4)]
            for future in futures:
                future.result(timeout=1.0)
    finally:
        session_store._session_json_files = original_session_files
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    elapsed = time.perf_counter() - started
    assert scan_attempts == 0, f"filesystem inventory ran {scan_attempts} times"
    assert elapsed < 1.0, f"four concurrent reads took {elapsed:.3f}s"


if __name__ == "__main__":
    _run()
    print("PASS session-list projection reads are filesystem-free")
