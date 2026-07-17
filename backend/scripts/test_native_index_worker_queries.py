from __future__ import annotations

import atexit
import multiprocessing
import os
import shutil
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-nti-worker-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import native_transcript_index as idx


def _seed_row(conn, text: str) -> None:
    row = (
        text, "/tmp/t.jsonl", "sid1", "/tmp", "claude", "assistant_text", "",
        "2026-07-17T00:00:00Z", "assistant", "el-1", 0,
        "h1", "n1", "", "", "", len(text), len(text),
    )
    cursor = conn.execute(
        f"INSERT INTO native_element_fts({', '.join(idx._FTS_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in idx._FTS_COLUMNS)})",
        row,
    )
    rowid = cursor.lastrowid
    conn.execute("INSERT INTO native_element_path(rowid, path) VALUES (?, ?)", (rowid, row[1]))
    conn.execute(
        f"INSERT INTO native_element_meta(rowid, {', '.join(idx._META_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in range(len(idx._META_COLUMNS) + 1))})",
        (rowid, *row[1:]),
    )
    conn.commit()


def _serve_query_child(ready_path: str) -> None:
    import native_transcript_index as child_idx

    conn = child_idx._writer_connection()
    _seed_row(conn, "worker served text")
    child_idx.ensure_fresh_for_read = lambda timeout=0.0: {"covered": True}
    child_idx._start_query_server()
    with open(ready_path, "w", encoding="utf-8") as handle:
        handle.write("ready")
    child_idx._stop.wait(30.0)


def _start_server_child() -> multiprocessing.Process:
    ready_path = os.path.join(_TMP_HOME, "query-server-ready")
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_serve_query_child, args=(ready_path,))
    process.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if os.path.exists(ready_path) and idx._read_query_token() is not None:
            return process
        time.sleep(0.02)
    process.terminate()
    raise AssertionError("query server child did not become ready")


def test_query_roundtrip_via_worker_endpoint() -> None:
    process = _start_server_child()
    try:
        result = idx._query_via_worker(
            "SELECT m.rowid, e.text FROM native_element_meta m "
            "CROSS JOIN native_element_fts e ON e.rowid = m.rowid",
            (),
            timeout_s=10.0,
            max_result_bytes=idx.SQL_RESULT_MAX_BYTES,
        )
        assert not result.get("error"), result
        assert result["rows"] and result["rows"][0][1] == "worker served text"

        rejected = idx._query_via_worker(
            "DELETE FROM native_element_meta", (), timeout_s=5.0,
            max_result_bytes=idx.SQL_RESULT_MAX_BYTES,
        )
        assert rejected.get("error"), "non-SELECT must be rejected by the worker"
    finally:
        process.terminate()
        process.join(timeout=5)


def test_worker_down_fails_closed_without_local_execution() -> None:
    # No live endpoint: the backend must return worker_unavailable, never
    # execute the heavy SQL in-process, and never blow up.
    idx._query_token_path().unlink(missing_ok=True)
    original_ensure = idx.ensure_started
    idx.ensure_started = lambda: None
    try:
        result = idx._query_via_worker(
            "SELECT COUNT(*) FROM native_element_meta", (), timeout_s=5.0,
            max_result_bytes=idx.SQL_RESULT_MAX_BYTES,
        )
        assert result.get("error_code") == "worker_unavailable", result
    finally:
        idx.ensure_started = original_ensure


def test_production_mode_routes_to_worker_endpoint() -> None:
    # In production (non-test-mode) the public API must route to the worker
    # client, never execute locally in the backend process.
    sentinel = {"routed": "worker"}
    original_client = idx._query_via_worker
    idx._query_via_worker = lambda *args, **kwargs: sentinel
    original_env = os.environ.pop("BETTER_AGENT_TEST_MODE", None)
    outcome: dict = {}

    def call() -> None:
        outcome["result"] = idx.run_readonly_sql("SELECT 1")

    try:
        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=10)
        assert outcome.get("result") is sentinel, outcome
    finally:
        if original_env is not None:
            os.environ["BETTER_AGENT_TEST_MODE"] = original_env
        idx._query_via_worker = original_client


def test_admission_is_bounded() -> None:
    original_timeout = idx._QUERY_ADMISSION_TIMEOUT_SECONDS
    idx._QUERY_ADMISSION_TIMEOUT_SECONDS = 0.05
    holders = [idx._query_admission.acquire(timeout=1) for _ in range(idx._QUERY_MAX_CONCURRENT)]
    try:
        assert all(holders)
        result = idx._execute_query_request({
            "op": "sql",
            "sql": "SELECT COUNT(*) FROM native_element_meta",
            "params": [],
            "timeout_s": 5.0,
            "max_result_bytes": idx.SQL_RESULT_MAX_BYTES,
        })
        assert result.get("error_code") == "admission_timeout", result
    finally:
        for held in holders:
            if held:
                idx._query_admission.release()
        idx._QUERY_ADMISSION_TIMEOUT_SECONDS = original_timeout


def test_request_validation_fails_closed() -> None:
    for request in (
        None,
        {"op": "nope"},
        {"op": "sql", "sql": 7},
        {"op": "sql", "sql": "SELECT 1", "params": [[1]]},
        {"op": "sql", "sql": "SELECT 1", "timeout_s": "NaN"},
    ):
        result = idx._execute_query_request(request)
        assert result.get("error"), request


def main_test() -> None:
    test_query_roundtrip_via_worker_endpoint()
    test_worker_down_fails_closed_without_local_execution()
    test_production_mode_routes_to_worker_endpoint()
    test_admission_is_bounded()
    test_request_validation_fails_closed()
    print("PASS: native index worker-process query service")


if __name__ == "__main__":
    main_test()
