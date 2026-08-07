from __future__ import annotations

import ctypes
import json
import logging
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

logger = logging.getLogger(__name__)


def _reply_error(send: Callable[[bytes], Any], exc: Exception) -> None:
    """Best-effort error reply to a peer that may already be gone.

    A client that disconnects mid-request leaves the serve loop holding a
    dead pipe/socket, so reporting the first failure raised a second one
    (`BrokenPipeError: [WinError 232]`) out of the except block and killed
    the serving thread — one bad request took the whole broker down.
    """
    try:
        send(_encode({"success": False, "error": str(exc)}))
    except Exception:
        logger.debug("runtime broker could not deliver error reply", exc_info=True)


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
        if os.name == "nt":  # pragma: no cover - Windows named-pipe broker
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
        if not self._ready.wait(timeout=5):  # pragma: no cover - serve thread always sets _ready in success and bind-failure paths; the deadline only fires on a non-deterministic thread hang
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
        elif listener is not None:  # pragma: no cover - Windows multiprocessing.connection.Listener teardown
            try:
                listener.close()
            except OSError:
                pass
        if self.address.startswith("pipe:"):  # pragma: no cover - Windows named-pipe teardown
            try:
                from multiprocessing.connection import Client

                Client(self.address.removeprefix("pipe:"), family="AF_PIPE", authkey=None).close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():  # pragma: no cover - requires a serve thread that ignores _stop past the join deadline; not deterministically reproducible
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
        # The loop has two exits (flag check at the top, OSError break when
        # stop() closes the listener); which one fires is a thread-timing race,
        # so don't enforce both arcs in a single run.
        while not self._stop.is_set():  # pragma: no branch
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
                    _reply_error(lambda payload: _send_frame(connection, payload), exc)

    def _serve_pipe(self) -> None:  # pragma: no cover - Windows named-pipe server
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
                    _reply_error(connection.send_bytes, exc)

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
    elif sys.platform == "darwin" and hasattr(socket, "LOCAL_PEERCRED"):  # pragma: no cover - macOS peer-cred
        raw = connection.getsockopt(0, socket.LOCAL_PEERCRED, 8)
        _version, uid = struct.unpack("II", raw)
    elif hasattr(connection, "getpeereid"):  # pragma: no cover - BSD getpeereid
        uid, _gid = connection.getpeereid()
    else:  # pragma: no cover - no peer-cred implementation
        raise PermissionError("runtime broker cannot verify the local peer")
    if uid != expected_uid:
        raise PermissionError("runtime broker peer belongs to a different user")


_win32_prototypes_declared = False
_WIN32_PROTOTYPE_LOCK = threading.Lock()


def _declare_win32_prototypes() -> None:  # pragma: no cover - Windows ctypes prototypes
    """Give every Win32 call below an explicit signature, once.

    Without argtypes/restype ctypes marshals a Python int as a 32-bit C
    ``int`` and assumes a 32-bit return. HANDLEs and SID pointers are
    64-bit on Win64, so an untyped call either truncates a handle or
    raises ``OverflowError: int too long to convert`` on an address that
    does not fit — which killed the peer check, and with it the broker
    pipe, on every run a Windows node tried to execute.
    """
    global _win32_prototypes_declared
    with _WIN32_PROTOTYPE_LOCK:
        if _win32_prototypes_declared:
            return
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32

        kernel32.GetNamedPipeClientProcessId.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG),
        ]
        kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL

        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        _win32_prototypes_declared = True


def _require_windows_peer(pipe_handle: int) -> None:  # pragma: no cover - Windows peer validation
    if os.name != "nt":
        raise PermissionError("Windows peer validation is unavailable")
    _declare_win32_prototypes()
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    client_pid = wintypes.ULONG()
    if not kernel32.GetNamedPipeClientProcessId(
        wintypes.HANDLE(pipe_handle), ctypes.byref(client_pid),
    ):
        raise PermissionError("runtime broker cannot identify the pipe peer")
    if _windows_process_sid(client_pid.value) != _windows_process_sid(os.getpid()):
        raise PermissionError("runtime broker peer belongs to a different user")


def _windows_process_sid(pid: int) -> str:  # pragma: no cover - Windows SID resolution
    from ctypes import wintypes

    _declare_win32_prototypes()
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32

    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        raise PermissionError("runtime broker cannot open peer process")
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise PermissionError("runtime broker cannot read peer identity")
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            needed.value,
            ctypes.byref(needed),
        ):
            raise PermissionError("runtime broker cannot read peer identity")
        # TOKEN_USER starts with a SID_AND_ATTRIBUTES whose first member is
        # the SID pointer. Keep it typed: dereferencing to a plain int hands
        # ctypes a 64-bit address it would try to squeeze into a C int.
        sid_pointer = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_void_p),
        ).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise PermissionError("runtime broker cannot format peer identity")
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


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
