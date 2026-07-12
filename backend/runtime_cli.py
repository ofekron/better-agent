"""`better-agent` CLI launcher for the decoupled runtime and BFF.

Client/launcher only — never owns session-root state itself. Run as
`python -m runtime_cli <command>` from the backend dir; a packaged
`better-agent` console script binds here later.

Commands:
  start          spawn the skeleton IPC daemon (writer lock + IPC only)
  stop           stop whichever runtime answers the IPC endpoint
  status         daemon/runtime/BFF liveness overview
  start-runtime  spawn the FULL runtime (whole app, API-only, on the
                 internal endpoint: unix socket / 127.0.0.1)
  stop-runtime   graceful runtime shutdown via the authenticated IPC op
  start-bff      spawn the browser-facing BFF proxy on a local port
  stop-bff       stop the BFF (SIGTERM via its pid file)

`--foreground` on the start commands runs the server in this process —
used by supervisors and tests; detached is the default.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import runtime_endpoints
import runtime_ipc
import runtime_ownership
from runtime_ipc import RuntimeIPCClient, RuntimeIPCError

_START_DEADLINE_SECONDS = 30.0
_STOP_DEADLINE_SECONDS = 15.0
_PROBE_INTERVAL_SECONDS = 0.05
LOG_FILE_NAME = "daemon.log"
RUNTIME_LOG_NAME = "runtime.log"
BFF_LOG_NAME = "bff.log"
BFF_PID_NAME = "bff.pid"
_BACKEND_DIR = Path(__file__).resolve().parent
_WINDOWS_RUNTIME_PORT_BASE = 49152
_WINDOWS_RUNTIME_PORT_COUNT = 16383


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


def _detach_kwargs() -> dict:
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def _spawn_detached(args: list[str], env: dict, log_name: str) -> subprocess.Popen:
    log_path = runtime_ownership.ensure_runtime_dir() / log_name
    with log_path.open("ab") as log_handle:
        return subprocess.Popen(
            args,
            cwd=str(_BACKEND_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **_detach_kwargs(),
        )


# ── skeleton daemon (IPC only) ────────────────────────────────────────


def cmd_start() -> int:
    alive = _ping()
    if alive is not None:
        _print({"running": True, "pid": alive.get("pid"), "already_running": True})
        return 0
    process = _spawn_detached(
        [sys.executable, "-m", "runtime_daemon"], {**os.environ}, LOG_FILE_NAME
    )
    if not _wait_for(lambda: _ping() is not None, _START_DEADLINE_SECONDS):
        log_path = runtime_ownership.runtime_dir() / LOG_FILE_NAME
        if process.poll() is not None:
            _print({"running": False, "error": "daemon exited during startup",
                    "exit_code": process.returncode, "log": str(log_path)})
        else:
            _terminate_child(process)  # no untracked daemon holding the writer lock
            _print({"running": False, "error": "daemon did not become ready",
                    "log": str(log_path)})
        return 1
    alive = _ping() or {}
    _print({"running": True, "pid": alive.get("pid")})
    return 0


def cmd_stop() -> int:
    try:
        RuntimeIPCClient().shutdown()
    except (RuntimeIPCError, ValueError):
        _print({"running": _ping() is not None, "already_stopped": _ping() is None})
        return 0 if _ping() is None else 1
    if not _wait_for(lambda: _ping() is None, _STOP_DEADLINE_SECONDS):
        _print({"running": True, "error": "runtime did not stop in time"})
        return 1
    runtime_endpoints.clear_app_endpoint()
    _print({"running": False, "stopped": True})
    return 0


# ── full runtime (whole app on the internal endpoint) ────────────────


def _runtime_descriptor() -> dict:
    if os.name == "nt":
        port = _WINDOWS_RUNTIME_PORT_BASE + (
            int(runtime_ipc.home_digest(), 16) % _WINDOWS_RUNTIME_PORT_COUNT
        )
        return {"kind": "tcp", "host": "127.0.0.1", "port": port}
    runtime_ipc.ensure_socket_dir()
    return {"kind": "uds", "path": str(runtime_endpoints.app_socket_path())}


def _runtime_env() -> dict:
    return {
        **os.environ,
        "BETTER_CLAUDE_API_ONLY": "1",
        "BETTER_AGENT_RUNTIME_MODE": "1",
        "PYTHONPATH": str(_BACKEND_DIR),
    }


def _uvicorn_args(descriptor: dict, app: str) -> list[str]:
    args = [sys.executable, "-m", "uvicorn", app]
    if descriptor["kind"] == "uds":
        # Trust forwarded headers on the UDS binding: only same-uid
        # locals (0700 socket dir) and the BFF can reach it, and the
        # BFF strips inbound forwarding headers before stamping the
        # real browser peer.
        args += ["--uds", descriptor["path"],
                 "--proxy-headers", "--forwarded-allow-ips", "*"]
    else:
        args += ["--host", descriptor["host"], "--port", str(descriptor["port"])]
    return args


def _runtime_app_alive() -> bool:
    try:
        descriptor = runtime_endpoints.read_app_endpoint()
    except runtime_endpoints.RuntimeEndpointError:
        return False
    try:
        status, _body = runtime_endpoints.http_get(descriptor, "/healthz", timeout=3.0)
    except OSError:
        return False
    return status == 200


def cmd_start_runtime(foreground: bool) -> int:
    if _runtime_app_alive():
        _print({"running": True, "already_running": True,
                "endpoint": runtime_endpoints.read_app_endpoint()})
        return 0
    descriptor = _runtime_descriptor()
    if descriptor["kind"] == "uds":
        stale = Path(descriptor["path"])
        if stale.exists():
            stale.unlink()  # nothing answered /healthz above: stale socket
    if foreground:
        runtime_endpoints.write_app_endpoint(descriptor)
        os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
        os.environ["BETTER_AGENT_RUNTIME_MODE"] = "1"
        import uvicorn

        try:
            if descriptor["kind"] == "uds":
                uvicorn.run("main:app", uds=descriptor["path"],
                            ws_per_message_deflate=False,
                            proxy_headers=True, forwarded_allow_ips="*")
            else:
                uvicorn.run("main:app", host=descriptor["host"],
                            port=descriptor["port"], ws_per_message_deflate=False)
        finally:
            runtime_endpoints.clear_app_endpoint()
        return 0
    process = _spawn_detached(
        _uvicorn_args(descriptor, "main:app"), _runtime_env(), RUNTIME_LOG_NAME
    )

    def _app_ready() -> bool:
        try:
            return runtime_endpoints.http_get(descriptor, "/healthz", timeout=2.0)[0] == 200
        except OSError:
            return False

    if not _wait_for(_app_ready, _START_DEADLINE_SECONDS):
        log_path = runtime_ownership.runtime_dir() / RUNTIME_LOG_NAME
        if process.poll() is not None:
            _print({"running": False, "error": "runtime exited during startup",
                    "exit_code": process.returncode, "log": str(log_path)})
        else:
            _terminate_child(process)  # no untracked half-up runtime
            _print({"running": False, "error": "runtime did not become ready",
                    "log": str(log_path)})
        return 1
    runtime_endpoints.write_app_endpoint(descriptor)
    _print({"running": True, "pid": process.pid, "endpoint": descriptor,
            "log": str(runtime_ownership.runtime_dir() / RUNTIME_LOG_NAME)})
    return 0


def cmd_stop_runtime() -> int:
    return cmd_stop()


# ── BFF ───────────────────────────────────────────────────────────────


def _bff_pid_path() -> Path:
    return runtime_ownership.runtime_dir() / BFF_PID_NAME


def _bff_alive(port: int, *, require_runtime: bool = True) -> bool:
    # "Ready" means the BFF is serving AND it can reach the runtime — a
    # BFF proxying to a dead runtime is not usable. stop-bff checks
    # liveness only (require_runtime=False) so teardown doesn't hang on
    # an already-dead runtime.
    try:
        status, body = runtime_endpoints.http_get(
            {"kind": "tcp", "host": "127.0.0.1", "port": port}, "/bff/healthz", timeout=2.0
        )
    except OSError:
        return False
    if status != 200:
        return False
    if not require_runtime:
        return True
    try:
        return bool(json.loads(body).get("runtime"))
    except (json.JSONDecodeError, ValueError):
        return False


def _terminate_child(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def cmd_start_bff(port: int, foreground: bool) -> int:
    try:
        runtime_endpoints.read_app_endpoint()
    except runtime_endpoints.RuntimeEndpointError as exc:
        _print({"running": False, "error": str(exc)})
        return 1
    if foreground:
        import uvicorn

        uvicorn.run("bff_server:app", host="127.0.0.1", port=port,
                    ws_per_message_deflate=False)
        return 0
    env = {**os.environ, "PYTHONPATH": str(_BACKEND_DIR)}
    process = _spawn_detached(
        [sys.executable, "-m", "uvicorn", "bff_server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env, BFF_LOG_NAME,
    )
    if not _wait_for(lambda: _bff_alive(port), _START_DEADLINE_SECONDS):
        # Never leave an untracked child: a BFF that came up but can't
        # reach the runtime is a failed start — reap it so it can't
        # proxy to a dead runtime with no pid file to stop it by.
        _terminate_child(process)
        log_path = runtime_ownership.runtime_dir() / BFF_LOG_NAME
        _print({"running": False, "error": "bff did not become ready",
                "exit_code": process.poll(), "log": str(log_path)})
        return 1
    _bff_pid_path().write_text(json.dumps({"pid": process.pid, "port": port}),
                               encoding="utf-8")
    _print({"running": True, "pid": process.pid, "port": port,
            "url": f"http://127.0.0.1:{port}"})
    return 0


def cmd_stop_bff() -> int:
    pid_path = _bff_pid_path()
    try:
        record = json.loads(pid_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _print({"running": False, "already_stopped": True})
        return 0
    pid, port = int(record["pid"]), int(record["port"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    if not _wait_for(lambda: not _bff_alive(port, require_runtime=False),
                     _STOP_DEADLINE_SECONDS):
        _print({"running": True, "error": "bff did not stop in time", "pid": pid})
        return 1
    try:
        pid_path.unlink()
    except OSError:
        pass
    _print({"running": False, "stopped": True})
    return 0


def cmd_start_stack(port: int) -> int:
    stopped = threading.Event()
    stop_signal = {"value": signal.SIGTERM}
    exits: queue.Queue[tuple[str, subprocess.Popen] | None] = queue.Queue()
    children: dict[str, subprocess.Popen | None] = {
        "runtime": None,
        "bff": None,
    }

    def watch(role: str, process: subprocess.Popen) -> None:
        process.wait()
        exits.put((role, process))

    def spawn(role: str) -> subprocess.Popen | None:
        frozen = bool(getattr(sys, "frozen", False))
        if role == "runtime":
            args = (
                [sys.executable, "--serve-runtime"]
                if frozen
                else [
                    sys.executable,
                    "-m",
                    "runtime_cli",
                    "start-runtime",
                    "--foreground",
                ]
            )
            ready = _runtime_app_alive
        else:
            args = (
                [sys.executable, "--serve-bff", "--port", str(port)]
                if frozen
                else [
                    sys.executable,
                    "-m",
                    "runtime_cli",
                    "start-bff",
                    "--foreground",
                    "--port",
                    str(port),
                ]
            )
            ready = lambda: _bff_alive(port)
        process = subprocess.Popen(
            args,
            cwd=str(_BACKEND_DIR),
            env={**os.environ, "PYTHONPATH": str(_BACKEND_DIR)},
        )
        if not _wait_for(
            lambda: process.poll() is None and ready(),
            _START_DEADLINE_SECONDS,
        ):
            _terminate_child(process)
            return None
        threading.Thread(
            target=watch,
            args=(role, process),
            name=f"better-agent-{role}-waiter",
            daemon=True,
        ).start()
        return process

    def request_stop(signum=None, _frame=None) -> None:
        if signum in (signal.SIGINT, signal.SIGTERM):
            stop_signal["value"] = signum
        stopped.set()
        exits.put(None)

    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    try:
        children["runtime"] = spawn("runtime")
        if children["runtime"] is None:
            return 1
        children["bff"] = spawn("bff")
        if children["bff"] is None:
            return 1
        while not stopped.is_set():
            event = exits.get()
            if event is None:
                continue
            role, process = event
            if children.get(role) is not process:
                continue
            if stopped.is_set():
                break
            if role == "runtime":
                try:
                    (runtime_ownership.runtime_dir().parent / "restart_requested").unlink()
                except FileNotFoundError:
                    pass
            children[role] = spawn(role)
            if children[role] is None:
                return 1
        return 0
    finally:
        bff = children["bff"]
        if bff is not None:
            _terminate_child(bff)
        runtime = children["runtime"]
        if runtime is not None:
            if runtime.poll() is None:
                runtime.send_signal(stop_signal["value"])
                try:
                    runtime.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    runtime.kill()
                    runtime.wait(timeout=10)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


# ── status ────────────────────────────────────────────────────────────


def cmd_status() -> int:
    ipc = _ping()
    app_alive = _runtime_app_alive()
    bff = None
    try:
        record = json.loads(_bff_pid_path().read_text(encoding="utf-8"))
        bff = {"pid": record["pid"], "port": record["port"],
               "running": _bff_alive(int(record["port"]))}
    except (OSError, json.JSONDecodeError):
        pass
    _print({
        "ipc": {"running": ipc is not None, **({"pid": ipc["pid"]} if ipc else {})},
        "runtime_app": {"running": app_alive},
        "bff": bff or {"running": False},
    })
    return 0 if (ipc is not None or app_alive) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="better-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    start_runtime = sub.add_parser("start-runtime")
    start_runtime.add_argument("--foreground", action="store_true")
    sub.add_parser("stop-runtime")
    start_bff = sub.add_parser("start-bff")
    start_bff.add_argument("--port", type=int, default=8787)
    start_bff.add_argument("--foreground", action="store_true")
    sub.add_parser("stop-bff")
    start_stack = sub.add_parser("start-stack")
    start_stack.add_argument(
        "--port",
        type=int,
        default=int(
            os.environ.get(
                "BETTER_AGENT_BACKEND_PORT",
                os.environ.get("BETTER_CLAUDE_BACKEND_PORT", "8000"),
            )
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "start":
        return cmd_start()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "status":
        return cmd_status()
    if args.command == "start-runtime":
        return cmd_start_runtime(args.foreground)
    if args.command == "stop-runtime":
        return cmd_stop_runtime()
    if args.command == "start-bff":
        return cmd_start_bff(args.port, args.foreground)
    if args.command == "start-stack":
        return cmd_start_stack(args.port)
    return cmd_stop_bff()


if __name__ == "__main__":
    sys.exit(main())
