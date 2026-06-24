from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paths import ba_home
from runs_dir import atomic_write_json


def _safe_id(delegation_id: str) -> str:
    return "".join(ch for ch in delegation_id if ch.isalnum() or ch in ("-", "_"))


def status_path(delegation_id: str) -> Path:
    return ba_home() / "delegate-status" / f"{_safe_id(delegation_id)}.json"


def write_status(delegation_id: str, **fields: Any) -> None:
    path = status_path(delegation_id)
    current = read_status(delegation_id) or {}
    current.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, current)


def read_status(delegation_id: str) -> dict[str, Any] | None:
    path = status_path(delegation_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
