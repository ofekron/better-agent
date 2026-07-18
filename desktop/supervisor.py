"""Backend subprocess lifecycle for the Better Agent desktop shell.

The shell runs the FastAPI backend as a supervised CHILD process — never
in-process: `/api/admin/restart` makes the backend terminate itself, and
an in-process backend would take the GUI down with it.

`BackendSupervisor` owns: spawning the backend with the user's real PATH
(see `shell_env.capture_login_path`), waiting for `/healthz`, deciding
restart-vs-quit from the `restart_requested` flag file, and stopping the
backend with the signal the close-dialog chose.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from collections.abc import Callable
from typing import Literal, Optional, TypedDict

from shell_env import capture_login_path

logger = logging.getLogger(__name__)

BACKEND_PORT = 8000  # fixed so other LAN devices can reach the backend
NODE_PORT = 8002
BackendRole = Literal["primary", "node"]
PortConflictAction = Literal["kill", "use_port"]


class PortListener(TypedDict):
    pid: int
    command: str


class PortConflictResolution(TypedDict):
    action: PortConflictAction
    port: int


PortConflictHandler = Callable[
    [int, list[PortListener]], Optional[PortConflictResolution]
]

# Combined log-disk budget across `shell.log` + `backend.log` is ~0.5 GB.
# The backend gets the bulk because uvicorn access logs are the volume;
# `desktop/shell.py` configures its own `shell.log` rotation.
_BACKEND_LOG_MAX_BYTES = 50 * 1024 * 1024   # 50 MB per file
_BACKEND_LOG_BACKUPS = 8                    # 9 files × 50 MB ≈ 450 MB

_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
# Source-mode import path for the platform daemon host package (repo root);
# in the frozen bundle it ships as a bundled module instead.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Put backend/ on the import path so desktop modules can reach `paths`,
# `auth_secrets`, etc. (in the frozen bundle these are bundled modules).
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from env_compat import dual_env_many


def _ba_home() -> Path:
    """The Better Agent state dir — resolved via `backend/paths.ba_home()`
    so configured state-home env vars are honored and the dir is never hardcoded."""
    from paths import ba_home
    return ba_home()


def _backend_lock_path() -> Path:
    return _ba_home() / "backend.lock"


def _backend_lock_holder_pid() -> Optional[int]:
    try:
        text = _backend_lock_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key != "pid":
            continue
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _process_command(pid: int) -> str:
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "commandline":
                return value.strip()
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _is_better_agent_backend_command(command: str) -> bool:
    if not command:
        return False
    backend_dir = str(_BACKEND_DIR)
    app_entry = str(_BACKEND_DIR / "app_entry.py")
    if backend_dir in command and "uvicorn" in command and "main:app" in command:
        return True
    if app_entry in command and "--serve" in command:
        return True
    return bool(getattr(sys, "frozen", False) and "--serve" in command and ("Better Agent" in command or "Better Agent.exe" in command))


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        from proc_control import process_control

        return process_control().pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if proc.returncode != 0:
        return False
    return "Z" not in proc.stdout.strip()


def _signal_stop_backend(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        logger.exception("failed to terminate backend lock holder pid %d", pid)


def _force_kill_backend(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        logger.exception("failed to force-kill backend lock holder pid %d", pid)


def kill_backend_lock_holder(*, timeout: float = 5.0) -> bool:
    pid = _backend_lock_holder_pid()
    if pid is None or pid == os.getpid() or not _process_exists(pid):
        return True
    command = _process_command(pid)
    if not _is_better_agent_backend_command(command):
        logger.warning(
            "backend lock holder pid %s was not recognized as Better Agent backend: %s",
            pid,
            command,
        )
        return False
    logger.info("terminating previous Better Agent backend lock holder pid %s", pid)
    _signal_stop_backend(pid)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.25)

    _force_kill_backend(pid)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.1)
    return not _process_exists(pid)


def _checkout_python(checkout: Path) -> Path:
    for path in (
        checkout / "backend" / ".venv" / "bin" / "python",
        checkout / "backend" / ".venv" / "Scripts" / "python.exe",
    ):
        if path.is_file():
            return path
    raise RuntimeError(f"Line checkout has no backend interpreter: {checkout}")


def backend_argv(role: BackendRole = "primary", checkout: Path | None = None) -> list[str]:
    """argv to start the backend server.

    Frozen: re-exec the single app binary with `--serve` — `app_main.py`
    dispatches that to the server role.
    Dev: run `backend/app_entry.py --serve` on the current interpreter.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--serve-node" if role == "node" else "--serve"]
    checkout = (checkout or _REPO_ROOT).resolve()
    app_entry = checkout / "backend" / "app_entry.py"
    if not app_entry.is_file():
        raise RuntimeError(f"Line checkout has no backend entrypoint: {checkout}")
    return [
        str(_checkout_python(checkout)),
        str(app_entry),
        "--serve-node" if role == "node" else "--serve",
    ]


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _listener_pids(port: int) -> list[int]:
    try:
        proc = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode not in (0, 1):
        return []
    own_pid = os.getpid()
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != own_pid and pid not in pids:
            pids.append(pid)
    return pids


def port_listener_details(port: int) -> list[PortListener]:
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode not in (0, 1):
        return []
    own_pid = os.getpid()
    listeners: list[PortListener] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        listeners.append({"pid": pid, "command": parts[0]})
    return listeners


def kill_port_listeners(port: int, *, timeout: float = 5.0) -> bool:
    pids = _listener_pids(port)
    if not pids:
        return True
    logger.info("terminating listener(s) on port %d: %s", port, pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            logger.exception("failed to terminate listener pid %d", pid)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_free(port):
            return True
        time.sleep(0.25)

    remaining = _listener_pids(port)
    if remaining:
        logger.warning(
            "force-killing listener(s) on port %d: %s", port, remaining,
        )
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            logger.exception("failed to force-kill listener pid %d", pid)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if port_is_free(port):
            return True
        time.sleep(0.1)
    return port_is_free(port)


class BackendSupervisor:
    """Spawns and supervises the backend child process."""

    def __init__(self, role: BackendRole = "primary", port: Optional[int] = None) -> None:
        self.role = role
        self.port = port or (NODE_PORT if role == "node" else BACKEND_PORT)
        self.health_url = self._health_url()
        self._proc: Optional[subprocess.Popen] = None
        self._backend_logger: Optional[logging.Logger] = None
        self._daemon_host = None
        self._daemon_host_thread: Optional[threading.Thread] = None
        self._active_checkout = _REPO_ROOT.resolve()
        # The backend — and every runner it spawns — inherits this PATH so
        # `claude`/`gemini`/`node` resolve under launchd's stripped PATH.
        self._env = {
            **os.environ,
            "PATH": capture_login_path(),
            **dual_env_many({"BETTER_CLAUDE_DESKTOP_SHELL": "1"}),
        }

    def start(
        self, on_port_conflict: Optional[PortConflictHandler] = None,
    ) -> None:
        """Spawn the backend after resolving any occupied port with the
        caller. Desktop startup must not kill arbitrary listeners without
        explicit user permission."""
        if self.role == "primary" and not kill_backend_lock_holder():
            raise RuntimeError("Another Better Agent backend is already using this state directory.")
        while not port_is_free(self.port):
            listeners = port_listener_details(self.port)
            if on_port_conflict is None:
                raise RuntimeError(self._port_conflict_message(listeners))
            resolution = on_port_conflict(self.port, listeners)
            if resolution is None:
                raise RuntimeError(self._port_conflict_message(listeners))
            if resolution["action"] == "kill":
                if not kill_port_listeners(self.port):
                    raise RuntimeError(self._port_conflict_message(listeners))
                continue
            self.port = resolution["port"]
            self.health_url = self._health_url()
        self._set_port_env()
        self._proc = self._spawn_backend()
        if self.role == "primary":
            self._start_daemon_host()

    def _start_daemon_host(self) -> None:
        """Run the platform daemon host in-process (macOS/Windows parity: the
        frozen bundle has no python interpreter to spawn it with). It
        supervises supervisor-lifecycle extension daemons across backend
        restarts; on desktop they live and die with the app."""
        if self._daemon_host_thread is not None and self._daemon_host_thread.is_alive():
            return
        try:
            from daemonhost.host import DaemonHost
        except ImportError:
            logger.exception("daemon host unavailable; supervisor daemons disabled")
            return
        self._daemon_host = DaemonHost()
        self._daemon_host_thread = threading.Thread(
            target=self._daemon_host.run, name="daemon-host", daemon=True
        )
        self._daemon_host_thread.start()

    def _stop_daemon_host(self) -> None:
        host, thread = self._daemon_host, self._daemon_host_thread
        self._daemon_host = None
        self._daemon_host_thread = None
        if host is not None:
            host.stop()
        if thread is not None and thread.is_alive():
            thread.join(timeout=35)

    def wait_healthy(self, timeout: float = 30.0) -> bool:
        """Poll `/healthz` until the backend answers, the process dies, or
        `timeout` elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(self.health_url, timeout=2) as r:
                    if r.status == 200:
                        self._confirm_active_checkout()
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.25)
        return False

    def wait_exit(self) -> int:
        """Block until the backend process exits; return its exit code."""
        if self._proc is None:
            return 0
        flag = _ba_home() / "restart_requested"
        while self._proc.poll() is None:
            if flag.exists():
                _signal_stop_backend(self._proc.pid)
            time.sleep(0.1)
        return int(self._proc.returncode or 0)

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def restart_was_requested(self) -> bool:
        """True if the backend exited because of `/api/admin/restart` —
        detected via the `restart_requested` flag file the endpoint writes.
        The flag is consumed so a later crash isn't mistaken for a
        restart."""
        flag = _ba_home() / "restart_requested"
        if flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass
            return True
        return False

    def restart(self) -> bool:
        """Respawn the backend and wait for it to become healthy again.
        Briefly polls for `BACKEND_PORT` to free first — the previous
        backend just exited, but a slow shutdown can hold the socket for
        a moment; spawning before that would die silently on bind."""
        deadline = time.monotonic() + 3.0
        while not port_is_free(self.port):
            if time.monotonic() >= deadline:
                logger.error(
                    "restart aborted: port %d still in use", self.port,
                )
                return False
            time.sleep(0.1)
        try:
            self._proc = self._spawn_backend()
        except RuntimeError as exc:
            if not self._recover_failed_switch(str(exc)):
                return False
            self._proc = self._spawn_backend()
        if self.wait_healthy():
            return True
        if not self._recover_failed_switch("backend failed to become healthy"):
            return False
        self._proc = self._spawn_backend()
        return self.wait_healthy()

    def _health_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/healthz"

    def _set_port_env(self) -> None:
        legacy_key = "BETTER_CLAUDE_NODE_PORT" if self.role == "node" else "BETTER_CLAUDE_BACKEND_PORT"
        self._env.update(dual_env_many({legacy_key: str(self.port)}))
        if self.role == "primary":
            self._env.update(dual_env_many({"BETTER_CLAUDE_BACKEND_URL": self.local_url().rstrip("/")}))

    def _port_conflict_message(self, listeners: list[PortListener]) -> str:
        if not listeners:
            return (
                f"Port {self.port} is already in use, but Better Agent "
                "could not identify the listener."
            )
        details = ", ".join(
            f"{listener['command']} (PID {listener['pid']})"
            for listener in listeners
        )
        return f"Port {self.port} is already in use by {details}."

    def _spawn_backend(self) -> subprocess.Popen:
        """Popen the backend with stdout+stderr piped through a daemon
        forwarder thread into a size-rotated `ba_home()/backend.log`.
        Without capture every uvicorn line and Python traceback from a
        Finder-launched .app vanishes into a closed fd; without
        rotation the file would grow unbounded."""
        self._ensure_backend_logger()
        checkout = self._resolved_checkout()
        self._active_checkout = checkout
        self._env.update(dual_env_many({
            "BETTER_CLAUDE_ACTIVE_CHECKOUT": str(checkout),
            "BETTER_CLAUDE_RUN_SH_SUPERVISOR": "1",
        }))
        proc = subprocess.Popen(
            backend_argv(self.role, checkout), env=self._env, cwd=checkout / "backend",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,  # line-buffered text stream
        )
        threading.Thread(
            target=self._forward_backend_output, args=(proc,),
            daemon=True, name="backend-log-forwarder",
        ).start()
        return proc

    def _resolved_checkout(self) -> Path:
        if self.role != "primary" or getattr(sys, "frozen", False):
            return _REPO_ROOT.resolve()
        from daemonhost import pointer

        checkout = Path(pointer.resolve(str(_REPO_ROOT))).resolve()
        if not (checkout / "frontend" / "dist" / "index.html").is_file():
            raise RuntimeError(f"Line checkout has no built frontend: {checkout}")
        _checkout_python(checkout)
        return checkout

    def _confirm_active_checkout(self) -> None:
        if self.role != "primary" or getattr(sys, "frozen", False):
            return
        from daemonhost import pointer, switch_control
        from daemonhost.jsonio import write_json
        from daemonhost.paths import refresh_result_path

        request_id = str(pointer.read().get("request_id") or "")
        pointer.confirm_healthy(str(self._active_checkout), request_id)
        if request_id:
            write_json(refresh_result_path(), {
                "request_id": request_id,
                "status": "succeeded",
                "error": None,
            })
            switch_control.service_tick(str(self._active_checkout))

    def _recover_failed_switch(self, error: str) -> bool:
        from daemonhost import pointer, switch_control
        from daemonhost.jsonio import write_json
        from daemonhost.paths import refresh_result_path

        pointer_data = pointer.read()
        if pointer_data.get("status") != "switching":
            return False
        request_id = str(pointer_data.get("request_id") or "")
        pointer.revert(error, request_id)
        write_json(refresh_result_path(), {
            "request_id": request_id,
            "status": "failed",
            "error": error,
        })
        switch_control.service_tick(str(self._active_checkout))
        return True

    def _ensure_backend_logger(self) -> None:
        """Configure the rotating `backend.log` handler once. Idempotent
        so repeated restarts don't stack handlers (multi-writer to one
        rotating file is unsafe; only the supervisor writes here)."""
        if self._backend_logger is not None:
            return
        log_path = _ba_home() / "backend.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        backend_logger = logging.getLogger("better_agent.backend")
        backend_logger.setLevel(logging.INFO)
        backend_logger.propagate = False
        for h in list(backend_logger.handlers):
            backend_logger.removeHandler(h)
        handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=_BACKEND_LOG_MAX_BYTES,
            backupCount=_BACKEND_LOG_BACKUPS,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        backend_logger.addHandler(handler)
        self._backend_logger = backend_logger

    def _forward_backend_output(self, proc: subprocess.Popen) -> None:
        """Daemon thread: drain the backend child's merged stdout/stderr
        line-by-line into the rotating backend logger. Exits cleanly on
        EOF when the child ends."""
        if proc.stdout is None or self._backend_logger is None:
            return
        try:
            for line in proc.stdout:
                self._backend_logger.info(line.rstrip())
        except Exception:
            logger.exception("backend log forwarder crashed")

    def shutdown(self, *, kill_runners: bool) -> None:
        """Stop the backend. `kill_runners=True` writes the explicit
        kill flag before SIGINT; `False` sends SIGTERM and leaves the
        detached runners alive to finish on their own. Hard-kills if it
        hangs."""
        self._stop_daemon_host()
        if self._proc is None or self._proc.poll() is not None:
            return
        flag = _ba_home() / "kill_runners_requested"
        if kill_runners:
            try:
                flag.write_text("1", encoding="utf-8")
            except OSError:
                logger.exception("failed to write kill-runners shutdown flag")
        else:
            try:
                flag.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("failed to clear kill-runners shutdown flag")
        self._proc.send_signal(
            signal.SIGINT if kill_runners else signal.SIGTERM
        )
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
