from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from paths import ba_home

_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


def _path() -> Path:
    root = ba_home() / "user_inputs"
    root.mkdir(parents=True, exist_ok=True)
    return root / "requests.json"


def _now() -> float:
    return time.time()


def _read_locked() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"schema_version": _SCHEMA_VERSION, "requests": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeError("unexpected user input store schema")
    requests = data.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("invalid user input store")
    return data


def _write_locked(data: dict[str, Any]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _public(req: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": req["request_id"],
        "app_session_id": req["app_session_id"],
        "questions": req["questions"],
        "status": req["status"],
        "created_at": req["created_at"],
        "expires_at": req.get("expires_at"),
        "resolved_at": req.get("resolved_at"),
    }


def create_request(
    *,
    app_session_id: str,
    questions: list[dict[str, Any]],
    timeout_seconds: float | None,
) -> dict[str, Any]:
    now = _now()
    request_id = uuid.uuid4().hex
    req = {
        "request_id": request_id,
        "app_session_id": app_session_id,
        "questions": questions,
        "status": "pending",
        "answers": {},
        "created_at": now,
        "expires_at": now + timeout_seconds if timeout_seconds else None,
        "resolved_at": None,
    }
    with _LOCK:
        data = _read_locked()
        data["requests"][request_id] = req
        _write_locked(data)
    return _public(req)


def pending_for_session(app_session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        data = _read_locked()
        return [
            _public(req)
            for req in data["requests"].values()
            if req.get("app_session_id") == app_session_id and req.get("status") == "pending"
        ]


def get_request(request_id: str) -> dict[str, Any] | None:
    with _LOCK:
        req = _read_locked()["requests"].get(request_id)
        return dict(req) if isinstance(req, dict) else None


def resolve_request(request_id: str, answers: dict[str, str]) -> dict[str, Any] | None:
    return _complete_request(request_id, "resolved", answers)


def cancel_request(request_id: str) -> dict[str, Any] | None:
    return _complete_request(request_id, "cancelled", {})


def expire_request(request_id: str) -> dict[str, Any] | None:
    return _complete_request(request_id, "expired", {})


def _complete_request(
    request_id: str,
    status: str,
    answers: dict[str, str],
) -> dict[str, Any] | None:
    with _LOCK:
        data = _read_locked()
        req = data["requests"].get(request_id)
        if not isinstance(req, dict):
            return None
        if req.get("status") != "pending":
            return dict(req)
        req["status"] = status
        req["answers"] = dict(answers)
        req["resolved_at"] = _now()
        _write_locked(data)
    return dict(req)


async def wait_for_completion(request_id: str, timeout_seconds: float | None) -> dict[str, Any] | None:
    deadline = _now() + timeout_seconds if timeout_seconds else None
    while True:
        existing = get_request(request_id)
        if existing is None or existing.get("status") != "pending":
            return existing
        if deadline is not None and _now() >= deadline:
            return expire_request(request_id)
        await asyncio.sleep(0.05)
