#!/usr/bin/env python3

from __future__ import annotations

import asyncio
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
        self.rows: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.rows.append(data)

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


async def _test_request_timeout_clears_pending_response() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = _FakeProc()
        client = _AppServerProcess(proc, Path(tmp))
        try:
            try:
                await client.request("thread/resume", {"threadId": "dead"}, timeout_s=0.01)
            except TimeoutError as e:
                assert "thread/resume" in str(e), str(e)
            else:
                raise AssertionError("request did not time out")
            assert client._responses == {}, client._responses
        finally:
            client._reader_task.cancel()
            client._steer_task.cancel()
            for task in (client._reader_task, client._steer_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def main() -> None:
    asyncio.run(_test_request_timeout_clears_pending_response())
    print("PASS: Codex app-server request timeout clears pending response")


if __name__ == "__main__":
    main()
