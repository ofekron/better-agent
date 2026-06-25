from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="bc-test-snapshot-warming-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.pop("BETTER_CLAUDE_HOME", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import session_store  # noqa: E402
import auth  # noqa: E402

HEADERS = {"Authorization": f"Bearer {auth.create_token('test')}"}


def _reset_summary_index() -> None:
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_store._summary_sorted_id_cache = []
    session_store._summary_sorted_cache_version = -1
    session_store._summary_order_version = 0
    session_store._summary_roots_fingerprint = None


def _write_root_session(session_id: str) -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{session_id}.json").write_text(json.dumps({
        "id": session_id,
        "name": "Existing",
        "cwd": "/tmp",
        "model": "sonnet",
        "messages": [],
        "updated_at": "2026-01-01T00:00:00",
    }))


def test_incomplete_snapshot_is_marked() -> None:
    _write_root_session("root-a")
    _reset_summary_index()
    original_wait = main._SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS
    original_warm = session_store._start_summary_index_warm
    main._SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = 0
    session_store._start_summary_index_warm = lambda: None
    try:
        body = TestClient(main.app).get("/api/sessions", headers=HEADERS).json()
    finally:
        main._SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = original_wait
        session_store._start_summary_index_warm = original_warm
    assert body["sessions"] == []
    assert body["snapshot_complete"] is False
    assert body["index_warming"] is True


def test_empty_home_snapshot_is_complete() -> None:
    for path in (Path(_TMP_HOME) / "sessions").glob("*.json"):
        path.unlink()
    _reset_summary_index()
    original_wait = main._SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS
    original_warm = session_store._start_summary_index_warm
    main._SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = 0
    session_store._start_summary_index_warm = lambda: None
    try:
        body = TestClient(main.app).get("/api/sessions", headers=HEADERS).json()
    finally:
        main._SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = original_wait
        session_store._start_summary_index_warm = original_warm
    assert body["sessions"] == []
    assert body["snapshot_complete"] is True
    assert body["index_warming"] is False


if __name__ == "__main__":
    test_incomplete_snapshot_is_marked()
    test_empty_home_snapshot_is_complete()
    print("PASS sessions snapshot warming")
