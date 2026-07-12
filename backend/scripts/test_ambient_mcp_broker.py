#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-ambient-broker-")
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import ambient_mcp_broker
import ambient_mcp_transport
import ambient_principal
import extension_store


def main() -> int:
    if os.name == "nt":
        return 0
    record = {
        "enabled": True,
        "entitlement": {"status": "active"},
        "manifest": {"entrypoints": {"mcp": [{
            "name": "tools",
            "native_exposure": {
                "allowed": True,
                "permissions": ["test.allowed"],
            },
        }]}},
    }
    extension_store.get_extension = lambda extension_id: record  # type: ignore[assignment]
    extension_store.is_extension_active = lambda extension_id: True  # type: ignore[assignment]
    extension_store.native_harness_exposed = lambda *args, **kwargs: True  # type: ignore[assignment]
    broker = ambient_mcp_broker.AmbientMcpBroker()
    broker.start()
    path = Path(ambient_mcp_transport.endpoint())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    connection, stream = ambient_mcp_transport.connect()
    ambient_mcp_transport.send_json(stream, {
        "extension_id": "test.extension",
        "server_name": "tools",
        "provider_id": "codex",
        "pid": os.getpid(),
    })
    grant = ambient_mcp_transport.recv_json(stream)
    token = grant["credential"]
    principal = ambient_principal.registry.resolve(token, permission="test.allowed")
    assert principal is not None and principal.os_user_id == str(os.getuid())
    assert ambient_principal.registry.resolve(token, permission="test.denied") is None
    stream.close()
    connection.close()
    for _ in range(100):
        if ambient_principal.registry.resolve(token) is None:
            break
        __import__("time").sleep(0.01)
    assert ambient_principal.registry.resolve(token) is None
    broker.stop()
    assert not path.exists()
    print("PASS ambient MCP broker lifecycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
