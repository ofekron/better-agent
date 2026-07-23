from __future__ import annotations

import json
from multiprocessing.connection import Client
import os
import socket
import struct
from typing import Any

_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class RuntimeTransport:
    def __init__(self, address: str | None = None) -> None:
        self._address = (
            address
            or os.environ.get("BETTER_AGENT_RUNTIME_BROKER")
            or os.environ.get("BETTER_CLAUDE_RUNTIME_BROKER")
            or ""
        ).strip()
        if not self._address:
            raise RuntimeError("Better Agent runtime broker is unavailable")

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = _encode(payload)
        if self._address.startswith("unix:"):
            response = self._request_unix(data)
        elif self._address.startswith("pipe:"):
            response = self._request_pipe(data)
        else:
            raise RuntimeError("Better Agent runtime broker address is invalid")
        result = _decode(response)
        if result.get("success") is False:
            raise RuntimeError(str(result.get("error") or "runtime broker request failed"))
        return result

    def _request_unix(self, data: bytes) -> bytes:
        path = self._address.removeprefix("unix:")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(path)
            connection.sendall(struct.pack("!I", len(data)) + data)
            size = struct.unpack("!I", _recv_exact(connection, 4))[0]
            if size > _MAX_MESSAGE_BYTES:
                raise RuntimeError("runtime broker response is too large")
            return _recv_exact(connection, size)

    def _request_pipe(self, data: bytes) -> bytes:
        address = self._address.removeprefix("pipe:")
        with Client(address, family="AF_PIPE", authkey=None) as connection:
            connection.send_bytes(data)
            return connection.recv_bytes(_MAX_MESSAGE_BYTES)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise RuntimeError("runtime broker connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _encode(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > _MAX_MESSAGE_BYTES:
        raise RuntimeError("runtime broker request is too large")
    return data


def _decode(data: bytes) -> dict[str, Any]:
    if len(data) > _MAX_MESSAGE_BYTES:
        raise RuntimeError("runtime broker response is too large")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("runtime broker response must be an object")
    return value
