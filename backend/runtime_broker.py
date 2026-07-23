from __future__ import annotations

import ctypes
import json
from multiprocessing.connection import Listener
import os
from pathlib import Path
import secrets
import socket
import struct
import sys
import tempfile
import threading
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class BrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    kind: str
    operation: str = ""
    payload: dict[str, Any] | None = None
    request_id: str = ""
    deadline_at: float | None = None
    generation: str = ""


class RuntimeBroker:
    def __init__(
        self,
        directory: Path,
        handler: Callable[[BrokerRequest], dict[str, Any]],
    ) -> None:
        self._directory = directory.resolve()
        self._handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | Listener | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._socket_directory: Path | None = None
        self.address = ""

    def start(self) -> str:
        if self._thread is not None:
            raise RuntimeError("runtime broker already started")
        self._directory.mkdir(parents=True, exist_ok=True)
        if self._directory.is_symlink() or not self._directory.is_dir():
            raise RuntimeError("runtime broker directory is invalid")
        if os.name == "nt":
            self.address = rf"pipe:\\.\pipe\better-agent-{secrets.token_hex(16)}"
            target = self._serve_pipe
        else:
            self._directory.chmod(0o700)
            socket_directory = self._directory
            candidate = socket_directory / f"broker-{secrets.token_hex(16)}.sock"
            if len(os.fsencode(candidate)) >= 96:
                socket_directory = Path(
                    tempfile.mkdtemp(prefix=f"ba-broker-{os.getuid()}-", dir="/tmp")
                ).resolve()
                socket_directory.chmod(0o700)
                self._socket_directory = socket_directory
            path = socket_directory / f"broker-{secrets.token_hex(8)}.sock"
            self.address = f"unix:{path}"
            target = self._serve_unix
        self._thread = threading.Thread(target=target, name="runtime-broker", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self.stop()
            raise RuntimeError("runtime broker did not start")
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise RuntimeError("runtime broker failed to start") from error
        return self.address

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if isinstance(listener, socket.socket):
            listener.close()
        elif listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self.address.startswith("pipe:"):
            try:
                from multiprocessing.connection import Client

                Client(self.address.removeprefix("pipe:"), family="AF_PIPE", authkey=None).close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("runtime broker did not stop")
        if self.address.startswith("unix:"):
            try:
                Path(self.address.removeprefix("unix:")).unlink()
            except FileNotFoundError:
                pass
        if self._socket_directory is not None:
            try:
                self._socket_directory.rmdir()
            except FileNotFoundError:
                pass
            self._socket_directory = None
        self._thread = None
        self._listener = None

    def _serve_unix(self) -> None:
        path = Path(self.address.removeprefix("unix:"))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        try:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(16)
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        listener.settimeout(0.5)
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    _require_posix_peer(connection)
                    request = _decode(_recv_frame(connection))
                    _send_frame(connection, _encode(self._dispatch(request)))
                except Exception as exc:
                    _send_frame(connection, _encode({"success": False, "error": str(exc)}))

    def _serve_pipe(self) -> None:
        address = self.address.removeprefix("pipe:")
        try:
            listener = Listener(address, family="AF_PIPE", authkey=None)
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._listener = listener
        self._ready.set()
        while not self._stop.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                break
            with connection:
                try:
                    _require_windows_peer(connection.fileno())
                    request = _decode(connection.recv_bytes(_MAX_MESSAGE_BYTES))
                    connection.send_bytes(_encode(self._dispatch(request)))
                except Exception as exc:
                    connection.send_bytes(_encode({"success": False, "error": str(exc)}))

    def _dispatch(self, raw: dict[str, Any]) -> dict[str, Any]:
        request = BrokerRequest.model_validate(raw)
        if request.version != 1:
            raise ValueError("unsupported runtime broker protocol version")
        if request.kind not in {"catalog", "invoke", "status", "cancel"}:
            raise ValueError("unsupported runtime broker request kind")
        return self._handler(request)


def _require_posix_peer(connection: socket.socket) -> None:
    expected_uid = os.getuid()
    if sys.platform.startswith("linux"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
    elif sys.platform == "darwin" and hasattr(socket, "LOCAL_PEERCRED"):
        raw = connection.getsockopt(0, socket.LOCAL_PEERCRED, 8)
        _version, uid = struct.unpack("II", raw)
    elif hasattr(connection, "getpeereid"):
        uid, _gid = connection.getpeereid()
    else:
        raise PermissionError("runtime broker cannot verify the local peer")
    if uid != expected_uid:
        raise PermissionError("runtime broker peer belongs to a different user")


def _require_windows_peer(pipe_handle: int) -> None:
    if os.name != "nt":
        raise PermissionError("Windows peer validation is unavailable")
    kernel32 = ctypes.windll.kernel32
    client_pid = ctypes.c_ulong()
    if not kernel32.GetNamedPipeClientProcessId(pipe_handle, ctypes.byref(client_pid)):
        raise PermissionError("runtime broker cannot identify the pipe peer")
    if _windows_process_sid(client_pid.value) != _windows_process_sid(os.getpid()):
        raise PermissionError("runtime broker peer belongs to a different user")


def _windows_process_sid(pid: int) -> str:
    from ctypes import wintypes

    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        raise PermissionError("runtime broker cannot open peer process")
    token = wintypes.HANDLE()
    try:
        if not ctypes.windll.advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise PermissionError("runtime broker cannot read peer identity")
        needed = wintypes.DWORD()
        ctypes.windll.advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not ctypes.windll.advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            needed,
            ctypes.byref(needed),
        ):
            raise PermissionError("runtime broker cannot read peer identity")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not ctypes.windll.advapi32.ConvertSidToStringSidW(
            sid_pointer,
            ctypes.byref(sid_text),
        ):
            raise PermissionError("runtime broker cannot format peer identity")
        try:
            return str(sid_text.value)
        finally:
            ctypes.windll.kernel32.LocalFree(sid_text)
    finally:
        if token:
            ctypes.windll.kernel32.CloseHandle(token)
        ctypes.windll.kernel32.CloseHandle(process)


def _recv_frame(connection: socket.socket) -> bytes:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if size > _MAX_MESSAGE_BYTES:
        raise ValueError("runtime broker request is too large")
    return _recv_exact(connection, size)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("runtime broker connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _send_frame(connection: socket.socket, data: bytes) -> None:
    connection.sendall(struct.pack("!I", len(data)) + data)


def _decode(data: bytes) -> dict[str, Any]:
    if len(data) > _MAX_MESSAGE_BYTES:
        raise ValueError("runtime broker message is too large")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime broker message must be an object")
    return value


def _encode(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > _MAX_MESSAGE_BYTES:
        raise ValueError("runtime broker response is too large")
    return data
