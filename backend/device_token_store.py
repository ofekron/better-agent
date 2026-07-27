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
NOTIFICATION_CATEGORIES = (
    "pending_approvals",
    "pending_questions",
    "completed_turns",
)
DEFAULT_NOTIFICATION_PREFERENCES = {
    category: True for category in NOTIFICATION_CATEGORIES
}


class NotificationPreferencesError(ValueError):
    pass


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
        return {
            "schema_version": _SCHEMA_VERSION,
            "devices": {},
            "notification_preferences": {},
        }
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
        return {
            "schema_version": _SCHEMA_VERSION,
            "devices": {},
            "notification_preferences": {},
        }
    if not isinstance(data.get("notification_preferences"), dict):
        data["notification_preferences"] = {}
    return data


def _write_locked(data: dict[str, Any]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _public(
    record: dict[str, Any],
    preferences: dict[str, bool],
) -> dict[str, Any]:
    return {
        "device_id": record["device_id"],
        "platform": record["platform"],
        "session_ids": list(record.get("session_ids") or []),
        "notification_preferences": preferences,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _preferences(stored: object) -> dict[str, bool]:
    if not isinstance(stored, dict):
        return dict(DEFAULT_NOTIFICATION_PREFERENCES)
    return {
        category: stored.get(category, default)
        if isinstance(stored.get(category, default), bool)
        else default
        for category, default in DEFAULT_NOTIFICATION_PREFERENCES.items()
    }


def _validate_preferences(
    preferences: object,
    *,
    partial: bool,
) -> dict[str, bool]:
    if not isinstance(preferences, dict):
        raise NotificationPreferencesError(
            "notification_preferences must be an object"
        )
    unexpected = set(preferences) - set(NOTIFICATION_CATEGORIES)
    if unexpected:
        raise NotificationPreferencesError(
            f"unsupported notification preference: {sorted(unexpected)[0]}"
        )
    if not partial and set(preferences) != set(NOTIFICATION_CATEGORIES):
        raise NotificationPreferencesError(
            "all notification preferences are required"
        )
    for category, enabled in preferences.items():
        if not isinstance(enabled, bool):
            raise NotificationPreferencesError(f"{category} must be a boolean")
    return dict(preferences)


def register_token(
    device_id: str,
    token: str,
    platform: str,
    session_id: str,
    notification_preferences: dict[str, bool] | None = None,
) -> dict[str, Any]:
    validated_preferences = (
        _validate_preferences(notification_preferences, partial=True)
        if notification_preferences is not None
        else {}
    )
    now = _now()
    with _LOCK:
        data = _read_locked()
        stored_preferences = data["notification_preferences"].get(device_id)
        preferences = {
            **_preferences(stored_preferences),
            **validated_preferences,
        }
        data["notification_preferences"][device_id] = preferences
        duplicate_device_ids = [
            registered_device_id
            for registered_device_id, registered in data["devices"].items()
            if (
                registered_device_id != device_id
                and isinstance(registered, dict)
                and registered.get("token") == token
            )
        ]
        for duplicate_device_id in duplicate_device_ids:
            del data["devices"][duplicate_device_id]
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
    return _public(record, preferences)


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
    return get_tokens_for_session_category(session_id)


def get_tokens_for_session_category(
    session_id: str,
    category: str | None = None,
) -> list[dict[str, Any]]:
    if category is not None and category not in NOTIFICATION_CATEGORIES:
        raise NotificationPreferencesError(
            f"unsupported notification preference: {category}"
        )
    with _LOCK:
        data = _read_locked()
        return [
            {
                "device_id": record["device_id"],
                "token": record["token"],
                "platform": record["platform"],
            }
            for record in data["devices"].values()
            if (
                isinstance(record, dict)
                and session_id in (record.get("session_ids") or [])
                and (
                    category is None
                    or _preferences(
                        data["notification_preferences"].get(
                            record.get("device_id")
                        )
                    )[category]
                )
            )
        ]


def get_notification_preferences(device_id: str) -> dict[str, bool]:
    with _LOCK:
        data = _read_locked()
        return _preferences(data["notification_preferences"].get(device_id))


def update_notification_preferences(
    device_id: str,
    patch: dict[str, bool],
) -> dict[str, bool]:
    validated = _validate_preferences(patch, partial=True)
    if not validated:
        raise NotificationPreferencesError(
            "at least one notification preference is required"
        )
    with _LOCK:
        data = _read_locked()
        preferences = {
            **_preferences(data["notification_preferences"].get(device_id)),
            **validated,
        }
        data["notification_preferences"][device_id] = preferences
        record = data["devices"].get(device_id)
        if isinstance(record, dict):
            record["updated_at"] = _now()
        _write_locked(data)
        return preferences
