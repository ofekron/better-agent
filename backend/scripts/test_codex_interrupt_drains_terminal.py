#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-codex-interrupt-drain-")

import runner_codex  # noqa: E402
from scripts.codex_execution_test_support import runner_authority  # noqa: E402


class _FakeStderr:
    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        return b""


class _FakeStdout:
    def __init__(
        self,
        owner: "_FakeCodexProcess",
        run_dir: Path,
        rows: list[dict] | None = None,
    ) -> None:
        self._owner = owner
        self._run_dir = run_dir
        self._rows = rows or [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started", "turn_id": "turn-1"},
            {
                "type": "turn.failed",
                "error": {"message": "interrupted by user"},
            },
        ]

    def __aiter__(self) -> "_FakeStdout":
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(0.05)
        if not self._rows:
            raise StopAsyncIteration
        row = self._rows.pop(0)
        if row["type"] == "turn.started":
            self._owner.turn_id = row["turn_id"]
            (self._run_dir / "cancel").touch()
            await asyncio.sleep(0.2)
        return (json.dumps(row) + "\n").encode("utf-8")


class _FakeCodexProcess:
    def __init__(self, run_dir: Path, rows: list[dict] | None = None) -> None:
        self.pid = 12345
        self.process_group_id = 54321
        self.returncode = None
        self.thread_id = "thread-1"
        self.turn_id: str | None = None
        self.stderr = _FakeStderr()
        self.stdout = _FakeStdout(self, run_dir, rows)
        self._stderr_task = asyncio.create_task(asyncio.sleep(0))
        self.requests: list[tuple[str, dict]] = []
        self.process_group_actions: list[tuple[str, int]] = []
        self._pending_tool_calls: set = set()

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {}

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    async def close_input(self) -> None:
        return None


def _assert_terminal_completion_cleanup(fake: _FakeCodexProcess) -> None:
    assert fake.process_group_actions == [
        ("signal", fake.process_group_id),
        ("kill", fake.process_group_id),
    ]


async def _run_with_fake_process(
    *,
    rows: list[dict] | None = None,
    inputs: dict | None = None,
    configure: Callable[[Path, _FakeCodexProcess], Callable[[], None] | None] | None = None,
) -> tuple[int, dict, _FakeCodexProcess]:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        fake = _FakeCodexProcess(run_dir, rows)

        async def start_app_server(*_args, **_kwargs):
            return fake

        original_start = runner_codex._start_app_server

        def record_group_action(action: str, group_id: int) -> None:
            assert group_id == fake.process_group_id
            fake.process_group_actions.append((action, group_id))

        class _FakeProcessControl:
            def signal_stop(self, _pid: int) -> None:
                return None

            def signal_owned_group(self, group_id: int) -> None:
                record_group_action("signal", group_id)

            def force_kill_owned_group(self, group_id: int) -> None:
                record_group_action("kill", group_id)

        original_process_control = runner_codex._process_control
        runner_codex._start_app_server = start_app_server  # type: ignore[assignment]
        runner_codex._process_control = lambda: _FakeProcessControl()  # type: ignore[assignment]
        cleanup = None
        try:
            if configure:
                cleanup = configure(run_dir, fake)
            payload = {"prompt": "go", "cwd": str(run_dir)}
            payload.update(inputs or {})
            payload.setdefault("provider_kind", "codex")
            contract, launch = runner_authority(run_dir)
            code = await runner_codex._run(
                run_dir,
                payload,
                contract,
                launch,
            )
        finally:
            if callable(cleanup):
                cleanup()
            runner_codex._start_app_server = original_start  # type: ignore[assignment]
            runner_codex._process_control = original_process_control  # type: ignore[assignment]

        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        return code, complete, fake


async def _test_interrupt_drains_terminal_event() -> None:
    code, complete, fake = await _run_with_fake_process()
    assert code == 1
    assert ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}) in fake.requests
    assert complete["error"] == "interrupted by user"
    assert fake.process_group_actions == [
        ("signal", fake.process_group_id),
        ("kill", fake.process_group_id),
    ]


async def _test_fork_thread_started_boundary_controls_terminal_scan() -> None:
    captured_offsets: list[int] = []
    initial_terminal_seen: list[bool | None] = []
    prefix = (
        json.dumps({
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "ready"},
        })
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
        + "\n"
    )
    boundary = len(prefix.encode("utf-8"))

    rows = [
        {
            "type": "thread.started",
            "thread_id": "child-thread",
            "rollout_byte_offset": boundary,
        },
        {"type": "turn.completed", "usage": {}, "assistant_seen": True},
    ]

    def configure(run_dir: Path, _fake: _FakeCodexProcess):
        import codex_native

        rollout = run_dir / "child-rollout.jsonl"
        rollout.write_text(prefix, encoding="utf-8")

        original_forward = runner_codex._forward_rollout_terminal
        original_resolve = codex_native.resolve_rollout_path
        original_resolve_polled = codex_native.resolve_rollout_path_polled

        async def forward(_proc, _rollout_path, *, byte_offset, cancel_path=None):
            captured_offsets.append(byte_offset)
            terminal, _usage, _assistant, _ = runner_codex._rollout_terminal_state(
                str(rollout),
                byte_offset=byte_offset,
            )
            initial_terminal_seen.append(terminal)

        async def resolve_polled(thread_id: str, timeout: float = 5.0):
            return rollout if thread_id == "child-thread" else None

        runner_codex._forward_rollout_terminal = forward
        codex_native.resolve_rollout_path = lambda _thread_id: None
        codex_native.resolve_rollout_path_polled = resolve_polled

        def cleanup():
            runner_codex._forward_rollout_terminal = original_forward
            codex_native.resolve_rollout_path = original_resolve
            codex_native.resolve_rollout_path_polled = original_resolve_polled

        return cleanup

    code, complete, fake = await _run_with_fake_process(
        rows=rows,
        inputs={"session_id": "parent-thread", "fork": True},
        configure=configure,
    )

    assert code == 0
    assert complete["success"] is True
    assert captured_offsets == [boundary]
    assert initial_terminal_seen == [None]
    _assert_terminal_completion_cleanup(fake)


async def _test_resume_keeps_existing_rollout_boundary() -> None:
    captured_offsets: list[int] = []
    expected_offsets: list[int] = []

    rows = [
        {"type": "thread.started", "thread_id": "parent-thread"},
        {"type": "turn.completed", "usage": {}, "assistant_seen": True},
    ]

    def configure(run_dir: Path, _fake: _FakeCodexProcess):
        import codex_native

        rollout = run_dir / "parent-rollout.jsonl"
        prefix = (
            json.dumps({
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "previous"},
            })
            + "\n"
        )
        rollout.write_text(prefix, encoding="utf-8")
        expected_offsets.append(len(prefix.encode("utf-8")))

        original_forward = runner_codex._forward_rollout_terminal
        original_resolve = codex_native.resolve_rollout_path
        original_resolve_polled = codex_native.resolve_rollout_path_polled

        async def forward(_proc, _rollout_path, *, byte_offset, cancel_path=None):
            captured_offsets.append(byte_offset)

        async def resolve_polled(thread_id: str, timeout: float = 5.0):
            return rollout if thread_id == "parent-thread" else None

        runner_codex._forward_rollout_terminal = forward
        codex_native.resolve_rollout_path = (
            lambda thread_id: rollout if thread_id == "parent-thread" else None
        )
        codex_native.resolve_rollout_path_polled = resolve_polled

        def cleanup():
            runner_codex._forward_rollout_terminal = original_forward
            codex_native.resolve_rollout_path = original_resolve
            codex_native.resolve_rollout_path_polled = original_resolve_polled

        return cleanup

    code, complete, fake = await _run_with_fake_process(
        rows=rows,
        inputs={"session_id": "parent-thread"},
        configure=configure,
    )

    assert code == 0
    assert complete["success"] is True
    assert captured_offsets == expected_offsets
    _assert_terminal_completion_cleanup(fake)


def main() -> None:
    asyncio.run(_test_interrupt_drains_terminal_event())
    asyncio.run(_test_fork_thread_started_boundary_controls_terminal_scan())
    asyncio.run(_test_resume_keeps_existing_rollout_boundary())
    print("PASS: Codex interrupt drains terminal turn event")
    print("PASS: Codex fork terminal scan honors thread.started boundary")
    print("PASS: Codex resume keeps existing rollout boundary")


if __name__ == "__main__":
    main()
