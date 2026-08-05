from __future__ import annotations

import time
from pathlib import Path

from json_store import read_json, write_json_durable
from paths import bc_home, make_private_file


_STATES = {"starting", "connected", "unreachable", "stopped"}


def status_path(state_root: Path | None = None) -> Path:
    return Path(state_root or bc_home()) / "node-connection-status.json"


def publish(
    state: str,
    *,
    generation: int,
    detail: str = "",
    state_root: Path | None = None,
) -> dict[str, object]:
    if state not in _STATES:
        raise ValueError(f"invalid node connection state: {state}")
    if generation < 0:
        raise ValueError("node connection generation must not be negative")
    projection: dict[str, object] = {
        "schema_version": 1,
        "state": state,
        "generation": generation,
        "updated_at": time.time(),
    }
    if detail:
        projection["detail"] = detail[:160]
    path = status_path(state_root)
    write_json_durable(path, projection)
    make_private_file(path)
    return projection


def read(state_root: Path | None = None) -> dict[str, object]:
    value = read_json(status_path(state_root), {})
    if value.get("schema_version") != 1 or value.get("state") not in _STATES:
        return {}
    return value
