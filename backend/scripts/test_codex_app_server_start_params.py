#!/usr/bin/env python3
import asyncio
import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import runner_codex


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
        names = [str(tool.get("name") or "") for tool in params.get("dynamicTools") or []]
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
    assert resume["config"]["mcpServers"]["server-x"]["command"] == "echo"
    assert client.tool_handlers["tool_x"] is tool_handler


async def test_resume_bridges_selected_extension_mcp_tools_as_dynamic_tools() -> None:
    original_launcher_configs = runner_codex.extension_store.native_mcp_launcher_server_configs
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
            "not-selected": {
                "command": "/fake/other-mcp",
                "args": [],
                "env": {},
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

    runner_codex.extension_store.native_mcp_launcher_server_configs = fake_launcher_configs  # type: ignore[method-assign]
    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_mcp_list_tools  # type: ignore[assignment]
    runner_codex.mcp_stdio_bridge.mcp_call_tool = fake_mcp_call_tool  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        tool_handlers: dict = {}
        existing_tool_names: set[str] = set()
        await runner_codex._bridge_resume_extension_mcp_dynamic_tools(
            inputs={"provider_kind": "codex", "app_session_id": "sender-1"},
            provider_run_config={
                "mcp_servers": {
                    "testape": {"command": "/fake/testape-mcp", "args": []},
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
        runner_codex.extension_store.native_mcp_launcher_server_configs = original_launcher_configs  # type: ignore[method-assign]
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_mcp_list_tools  # type: ignore[assignment]
        runner_codex.mcp_stdio_bridge.mcp_call_tool = original_mcp_call_tool  # type: ignore[assignment]

    assert [tool["name"] for tool in dynamic_tools] == ["test_ui"]
    assert [tool["name"] for tool in missing] == ["test_ui"]
    assert missing_error is None
    assert dynamic_tools[0]["inputSchema"]["required"] == ["task", "repo_path"]
    assert "test_ui" in existing_tool_names
    assert result == {
        "contentItems": [{
            "type": "inputText",
            "text": '{"content":[{"type":"text","text":"ok"}]}',
        }],
        "success": True,
    }


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
    assert start["config"]["mcpServers"]["server-x"]["command"] == "echo"


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
    asyncio.run(test_resume_bridges_selected_extension_mcp_tools_as_dynamic_tools())
    asyncio.run(test_app_server_resume_preserves_mcp_tool_timeout())
    asyncio.run(test_app_server_fork_receives_capability_config())
    asyncio.run(test_app_server_start_registers_dynamic_tools())
    asyncio.run(test_app_server_passes_config_overrides_before_subcommand())
    test_resume_dynamic_tools_use_only_missing_rollout_tools()
    test_resume_ignores_codex_added_rollout_fields()
    test_codex_config_overrides_preserve_mcp_tool_timeout()
