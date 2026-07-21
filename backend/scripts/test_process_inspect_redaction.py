#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from process_inspect import inspect_process_tree


def main() -> None:
    secret = "must-not-reach-run-details"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            f"BETTER_CLAUDE_INTERNAL_TOKEN={secret}",
            "--api-key",
            secret,
        ]
    )
    try:
        time.sleep(0.1)
        processes = inspect_process_tree(proc.pid)
        assert processes
        command = processes[0]["command"]
        assert secret not in command
        assert "BETTER_CLAUDE_INTERNAL_TOKEN=[REDACTED]" in command
        assert "--api-key [REDACTED]" in command
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    print("process inspection redaction regression test passed")


if __name__ == "__main__":
    main()
