"""Phase 1: ask restart-resilience.

Proves the restart re-attach contract — a stable client-side `ask_id` lets a
retried `ask` call return the already-completed result instead of re-queueing
a duplicate prompt on the target — plus the durable completion signal
(terminal lifecycle event scan) that closes the crash-before-persist window.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ask-reattach-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_status_store
import paths
import user_msg_lifecycle
from orchestrator import Coordinator


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_ask_status_store_roundtrip():
    assert ask_status_store.read_status("ask_1") is None
    ask_status_store.write_status(
        "ask_1",
        lifecycle_msg_id="life-1",
        target_session_id="target-sess",
    )
    status = ask_status_store.read_status("ask_1")
    assert status["lifecycle_msg_id"] == "life-1"
    assert status["target_session_id"] == "target-sess"
    # merge, not overwrite
    ask_status_store.write_status("ask_1", result={"success": True})
    status = ask_status_store.read_status("ask_1")
    assert status["lifecycle_msg_id"] == "life-1"
    assert status["result"] == {"success": True}
    ask_status_store.delete_status("ask_1")
    assert ask_status_store.read_status("ask_1") is None


def test_ask_returns_cached_result_without_requeue():
    """Re-attach path: a stored result short-circuits before submit_prompt,
    so a backend-restart retry does NOT re-queue a duplicate target prompt."""
    cached = {
        "success": True,
        "target_session_id": "target-sess",
        "queued_id": "q-1",
        "response_message_id": "m-1",
        "assistant_content": "done already",
    }
    ask_status_store.write_status("ask_restart1", result=cached)

    coordinator = Coordinator()
    submit_calls: list[tuple] = []

    def _record_submit(app_session_id, params):
        submit_calls.append((app_session_id, params))
        return "q-x"

    coordinator.submit_prompt = _record_submit  # type: ignore[assignment]

    result = asyncio.run(
        coordinator.ask_team_message(
            sender_session_id="sender-sess",
            target_session_id="target-sess",
            message="hello",
            ask_id="ask_restart1",
        )
    )
    assert result == cached
    assert submit_calls == [], "reattach must not re-queue the target prompt"
    ask_status_store.delete_status("ask_restart1")


def test_terminal_event_for_lifecycle_scans_events_jsonl():
    """The durable completion signal used when the target turn finished during
    the restart window before the result was persisted."""
    from session_manager import manager as session_manager

    target = session_manager.create(name="t", cwd="/repo", orchestration_mode="native")
    root_id = session_manager._root_id_for(target["id"])
    assert root_id
    # Write under the RUNTIME ba_home() — when several test modules each set
    # BETTER_CLAUDE_HOME at import, pytest's single shared home may not equal
    # this module's _TMP_HOME, so resolve the actual home paths.ba_home() uses.
    events_path = paths.ba_home() / "sessions" / root_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)

    # No terminal event yet.
    events_path.write_text(
        json.dumps({"type": "user_message_sent", "data": {"lifecycle_msg_id": "life-x"}}) + "\n",
        encoding="utf-8",
    )
    assert user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x") is None

    # A done event for a DIFFERENT lifecycle must not match.
    events_path.write_text(
        json.dumps({"type": "user_message_done", "data": {"lifecycle_msg_id": "other"}}) + "\n",
        encoding="utf-8",
    )
    assert user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x") is None

    # Append the matching done event; scan finds it (even with older lines after).
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user_message_queued", "data": {"lifecycle_msg_id": "life-x"}}) + "\n")
        fh.write(json.dumps({"type": "user_message_done", "data": {"lifecycle_msg_id": "life-x"}}) + "\n")
        fh.write(json.dumps({"type": "agent_message", "data": {"uuid": "u1"}}) + "\n")
    terminal = user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x")
    assert terminal is not None
    assert terminal["type"] == "user_message_done"

    # A failed event is also matched.
    events_path.write_text(
        json.dumps({"type": "user_message_failed", "data": {"lifecycle_msg_id": "life-x", "error": "boom"}}) + "\n",
        encoding="utf-8",
    )
    terminal = user_msg_lifecycle.terminal_event_for_lifecycle(target["id"], "life-x")
    assert terminal is not None
    assert terminal["type"] == "user_message_failed"
