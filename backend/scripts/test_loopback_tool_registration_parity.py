from __future__ import annotations

import atexit
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import _test_home

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TEST_HOME = _test_home.TestHome.acquire("bc-test-loopback-tool-parity-")
atexit.register(_TEST_HOME.release)
os.environ["BETTER_AGENT_RUNTIME_BROKER"] = "unix:/tmp/better-agent-test.sock"

import runner  # noqa: E402
import runner_codex  # noqa: E402
import installation_profile  # noqa: E402
from runs_dir import BACKGROUND_WORK_TOOLS, TIMER_TOOLS  # noqa: E402

installation_profile.integrations_enabled = lambda: True


def _tool_name(tool) -> str:
    return str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))


def _runtime_stub():
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            installation_decisions={
                "capabilities": {"integrations_enabled": True},
            },
        ),
    )


def _successful_turn() -> dict:
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


def _claude_inputs(*, bare: bool, team: bool = False) -> dict:
    return {
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
            *BACKGROUND_WORK_TOOLS,
        ],
        "setting_sources": [],
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "tok",
        "fork": False,
        "supervised": False,
        "browser_harness_enabled": False,
        "user_facing": False,
        "bare_config": bare,
        "team_orchestration_enabled": team,
    }


def test_claude_native_non_user_registers_loopback_tools() -> None:
    captured_servers: dict[str, list[str]] = {}
    original_create_server = runner.create_sdk_mcp_server
    original_client = runner.ClaudeSDKClient
    original_run_one_turn = runner._run_one_turn
    original_runtime_capabilities = runner._runtime_capabilities

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
        return _successful_turn()

    def fake_runtime_capabilities(_run_dir, _inputs, _runtime):
        return ({"mcp_servers": []}, {}, {}, "/fake/claude")

    runner.create_sdk_mcp_server = fake_create_sdk_mcp_server  # type: ignore[assignment]
    runner.ClaudeSDKClient = FakeClient  # type: ignore[assignment]
    runner._run_one_turn = fake_run_one_turn  # type: ignore[assignment]
    runner._runtime_capabilities = fake_runtime_capabilities  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory(prefix="claude-loopback-no-team-run-") as raw:
            code_without_team = asyncio.run(
                runner._run(
                    Path(raw),
                    _claude_inputs(bare=False, team=False),
                    _runtime_stub(),
                ),
            )
            without_team = {
                name: list(tools)
                for name, tools in captured_servers.items()
            }
        captured_servers.clear()
        with tempfile.TemporaryDirectory(prefix="claude-loopback-team-run-") as raw:
            code = asyncio.run(
                runner._run(
                    Path(raw),
                    _claude_inputs(bare=False, team=True),
                    _runtime_stub(),
                ),
            )
    finally:
        runner.create_sdk_mcp_server = original_create_server  # type: ignore[assignment]
        runner.ClaudeSDKClient = original_client  # type: ignore[assignment]
        runner._run_one_turn = original_run_one_turn  # type: ignore[assignment]
        runner._runtime_capabilities = original_runtime_capabilities  # type: ignore[assignment]

    assert code_without_team == 0
    assert "ensure_named_worker" not in without_team["communicate"]
    assert code == 0
    assert "mssg" in captured_servers["communicate"]
    assert "ask" in captured_servers["communicate"]
    assert "ensure_named_worker" in captured_servers["communicate"]
    assert "list_available_provider_models" in captured_servers["communicate"]
    assert "delegate_task" in captured_servers["handoff"]
    assert "create_session" in captured_servers["handoff"]
    assert "create_sub_session" in captured_servers["handoff"]


def test_claude_bare_native_bridges_extension_mcp_tools() -> None:
    captured_servers: dict[str, list[str]] = {}
    captured_tools: dict[str, list] = {}
    captured_options = {}
    original_create_server = runner.create_sdk_mcp_server
    original_client = runner.ClaudeSDKClient
    original_run_one_turn = runner._run_one_turn
    original_runtime_capabilities = runner._runtime_capabilities
    original_mcp_list_tools = runner._mcp_list_tools
    original_mcp_call_tool = runner._mcp_call_tool

    def fake_create_sdk_mcp_server(*, name: str, version: str, tools: list):
        captured_tools[name] = tools
        captured_servers[name] = [_tool_name(tool) for tool in tools]
        return {"type": "sdk", "name": name, "tools": captured_servers[name]}

    class FakeClient:
        def __init__(self, *, options):
            captured_options["mcp_servers"] = options.mcp_servers

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    async def fake_run_one_turn(**_kwargs):
        return _successful_turn()

    def fake_runtime_capabilities(_run_dir, _inputs, _runtime):
        return (
            {
                "mcp_servers": [
                    {
                        "name": "runtime-owned",
                        "config": {
                            "runtime": {"type": "runtime"},
                            "native": {
                                "command": "/fake/native-runtime-owned",
                                "args": [],
                                "env": {},
                            },
                        },
                    },
                    {
                        "name": "testape",
                        "config": {
                            "native": {
                                "command": "/fake/testape-mcp",
                                "args": [],
                                "env": {
                                    "BETTER_CLAUDE_EXTENSION_ID": "ofek.testape",
                                    "BETTER_CLAUDE_BARE_CONFIG": "1",
                                },
                            },
                        },
                    },
                ],
            },
            {},
            {},
            "/fake/claude",
        )

    async def fake_mcp_list_tools(server_name: str, config: dict):
        assert server_name in {"testape", "runtime-owned"}
        if server_name == "testape":
            assert config["command"] == "/fake/testape-mcp"
        assert "BETTER_CLAUDE_INTERNAL_TOKEN" not in config.get("env", {})
        if server_name != "testape":
            return []
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI test",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "repo_path": {"type": "string"},
                },
            },
        }]

    async def fake_mcp_call_tool(config: dict, tool_name: str, args: dict):
        assert config["command"] == "/fake/testape-mcp"
        assert "BETTER_CLAUDE_INTERNAL_TOKEN" not in config.get("env", {})
        assert tool_name == "test_ui"
        assert args == {"task": "check", "repo_path": "/repo"}
        return {"content": [{"type": "text", "text": "ok"}]}

    runner.create_sdk_mcp_server = fake_create_sdk_mcp_server  # type: ignore[assignment]
    runner.ClaudeSDKClient = FakeClient  # type: ignore[assignment]
    runner._run_one_turn = fake_run_one_turn  # type: ignore[assignment]
    runner._runtime_capabilities = fake_runtime_capabilities  # type: ignore[assignment]
    runner._mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    runner._mcp_call_tool = fake_mcp_call_tool  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory(
            prefix="claude-bare-extension-mcp-run-",
        ) as raw:
            code = asyncio.run(
                runner._run(
                    Path(raw),
                    _claude_inputs(bare=True),
                    _runtime_stub(),
                ),
            )
        call_result = asyncio.run(
            captured_tools["testape"][0].handler({
                "task": "check",
                "repo_path": "/repo",
            }),
        )
    finally:
        runner.create_sdk_mcp_server = original_create_server  # type: ignore[assignment]
        runner.ClaudeSDKClient = original_client  # type: ignore[assignment]
        runner._run_one_turn = original_run_one_turn  # type: ignore[assignment]
        runner._runtime_capabilities = original_runtime_capabilities  # type: ignore[assignment]
        runner._mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]
        runner._mcp_call_tool = original_mcp_call_tool  # type: ignore[assignment]

    assert code == 0
    assert captured_servers["testape"] == ["test_ui"]
    assert captured_options["mcp_servers"]["testape"]["type"] == "sdk"
    assert captured_options["mcp_servers"]["runtime-owned"]["type"] == "runtime"
    assert call_result == {"content": [{"type": "text", "text": "ok"}]}


def test_bridged_mcp_call_normalizes_structured_result() -> None:
    original_json_request = runner._mcp_json_request

    async def fake_json_request(_config: dict, method: str, params: dict, *, timeout: float):
        assert method == "tools/call"
        assert params == {"name": "test_ui", "arguments": {"task": "check"}}
        assert timeout == runner._MCP_CALL_TIMEOUT_S
        return {"structuredContent": {"ok": True}}

    runner._mcp_json_request = fake_json_request  # type: ignore[assignment]
    try:
        result = asyncio.run(runner._mcp_call_tool({"command": "unused"}, "test_ui", {"task": "check"}))
    finally:
        runner._mcp_json_request = original_json_request  # type: ignore[assignment]

    assert result["content"] == [{"type": "text", "text": '{\n  "ok": true\n}'}]
    assert result["structuredContent"] == {"ok": True}


def test_requirements_wait_true_uses_long_bridged_mcp_timeout() -> None:
    original_json_request = runner._mcp_json_request

    async def fake_json_request(_config: dict, method: str, params: dict, *, timeout: float):
        assert method == "tools/call"
        assert params == {"name": "fire_get_requirements", "arguments": {"query": "q", "wait": True}}
        assert timeout == runner._REQUIREMENTS_WAIT_TRUE_MCP_CALL_TIMEOUT_S
        return {"structuredContent": {"success": True}}

    runner._mcp_json_request = fake_json_request  # type: ignore[assignment]
    try:
        result = asyncio.run(
            runner._mcp_call_tool(
                {"command": "unused", "tool_timeout_sec": runner._REQUIREMENTS_WAIT_TRUE_MCP_CALL_TIMEOUT_S},
                "fire_get_requirements",
                {"query": "q", "wait": True},
            )
        )
    finally:
        runner._mcp_json_request = original_json_request  # type: ignore[assignment]

    assert result["structuredContent"] == {"success": True}


def test_codex_native_non_user_registers_loopback_tools() -> None:
    without_team_tools, without_team_handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="sender-1",
        backend_url="http://127.0.0.1:8000",
        internal_token="tok",
        mssg_sender_session_id="sender-1",
        cwd="/tmp",
        model="gpt",
        user_facing=False,
        request_user_input_enabled=False,
        file_editing_mode=False,
        team_orchestration_enabled=False,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    tools, handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="sender-1",
        backend_url="http://127.0.0.1:8000",
        internal_token="tok",
        mssg_sender_session_id="sender-1",
        cwd="/tmp",
        model="gpt",
        user_facing=False,
        request_user_input_enabled=False,
        file_editing_mode=False,
        team_orchestration_enabled=True,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    without_team_names = {tool["name"] for tool in without_team_tools}
    assert "ensure_named_worker" not in without_team_names
    assert "ensure_named_worker" not in without_team_handlers
    names = {tool["name"] for tool in tools}
    expected = {
        "mssg",
        "ask",
        "ensure_named_worker",
        "list_available_provider_models",
        "delegate_task",
        "create_session",
        "create_sub_session",
    }
    assert expected <= names
    assert expected <= set(handlers)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
