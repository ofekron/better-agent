#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ambient_mcp_windows
import ambient_mcp_broker
import ambient_principal
import coordination


class _FakeWindowsConnection:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.grant: dict | None = None

    def recv(self) -> dict:
        return {"source_kind": "core", "server_name": "ui", "provider_id": "codex"}

    def send(self, value: dict) -> None:
        self.grant = value

    def recv_bytes(self) -> bytes:
        self.closed.wait(2)
        raise EOFError

    def close(self) -> None:
        self.closed.set()


async def _test_in_process_stop_revokes_windows_client() -> None:
    broker = ambient_mcp_broker.AmbientMcpBroker()
    broker._event_loop = asyncio.get_running_loop()
    connection = _FakeWindowsConnection()
    released: list[str] = []
    original_release = coordination.release_principal_locks

    async def release(principal_id: str) -> list[str]:
        released.append(principal_id)
        return []

    coordination.release_principal_locks = release
    thread = threading.Thread(
        target=broker._handle_windows,
        args=(connection, "test-user-sid"),
        daemon=True,
    )
    try:
        thread.start()
        for _ in range(100):
            if connection.grant is not None:
                break
            await asyncio.sleep(0.01)
        assert connection.grant is not None
        principal_id = connection.grant["principal_id"]
        token = connection.grant["credential"]
        assert ambient_principal.registry.resolve(token) is not None
        broker.stop()
        thread.join(timeout=2)
        for _ in range(100):
            if ambient_principal.registry.resolve(token) is None and principal_id in released:
                break
            await asyncio.sleep(0.01)
        assert not thread.is_alive()
        assert ambient_principal.registry.resolve(token) is None
        assert principal_id in released
    finally:
        coordination.release_principal_locks = original_release
        connection.close()


def main() -> int:
    value = {"provider_id": "codex", "pid": 42}
    frame = ambient_mcp_windows.encode_frame(value)
    assert struct.unpack("<I", frame[:4])[0] == len(frame) - 4
    assert ambient_mcp_windows.decode_frame(frame[4:]) == value

    try:
        ambient_mcp_windows.decode_frame(json.dumps(["not-an-object"]).encode())
    except ValueError:
        pass
    else:
        raise AssertionError("non-object frame accepted")

    oversized = b"x" * (ambient_mcp_windows.MAX_FRAME_SIZE + 1)
    try:
        ambient_mcp_windows.decode_frame(oversized)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized frame accepted")

    if os.name != "nt":
        try:
            ambient_mcp_windows.connect(r"\\.\pipe\unavailable")
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-Windows transport did not fail closed")

    asyncio.run(_test_in_process_stop_revokes_windows_client())

    print("PASS ambient MCP Windows framing and platform guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
