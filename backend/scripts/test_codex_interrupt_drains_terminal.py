#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys

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
        try:
            payload = {"prompt": "go", "cwd": str(run_dir)}
            payload.update(inputs or {})
            code = await runner_codex._run(run_dir, payload)
        finally:
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


def main() -> None:
    asyncio.run(_test_interrupt_drains_terminal_event())
    print("PASS: Codex interrupt drains terminal turn event")


if __name__ == "__main__":
    main()
