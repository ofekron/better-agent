from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from json_store import read_json, write_json
from paths import ba_home

SCHEMA_VERSION = 1
MAX_PENDING_INCIDENTS = 1000

_LOCK = threading.Lock()
_KINDS = {"slow_backend_call", "backend_timeout"}


class IncidentOutboxFull(RuntimeError):
    pass


def _path():
    return ba_home() / "node-extension-incidents" / "outbox.json"


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "incidents": []}


def _load() -> dict[str, Any]:
    data = read_json(_path(), _empty())
    if data.get("schema_version") != SCHEMA_VERSION:
        return _empty()
    incidents = data.get("incidents")
    if not isinstance(incidents, list):
        return _empty()
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(_path(), data)


def enqueue(
    *,
    kind: str,
    extension_id: str,
    activation_id: str,
    elapsed_seconds: float,
    path: str | None = None,
    occurred_at: float | None = None,
) -> dict[str, Any]:
    if kind not in _KINDS:
        raise ValueError("unsupported extension incident kind")
    fact = {
        "incident_id": uuid.uuid4().hex,
        "kind": kind,
        "extension_id": str(extension_id),
        "activation_id": str(activation_id),
        "elapsed_seconds": float(elapsed_seconds),
        "occurred_at": float(occurred_at if occurred_at is not None else time.time()),
    }
    if path is not None:
        fact["path"] = str(path)
    with _LOCK:
        data = _load()
        incidents = data["incidents"]
        if len(incidents) >= MAX_PENDING_INCIDENTS:
            raise IncidentOutboxFull("extension incident outbox is full")
        incidents.append(fact)
        _save(data)
    return fact


def pending() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(item) for item in _load()["incidents"] if isinstance(item, dict)]


def ack(incident_id: str) -> bool:
    incident_id = str(incident_id or "")
    if not incident_id:
        return False
    with _LOCK:
        data = _load()
        incidents = [
            item for item in data["incidents"]
            if not (isinstance(item, dict) and item.get("incident_id") == incident_id)
        ]
        if len(incidents) == len(data["incidents"]):
            return False
        data["incidents"] = incidents
        _save(data)
        return True
