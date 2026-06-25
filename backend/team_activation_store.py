from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from json_store import read_json, write_json
from paths import ba_home

SCHEMA_VERSION = 1


class TeamActivationError(ValueError):
    pass


def _root() -> Path:
    return ba_home() / "team-activations"


def _clean_id(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise TeamActivationError(f"{field} is required")
    if any(part in clean for part in ("/", "\\", "..")):
        raise TeamActivationError(f"{field} is invalid")
    return clean


def _path(activation_id: str) -> Path:
    return _root() / f"{_clean_id(activation_id, 'activation_id')}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(*, root_session_id: str, team_instance_id: str, source_id: str, profile: str) -> dict[str, Any]:
    activation_id = f"team-act-{uuid4().hex}"
    now = _now()
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": activation_id,
        "status": "running",
        "root_session_id": str(root_session_id or "").strip(),
        "team_instance_id": str(team_instance_id or "").strip(),
        "source_id": str(source_id or "").strip(),
        "profile": str(profile or "").strip(),
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
        "error": "",
        "steps": [],
        "result": {},
        "rolled_back_worker_ids": [],
    }
    write_json(_path(activation_id), record)
    return record


def get(activation_id: str) -> dict[str, Any] | None:
    path = _path(activation_id)
    if not path.exists():
        return None
    data = read_json(path, {})
    if data.get("schema_version") != SCHEMA_VERSION:
        raise TeamActivationError("Unsupported team activation schema; wipe team-activations/*.json to start fresh")
    if not isinstance(data.get("steps"), list):
        raise TeamActivationError("Malformed team activation: steps must be a list")
    data.setdefault("rolled_back_worker_ids", [])
    return data


def append_step(activation_id: str, label: str, *, status: str = "done", data: dict[str, Any] | None = None) -> None:
    record = get(activation_id)
    if record is None:
        raise TeamActivationError("activation_id does not exist")
    now = _now()
    record["steps"].append(
        {
            "label": str(label or "").strip(),
            "status": str(status or "done").strip(),
            "at": now,
            "data": dict(data or {}),
        }
    )
    record["updated_at"] = now
    write_json(_path(activation_id), record)


def complete(activation_id: str, result: dict[str, Any]) -> None:
    record = get(activation_id)
    if record is None:
        raise TeamActivationError("activation_id does not exist")
    now = _now()
    record["status"] = "complete"
    record["updated_at"] = now
    record["completed_at"] = now
    record["result"] = dict(result or {})
    write_json(_path(activation_id), record)


def fail(
    activation_id: str,
    error: str,
    *,
    rolled_back_worker_ids: list[str] | None = None,
) -> None:
    record = get(activation_id)
    if record is None:
        raise TeamActivationError("activation_id does not exist")
    now = _now()
    record["status"] = "failed"
    record["updated_at"] = now
    record["completed_at"] = now
    record["error"] = str(error or "").strip()
    record["rolled_back_worker_ids"] = list(rolled_back_worker_ids or [])
    write_json(_path(activation_id), record)
