from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import runner_codex  # noqa: E402


_FIXTURE = r"""
import os
import signal
import subprocess
import sys
import time

ignore = sys.argv[1] == "ignore"
if ignore:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
if ignore:
    time.sleep(300)
sys.stdin.buffer.read()
"""


class _OwnedProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.pid = process.pid
        self.process_group_id = runner_codex._process_control().owned_group_id(process.pid)

    @property
    def returncode(self):
        return self._process.returncode

    async def close_input(self) -> None:
        assert self._process.stdin is not None
        self._process.stdin.close()
        await self._process.stdin.wait_closed()

    async def wait(self) -> int:
        return await self._process.wait()


async def _wait_dead(pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while runner_codex._process_control().pid_alive(pid):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"process remained alive: {pid}")
        await asyncio.sleep(0.01)


async def _run_case(*, forced: bool) -> None:
    spawn_kwargs = runner_codex._process_control().detach_spawn_kwargs()
    app = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _FIXTURE,
        "ignore" if forced else "graceful",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **spawn_kwargs,
    )
    sibling = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(300)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **spawn_kwargs,
    )
    assert app.stdout is not None
    child_pid = int((await app.stdout.readline()).decode("ascii"))
    owned = _OwnedProcess(app)
    try:
        await runner_codex._settle_app_server_process(
            owned,
            rollout_terminal_completion=not forced,
            stop_requested=forced,
            log=logging.getLogger("test"),
        )
        await _wait_dead(app.pid)
        await _wait_dead(child_pid)
        assert runner_codex._process_control().pid_alive(sibling.pid)
    finally:
        runner_codex._process_control().force_kill_owned_group(
            runner_codex._process_control().owned_group_id(sibling.pid)
        )
        await sibling.wait()
        if app.returncode is None:
            runner_codex._process_control().force_kill_owned_group(owned.process_group_id)
            await app.wait()


async def main() -> None:
    await _run_case(forced=False)
    await _run_case(forced=True)
    print("PASS Codex app-server cleanup is group-owned and sibling-isolated")


if __name__ == "__main__":
    asyncio.run(main())
