#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import socket
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="ba-run-sh-normal-exit-") as home:
        env = os.environ.copy()
        env["BETTER_AGENT_HOME"] = home
        env["BETTER_CLAUDE_HOME"] = home
        env["BETTER_AGENT_RUN_SH_TEST_NORMAL_EXIT_CLEANUP"] = "1"
        env["BETTER_AGENT_RUN_SH_SERVICE_CHILD"] = "1"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            env["BETTER_AGENT_BACKEND_PORT"] = str(listener.getsockname()[1])
        proc = subprocess.run(
            ["bash", str(repo / "run.sh")],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )

    stdout = proc.stdout
    if proc.returncode != 0:
        raise AssertionError(f"expected normal exit code 0, got {proc.returncode}\n{stdout}")
    match = re.search(
        r"Normal exit cleanup test ready: frontend=(\d+) daemon=(\d+) checker=(\d+)",
        stdout,
    )
    if not match:
        raise AssertionError(f"run.sh did not reach normal cleanup test hook\n{stdout}")

    frontend_pid, daemon_pid, checker_pid = (int(value) for value in match.groups())
    stopped = [pid for pid in (frontend_pid, daemon_pid) if not _pid_alive(pid)]
    alive_owned = [pid for pid in (frontend_pid, daemon_pid) if _pid_alive(pid)]
    checker_alive = _pid_alive(checker_pid)
    if checker_alive:
        _terminate(checker_pid)
    if alive_owned or not checker_alive:
        raise AssertionError(
            "normal exit cleanup did not match ownership contract: "
            f"stopped={stopped} alive_owned={alive_owned} checker_alive={checker_alive}\n{stdout}"
        )
    print("run.sh normal exit cleanup passed")


if __name__ == "__main__":
    main()
