from __future__ import annotations

import argparse
import json
import os
import signal
import select
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from credential_session import ProviderCredentialBroker, ProviderCredentialSession

_MAX_CONTROL_BYTES = 32 * 1024


def _parent_pid(pid: int) -> int | None:
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("PPid:"):
                    return int(line.split(":", 1)[1].strip())
        except (OSError, ValueError):
            return None
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _peer_pid(connection: socket.socket) -> int | None:
    if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[0]
    if sys.platform == "darwin":
        raw = connection.getsockopt(0, 2, struct.calcsize("i"))
        return struct.unpack("i", raw)[0]
    return None


def _is_direct_child(pid: int, parent_pid: int) -> bool:
    return _parent_pid(pid) == parent_pid


def _arm_controller_death(controller_pid: int, on_death: Any) -> None:
    if os.getppid() != controller_pid:
        raise RuntimeError("credential supervisor is not owned by the launcher")
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:
            raise OSError(ctypes.get_errno(), "failed to arm parent-death signal")
        if os.getppid() != controller_pid:
            os.kill(os.getpid(), signal.SIGTERM)
        return
    if sys.platform == "darwin":
        queue = select.kqueue()
        event = select.kevent(
            controller_pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
            fflags=select.KQ_NOTE_EXIT,
        )
        queue.control([event], 0)
        if os.getppid() != controller_pid:
            queue.close()
            on_death()
            return

        def wait_for_exit() -> None:
            try:
                queue.control(None, 1)
                on_death()
            finally:
                queue.close()

        threading.Thread(
            target=wait_for_exit,
            name="browser-controller-watch",
            daemon=True,
        ).start()
        return
    raise RuntimeError("controller death monitoring is unavailable")


class BrowserBackendSupervisor:
    def __init__(self, launcher_root: Path, base_env: dict[str, str]) -> None:
        self._launcher_root = launcher_root.resolve()
        self._base_env = dict(base_env)
        for key in (
            "BETTER_AGENT_CREDENTIAL_SESSION_ADDRESS",
            "BETTER_AGENT_CREDENTIAL_SESSION_AUTH",
            "BETTER_AGENT_CREDENTIAL_SESSION_FAMILY",
            "BETTER_AGENT_CREDENTIAL_SESSION_FD",
        ):
            self._base_env.pop(key, None)
        self._broker = ProviderCredentialBroker()
        self._session: ProviderCredentialSession | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._last_exit_code: int | None = None
        self._lock = threading.RLock()
        self._stopping = threading.Event()

    def handle(self, request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        op = request.get("op")
        if op == "start":
            return self._start(request)
        if op == "status":
            return self._status()
        if op == "signal":
            self._signal_backend(request.get("signal"))
            return self._status()
        if op == "shutdown":
            self.shutdown()
            return {"ok": True}
        raise ValueError("unsupported operation")

    def _resolved_checkout(self) -> Path:
        from daemonhost import pointer

        checkout = Path(pointer.resolve(str(self._launcher_root))).resolve()
        python = checkout / "backend" / ".venv" / "bin" / "python"
        if not python.is_file() or not (checkout / "backend" / "main.py").is_file():
            raise RuntimeError("resolved checkout is not runnable")
        return checkout

    def _start(self, request: dict[str, Any]) -> dict[str, Any]:
        host = request.get("host")
        port = request.get("port")
        requested_checkout = request.get("checkout")
        if host not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError("invalid backend host")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("invalid backend port")
        checkout = self._resolved_checkout()
        if not isinstance(requested_checkout, str) or Path(requested_checkout).resolve() != checkout:
            raise ValueError("active checkout changed before backend start")
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("backend is already running")
            self._retire_generation_locked()
            session = self._broker.open_session()
            session.start()
            env = {
                **self._base_env,
                **session.backend_env(),
                "BETTER_AGENT_ACTIVE_CHECKOUT": str(checkout),
                "BETTER_AGENT_BACKEND_PORT": str(port),
                "BETTER_AGENT_BACKEND_URL": f"http://127.0.0.1:{port}",
                "BETTER_CLAUDE_BACKEND_PORT": str(port),
                "BETTER_CLAUDE_BACKEND_URL": f"http://127.0.0.1:{port}",
                "BA_BACKEND_PORT": str(port),
            }
            command = [
                str(checkout / "backend" / ".venv" / "bin" / "python"),
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                host,
                "--port",
                str(port),
                "--no-proxy-headers",
                "--ws-per-message-deflate",
                "false",
            ]
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=checkout / "backend",
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    **session.backend_popen_kwargs(),
                )
            except Exception:
                session.stop()
                raise
            session.revoke_backend_inheritance()
            self._session = session
            self._proc = proc
            self._last_exit_code = None
            threading.Thread(
                target=self._forward_output,
                args=(proc,),
                name="browser-backend-output",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._watch_generation,
                args=(proc, session),
                name="browser-backend-watch",
                daemon=True,
            ).start()
            return {"ok": True, "pid": proc.pid}

    def _forward_output(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        from paths import bc_home

        log_path = bc_home() / "backend-run.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()

    def _watch_generation(
        self,
        proc: subprocess.Popen[str],
        session: ProviderCredentialSession,
    ) -> None:
        returncode = proc.wait()
        with self._lock:
            session.stop()
            if self._proc is proc:
                self._last_exit_code = returncode
                self._proc = None
                self._session = None

    def _status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is None:
                return {"ok": True, "running": False, "returncode": self._last_exit_code}
            return {
                "ok": True,
                "running": proc.poll() is None,
                "pid": proc.pid,
                "returncode": proc.poll(),
            }

    def _signal_backend(self, requested_signal: object) -> None:
        signals = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "KILL": signal.SIGKILL}
        if requested_signal not in signals:
            raise ValueError("invalid signal")
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                proc.send_signal(signals[requested_signal])

    def _retire_generation_locked(self) -> None:
        session = self._session
        self._session = None
        self._proc = None
        if session is not None:
            session.stop()

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        with self._lock:
            self._retire_generation_locked()
            self._broker.clear()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()


def _recv_request(connection: socket.socket) -> object:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(4096, _MAX_CONTROL_BYTES - size + 1))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > _MAX_CONTROL_BYTES:
            raise ValueError("control request is too large")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    return json.loads(raw.decode("utf-8"))


def serve(control_path: Path, launcher_root: Path, controller_pid: int) -> int:
    control_path = control_path.resolve()
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.parent.chmod(0o700)
    try:
        control_path.unlink()
    except FileNotFoundError:
        pass
    supervisor = BrowserBackendSupervisor(launcher_root, dict(os.environ))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(control_path))
    control_path.chmod(0o600)
    server.listen(8)
    server.settimeout(0.5)
    previous_handlers = {
        signum: signal.signal(signum, lambda *_: supervisor.shutdown())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    _arm_controller_death(controller_pid, supervisor.shutdown)
    try:
        while not supervisor.stopping:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                try:
                    peer_pid = _peer_pid(connection)
                    if peer_pid is None or not _is_direct_child(peer_pid, controller_pid):
                        raise PermissionError("control caller is not a direct launcher child")
                    response = supervisor.handle(_recv_request(connection))
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                try:
                    connection.sendall(
                        json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                    )
                except OSError:
                    pass
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        supervisor.shutdown()
        server.close()
        try:
            control_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--launcher-root", required=True, type=Path)
    parser.add_argument("--controller-pid", required=True, type=int)
    args = parser.parse_args(argv)
    return serve(args.control, args.launcher_root, args.controller_pid)


if __name__ == "__main__":
    raise SystemExit(main())
