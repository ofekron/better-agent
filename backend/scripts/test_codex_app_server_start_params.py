#!/usr/bin/env python3
import asyncio
import json
import tempfile
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import runner_codex
from scripts.codex_execution_test_support import runner_authority


class _FakeMapped:
    async def put(self, _data: bytes) -> None:
        return None


class _FakeAppServerProcess:
    requests: list[tuple[str, dict]]
    notifications: list[tuple[str, dict]]
    tool_handlers: dict

    def __init__(self, _proc, _run_dir: Path, *, tool_handlers=None, approval_ctx=None):
        del approval_ctx
        self.thread_id = None
        self.requests = []
        self.notifications = []
        self.tool_handlers = tool_handlers or {}
        self._mapped = _FakeMapped()

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        names = [
            str(tool.get("name") or "")
            for tool in params.get("dynamicTools") or []
        ]
        if len(names) != len(set(names)):
            raise RuntimeError("request_user_input already registered")
        if method in ("thread/start", "thread/resume", "thread/fork"):
            return {"thread": {"id": "thread-1"}}
        return {}

    async def notify(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))


class _FakeProcess:
    returncode = None
    pid = 123

    async def wait(self) -> int:
        self.returncode = 0
        return 0


async def _fake_create_subprocess_exec(*_args, **_kwargs):
    return _FakeProcess()


async def test_app_server_uses_structured_sandbox_policy() -> None:
    created_clients, _argv = await _record_start_app_server(
        session_id=None,
        dynamic_tools=None,
        provider_run_config=None,
    )

    client = created_clients[0]
    thread_start = next(params for method, params in client.requests if method == "thread/start")
    turn_start = next(params for method, params in client.requests if method == "turn/start")
    expected_policy = {"type": "dangerFullAccess"}

    assert thread_start["sandboxPolicy"] == expected_policy
    assert turn_start["sandboxPolicy"] == expected_policy
    assert "sandbox" not in thread_start


async def test_app_server_resume_receives_capability_config() -> None:
    async def tool_handler(_params: dict) -> dict:
        return {"ok": True}

    created_clients, _argv = await _record_start_app_server(
        session_id="thread-existing",
        dynamic_tools=[{"name": "tool_x", "description": "Tool X", "inputSchema": {"type": "object"}}],
        tool_handlers={"tool_x": tool_handler},
        provider_run_config={"mcp_servers": {"server-x": {"command": "echo", "args": ["ok"]}}},
    )

    client = created_clients[0]
    resume = next(params for method, params in client.requests if method == "thread/resume")
    assert resume["threadId"] == "thread-existing"
    assert resume["dynamicTools"][0]["name"] == "tool_x"
    assert resume["dynamicTools"][0]["type"] == "function"
    assert resume["config"]["mcpServers"]["server-x"]["command"] == "echo"
    assert client.tool_handlers["tool_x"] is tool_handler


async def test_initialize_timeout_retries_after_full_client_cleanup() -> None:
    original_create_subprocess_exec = runner_codex.asyncio.create_subprocess_exec
    original_app_server_process = runner_codex._AppServerProcess
    original_process_control = runner_codex._process_control
    created_clients = []
    stopped_pids = []

    class RetryProcess(_FakeProcess):
        next_pid = 500

        def __init__(self):
            self.returncode = None
            self.pid = self.next_pid
            type(self).next_pid += 1

    class RetryClient(_FakeAppServerProcess):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._proc = args[0]
            self.reader_done = False
            self.steer_done = False
            self.stderr_done = False
            created_clients.append(self)

        async def request(self, method: str, params: dict) -> dict:
            self.requests.append((method, params))
            if method == "initialize" and len(created_clients) == 1:
                raise TimeoutError("codex app-server request timed out: initialize")
            if method == "thread/resume":
                return {"thread": {"id": "thread-existing"}}
            return {}

        async def wait(self) -> int:
            await self._proc.wait()
            self.reader_done = True
            self.steer_done = True
            self.stderr_done = True
            return 0

    class RetryProcessControl:
        def detach_spawn_kwargs(self) -> dict:
            return {}

        def signal_stop(self, pid: int) -> None:
            stopped_pids.append(pid)

        def force_kill(self, _pid: int) -> None:
            raise AssertionError("graceful cleanup should reap the fake process")

    async def create_retry_process(*_args, **_kwargs):
        return RetryProcess()

    try:
        runner_codex.asyncio.create_subprocess_exec = create_retry_process
        runner_codex._AppServerProcess = RetryClient
        runner_codex._process_control = lambda: RetryProcessControl()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            client = await runner_codex._start_app_server_with_retry(
                "codex",
                run_dir=tmp_path,
                cwd=str(tmp_path),
                model="gpt-5",
                reasoning_effort="low",
                session_id="thread-existing",
                turn_input=[],
                cancel_path=tmp_path / "cancel",
            )
    finally:
        runner_codex.asyncio.create_subprocess_exec = original_create_subprocess_exec
        runner_codex._AppServerProcess = original_app_server_process
        runner_codex._process_control = original_process_control

    assert len(created_clients) == 2
    first, second = created_clients
    assert stopped_pids == [first._proc.pid]
    assert first.reader_done and first.steer_done and first.stderr_done
    assert [method for method, _params in first.requests] == ["initialize"]
    assert [method for method, _params in second.requests] == [
        "initialize",
        "thread/resume",
        "turn/start",
    ]
    assert client is second


async def test_app_server_wait_joins_owned_tasks() -> None:
    async def pending() -> None:
        await asyncio.Future()

    client = object.__new__(runner_codex._AppServerProcess)
    client._proc = _FakeProcess()
    client._reader_task = asyncio.create_task(pending())
    client._steer_task = asyncio.create_task(pending())
    client._stderr_task = asyncio.create_task(asyncio.sleep(0))
    client._pending_tool_calls = set()

    await client.wait()

    assert client._reader_task.done()
    assert client._steer_task.done()
    assert client._stderr_task.done()


async def test_initialize_timeout_does_not_retry_after_cancel() -> None:
    original_start = runner_codex._start_app_server
    attempts = 0
    with tempfile.TemporaryDirectory() as tmp:
        cancel_path = Path(tmp) / "cancel"

        async def timeout_then_cancel(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            cancel_path.touch()
            raise TimeoutError(runner_codex.APP_SERVER_INITIALIZE_TIMEOUT)

        try:
            runner_codex._start_app_server = timeout_then_cancel
            try:
                await runner_codex._start_app_server_with_retry(
                    "codex",
                    cancel_path=cancel_path,
                )
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancellation should stop app-server retry")
        finally:
            runner_codex._start_app_server = original_start

    assert attempts == 1


async def test_bridges_selected_extension_mcp_tools_as_dynamic_tools() -> None:
    original_launcher_configs = runner_codex.extension_store.native_mcp_server_configs
    original_mcp_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools
    original_mcp_call_tool = runner_codex.mcp_stdio_bridge.mcp_call_tool

    def fake_launcher_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        assert user_facing is True
        assert bare is False
        return {
            "testape": {
                "command": "/fake/testape-mcp",
                "args": [],
                "env": {"BETTER_CLAUDE_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_mcp_list_tools(server_name: str, config: dict):
        assert server_name == "testape"
        assert config["_server_name"] == "testape"
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI test",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "repo_path": {"type": "string"},
                },
                "required": ["task", "repo_path"],
            },
        }]

    async def fake_mcp_call_tool(config: dict, tool_name: str, args: dict):
        assert config["command"] == "/fake/testape-mcp"
        assert config["_server_name"] == "testape"
        assert tool_name == "test_ui"
        assert args == {"task": "check", "repo_path": "/repo"}
        return {"content": [{"type": "text", "text": "ok"}]}

    runner_codex.extension_store.native_mcp_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    runner_codex.mcp_stdio_bridge.mcp_call_tool = fake_mcp_call_tool  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        tool_handlers: dict = {}
        existing_tool_names: set[str] = set()
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config={
                "mcp_servers": {
                    "testape": {
                        "command": "/fake/testape-mcp",
                        "args": [],
                        "env": {"BETTER_CLAUDE_EXTENSION_ID": "ofek.testape"},
                    },
                },
            },
            dynamic_tools=dynamic_tools,
            tool_handlers=tool_handlers,
            existing_tool_names=existing_tool_names,
            user_facing=True,
            bare_config=False,
        )
        result = await tool_handlers["test_ui"]({
            "arguments": {"task": "check", "repo_path": "/repo"},
        })
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"dynamic_tools": []}}) + "\n",
                encoding="utf-8",
            )
            missing, missing_error = runner_codex._dynamic_tools_missing_from_rollout(
                rollout,
                dynamic_tools,
            )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]
        runner_codex.mcp_stdio_bridge.mcp_call_tool = original_mcp_call_tool  # type: ignore[assignment]

    assert [tool["name"] for tool in dynamic_tools] == ["test_ui"]
    assert [tool["name"] for tool in missing] == ["test_ui"]
    assert missing_error is None
    assert dynamic_tools[0]["inputSchema"]["required"] == ["task", "repo_path"]
    assert "test_ui" in existing_tool_names
    assert dynamic_tools
    assert result == {
        "contentItems": [{
            "type": "inputText",
            "text": '{"content":[{"type":"text","text":"ok"}]}',
        }],
        "success": True,
    }


async def test_bridge_uses_selected_extension_when_provider_config_has_only_skills() -> None:
    original_launcher_configs = runner_codex.extension_store.native_mcp_server_configs
    original_mcp_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_launcher_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        assert user_facing is True
        assert bare is False
        return {
            "testape": {
                "command": "/fake/testape-launcher",
                "args": [],
                "env": {"BETTER_CLAUDE_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_mcp_list_tools(server_name: str, _config: dict):
        assert server_name == "testape"
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI test",
            "inputSchema": {"type": "object"},
        }]

    runner_codex.extension_store.native_mcp_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={
                "provider_kind": "codex",
                "app_session_id": "sender-1",
                "extra_mcp_servers": ["testape"],
            },
            provider_run_config={"skills": {"implement": "instructions"}},
            dynamic_tools=dynamic_tools,
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]

    assert [tool["name"] for tool in dynamic_tools] == ["test_ui"]


async def test_bridge_preserves_configured_extension_during_resolution_race() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools
    list_attempts = 0

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {}

    async def fake_list_tools(server_name: str, config: dict):
        nonlocal list_attempts
        list_attempts += 1
        raise AssertionError(f"unexpected tools/list for {server_name}: {config}")

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        provider_run_config = {
            "mcp_servers": {
                "testape": {
                    "command": "/configured/testape-mcp",
                    "args": [],
                    "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
                    "tool_names": ["test_ui"],
                },
            },
        }
        dynamic_tools: list[dict] = []
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config=provider_run_config,
            dynamic_tools=dynamic_tools,
            tool_handlers={},
            existing_tool_names={"test_ui"},
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]

    assert dynamic_tools == []
    assert provider_run_config["mcp_servers"]["testape"]["command"] == "/configured/testape-mcp"
    assert list_attempts == 0


async def test_bridge_probes_extension_servers_concurrently() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools
    both_started = asyncio.Event()
    active = 0

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "first": {"command": "/fake/first", "args": [], "env": {}},
            "second": {"command": "/fake/second", "args": [], "env": {}},
        }

    async def fake_list_tools(server_name: str, _config: dict):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.2)
        return [{
            "name": f"{server_name}_tool",
            "description": "",
            "inputSchema": {"type": "object"},
        }]

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config={},
            dynamic_tools=dynamic_tools,
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]

    assert [tool["name"] for tool in dynamic_tools] == [
        "first_tool",
        "second_tool",
    ]


async def test_bridge_preserves_custom_same_name_mcp_server() -> None:
    original_launcher_configs = runner_codex.extension_store.native_mcp_server_configs
    original_mcp_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_launcher_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/fake/testape-mcp",
                "args": [],
                "env": {"BETTER_CLAUDE_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_mcp_list_tools(_server_name: str, _config: dict):
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI test",
            "inputSchema": {"type": "object"},
        }]

    runner_codex.extension_store.native_mcp_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        tool_handlers: dict = {}
        existing_tool_names: set[str] = set()
        provider_run_config = {
            "mcp_servers": {
                "testape": {
                    "command": "/custom/testape",
                    "args": [],
                    "env": {},
                },
            },
        }
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config=provider_run_config,
            dynamic_tools=dynamic_tools,
            tool_handlers=tool_handlers,
            existing_tool_names=existing_tool_names,
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]

    assert [tool["name"] for tool in dynamic_tools] == ["test_ui"]
    assert provider_run_config["mcp_servers"]["testape"]["command"] == "/custom/testape"


async def test_bridge_prunes_extension_owned_mcp_server_after_registration() -> None:
    original_launcher_configs = runner_codex.extension_store.native_mcp_server_configs
    original_mcp_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_launcher_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/fake/testape-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_mcp_list_tools(_server_name: str, _config: dict):
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI test",
            "inputSchema": {"type": "object"},
        }]

    runner_codex.extension_store.native_mcp_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        tool_handlers: dict = {}
        existing_tool_names: set[str] = set()
        provider_run_config = {
            "mcp_servers": {
                "testape": {
                    "command": "/fake/testape-mcp",
                    "args": [],
                    "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
                },
            },
        }
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config=provider_run_config,
            dynamic_tools=dynamic_tools,
            tool_handlers=tool_handlers,
            existing_tool_names=existing_tool_names,
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]

    assert [tool["name"] for tool in dynamic_tools] == ["test_ui"]
    assert "testape" not in provider_run_config["mcp_servers"]


async def test_bridge_atomically_quarantines_partially_valid_ambient_server() -> None:
    original_launcher_configs = runner_codex.extension_store.native_mcp_server_configs
    original_mcp_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_launcher_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/fake/testape-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_mcp_list_tools(_server_name: str, _config: dict):
        return [
            {
                "name": "test_ui",
                "description": "Run TestApe UI test",
                "inputSchema": {"type": "object"},
            },
            {
                "name": "broken_tool",
                "description": "Broken",
                "inputSchema": None,
            },
        ]

    runner_codex.extension_store.native_mcp_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        tool_handlers: dict = {}
        existing_tool_names: set[str] = set()
        provider_run_config = {
            "mcp_servers": {
                "testape": {
                    "command": "/fake/testape-mcp",
                    "args": [],
                    "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
                },
            },
        }
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config=provider_run_config,
            dynamic_tools=dynamic_tools,
            tool_handlers=tool_handlers,
            existing_tool_names=existing_tool_names,
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]

    assert dynamic_tools == []
    assert tool_handlers == {}
    assert existing_tool_names == set()
    assert "testape" not in provider_run_config["mcp_servers"]


async def test_bridge_continues_when_selected_extension_cannot_list_tools() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "working": {
                "command": "/resolved/working-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.working"},
            },
            "testape": {
                "command": "/resolved/testape-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_list_tools(server_name: str, _config: dict):
        if server_name == "working":
            return [{
                "name": "working_tool",
                "description": "Working",
                "inputSchema": {"type": "object"},
            }]
        raise RuntimeError("server startup failed")

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config={"skills": {"implement": "instructions"}},
            dynamic_tools=dynamic_tools,
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
        assert [tool["name"] for tool in dynamic_tools] == ["working_tool"]
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]


async def test_bridge_quarantines_configured_extension_that_cannot_list_tools() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/resolved/testape-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_list_tools(_server_name: str, _config: dict):
        raise RuntimeError("server startup failed")

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        provider_run_config = {
            "mcp_servers": {
                "testape": {
                    "command": "/resolved/testape-mcp",
                    "args": [],
                    "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
                },
                "custom": {
                    "command": "/custom/mcp",
                    "args": [],
                },
            },
        }
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config=provider_run_config,
            dynamic_tools=[],
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
        assert provider_run_config["mcp_servers"] == {
            "custom": {
                "command": "/custom/mcp",
                "args": [],
            },
        }
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]


async def test_bridge_continues_when_selected_extension_exposes_no_tools() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/resolved/testape-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_list_tools(_server_name: str, _config: dict):
        return []

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config={},
            dynamic_tools=[],
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]


async def test_bridge_quarantines_configured_extension_that_exposes_no_tools() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "provider-config-sync": {
                "command": "/resolved/provider-config-sync-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "better-agent.provider-config-sync"},
            },
        }

    async def fake_list_tools(_server_name: str, _config: dict):
        return []

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        provider_run_config = {
            "mcp_servers": {
                "provider-config-sync": {
                    "command": "/resolved/provider-config-sync-mcp",
                    "args": [],
                    "env": {"BETTER_AGENT_EXTENSION_ID": "better-agent.provider-config-sync"},
                },
            },
        }
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config=provider_run_config,
            dynamic_tools=[],
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
        assert provider_run_config["mcp_servers"] == {}
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]


async def test_bridge_fails_closed_for_required_same_extension_discovery() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs
    original_required = runner_codex.extension_store.required_profile_mcp_server_names
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/resolved/testape-mcp",
                "args": [],
                "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
            },
        }

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    runner_codex.extension_store.required_profile_mcp_server_names = lambda _inputs: {"testape"}  # type: ignore[method-assign]
    try:
        provider_run_config = {
            "mcp_servers": {
                "testape": {
                    "command": "/resolved/testape-mcp",
                    "args": [],
                    "env": {"BETTER_AGENT_EXTENSION_ID": "ofek.testape"},
                },
            },
        }

        async def failed_list(_server_name: str, _config: dict):
            raise RuntimeError("server startup failed")

        runner_codex.mcp_stdio_bridge.mcp_list_tools = failed_list  # type: ignore[assignment]
        try:
            await runner_codex._bridge_extension_mcp_dynamic_tools(
                inputs={"provider_kind": "codex"},
                provider_run_config=provider_run_config,
                dynamic_tools=[],
                tool_handlers={},
                existing_tool_names=set(),
                user_facing=True,
                bare_config=False,
            )
        except RuntimeError as exc:
            assert str(exc) == (
                "required extension MCP 'testape' tools/list failed: "
                "server startup failed"
            )
        else:
            raise AssertionError("required transport failure did not fail closed")
        assert "testape" in provider_run_config["mcp_servers"]

        async def empty_list(_server_name: str, _config: dict):
            return []

        runner_codex.mcp_stdio_bridge.mcp_list_tools = empty_list  # type: ignore[assignment]
        try:
            await runner_codex._bridge_extension_mcp_dynamic_tools(
                inputs={"provider_kind": "codex"},
                provider_run_config=provider_run_config,
                dynamic_tools=[],
                tool_handlers={},
                existing_tool_names=set(),
                user_facing=True,
                bare_config=False,
            )
        except RuntimeError as exc:
            assert str(exc) == (
                "required extension MCP 'testape' advertised no tools"
            )
        else:
            raise AssertionError("required empty discovery did not fail closed")
        assert "testape" in provider_run_config["mcp_servers"]

        async def malformed_list(_server_name: str, _config: dict):
            return [{"name": "broken", "inputSchema": None}]

        runner_codex.mcp_stdio_bridge.mcp_list_tools = malformed_list  # type: ignore[assignment]
        try:
            await runner_codex._bridge_extension_mcp_dynamic_tools(
                inputs={"provider_kind": "codex"},
                provider_run_config=provider_run_config,
                dynamic_tools=[],
                tool_handlers={},
                existing_tool_names=set(),
                user_facing=True,
                bare_config=False,
            )
        except RuntimeError as exc:
            assert str(exc) == (
                "required extension MCP 'testape' "
                "exposed malformed tool declarations"
            )
        else:
            raise AssertionError("required malformed discovery did not fail closed")

        runner_codex.extension_store.native_mcp_server_configs = (  # type: ignore[method-assign]
            lambda _inputs, *, user_facing, bare: {}
        )
        try:
            await runner_codex._bridge_extension_mcp_dynamic_tools(
                inputs={"provider_kind": "codex"},
                provider_run_config=provider_run_config,
                dynamic_tools=[],
                tool_handlers={},
                existing_tool_names=set(),
                user_facing=True,
                bare_config=False,
            )
        except RuntimeError as exc:
            assert str(exc) == (
                "required extension MCP config unavailable: 'testape'"
            )
        else:
            raise AssertionError("missing required config did not fail closed")
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]
        runner_codex.extension_store.required_profile_mcp_server_names = original_required  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]


async def test_bridge_preserves_explicit_servers_when_no_extension_is_selected() -> None:
    original_configs = runner_codex.extension_store.native_mcp_server_configs

    def fake_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        assert user_facing is False
        assert bare is True
        return {}

    runner_codex.extension_store.native_mcp_server_configs = fake_configs  # type: ignore[method-assign]
    try:
        provider_run_config = {
            "mcp_servers": {
                "custom": {"command": "/custom/mcp", "args": []},
            },
        }
        dynamic_tools: list[dict] = []
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs={
                "provider_kind": "codex",
                "disabled_builtin_extensions": ["ofek.testape"],
            },
            provider_run_config=provider_run_config,
            dynamic_tools=dynamic_tools,
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=False,
            bare_config=True,
        )
    finally:
        runner_codex.extension_store.native_mcp_server_configs = original_configs  # type: ignore[method-assign]

    assert dynamic_tools == []
    assert provider_run_config["mcp_servers"]["custom"]["command"] == "/custom/mcp"


async def test_fresh_run_bridges_extension_mcp_before_thread_start() -> None:
    original_start = runner_codex._start_app_server
    original_with_builtin = runner_codex.with_builtin_mcp_servers
    original_launcher_configs = runner_codex.extension_store.native_mcp_server_configs
    original_mcp_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools
    captured: dict = {}

    def fake_with_builtin(_inputs: dict, _config: dict):
        return {
            "mcp_servers": {
                "testape": {
                    "command": "/fake/testape-mcp",
                    "args": [],
                    "env": {"BETTER_CLAUDE_EXTENSION_ID": "ofek.testape"},
                },
                "custom": {"command": "/custom/mcp", "args": []},
            },
        }

    def fake_launcher_configs(_inputs: dict, *, user_facing: bool, bare: bool):
        return {
            "testape": {
                "command": "/fake/testape-launcher",
                "args": [],
                "env": {"BETTER_CLAUDE_EXTENSION_ID": "ofek.testape"},
            },
        }

    async def fake_mcp_list_tools(_server_name: str, _config: dict):
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI test",
            "inputSchema": {"type": "object"},
        }]

    async def fake_start_app_server(*_args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    runner_codex._start_app_server = fake_start_app_server  # type: ignore[assignment]
    runner_codex.with_builtin_mcp_servers = fake_with_builtin  # type: ignore[assignment]
    runner_codex.extension_store.native_mcp_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            contract, launch = runner_authority(tmp_path)
            code = await runner_codex._run(tmp_path, {
                "prompt": "check testape",
                "provider_kind": "codex",
                "cwd": str(tmp_path),
                "mode": "native",
                "app_session_id": "app-1",
                "user_facing": True,
                "backend_url": "http://localhost:8000",
                "internal_token": "token-1",
                "permission": {"approval_policy": "never", "sandbox": "danger-full-access"},
            }, contract, launch)
    finally:
        runner_codex._start_app_server = original_start  # type: ignore[assignment]
        runner_codex.with_builtin_mcp_servers = original_with_builtin  # type: ignore[assignment]
        runner_codex.extension_store.native_mcp_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]

    assert code == 1
    assert "test_ui" in [tool["name"] for tool in captured["dynamic_tools"]]
    assert "testape" not in captured["provider_run_config"]["mcp_servers"]
    assert captured["provider_run_config"]["mcp_servers"]["custom"]["command"] == "/custom/mcp"


async def test_app_server_resume_preserves_mcp_tool_timeout() -> None:
    created_clients, _argv = await _record_start_app_server(
        session_id="thread-existing",
        dynamic_tools=None,
        provider_run_config={
            "mcp_servers": {
                "get-requirements": {
                    "command": "echo",
                    "args": ["ok"],
                    "tool_timeout_sec": 1380.0,
                }
            }
        },
    )

    client = created_clients[0]
    resume = next(params for method, params in client.requests if method == "thread/resume")
    assert resume["config"]["mcpServers"]["get-requirements"]["tool_timeout_sec"] == 1380.0


async def test_app_server_fork_receives_capability_config() -> None:
    async def tool_handler(_params: dict) -> dict:
        return {"ok": True}

    created_clients, _argv = await _record_start_app_server(
        session_id="thread-existing",
        fork=True,
        dynamic_tools=[{"name": "tool_x", "description": "Tool X", "inputSchema": {"type": "object"}}],
        tool_handlers={"tool_x": tool_handler},
        provider_run_config={"mcp_servers": {"server-x": {"command": "echo", "args": ["ok"]}}},
    )

    client = created_clients[0]
    fork = next(params for method, params in client.requests if method == "thread/fork")
    assert fork["threadId"] == "thread-existing"
    assert fork["dynamicTools"][0]["name"] == "tool_x"
    assert fork["dynamicTools"][0]["type"] == "function"
    assert fork["config"]["mcpServers"]["server-x"]["command"] == "echo"
    assert client.tool_handlers["tool_x"] is tool_handler


async def test_app_server_start_registers_dynamic_tools() -> None:
    created_clients, _argv = await _record_start_app_server(
        session_id=None,
        dynamic_tools=[{"name": "request_user_input", "description": "Ask", "inputSchema": {"type": "object"}}],
        provider_run_config={"mcp_servers": {"server-x": {"command": "echo", "args": ["ok"]}}},
    )

    client = created_clients[0]
    start = next(params for method, params in client.requests if method == "thread/start")
    assert start["dynamicTools"][0]["name"] == "request_user_input"
    assert start["dynamicTools"][0]["type"] == "function"
    assert start["config"]["mcpServers"]["server-x"]["command"] == "echo"


async def test_app_server_start_passes_testape_mcp_server_config() -> None:
    created_clients, _argv = await _record_start_app_server(
        session_id=None,
        dynamic_tools=None,
        provider_run_config={"mcp_servers": {"testape": {"command": "python", "args": ["server.py"]}}},
    )

    client = created_clients[0]
    start = next(params for method, params in client.requests if method == "thread/start")
    assert start["config"]["mcpServers"]["testape"]["command"] == "python"


def test_resume_dynamic_tools_use_only_missing_rollout_tools() -> None:
    desired = [
        {"name": "mssg", "description": "Send", "inputSchema": {"type": "object"}},
        {"name": "inbox", "description": "Read", "inputSchema": {"type": "object"}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"dynamic_tools": desired[:1]},
            }) + "\n",
            encoding="utf-8",
        )
        missing, missing_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired,
        )
        unchanged, unchanged_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired[:1],
        )
        same_name_changed_schema = [desired[0] | {"inputSchema": {"type": "string"}}]
        schema_changed, schema_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout,
            same_name_changed_schema,
        )
        rollout.write_text(
            '{"type":"session_meta","payload":{"dynamic_tools":[{"name":"mssg"},null]}}\n',
            encoding="utf-8",
        )
        malformed, malformed_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired,
        )

    assert missing == [desired[1]]
    assert missing_error is None
    assert unchanged == []
    assert unchanged_error is None
    assert schema_changed == same_name_changed_schema
    assert schema_error is None
    none_missing, none_error = runner_codex._dynamic_tools_missing_from_rollout(None, desired)
    assert none_missing == []
    assert none_error == runner_codex.CODEX_RESUME_CAPABILITY_METADATA_UNAVAILABLE
    assert malformed == []
    assert malformed_error == runner_codex.CODEX_RESUME_CAPABILITY_METADATA_UNAVAILABLE


def test_resume_supplies_all_dynamic_tools_when_session_meta_omits_them() -> None:
    desired = [
        {"name": "mssg", "description": "Send", "inputSchema": {"type": "object"}},
        {"name": "inbox", "description": "Read", "inputSchema": {"type": "object"}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "019fa434-ed1e-7c31-ad76-5a7156851517",
                    "timestamp": "2026-07-27T15:32:39.000Z",
                    "originator": "codex_cli_rs",
                    "cli_version": "0.0.0",
                    "source": "app_server",
                    "model_provider": "openai",
                    "cwd": "/tmp/project",
                },
            }) + "\n",
            encoding="utf-8",
        )

        first, first_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired,
        )
        repeated, repeated_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired,
        )

    assert first == desired
    assert first_error is None
    assert repeated == desired
    assert repeated_error is None


def test_resume_rejects_present_invalid_dynamic_tool_metadata() -> None:
    desired = [{"name": "mssg", "description": "Send", "inputSchema": {"type": "object"}}]
    invalid_values = [None, {}, "unknown"]
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        for invalid in invalid_values:
            rollout.write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {"dynamic_tools": invalid},
                }) + "\n",
                encoding="utf-8",
            )
            missing, error = runner_codex._dynamic_tools_missing_from_rollout(
                rollout, desired,
            )
            assert missing == []
            assert error == runner_codex.CODEX_RESUME_CAPABILITY_METADATA_UNAVAILABLE

        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"dynamic_tools": [desired[0], desired[0] | {"description": "Changed"}]},
            }) + "\n",
            encoding="utf-8",
        )
        duplicate, duplicate_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired,
        )

    assert duplicate == []
    assert duplicate_error == runner_codex.CODEX_RESUME_CAPABILITY_METADATA_UNAVAILABLE


def test_resume_ignores_codex_added_rollout_fields() -> None:
    """Codex stamps its own `type`/`deferLoading` defaults into session_meta.

    Those are not drift: a schema change we shipped must be re-supplied, but a
    byte-identical tool must not be, or every resume re-sends every tool.
    """
    desired = [{"name": "mssg", "description": "Send", "inputSchema": {"type": "object"}}]
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "dynamic_tools": [
                        desired[0] | {"type": "function", "deferLoading": False},
                    ],
                },
            }) + "\n",
            encoding="utf-8",
        )
        unchanged, unchanged_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, desired,
        )

        # A field we now send that the persisted contract lacks is real drift:
        # exactly the harness_profile_id case that bricked pre-existing threads.
        widened = [desired[0] | {"inputSchema": {"type": "object", "properties": {}}}]
        drifted, drifted_error = runner_codex._dynamic_tools_missing_from_rollout(
            rollout, widened,
        )

    assert unchanged == []
    assert unchanged_error is None
    assert drifted == widened
    assert drifted_error is None


async def test_app_server_passes_config_overrides_before_subcommand() -> None:
    _clients, argv = await _record_start_app_server(
        session_id=None,
        dynamic_tools=None,
        provider_run_config=None,
        config_overrides=["model_provider=\"sakana\"", "model=\"fugu\""],
    )

    assert argv == [
        "codex",
        "-c", "model_provider=\"sakana\"",
        "-c", "model=\"fugu\"",
        "app-server",
    ]


def test_codex_config_overrides_preserve_mcp_tool_timeout() -> None:
    overrides = runner_codex._codex_config_overrides(
        Path("/tmp/run"),
        {
            "mcp_servers": {
                "get-requirements": {
                    "command": "echo",
                    "args": ["ok"],
                    "tool_timeout_sec": 1380.0,
                }
            }
        },
    )

    assert len(overrides) == 1
    assert overrides[0].startswith("mcp_servers=")
    assert "tool_timeout_sec" in overrides[0]
    assert "1380.0" in overrides[0]


async def _record_start_app_server(
    *,
    session_id: str | None,
    dynamic_tools: list[dict] | None,
    provider_run_config: dict | None,
    tool_handlers: dict | None = None,
    fork: bool = False,
    config_overrides: list[str] | None = None,
) -> tuple[list[_FakeAppServerProcess], list[str]]:
    original_create_subprocess_exec = runner_codex.asyncio.create_subprocess_exec
    original_app_server_process = runner_codex._AppServerProcess
    created_clients: list[_FakeAppServerProcess] = []
    captured_argv: list[str] = []

    async def recording_create_subprocess_exec(*args, **kwargs):
        captured_argv[:] = [str(arg) for arg in args]
        return await _fake_create_subprocess_exec(*args, **kwargs)

    class RecordingAppServerProcess(_FakeAppServerProcess):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_clients.append(self)

    try:
        runner_codex.asyncio.create_subprocess_exec = recording_create_subprocess_exec
        runner_codex._AppServerProcess = RecordingAppServerProcess
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            await runner_codex._start_app_server(
                "codex",
                run_dir=tmp_path,
                cwd=str(tmp_path),
                model="gpt-5",
                reasoning_effort="low",
                session_id=session_id,
                fork=fork,
                turn_input=[],
                dynamic_tools=dynamic_tools,
                tool_handlers=tool_handlers,
                provider_run_config=provider_run_config,
                config_overrides=config_overrides,
            )
    finally:
        runner_codex.asyncio.create_subprocess_exec = original_create_subprocess_exec
        runner_codex._AppServerProcess = original_app_server_process

    return created_clients, captured_argv


if __name__ == "__main__":
    asyncio.run(test_app_server_uses_structured_sandbox_policy())
    asyncio.run(test_app_server_resume_receives_capability_config())
    asyncio.run(test_initialize_timeout_retries_after_full_client_cleanup())
    asyncio.run(test_app_server_wait_joins_owned_tasks())
    asyncio.run(test_initialize_timeout_does_not_retry_after_cancel())
    asyncio.run(test_bridges_selected_extension_mcp_tools_as_dynamic_tools())
    asyncio.run(test_bridge_uses_selected_extension_when_provider_config_has_only_skills())
    asyncio.run(test_bridge_preserves_configured_extension_during_resolution_race())
    asyncio.run(test_bridge_probes_extension_servers_concurrently())
    asyncio.run(test_bridge_preserves_custom_same_name_mcp_server())
    asyncio.run(test_bridge_prunes_extension_owned_mcp_server_after_registration())
    asyncio.run(test_bridge_atomically_quarantines_partially_valid_ambient_server())
    asyncio.run(test_bridge_continues_when_selected_extension_cannot_list_tools())
    asyncio.run(test_bridge_quarantines_configured_extension_that_cannot_list_tools())
    asyncio.run(test_bridge_continues_when_selected_extension_exposes_no_tools())
    asyncio.run(test_bridge_quarantines_configured_extension_that_exposes_no_tools())
    asyncio.run(test_bridge_fails_closed_for_required_same_extension_discovery())
    asyncio.run(test_bridge_preserves_explicit_servers_when_no_extension_is_selected())
    asyncio.run(test_fresh_run_bridges_extension_mcp_before_thread_start())
    asyncio.run(test_app_server_resume_preserves_mcp_tool_timeout())
    asyncio.run(test_app_server_fork_receives_capability_config())
    asyncio.run(test_app_server_start_registers_dynamic_tools())
    asyncio.run(test_app_server_start_passes_testape_mcp_server_config())
    asyncio.run(test_app_server_passes_config_overrides_before_subcommand())
    test_resume_dynamic_tools_use_only_missing_rollout_tools()
    test_resume_ignores_codex_added_rollout_fields()
    test_codex_config_overrides_preserve_mcp_tool_timeout()
