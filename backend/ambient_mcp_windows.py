from __future__ import annotations

import json
import os
import struct
import threading
from typing import Any, Callable

MAX_FRAME_SIZE = 65536


def encode_frame(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError("ambient MCP broker frame is too large")
    return struct.pack("<I", len(payload)) + payload


def decode_frame(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError("ambient MCP broker frame is too large")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("ambient MCP broker frame must be an object")
    return value


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    OPEN_EXISTING = 3
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_PIPE_CONNECTED = 535
    ERROR_BROKEN_PIPE = 109
    SDDL_REVISION_1 = 1

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER_STRUCT(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    def _raise_last_error(message: str) -> None:
        raise OSError(ctypes.get_last_error(), message)

    def _close(handle: int | None) -> None:
        if handle and handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(handle)

    def _token_sid(token: int) -> str:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(needed))
        if not needed.value:
            _raise_last_error("failed to size Windows token user")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER, buffer, needed, ctypes.byref(needed)
        ):
            _raise_last_error("failed to read Windows token user")
        sid = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER_STRUCT)).contents.User.Sid
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            _raise_last_error("failed to stringify Windows SID")
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)

    def _process_sid(process_handle: int) -> str:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
            _raise_last_error("failed to open Windows process token")
        try:
            return _token_sid(token.value)
        finally:
            _close(token.value)

    def current_user_sid() -> str:
        return _process_sid(kernel32.GetCurrentProcess())

    def _client_sid(pipe: int) -> str:
        pid = wintypes.ULONG()
        if not kernel32.GetNamedPipeClientProcessId(pipe, ctypes.byref(pid)):
            _raise_last_error("failed to identify named-pipe client")
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            _raise_last_error("failed to open named-pipe client process")
        try:
            return _process_sid(process)
        finally:
            _close(process)

    def _security_attributes(owner_sid: str) -> tuple[SECURITY_ATTRIBUTES, int]:
        descriptor = wintypes.LPVOID()
        sddl = f"D:P(A;;GA;;;{owner_sid})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            _raise_last_error("failed to create named-pipe security descriptor")
        attributes = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False
        )
        return attributes, descriptor.value

    def _read_exact(handle: int, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = ctypes.create_string_buffer(size - len(result))
            read = wintypes.DWORD()
            if not kernel32.ReadFile(handle, chunk, len(chunk), ctypes.byref(read), None):
                error = ctypes.get_last_error()
                if error == ERROR_BROKEN_PIPE:
                    raise EOFError
                raise OSError(error, "failed to read named pipe")
            if not read.value:
                raise EOFError
            result.extend(chunk.raw[: read.value])
        return bytes(result)

    def _write_all(handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = wintypes.DWORD()
            chunk = data[offset:]
            if not kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None):
                _raise_last_error("failed to write named pipe")
            if not written.value:
                raise ConnectionError("named-pipe write made no progress")
            offset += written.value

    class Connection:
        def __init__(self, handle: int) -> None:
            self._handle = handle
            self._lock = threading.Lock()

        def recv_bytes(self) -> bytes:
            length = struct.unpack("<I", _read_exact(self._handle, 4))[0]
            if length > MAX_FRAME_SIZE:
                raise ValueError("ambient MCP broker frame is too large")
            return _read_exact(self._handle, length)

        def recv(self) -> dict[str, Any]:
            return decode_frame(self.recv_bytes())

        def send(self, value: dict[str, Any]) -> None:
            data = encode_frame(value)
            with self._lock:
                _write_all(self._handle, data)

        def close(self) -> None:
            handle, self._handle = self._handle, 0
            _close(handle)

    class Listener:
        def __init__(self, address: str, handler: Callable[[Connection, str], None]) -> None:
            self._address = address
            self._handler = handler
            self._owner_sid = current_user_sid()
            self._stop = threading.Event()
            self._thread: threading.Thread | None = None
            self._pending: int | None = None

        def start(self) -> None:
            self._thread = threading.Thread(target=self._serve, daemon=True, name="ambient-mcp-pipe")
            self._thread.start()

        def close(self) -> None:
            self._stop.set()
            try:
                client = connect(self._address)
                client.close()
            except OSError:
                pass
            if self._thread:
                self._thread.join(timeout=2)

        def _serve(self) -> None:
            while not self._stop.is_set():
                attributes, descriptor = _security_attributes(self._owner_sid)
                pipe = kernel32.CreateNamedPipeW(
                    self._address,
                    PIPE_ACCESS_DUPLEX,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                    16,
                    MAX_FRAME_SIZE + 4,
                    MAX_FRAME_SIZE + 4,
                    0,
                    ctypes.byref(attributes),
                )
                kernel32.LocalFree(descriptor)
                if pipe == INVALID_HANDLE_VALUE:
                    _raise_last_error("failed to create ambient MCP named pipe")
                self._pending = pipe
                connected = kernel32.ConnectNamedPipe(pipe, None)
                self._pending = None
                if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                    _close(pipe)
                    if self._stop.is_set():
                        return
                    continue
                if self._stop.is_set():
                    _close(pipe)
                    return
                try:
                    peer_sid = _client_sid(pipe)
                    if peer_sid != self._owner_sid:
                        raise PermissionError("ambient MCP peer belongs to another OS user")
                except (OSError, PermissionError):
                    kernel32.DisconnectNamedPipe(pipe)
                    _close(pipe)
                    continue
                threading.Thread(
                    target=self._handler,
                    args=(Connection(pipe), peer_sid),
                    daemon=True,
                    name="ambient-mcp-pipe-client",
                ).start()

    def connect(address: str) -> Connection:
        handle = kernel32.CreateFileW(
            address,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            _raise_last_error("failed to connect to ambient MCP named pipe")
        return Connection(handle)

else:
    class Listener:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("Windows named-pipe support is unavailable on this platform")

    def connect(_address: str) -> Any:
        raise RuntimeError("Windows named-pipe support is unavailable on this platform")
