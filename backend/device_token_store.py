from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from paths import ba_home

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SCHEMA_VERSION = 1
_PATH_CACHE_HOME: Path | None = None
_PATH_CACHE_VALUE: Path | None = None


def _path() -> Path:
    global _PATH_CACHE_HOME, _PATH_CACHE_VALUE
    home = ba_home()
    if _PATH_CACHE_HOME == home and _PATH_CACHE_VALUE is not None:
        return _PATH_CACHE_VALUE
    root = home / "push"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "device_tokens.json"
    _PATH_CACHE_HOME = home
    _PATH_CACHE_VALUE = path
    return path


def _now() -> float:
    return time.time()


def _read_locked() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"schema_version": _SCHEMA_VERSION, "devices": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    schema_version = data.get("schema_version")
    devices = data.get("devices")
    if schema_version != _SCHEMA_VERSION or not isinstance(devices, dict):
        # Rebuildable registration table, not a durable source of truth:
        # self-heal to empty rather than 500ing every caller.
        logger.warning(
            "device_token_store: resetting store with stale schema_version=%r at %s",
            schema_version,
            path,
        )
        return {"schema_version": _SCHEMA_VERSION, "devices": {}}
    return data


def _write_locked(data: dict[str, Any]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "device_id": record["device_id"],
        "platform": record["platform"],
        "session_ids": list(record.get("session_ids") or []),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def register_token(
    device_id: str,
    token: str,
    platform: str,
    session_id: str,
) -> dict[str, Any]:
    now = _now()
    with _LOCK:
        data = _read_locked()
        existing = data["devices"].get(device_id)
        if isinstance(existing, dict):
            session_ids = list(existing.get("session_ids") or [])
            if session_id not in session_ids:
                session_ids.append(session_id)
            record = {
                "device_id": device_id,
                "token": token,
                "platform": platform,
                "session_ids": session_ids,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }
        else:
            record = {
                "device_id": device_id,
                "token": token,
                "platform": platform,
                "session_ids": [session_id],
                "created_at": now,
                "updated_at": now,
            }
        data["devices"][device_id] = record
        _write_locked(data)
    return _public(record)


def unregister_token(device_id: str) -> bool:
    with _LOCK:
        data = _read_locked()
        if device_id not in data["devices"]:
            return False
        del data["devices"][device_id]
        _write_locked(data)
    return True


def unregister_token_for_value(token: str) -> bool:
    """Remove any device registration holding this exact FCM token.

    Used by push_sender to self-heal when FCM reports a token as
    invalid/unregistered.
    """
    with _LOCK:
        data = _read_locked()
        stale_ids = [
            device_id
            for device_id, record in data["devices"].items()
            if isinstance(record, dict) and record.get("token") == token
        ]
        if not stale_ids:
            return False
        for device_id in stale_ids:
            del data["devices"][device_id]
        _write_locked(data)
    return True


def get_tokens_for_session(session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        data = _read_locked()
        return [
            {
                "device_id": record["device_id"],
                "token": record["token"],
                "platform": record["platform"],
            }
            for record in data["devices"].values()
            if isinstance(record, dict) and session_id in (record.get("session_ids") or [])
        ]
