#!/usr/bin/env python3
from __future__ import annotations

import os
import re
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


def _descendants(pid: int) -> list[int]:
    child_output = subprocess.run(
        ["pgrep", "-P", str(pid)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    ).stdout
    children = [int(item) for item in child_output.split() if item.isdigit()]
    result: list[int] = []
    for child in children:
        result.append(child)
        result.extend(_descendants(child))
    return result


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="ba-run-sh-signal-") as home:
        env = os.environ.copy()
        env["BETTER_AGENT_HOME"] = home
        env["BETTER_CLAUDE_HOME"] = home
        env["BETTER_AGENT_RUN_SH_TEST_SIGNAL_CLEANUP"] = "1"
        proc = subprocess.Popen(
            ["bash", str(repo / "run.sh")],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        output_lines: list[str] = []
        ready_line = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line:
                output_lines.append(line)
                if line.startswith("Signal cleanup test ready:"):
                    ready_line = line
                    break
            elif proc.poll() is not None:
                break
        if not ready_line:
            proc.kill()
            raise AssertionError(f"run.sh did not reach signal cleanup test hook\n{''.join(output_lines)}")

        tracked_pids = [int(pid) for pid in re.findall(r"=(\d+)", ready_line)]
        descendant_pids: list[int] = []
        for pid in tracked_pids:
            descendant_pids.extend(_descendants(pid))
        if len(descendant_pids) < 4:
            proc.kill()
            raise AssertionError(
                f"expected recursive descendants, got {descendant_pids}\n{''.join(output_lines)}"
            )

        os.kill(proc.pid, signal.SIGINT)
        remaining_stdout, _ = proc.communicate(timeout=15)
        stdout = "".join(output_lines) + remaining_stdout

    if proc.returncode != 130:
        raise AssertionError(f"expected SIGINT exit code 130, got {proc.returncode}\n{stdout}")
    pids = [int(pid) for pid in re.findall(r"PID ([0-9]+)", stdout)]
    if len(pids) != 3:
        raise AssertionError(f"expected three child cleanup lines, got {pids}\n{stdout}")
    alive = [pid for pid in [*tracked_pids, *descendant_pids] if _pid_alive(pid)]
    if alive:
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise AssertionError(f"child processes survived cleanup: {alive}\n{stdout}")
    print("run.sh signal cleanup passed")


if __name__ == "__main__":
    main()
