from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

MAX_MESSAGE_BYTES = 2048
ActivationEvent = dict[str, str | int]
_EVENT_FIELDS = {
    "activate": {"type"},
    "marketplace_pair": {"type", "intent", "version"},
}


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
    import ctypes
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            process,
            ctypes.byref(exit_code),
        ):
            return False
        return exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def _valid_event(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return False
    expected = _EVENT_FIELDS.get(value["type"])
    if expected is None or set(value) != expected:
        return False
    if value["type"] == "activate":
        return True
    return (
        isinstance(value.get("intent"), str)
        and isinstance(value.get("version"), int)
        and not isinstance(value.get("version"), bool)
        and value["version"] == 1
    )


class ActivationServer:
    def __init__(self, state_dir: Path, on_activation: Callable[[ActivationEvent], None]):
        self._state_path = state_dir / "desktop_activation.json"
        self._owner_path = state_dir / "desktop_activation.owner"
        self._on_activation = on_activation
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._secret = secrets.token_urlsafe(32)
        self._connections = threading.BoundedSemaphore(4)
        self._owner_fd: int | None = None

    def start(self) -> None:
        self._claim_ownership()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
        except OSError:
            self._release_ownership()
            raise
        listener.listen(4)
        listener.settimeout(0.5)
        self._socket = listener
        self._write_state(listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _claim_ownership(self) -> None:
        self._owner_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(
                    self._owner_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    owner = json.loads(self._owner_path.read_text(encoding="utf-8"))
                    pid = owner.get("pid") if isinstance(owner, dict) else None
                    if isinstance(pid, int) and _process_alive(pid):
                        raise RuntimeError("desktop instance already running")
                    self._owner_path.unlink(missing_ok=True)
                    continue
                except (OSError, json.JSONDecodeError):
                    raise RuntimeError("desktop instance ownership is unavailable")
                raise RuntimeError("desktop instance already running")  # pragma: no cover
            self._owner_fd = fd
            os.write(fd, json.dumps({"pid": os.getpid(), "secret": self._secret}).encode())
            os.fsync(fd)
            return
        raise RuntimeError("desktop instance ownership is unavailable")

    def _release_ownership(self) -> None:
        if self._owner_fd is None:
            return
        os.close(self._owner_fd)
        self._owner_fd = None
        try:
            owner = json.loads(self._owner_path.read_text(encoding="utf-8"))
            if isinstance(owner, dict) and hmac.compare_digest(
                str(owner.get("secret", "")),
                self._secret,
            ):
                self._owner_path.unlink()
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass

    def _write_state(self, port: int) -> None:
        payload = json.dumps(
            {"version": 1, "pid": os.getpid(), "port": port, "secret": self._secret},
            separators=(",", ":"),
        )
        temp = self._state_path.with_suffix(".tmp")
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self._state_path)

    def _serve(self) -> None:
        assert self._socket is not None
        while self._socket is not None:
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._connections.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(
                target=self._handle_connection,
                args=(connection,),
                daemon=True,
            ).start()

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(0.5)
                data = connection.recv(MAX_MESSAGE_BYTES + 1)
                if len(data) > MAX_MESSAGE_BYTES:
                    return
                try:
                    message = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                if not isinstance(message, dict):
                    return
                secret = message.pop("secret", None)
                if not isinstance(secret, str) or not hmac.compare_digest(secret, self._secret):
                    return
                event = message.get("event")
                if _valid_event(event):
                    self._on_activation(event)
                    connection.sendall(b'{"ok":true}')
        except (OSError, socket.timeout):
            return
        finally:
            self._connections.release()

    def close(self) -> None:
        listener = self._socket
        self._socket = None
        if listener is not None:
            listener.close()
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and hmac.compare_digest(
                str(state.get("secret", "")),
                self._secret,
            ):
                self._state_path.unlink()
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._release_ownership()


def forward_activation(state_dir: Path, event: ActivationEvent) -> bool:
    if not _valid_event(event):
        return False
    try:
        state = json.loads((state_dir / "desktop_activation.json").read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or
            state.get("version") != 1
            or not isinstance(state.get("port"), int)
            or not isinstance(state.get("secret"), str)
        ):
            return False
        payload = json.dumps(
            {"secret": state["secret"], "event": event},
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            return False
        with socket.create_connection(("127.0.0.1", state["port"]), timeout=1) as connection:
            connection.sendall(payload)
            return json.loads(connection.recv(128)).get("ok") is True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def forward_activation_when_ready(
    state_dir: Path,
    event: ActivationEvent,
    timeout: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if forward_activation(state_dir, event):
            return True
        time.sleep(0.02)
    return False
