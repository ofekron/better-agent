from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import node_client


def test_repository_manifest_is_ready_before_websocket_connect() -> None:
    events: list[object] = []
    sent: list[dict] = []
    build_info = {"commit": "abc123"}
    repositories = [{"role": "app_public", "commit": "abc123"}]
    topology = type(
        "Topology",
        (),
        {"primary": type("Primary", (), {"address": "ws://primary:18765"})()},
    )()
    identity = type(
        "Identity",
        (),
        {
            "secret": "secret",
            "node_id": "lenovo",
            "address": "http://lenovo:8002",
            "cwd_roots": ("C:\\Users\\Lenovo",),
        },
    )()

    class WebSocket:
        async def send(self, payload: str) -> None:
            events.append("send")
            sent.append(json.loads(payload))

        async def recv(self) -> str:
            return json.dumps({
                "type": "handshake",
                "protocol_version": node_client.PROTOCOL_VERSION,
                "repository_alignment_accepted": True,
            })

    class Connect:
        async def __aenter__(self) -> WebSocket:
            events.append("connect")
            return WebSocket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    def current_build_info() -> dict:
        events.append("build")
        return build_info

    def current_repositories(observed_build_info: dict) -> list[dict]:
        events.append(("repositories-start", observed_build_info))
        time.sleep(0.05)
        events.append("repositories-end")
        return repositories

    client = node_client.NodeClient()

    async def transport_ends(*_args: object) -> None:
        return None

    def incident_replay() -> asyncio.Task:
        return asyncio.create_task(asyncio.sleep(3600))

    async def run() -> None:
        connection = asyncio.create_task(client._connect_once())
        await asyncio.sleep(0.01)
        events.append("heartbeat")
        await connection

    with (
        patch.object(node_client, "load_topology", return_value=topology),
        patch.object(node_client.node_identity, "load_or_create", return_value=identity),
        patch.object(
            node_client.app_version,
            "current_build_info",
            side_effect=current_build_info,
        ),
        patch.object(
            node_client.repository_alignment,
            "current_repositories",
            side_effect=current_repositories,
        ),
        patch.object(
            node_client.repository_alignment,
            "record_primary_attestation",
        ),
        patch.object(node_client.websockets, "connect", return_value=Connect()),
        patch.object(client, "_sender_loop", side_effect=transport_ends),
        patch.object(client, "_receiver_loop", side_effect=transport_ends),
        patch.object(client, "_start_extension_incident_replay", side_effect=incident_replay),
    ):
        asyncio.run(run())

    assert events == [
        "build",
        ("repositories-start", build_info),
        "heartbeat",
        "repositories-end",
        "connect",
        "send",
    ]
    assert sent == [{
        "type": "handshake",
        "protocol_version": node_client.PROTOCOL_VERSION,
        "node_id": "lenovo",
        "app_version": build_info,
        "repositories": repositories,
        "registration": {
            "address": "http://lenovo:8002",
            "cwd_roots": ["C:\\Users\\Lenovo"],
        },
    }]
