#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if os.name != "nt":
    print("SKIP Windows named-pipe integration")
    raise SystemExit(0)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
home = tempfile.mkdtemp(prefix="ba-ambient-windows-")
os.environ["BETTER_AGENT_HOME"] = home
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import ambient_mcp_broker
import ambient_mcp_transport
import ambient_principal
import ambient_mcp_windows
import extension_store


def main() -> int:
    record = {"enabled": True, "entitlement": {"status": "active"}}
    extension_store.get_extension = lambda extension_id: record  # type: ignore[assignment]
    extension_store.is_extension_active = lambda extension_id: True  # type: ignore[assignment]
    extension_store.effective_permissions = lambda value: {"internal_loopback": True}  # type: ignore[assignment]
    broker = ambient_mcp_broker.AmbientMcpBroker()
    broker.start()
    try:
        connection, stream = ambient_mcp_transport.connect()
        stream.send({
            "extension_id": "test.extension",
            "server_name": "tools",
            "provider_id": "codex",
            "pid": os.getpid(),
        })
        grant = stream.recv()
        token = grant["credential"]
        principal = ambient_principal.registry.resolve(token, permission="internal_loopback")
        assert principal is not None
        assert principal.os_user_id == ambient_mcp_windows.current_user_sid()
        stream.close()
        for _ in range(100):
            if ambient_principal.registry.resolve(token) is None:
                break
            time.sleep(0.01)
        assert ambient_principal.registry.resolve(token) is None
        connection.close()
    finally:
        broker.stop()
        shutil.rmtree(home, ignore_errors=True)
    print("PASS ambient MCP Windows named-pipe lifecycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
