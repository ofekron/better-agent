from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    control, checkout, port, pids_path = sys.argv[1:]
    manager = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "desktop.browser_backend_supervisor",
            "--control",
            control,
            "--launcher-root",
            checkout,
            "--controller-pid",
            str(os.getpid()),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while not Path(control).exists():
        if time.time() >= deadline:
            raise RuntimeError("control socket timeout")
        time.sleep(0.02)
    started = subprocess.run(
        [
            sys.executable,
            "-m",
            "desktop.browser_backend_control",
            "--control",
            control,
            "start",
            "--checkout",
            checkout,
            "--host",
            "127.0.0.1",
            "--port",
            port,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    Path(pids_path).write_text(
        json.dumps({"manager": manager.pid, "backend": int(started.stdout.strip())}),
        encoding="utf-8",
    )
    os._exit(0)


if __name__ == "__main__":
    main()
