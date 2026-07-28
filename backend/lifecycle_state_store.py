from __future__ import annotations

from pathlib import Path
from typing import Any

from json_store import read_json, write_json
from paths import ba_home


SCHEMA_VERSION = 1


def _path() -> Path:
    return ba_home() / "lifecycle-state.json"


def load() -> dict[str, Any]:
    value = read_json(_path(), {})
    if not value:
        return {"version": SCHEMA_VERSION, "sessions": {}}
    if value.get("version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported lifecycle state schema")
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError("invalid lifecycle state projection")
    return value


def save(projection: dict[str, Any]) -> None:
    if projection.get("version") != SCHEMA_VERSION:
        raise RuntimeError("invalid lifecycle state schema")
    write_json(_path(), projection)
