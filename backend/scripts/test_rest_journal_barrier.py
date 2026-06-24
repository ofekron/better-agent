"""Regression test for the REST snapshot to WS journal-watermark boundary.

Run with:
    cd backend && .venv/bin/python scripts/test_rest_journal_barrier.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-rest-journal-barrier-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
from event_journal import (  # noqa: E402
    event_journal_reader,
    publish_event_sync,
)
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def main_test() -> bool:
    session = session_manager.create(name="barrier", cwd="/tmp")
    sid = session["id"]
    first = publish_event_sync(
        session_id=sid,
        event_type="command_received",
        data={"uuid": "before-barrier"},
        source="test",
    )

    original = session_manager.get_root_tree_stubbed
    inserted: list[int] = []

    def _snapshot_then_write(*args, **kwargs):
        tree = original(*args, **kwargs)
        written = publish_event_sync(
            session_id=sid,
            event_type="command_received",
            data={"uuid": "after-barrier"},
            source="test",
        )
        inserted.append(written.seq)
        return tree

    session_manager.get_root_tree_stubbed = _snapshot_then_write
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
            authenticate_client(client)
            response = client.get(f"/api/sessions/{sid}")
    finally:
        session_manager.get_root_tree_stubbed = original

    payload = response.json()
    watermark = (payload.get("max_seq_by_sid") or {}).get(sid)
    cursor = event_journal_reader.cursor(sid)
    ok = (
        response.status_code == 200
        and first.seq == 1
        and inserted == [2]
        and watermark == 1
        and cursor == 2
    )
    print(
        f"{PASS if ok else FAIL} REST watermark is capped at the "
        f"pre-snapshot writer barrier -- {watermark=} {cursor=} {inserted=}",
    )
    return ok


if __name__ == "__main__":
    try:
        sys.exit(0 if main_test() else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
