#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


home = tempfile.mkdtemp(prefix="better-agent-pending-snapshot-")
os.environ["BETTER_AGENT_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pending_user_input_projection
import user_input_store


def test_request_created_between_rest_and_ws_is_in_subscribe_snapshot() -> None:
    sid = "session-gap"
    rest_snapshot = pending_user_input_projection.snapshot(sid)
    assert rest_snapshot["requests"] == []
    rest_revision = rest_snapshot["revision"]
    request = user_input_store.create_request(
        app_session_id=sid,
        questions=[{
            "id": "decision",
            "header": "Decision",
            "question": "Continue?",
            "options": [{"label": "Yes", "description": "Continue"}],
        }],
        timeout_seconds=60,
    )
    ws_snapshot = pending_user_input_projection.snapshot(sid)
    assert ws_snapshot["app_session_id"] == sid
    assert ws_snapshot["requests"] == [request]
    assert ws_snapshot["revision"] > rest_revision


def test_resolve_after_snapshot_has_newer_revision() -> None:
    sid = "session-resolve-gap"
    request = user_input_store.create_request(
        app_session_id=sid,
        questions=[{"id": "x", "header": "X", "question": "X?", "options": []}],
        timeout_seconds=60,
    )
    stale = pending_user_input_projection.snapshot(sid)
    user_input_store.resolve_request(request["request_id"], {"x": "done"})
    resolved = pending_user_input_projection.snapshot(sid)
    assert resolved["revision"] > stale["revision"]
    assert resolved["requests"] == []


if __name__ == "__main__":
    try:
        test_request_created_between_rest_and_ws_is_in_subscribe_snapshot()
        print("PASS test_request_created_between_rest_and_ws_is_in_subscribe_snapshot")
        test_resolve_after_snapshot_has_newer_revision()
        print("PASS test_resolve_after_snapshot_has_newer_revision")
    finally:
        shutil.rmtree(home)
