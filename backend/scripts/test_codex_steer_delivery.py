from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from runner_codex import _AppServerProcess


class _Stdout:
    async def readline(self) -> bytes:
        await asyncio.Event().wait()


class _Stdin:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class _Process:
    def __init__(self) -> None:
        self.pid = 1
        self.returncode = None
        self.stdin = _Stdin()
        self.stdout = _Stdout()
        self.stderr = None


async def _close(client: _AppServerProcess, process: _Process) -> None:
    process.returncode = 0
    for task in (client._steer_task, client._reader_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_retry_and_partial_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        process = _Process()
        client = _AppServerProcess(process, run_dir)
        delivered: list[str] = []
        attempts = 0

        async def request(_method: str, params: dict, **_kwargs) -> dict:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient")
            delivered.append(params["input"][0]["text"])
            return {}

        client.request = request
        try:
            inbox = run_dir / "steer.jsonl"
            inbox.write_text('{"prompt":"fir', encoding="utf-8")
            await asyncio.sleep(0.15)
            assert delivered == []
            client.thread_id = "thread"
            client.turn_id = "turn"
            with inbox.open("a", encoding="utf-8") as file:
                file.write('st"}\n')
                file.write(json.dumps({"prompt": "second"}) + "\n")
            await asyncio.sleep(0.35)
            assert delivered == ["first", "second"]
            assert attempts >= 3
        finally:
            await _close(client, process)


if __name__ == "__main__":
    asyncio.run(test_retry_and_partial_write())
    print("PASS Codex steer delivery")
