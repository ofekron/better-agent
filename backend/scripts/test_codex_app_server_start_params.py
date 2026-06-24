#!/usr/bin/env python3
import asyncio
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

    def __init__(self, _proc, _run_dir: Path, *, tool_handlers=None):
        self.thread_id = None
        self.requests = []
        self.notifications = []
        self._mapped = _FakeMapped()

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
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
    created_clients = await _record_start_app_server(
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
    created_clients = await _record_start_app_server(
        session_id="thread-existing",
        dynamic_tools=[{"name": "tool_x", "description": "Tool X", "inputSchema": {"type": "object"}}],
        provider_run_config={"mcp_servers": {"server-x": {"command": "echo", "args": ["ok"]}}},
    )

    client = created_clients[0]
    resume = next(params for method, params in client.requests if method == "thread/resume")
    assert resume["threadId"] == "thread-existing"
    assert resume["dynamicTools"][0]["name"] == "tool_x"
    assert resume["config"]["mcpServers"]["server-x"]["command"] == "echo"


async def test_app_server_fork_receives_capability_config() -> None:
    created_clients = await _record_start_app_server(
        session_id="thread-existing",
        fork=True,
        dynamic_tools=[{"name": "tool_x", "description": "Tool X", "inputSchema": {"type": "object"}}],
        provider_run_config={"mcp_servers": {"server-x": {"command": "echo", "args": ["ok"]}}},
    )

    client = created_clients[0]
    fork = next(params for method, params in client.requests if method == "thread/fork")
    assert fork["threadId"] == "thread-existing"
    assert fork["dynamicTools"][0]["name"] == "tool_x"
    assert fork["config"]["mcpServers"]["server-x"]["command"] == "echo"


async def _record_start_app_server(
    *,
    session_id: str | None,
    dynamic_tools: list[dict] | None,
    provider_run_config: dict | None,
    fork: bool = False,
) -> list[_FakeAppServerProcess]:
    original_create_subprocess_exec = runner_codex.asyncio.create_subprocess_exec
    original_app_server_process = runner_codex._AppServerProcess
    created_clients: list[_FakeAppServerProcess] = []

    class RecordingAppServerProcess(_FakeAppServerProcess):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_clients.append(self)

    try:
        runner_codex.asyncio.create_subprocess_exec = _fake_create_subprocess_exec
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
                provider_run_config=provider_run_config,
            )
    finally:
        runner_codex.asyncio.create_subprocess_exec = original_create_subprocess_exec
        runner_codex._AppServerProcess = original_app_server_process

    return created_clients


if __name__ == "__main__":
    asyncio.run(test_app_server_uses_structured_sandbox_policy())
    asyncio.run(test_app_server_resume_receives_capability_config())
    asyncio.run(test_app_server_fork_receives_capability_config())
