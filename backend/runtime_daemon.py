"""better-agent-runtime daemon entry (daemon-ownership phase, skeleton).

Owns the session-root writer lock and serves the runtime IPC endpoint
for its `ba_home()`. It does not yet host the coordinator/orchestration
core — that moves here when FastAPI becomes a pure BFF; today it serves
the disk-backed runtime services (operation status) for native-first
clients and the CLI.

Refuses to start when another writer (a running monolith backend or
another daemon) holds the lock — one canonical writer per home, always.

Runs in the foreground; detaching is the CLI launcher's job. Emits one
JSON line per lifecycle event on stdout (`ready`, `stopped`) so parents
can wait on real events instead of timers.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading

import runtime_ownership
from runtime_ipc import RuntimeIPCServer

PID_FILE_NAME = "daemon.pid"


def pid_file_path():
    return runtime_ownership.runtime_dir() / PID_FILE_NAME


def _emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, "pid": os.getpid(), **fields}), flush=True)


def _lock_timeout_seconds() -> float:
    raw = os.environ.get("BETTER_AGENT_RUNTIME_LOCK_TIMEOUT", "")
    try:
        return float(raw)
    except ValueError:
        return 30.0


def main() -> int:
    try:
        runtime_ownership.acquire_runtime_writer_lock(
            blocking=True, timeout_seconds=_lock_timeout_seconds()
        )
        runtime_ownership.register_current_process_writer()
    except runtime_ownership.RuntimeOwnershipError as exc:
        _emit("error", error=str(exc))
        return 2

    stop_event = threading.Event()
    server = RuntimeIPCServer()
    server.on_shutdown_request = stop_event.set

    def _handle_signal(signum: int, _frame) -> None:
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _handle_signal)

    try:
        endpoint = server.start()
    except Exception as exc:  # noqa: BLE001 — startup must fail loud, not hang
        _emit("error", error=str(exc))
        runtime_ownership.unregister_current_process_writer()
        return 2

    pid_path = pid_file_path()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    _emit("ready", endpoint=endpoint)

    stop_event.wait()

    server.stop()
    try:
        pid_path.unlink()
    except OSError:
        pass
    runtime_ownership.unregister_current_process_writer()
    _emit("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
