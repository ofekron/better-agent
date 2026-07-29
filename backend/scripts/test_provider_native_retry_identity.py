from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import _test_home

TEST_HOME = Path(_test_home.isolate("ba-test-native-retry-"))
os.environ["BETTER_AGENT_RUNTIME_BROKER"] = "unix:/tmp/native-retry-test.sock"

import codex_native  # noqa: E402
import runner  # noqa: E402
import runner_agy  # noqa: E402
import runner_codex  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from runs_dir import BACKGROUND_WORK_TOOLS, TIMER_TOOLS  # noqa: E402
from scripts.codex_execution_test_support import runner_authority  # noqa: E402


async def _forbid_election(*_args, **_kwargs):
    raise AssertionError("provider-native retry invoked lifecycle election")


def _claude_inputs() -> dict:
    return {
        "prompt": "reply",
        "images": [],
        "files": [],
        "cwd": "/tmp",
        "model": "sonnet",
        "permission": {"mode": "bypassPermissions"},
        "session_id": None,
        "mode": "native",
        "app_session_id": "native-retry-claude",
        "disallowed_tools": [
            "AskUserQuestion",
            "EnterPlanMode",
            "ExitPlanMode",
            *TIMER_TOOLS,
            *BACKGROUND_WORK_TOOLS,
        ],
        "setting_sources": [],
        "bare_config": True,
    }


async def prove_claude_one_run() -> None:
    run_dir = TEST_HOME / "runs" / "claude-one-run"
    run_dir.mkdir(parents=True)
    attempts: list[Path] = []
    original_client = runner.ClaudeSDKClient
    original_turn = runner._run_one_turn
    original_caps = runner._runtime_capabilities

    class Client:
        def __init__(self, *, options):
            self.options = options

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    async def one_turn(**kwargs):
        attempts.append(Path(kwargs["run_dir"]))
        if len(attempts) == 1:
            raise ConnectionError("connection reset by peer")
        return {
            "discovered_sid": None,
            "total_usage": None,
            "error": None,
            "cancelled": False,
            "sdk_output_parts": ["done"],
            "final_success": True,
            "context_window": None,
            "outstanding_tasks": set(),
        }

    runner.ClaudeSDKClient = Client
    runner._run_one_turn = one_turn
    runner._runtime_capabilities = lambda *_args: (
        {"mcp_servers": []},
        {},
        {},
        "/fake/claude",
    )
    try:
        code = await runner._run(
            run_dir,
            _claude_inputs(),
            SimpleNamespace(capabilities=SimpleNamespace(
                installation_decisions={
                    "capabilities": {"integrations_enabled": True},
                },
            )),
        )
    finally:
        runner.ClaudeSDKClient = original_client
        runner._run_one_turn = original_turn
        runner._runtime_capabilities = original_caps
    assert code == 0
    assert attempts == [run_dir, run_dir]
    assert json.loads((run_dir / "complete.json").read_text())["success"] is True


async def prove_codex_one_run() -> None:
    run_dir = TEST_HOME / "runs" / "codex-one-run"
    run_dir.mkdir(parents=True)
    rollout = run_dir / "rollout.jsonl"
    rollout.write_text("")
    starts: list[Path] = []
    terminals = [
        (False, None, False, None),
        (True, {"input_tokens": 1}, True, None),
    ]
    originals = (
        runner_codex._start_app_server,
        codex_native.resolve_rollout_path,
        codex_native.resolve_rollout_path_polled,
        runner_codex._bridge_extension_mcp_dynamic_tools,
        runner_codex._wait_rollout_terminal_state,
        runner_codex._process_control,
    )

    class Stdout:
        def __init__(self):
            self.rows = [
                {"type": "thread.started", "thread_id": "codex-thread"},
                {"type": "turn.started", "turn_id": "codex-turn"},
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.rows:
                raise StopAsyncIteration
            return (json.dumps(self.rows.pop(0)) + "\n").encode()

    class Proc:
        pid = 111
        process_group_id = 111
        returncode = None
        thread_id = "codex-thread"
        turn_id = "codex-turn"
        _pending_tool_calls: set = set()

        def __init__(self):
            self.stdout = Stdout()
            self._mapped = asyncio.Queue()
            self._stderr_task = asyncio.create_task(asyncio.Event().wait())

        async def request(self, *_args, **_kwargs):
            return None

        async def close_input(self):
            self.returncode = 0

        async def wait(self):
            return 0

    class Control:
        def signal_owned_group(self, _group_id):
            return None

        def force_kill_owned_group(self, _group_id):
            return None

    async def start(*args, **kwargs):
        del args, kwargs
        starts.append(run_dir)
        return Proc()

    async def resolve(_sid, timeout=5.0):
        del timeout
        return rollout

    async def wait_terminal(*_args, **_kwargs):
        return terminals.pop(0)

    runner_codex._start_app_server = start
    codex_native.resolve_rollout_path = lambda _sid: rollout
    codex_native.resolve_rollout_path_polled = resolve
    runner_codex._bridge_extension_mcp_dynamic_tools = (
        lambda **_kwargs: asyncio.sleep(0)
    )
    runner_codex._wait_rollout_terminal_state = wait_terminal
    runner_codex._process_control = lambda: Control()
    try:
        contract, launch = runner_authority(run_dir)
        code = await runner_codex._run(run_dir, {
            "prompt": "continue",
            "provider_kind": "codex",
            "cwd": "/tmp",
            "mode": "native",
            "session_id": None,
            "app_session_id": "native-retry-codex",
            "permission": {
                "approval_policy": "never",
                "sandbox": "danger-full-access",
            },
            "bare_config": True,
            "provider_run_config": {},
        }, contract, launch)
    finally:
        (
            runner_codex._start_app_server,
            codex_native.resolve_rollout_path,
            codex_native.resolve_rollout_path_polled,
            runner_codex._bridge_extension_mcp_dynamic_tools,
            runner_codex._wait_rollout_terminal_state,
            runner_codex._process_control,
        ) = originals
    assert code == 0
    assert starts == [run_dir]
    assert not terminals
    assert json.loads((run_dir / "complete.json").read_text())["success"] is True


async def prove_agy_one_run() -> None:
    run_dir = TEST_HOME / "runs" / "agy-one-run"
    run_dir.mkdir(parents=True)
    config_root = TEST_HOME / "agy-config"
    config_root.mkdir()
    count_path = TEST_HOME / "agy-count"
    executable = TEST_HOME / "agy"
    executable.write_text(
        "#!/bin/sh\n"
        f"echo x >> {count_path}\n"
        "printf 'done'\n",
    )
    executable.chmod(0o700)
    original_caps = runner_agy._runtime_capabilities
    runner_agy._runtime_capabilities = lambda *_args: ({"mcp_servers": []}, {})

    class LaunchContext:
        def __enter__(self):
            return SimpleNamespace(argv=(str(executable),), pass_fds=())

        def __exit__(self, *_args):
            return None

    runtime = SimpleNamespace(
        launch=SimpleNamespace(
            config=SimpleNamespace(
                root=SimpleNamespace(resolved_path=str(config_root)),
            ),
            open_downstream=lambda: LaunchContext(),
        ),
    )
    try:
        code = await runner_agy._run(run_dir, {
            "prompt": "reply",
            "cwd": "/tmp",
            "model": "",
            "session_id": None,
            "app_session_id": "native-retry-agy",
            "mode": "native",
            "agy_config_root": str(config_root),
            "agy_settings": {},
            "provider_run_config": {},
        }, runtime)
    finally:
        runner_agy._runtime_capabilities = original_caps
    assert code == 0
    assert count_path.read_text().splitlines() == ["x"]
    assert json.loads((run_dir / "complete.json").read_text())["success"] is True


async def main() -> None:
    original_elect = LifecycleCommandEngine.elect_execution_attempt
    LifecycleCommandEngine.elect_execution_attempt = _forbid_election
    try:
        await prove_claude_one_run()
        await prove_codex_one_run()
        await prove_agy_one_run()
    finally:
        LifecycleCommandEngine.elect_execution_attempt = original_elect
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("PASS provider-native retry identity")


if __name__ == "__main__":
    asyncio.run(main())
