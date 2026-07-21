from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("ba-test-inbox-provider-parity-")
os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "caller-session"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import communicate_mcp
import orchestration_tool_descriptions as descriptions
import orchestration_tool_schemas as schemas
import runner
import runner_codex
import session_store


def test_shared_descriptions_and_schemas() -> None:
    assert "targets send" in descriptions.INBOX_DESCRIPTION
    assert "inbox(recipient_session_id=<caller session id>" in descriptions.INBOX_DESCRIPTION
    assert "call inbox()" in descriptions.INBOX_DESCRIPTION
    assert runner._INBOX_DESCRIPTION is descriptions.INBOX_DESCRIPTION
    assert runner_codex._INBOX_DESCRIPTION is descriptions.INBOX_DESCRIPTION
    assert runner._READ_INBOX_HISTORY_DESCRIPTION is descriptions.READ_INBOX_HISTORY_DESCRIPTION
    assert runner_codex._READ_INBOX_HISTORY_DESCRIPTION is descriptions.READ_INBOX_HISTORY_DESCRIPTION
    assert runner._INBOX_INPUT_SCHEMA is schemas.INBOX_INPUT_SCHEMA
    assert runner_codex._INBOX_INPUT_SCHEMA is schemas.INBOX_INPUT_SCHEMA
    assert runner._READ_INBOX_HISTORY_INPUT_SCHEMA is schemas.READ_INBOX_HISTORY_INPUT_SCHEMA
    assert runner_codex._READ_INBOX_HISTORY_INPUT_SCHEMA is schemas.READ_INBOX_HISTORY_INPUT_SCHEMA


def test_gemini_exposes_the_same_private_contract() -> None:
    tools = {tool.name: tool for tool in communicate_mcp.build_server()._tool_manager.list_tools()}
    assert tools["inbox"].description == descriptions.INBOX_DESCRIPTION
    assert tools["read_inbox_history"].description == descriptions.READ_INBOX_HISTORY_DESCRIPTION
    assert set(tools["inbox"].parameters["properties"]) == set(
        schemas.INBOX_INPUT_SCHEMA["properties"]
    )
    history_properties = set(tools["read_inbox_history"].parameters["properties"])
    assert history_properties == set(schemas.READ_INBOX_HISTORY_INPUT_SCHEMA["properties"])
    assert "recipient_session_id" not in history_properties


def test_disable_lists_cover_both_inbox_tools() -> None:
    for disabled in (
        runner._DISABLEABLE_BUILTIN_TOOLS,
        runner_codex._DISABLEABLE_BUILTIN_TOOLS,
        communicate_mcp._DISABLEABLE_BUILTIN_TOOLS,
    ):
        assert {"inbox", "read_inbox_history"} <= disabled


def test_gemini_reads_only_the_bound_session() -> None:
    for session_id in ("sender-session", "recipient-session"):
        session_store.create_session(
            id=session_id,
            name=session_id,
            model="test-model",
            cwd="/tmp",
            orchestration_mode="native",
        )
    tools = {tool.name: tool for tool in communicate_mcp.build_server()._tool_manager.list_tools()}
    original_sender = os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"]
    try:
        os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "sender-session"
        receipt = tools["inbox"].fn(
            recipient_session_id="recipient-session",
            message="private",
        )
        assert receipt["sent"] is True
        assert tools["inbox"].fn()["count"] == 0

        os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "recipient-session"
        received = tools["inbox"].fn()
        assert received["count"] == 1
        assert received["new_messages"][0]["text"] == "private"
    finally:
        os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = original_sender


if __name__ == "__main__":
    test_shared_descriptions_and_schemas()
    test_gemini_exposes_the_same_private_contract()
    test_disable_lists_cover_both_inbox_tools()
    test_gemini_reads_only_the_bound_session()
    print("inbox provider parity tests: OK")
