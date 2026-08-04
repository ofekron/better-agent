from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from paths import ba_home
from runs_dir import atomic_write_json

logger = logging.getLogger(__name__)

StatusListener = Callable[[dict[str, Any]], None]
_LISTENERS_LOCK = threading.Lock()
_LISTENERS: dict[str, set[StatusListener]] = {}
_STATUS_IO_LOCK = threading.RLock()


def _safe_id(delegation_id: str) -> str:
    return "".join(ch for ch in delegation_id if ch.isalnum() or ch in ("-", "_"))


def status_path(delegation_id: str) -> Path:
    return ba_home() / "delegate-status" / f"{_safe_id(delegation_id)}.json"


def write_status(delegation_id: str, **fields: Any) -> None:
    with _STATUS_IO_LOCK:
        current = read_status(delegation_id) or {}
        current.update(fields)
        _persist_status(delegation_id, current)


def begin_status(delegation_id: str, **fields: Any) -> None:
    with _STATUS_IO_LOCK:
        current = read_status(delegation_id) or {}
        status = dict(fields)
        if current.get("cancel_requested") is True:
            status["cancel_requested"] = True
            if "cancel_requested_at" in current:
                status["cancel_requested_at"] = current["cancel_requested_at"]
        _persist_status(delegation_id, status)


def start_unless_cancelled(
    delegation_id: str,
    operation: Callable[[], dict[str, Any]],
) -> bool:
    with _STATUS_IO_LOCK:
        current = read_status(delegation_id) or {}
        if (
            current.get("cancel_requested") is True
            or current.get("status") == "complete"
            or isinstance(current.get("result"), dict)
        ):
            return False
        fields = operation()
        current.update(fields)
        _persist_status(delegation_id, current)
        return True


def complete_status(
    delegation_id: str,
    result: dict[str, Any],
    cancelled_result: dict[str, Any],
) -> dict[str, Any]:
    with _STATUS_IO_LOCK:
        current = read_status(delegation_id) or {}
        existing_result = current.get("result")
        if current.get("status") == "complete" and isinstance(existing_result, dict):
            return dict(existing_result)
        committed = (
            dict(cancelled_result)
            if current.get("cancel_requested") is True
            else dict(result)
        )
        current.update(status="complete", result=committed)
        _persist_status(delegation_id, current)
        return committed


def _persist_status(delegation_id: str, status: dict[str, Any]) -> None:
    path = status_path(delegation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, status)
    _notify_status(delegation_id, status)


async def write_status_async(delegation_id: str, **fields: Any) -> None:
    await asyncio.to_thread(write_status, delegation_id, **fields)


async def begin_status_async(delegation_id: str, **fields: Any) -> None:
    await asyncio.to_thread(begin_status, delegation_id, **fields)


def read_status(delegation_id: str) -> dict[str, Any] | None:
    with _STATUS_IO_LOCK:
        path = status_path(delegation_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None


def subscribe_status(
    delegation_id: str,
    listener: StatusListener,
    *,
    replay: bool = False,
) -> Callable[[], None]:
    safe_id = _safe_id(delegation_id)
    with _LISTENERS_LOCK:
        _LISTENERS.setdefault(safe_id, set()).add(listener)
    if replay:
        current = read_status(delegation_id)
        if current is not None:
            _notify_listener(listener, current)

    def unsubscribe() -> None:
        with _LISTENERS_LOCK:
            listeners = _LISTENERS.get(safe_id)
            if listeners is None:
                return
            listeners.discard(listener)
            if not listeners:
                _LISTENERS.pop(safe_id, None)

    return unsubscribe


def _notify_status(delegation_id: str, status: dict[str, Any]) -> None:
    with _LISTENERS_LOCK:
        listeners = tuple(_LISTENERS.get(_safe_id(delegation_id), ()))
    for listener in listeners:
        _notify_listener(listener, status)


def _notify_listener(listener: StatusListener, status: dict[str, Any]) -> None:
    try:
        listener(dict(status))
    except Exception:
        logger.exception("delegation status listener failed")
