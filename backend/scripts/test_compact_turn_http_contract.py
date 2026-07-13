#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path


os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="better-agent-compact-http-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Response
import main


def test_compact_endpoint_is_no_store_and_defaults_to_five() -> None:
    original_page = main.session_manager.get_compact_turn_page
    original_pending = main.pending_user_input_projection.snapshot
    captured: dict[str, int] = {}
    try:
        def page(_sid: str, *, turn_limit: int, before_seq=None):
            captured["limit"] = turn_limit
            return {"turns": [], "page_cursor": {}}
        main.session_manager.get_compact_turn_page = page
        main.pending_user_input_projection.snapshot = lambda _sid: {"requests": [], "revision": 0}
        response = Response()
        default_limit = inspect.signature(main.get_compact_turns).parameters["limit"].default
        assert default_limit.default == 5
        asyncio.run(main.get_compact_turns("session", response, limit=5, before_seq=None))
        assert captured["limit"] == 5
        assert response.headers["cache-control"] == "no-store"
    finally:
        main.session_manager.get_compact_turn_page = original_page
        main.pending_user_input_projection.snapshot = original_pending


def test_subscribe_run_state_journal_dependency_is_bound() -> None:
    assert main._current_event_journal_seq("missing-session") is None


if __name__ == "__main__":
    test_compact_endpoint_is_no_store_and_defaults_to_five()
    print("PASS test_compact_endpoint_is_no_store_and_defaults_to_five")
    test_subscribe_run_state_journal_dependency_is_bound()
    print("PASS test_subscribe_run_state_journal_dependency_is_bound")
