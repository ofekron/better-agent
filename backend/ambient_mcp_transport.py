from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

import runtime_ipc
from paths import ba_home


def endpoint() -> str:
    if os.name == "nt":
        import hashlib

        owner = os.environ.get("USERNAME", "")
        suffix = hashlib.sha256(f"{ba_home()}:{owner}".encode()).hexdigest()[:20]
        return rf"\\.\pipe\better-agent-ambient-mcp-{suffix}"
    # AF_UNIX paths are capped (~104 bytes on macOS) and homes can be
    # deep, so the socket lives in the short per-user 0700 socket dir
    # under a per-home hashed name — the POSIX mirror of the pipe hash.
    return str(runtime_ipc.socket_dir() / f"{runtime_ipc.home_digest()}-mcp.sock")


def prepare_posix_listener() -> socket.socket:
    if os.name == "nt":
        raise RuntimeError("POSIX listener requested on Windows")
    runtime_ipc.ensure_socket_dir()
    path = Path(endpoint())
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(16)
    return listener


def posix_peer_user(connection: socket.socket) -> str:
    if hasattr(connection, "getpeereid"):
        uid, _ = connection.getpeereid()
    elif hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", raw)
    else:
        import ctypes

        uid_value = ctypes.c_uint()
        gid_value = ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(connection.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)) != 0:
            raise PermissionError("peer credentials are unavailable")
        uid = uid_value.value
    if uid != os.getuid():
        raise PermissionError("ambient MCP peer belongs to another OS user")
    return str(uid)


def send_json(stream: Any, value: dict[str, Any]) -> None:
    if os.name == "nt":
        stream.send(value)
        return
    stream.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    stream.flush()


def recv_json(stream: Any) -> dict[str, Any]:
    if os.name == "nt":
        return stream.recv()
    line = stream.readline(65537)
    if not line or len(line) > 65536:
        raise ConnectionError("invalid ambient MCP broker frame")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("ambient MCP broker frame must be an object")
    return value


def connect() -> tuple[Any, Any]:
    if os.name == "nt":
        import ambient_mcp_windows

        connection = ambient_mcp_windows.connect(endpoint())
        return connection, connection
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(endpoint())
        # The socket dir is shared per-user; refuse to speak to a broker
        # that is not running as this OS user before any frame is sent.
        posix_peer_user(connection)
    except BaseException:
        connection.close()
        raise
    stream = connection.makefile("rwb", buffering=0)
    return connection, stream
