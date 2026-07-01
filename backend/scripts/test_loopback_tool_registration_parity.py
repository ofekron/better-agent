from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-loopback-tool-parity-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402
import runner_codex  # noqa: E402
import runner_gemini  # noqa: E402
from runs_dir import TIMER_TOOLS  # noqa: E402


def _tool_name(tool) -> str:
    return str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))


def test_claude_native_non_user_registers_loopback_tools() -> None:
    captured_servers: dict[str, list[str]] = {}
    original_create_server = runner.create_sdk_mcp_server
    original_client = runner.ClaudeSDKClient
    original_run_one_turn = runner._run_one_turn
    original_linger = runner._linger_for_background_work

    def fake_create_sdk_mcp_server(*, name: str, version: str, tools: list):
        captured_servers[name] = [_tool_name(tool) for tool in tools]
        return {"name": name, "version": version, "tools": captured_servers[name]}

    class FakeClient:
        def __init__(self, *, options):
            self.options = options

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    async def fake_run_one_turn(**_kwargs):
        return {
            "discovered_sid": None,
            "total_usage": None,
            "error": None,
            "cancelled": False,
            "sdk_output_parts": [],
            "final_success": True,
            "context_window": None,
            "outstanding_tasks": set(),
        }

    async def fake_linger(*_args, **_kwargs):
        return None

    runner.create_sdk_mcp_server = fake_create_sdk_mcp_server  # type: ignore[assignment]
    runner.ClaudeSDKClient = FakeClient  # type: ignore[assignment]
    runner._run_one_turn = fake_run_one_turn  # type: ignore[assignment]
    runner._linger_for_background_work = fake_linger  # type: ignore[assignment]
    try:
        run_dir = Path(tempfile.mkdtemp(prefix="claude-loopback-run-"))
        code = asyncio.run(runner._run(run_dir, {
            "prompt": "reply",
            "images": [],
            "files": [],
            "cwd": "/tmp",
            "model": "sonnet",
            "permission": {"mode": "bypassPermissions"},
            "session_id": None,
            "mode": "native",
            "app_session_id": "sender-1",
            "disallowed_tools": [
                "AskUserQuestion",
                "EnterPlanMode",
                "ExitPlanMode",
                *TIMER_TOOLS,
            ],
            "setting_sources": [],
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "tok",
            "fork": False,
            "supervised": False,
            "browser_harness_enabled": False,
            "open_file_panel_enabled": False,
            "bare_config": False,
        }))
    finally:
        runner.create_sdk_mcp_server = original_create_server  # type: ignore[assignment]
        runner.ClaudeSDKClient = original_client  # type: ignore[assignment]
        runner._run_one_turn = original_run_one_turn  # type: ignore[assignment]
        runner._linger_for_background_work = original_linger  # type: ignore[assignment]

    assert code == 0
    assert "mssg" in captured_servers["communicate"]
    assert "ask" in captured_servers["communicate"]
    assert "ensure_named_worker" in captured_servers["communicate"]
    assert "delegate_task" in captured_servers["handoff"]
    assert "create_session" in captured_servers["handoff"]
    assert "create_sub_session" in captured_servers["handoff"]


def test_codex_native_non_user_registers_loopback_tools() -> None:
    tools, handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="sender-1",
        backend_url="http://127.0.0.1:8000",
        internal_token="tok",
        mssg_sender_session_id="sender-1",
        cwd="/tmp",
        model="gpt",
        open_file_panel_enabled=False,
        file_editing_mode=False,
        team_orchestration_enabled=True,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    names = {tool["name"] for tool in tools}
    assert {"mssg", "ask", "ensure_named_worker", "delegate_task", "create_session", "create_sub_session"} <= names
    assert {"mssg", "ask", "ensure_named_worker", "delegate_task", "create_session", "create_sub_session"} <= set(handlers)


def test_gemini_native_non_user_injects_communicate_mcp() -> None:
    config = runner_gemini._with_communicate_mcp({
        "app_session_id": "sender-1",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "tok",
        "cwd": "/tmp",
        "model": "gemini",
        "disabled_builtin_tools": [],
    }, {})
    communicate = config["mcp_servers"]["communicate"]
    env = communicate["env"]
    assert "communicate_mcp.py" in " ".join(communicate["args"])
    assert env["BETTER_CLAUDE_BACKEND_URL"] == "http://127.0.0.1:8000"
    assert env["BETTER_CLAUDE_INTERNAL_TOKEN"] == "tok"
    assert env["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] == "sender-1"
    assert env["BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS"] == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
