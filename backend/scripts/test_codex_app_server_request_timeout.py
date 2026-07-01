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
    def __init__(self, rows: list[dict] | None = None, *, hang_after_rows: bool = True) -> None:
        self._hang_after_rows = hang_after_rows
        self._rows = [
            (json.dumps(row) + "\n").encode("utf-8")
            for row in rows or []
        ]

    async def readline(self) -> bytes:
        if self._rows:
            await asyncio.sleep(0)
            return self._rows.pop(0)
        if not self._hang_after_rows:
            return b""
        await asyncio.sleep(10)
        return b""


class _FakeStdin:
    def __init__(self, *, fail_drain: bool = False) -> None:
        self.fail_drain = fail_drain
        self.rows: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.rows.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)
        if self.fail_drain:
            raise ConnectionResetError("Connection lost")


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stderr = None
        self.stdout = _FakeStdout()

    async def wait(self) -> int:
        return 0


class _FakeRequestProc:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = None
        self.stdin = _FakeStdin(fail_drain=True)
        self.stderr = None
        self.stdout = _FakeStdout(
            [
                {
                    "id": 1,
                    "method": "item/tool/call",
                    "params": {"tool": "ok"},
                },
            ],
            hang_after_rows=False,
        )

    async def wait(self) -> int:
        return 0


class _FakeHandlerErrorProc:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stderr = None
        self.stdout = _FakeStdout(
            [
                {
                    "id": 1,
                    "method": "item/tool/call",
                    "params": {"tool": "bad"},
                },
            ],
            hang_after_rows=False,
        )

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


async def _test_server_request_handler_error_sends_error_response() -> None:
    async def bad_handler(_: dict) -> dict:
        raise RuntimeError("handler failed")

    with tempfile.TemporaryDirectory() as tmp:
        proc = _FakeHandlerErrorProc()
        client = _AppServerProcess(proc, Path(tmp), tool_handlers={"bad": bad_handler})
        try:
            await asyncio.wait_for(client._reader_task, timeout=2)
            assert client._reader_task.exception() is None
            assert len(proc.stdin.rows) == 1, proc.stdin.rows
            response = json.loads(proc.stdin.rows[0])
            assert response == {
                "id": 1,
                "error": {"code": -32000, "message": "handler failed"},
            }, response
        finally:
            client._steer_task.cancel()
            try:
                await client._steer_task
            except asyncio.CancelledError:
                pass


async def _test_server_request_send_close_exits_reader_cleanly() -> None:
    async def ok_handler(_: dict) -> dict:
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        proc = _FakeRequestProc()
        client = _AppServerProcess(proc, Path(tmp), tool_handlers={"ok": ok_handler})
        try:
            await asyncio.wait_for(client._reader_task, timeout=2)
            assert client._reader_task.exception() is None
            assert len(proc.stdin.rows) == 1, proc.stdin.rows
        finally:
            client._steer_task.cancel()
            try:
                await client._steer_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    asyncio.run(_test_request_timeout_clears_pending_response())
    asyncio.run(_test_server_request_handler_error_sends_error_response())
    asyncio.run(_test_server_request_send_close_exits_reader_cleanly())
    print("PASS: Codex app-server request timeout clears pending response")


if __name__ == "__main__":
    main()
