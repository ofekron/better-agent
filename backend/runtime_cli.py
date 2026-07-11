"""`better-agent` CLI launcher for the runtime daemon (CLI phase seed).

Client/launcher only — never owns session-root state itself. Run as
`python -m runtime_cli <command>` from the backend dir; a packaged
`better-agent` console script binds here later.

Commands:
  start   spawn a detached runtime daemon for the current home
  stop    ask the running daemon to shut down (authenticated IPC op)
  status  report daemon liveness via an authenticated ping
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import runtime_ownership
from runtime_ipc import RuntimeIPCClient, RuntimeIPCError

_START_DEADLINE_SECONDS = 15.0
_STOP_DEADLINE_SECONDS = 15.0
_PROBE_INTERVAL_SECONDS = 0.05
LOG_FILE_NAME = "daemon.log"


def _ping() -> dict | None:
    try:
        return RuntimeIPCClient().ping()
    except RuntimeIPCError:
        return None


def _print(payload: dict) -> None:
    print(json.dumps(payload))


def _wait_for(predicate, deadline_seconds: float) -> bool:
    # The authenticated connect IS the readiness/teardown event; the
    # loop only bounds how long we keep asking for it.
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_PROBE_INTERVAL_SECONDS)
    return predicate()


def cmd_start() -> int:
    alive = _ping()
    if alive is not None:
        _print({"running": True, "pid": alive.get("pid"), "already_running": True})
        return 0
    log_path = runtime_ownership.ensure_runtime_dir() / LOG_FILE_NAME
    spawn_kwargs: dict = {}
    if sys.platform == "win32":
        spawn_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        spawn_kwargs["start_new_session"] = True
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "runtime_daemon"],
            cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **spawn_kwargs,
        )
    if not _wait_for(lambda: _ping() is not None, _START_DEADLINE_SECONDS):
        if process.poll() is not None:
            _print({"running": False, "error": "daemon exited during startup",
                    "exit_code": process.returncode, "log": str(log_path)})
        else:
            _print({"running": False, "error": "daemon did not become ready",
                    "log": str(log_path)})
        return 1
    alive = _ping() or {}
    _print({"running": True, "pid": alive.get("pid"), "log": str(log_path)})
    return 0


def cmd_stop() -> int:
    try:
        RuntimeIPCClient().shutdown()
    except RuntimeIPCError:
        _print({"running": False, "already_stopped": True})
        return 0
    if not _wait_for(lambda: _ping() is None, _STOP_DEADLINE_SECONDS):
        _print({"running": True, "error": "daemon did not stop in time"})
        return 1
    _print({"running": False, "stopped": True})
    return 0


def cmd_status() -> int:
    alive = _ping()
    if alive is None:
        _print({"running": False})
        return 1
    _print({
        "running": True,
        "pid": alive.get("pid"),
        "endpoint": alive.get("endpoint"),
        "schema_version": alive.get("schema_version"),
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="better-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
