from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any


def artifact_bytes() -> bytes:
    return (
        files("better_agent_sdk.marketplace_protocol")
        .joinpath("v1", "protocol.json")
        .read_bytes()
    )


def protocol_hash() -> str:
    return hashlib.sha256(artifact_bytes()).hexdigest()


def load_protocol() -> dict[str, Any]:
    payload = json.loads(artifact_bytes())
    if payload.get("name") != "better-agent-marketplace" or payload.get("version") != 1:
        raise ValueError("unsupported Marketplace protocol artifact")
    return payload


__all__ = ["artifact_bytes", "load_protocol", "protocol_hash"]
