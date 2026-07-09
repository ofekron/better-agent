#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from runner_codex import _AppServerProcess  # noqa: E402


class _FakeStdout:
    async def readline(self) -> bytes:
        await asyncio.sleep(10)
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, data: bytes) -> None:
        self.rows.append(json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stderr = None
        self.stdout = _FakeStdout()

    async def wait(self) -> int:
        return 0


async def _close(client: _AppServerProcess, proc: _FakeProc) -> None:
    proc.returncode = 0
    for task in (client._steer_task, client._reader_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _test_bad_steer_does_not_block_next_line() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        proc = _FakeProc()
        client = _AppServerProcess(proc, run_dir)
        client.thread_id = "thread-1"
        client.turn_id = "turn-1"
        calls = 0

        async def request(method: str, params: dict, *, timeout_s: float = 60.0) -> dict:
            nonlocal calls
            del timeout_s
            calls += 1
            if calls == 1:
                raise RuntimeError("expected active turn id `turn-1` but found `old-turn`")
            proc.stdin.rows.append({"method": method, "params": params})
            return {}

        client.request = request  # type: ignore[method-assign]
        try:
            with (run_dir / "steer.jsonl").open("w", encoding="utf-8") as f:
                f.write(json.dumps({"prompt": "stale"}) + "\n")
                f.write(json.dumps({"prompt": "continue"}) + "\n")
            await asyncio.sleep(0.4)
            assert calls == 2, calls
            assert len(proc.stdin.rows) == 1, proc.stdin.rows
            request_row = proc.stdin.rows[0]
            assert request_row["method"] == "turn/steer", request_row
            assert request_row["params"]["input"][0]["text"] == "continue", request_row
        finally:
            await _close(client, proc)


async def _test_inactive_turn_line_is_consumed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        proc = _FakeProc()
        client = _AppServerProcess(proc, run_dir)

        try:
            (run_dir / "steer.jsonl").write_text(
                json.dumps({"prompt": "continue"}) + "\n",
                encoding="utf-8",
            )
            await asyncio.sleep(0.2)
            client.thread_id = "thread-1"
            client.turn_id = "turn-1"
            with (run_dir / "steer.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"prompt": "next"}) + "\n")
            await asyncio.sleep(0.3)
            assert len(proc.stdin.rows) == 1, proc.stdin.rows
            assert proc.stdin.rows[0]["params"]["input"][0]["text"] == "next"
        finally:
            await _close(client, proc)


def main() -> None:
    asyncio.run(_test_bad_steer_does_not_block_next_line())
    asyncio.run(_test_inactive_turn_line_is_consumed())
    print("PASS: Codex steer inbox consumes stale entries")


if __name__ == "__main__":
    main()
