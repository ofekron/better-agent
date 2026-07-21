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
        self.returncode = None
        self.thread_id = "thread-1"
        self.turn_id: str | None = None
        self.stderr = _FakeStderr()
        self.stdout = _FakeStdout(self, run_dir, rows)
        self._stderr_task = asyncio.create_task(asyncio.sleep(0))
        self.requests: list[tuple[str, dict]] = []
        self._pending_tool_calls: dict = {}

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {}

    async def _fail_pending_tool_calls(self, _reason: str) -> None:
        return None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


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

        original_resolve = runner_codex._resolve_codex_cli
        original_start = runner_codex._start_app_server
        original_signal_stop = runner_codex._process_control().signal_stop
        runner_codex._resolve_codex_cli = lambda _inputs=None: "codex"  # type: ignore[assignment]
        runner_codex._start_app_server = start_app_server  # type: ignore[assignment]
        runner_codex._process_control().signal_stop = lambda _pid: None  # type: ignore[method-assign]
        cleanup = None
        try:
            if configure:
                cleanup = configure(run_dir, fake)
            payload = {"prompt": "go", "cwd": str(run_dir)}
            payload.update(inputs or {})
            code = await runner_codex._run(run_dir, payload)
        finally:
            if callable(cleanup):
                cleanup()
            runner_codex._resolve_codex_cli = original_resolve  # type: ignore[assignment]
            runner_codex._start_app_server = original_start  # type: ignore[assignment]
            runner_codex._process_control().signal_stop = original_signal_stop  # type: ignore[method-assign]

        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        return code, complete, fake


async def _test_interrupt_drains_terminal_event() -> None:
    code, complete, fake = await _run_with_fake_process()
    assert code == 1
    assert ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}) in fake.requests
    assert complete["error"] == "interrupted by user"


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
            terminal, _usage, _assistant = runner_codex._rollout_terminal_state(
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

    code, complete, _fake = await _run_with_fake_process(
        rows=rows,
        inputs={"session_id": "parent-thread", "fork": True},
        configure=configure,
    )

    assert code == 0
    assert complete["success"] is True
    assert captured_offsets == [boundary]
    assert initial_terminal_seen == [None]


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

    code, complete, _fake = await _run_with_fake_process(
        rows=rows,
        inputs={"session_id": "parent-thread"},
        configure=configure,
    )

    assert code == 0
    assert complete["success"] is True
    assert captured_offsets == expected_offsets


def main() -> None:
    asyncio.run(_test_interrupt_drains_terminal_event())
    asyncio.run(_test_fork_thread_started_boundary_controls_terminal_scan())
    asyncio.run(_test_resume_keeps_existing_rollout_boundary())
    print("PASS: Codex interrupt drains terminal turn event")
    print("PASS: Codex fork terminal scan honors thread.started boundary")
    print("PASS: Codex resume keeps existing rollout boundary")


if __name__ == "__main__":
    main()
