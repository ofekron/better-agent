"""Persisted session ownership for the extension SDK.

Records which extension created a session so the SDK's session-message
mutation endpoints can enforce that an extension only mutates sessions it
created — never arbitrary sessions. A separate json map (not a session-schema
field), so it survives restarts without a schema migration.

Entries are best-effort metadata: if the map is lost, mutation simply
fail-closes (extensions lose write access until they recreate their sessions),
which is the safe direction.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from paths import ba_home

_lock = threading.Lock()


def _path() -> Path:
    return ba_home() / "extension_session_ownership.json"


def _load() -> dict[str, str]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, str]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def claim(session_id: str, extension_id: str) -> None:
    """Record that ``extension_id`` created ``session_id``. Idempotent."""
    if not session_id or not extension_id:
        return
    with _lock:
        data = _load()
        data[session_id] = extension_id
        _save(data)


def owner(session_id: str) -> str | None:
    with _lock:
        return _load().get(session_id)


def is_owner(session_id: str, extension_id: str) -> bool:
    return bool(session_id) and owner(session_id) == extension_id


def owned_session_ids() -> tuple[str, ...]:
    with _lock:
        return tuple(_load())


def disown(session_id: str) -> None:
    with _lock:
        data = _load()
        if data.pop(session_id, None) is not None:
            _save(data)
