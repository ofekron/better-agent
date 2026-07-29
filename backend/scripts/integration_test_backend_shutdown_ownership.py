from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before deadline")


def _listener_accepts(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def test_active_cpu_search_releases_backend_process_on_shutdown() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        started = temp_root / "started"
        stopped = temp_root / "stopped"
        port = _unused_port()
        env = os.environ.copy()
        env.update(
            {
                "BETTER_AGENT_HOME": str(temp_root / "state"),
                "SHUTDOWN_TEST_STARTED": str(started),
                "SHUTDOWN_TEST_STOPPED": str(stopped),
                "PYTHONPATH": str(ROOT),
            }
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.fixtures.shutdown_requirements_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-graceful-shutdown",
                "1",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        def request_work() -> None:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/work",
                    timeout=10,
                ).read()
            except Exception:
                return

        request_thread = threading.Thread(target=request_work, daemon=True)
        try:
            _wait_until(lambda: _listener_accepts(port), 10)
            request_thread.start()
            _wait_until(started.exists, 10)
            process.send_signal(signal.SIGTERM)
            _wait_until(lambda: not _listener_accepts(port), 5)
            process.wait(timeout=5)
            request_thread.join(timeout=2)

            assert stopped.exists(), "CPU worker survived request cancellation"
            with socket.socket() as replacement:
                replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                replacement.bind(("127.0.0.1", port))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    test_active_cpu_search_releases_backend_process_on_shutdown()
    print("PASS active CPU search releases backend process on shutdown")
