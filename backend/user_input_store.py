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
_SCHEMA_VERSION = 2
_COUNTS_LOADED_PATH: Path | None = None
_PENDING_COUNTS_BY_SESSION: dict[str, int] = {}
_PENDING_REQUESTS_BY_SESSION: dict[str, list[dict[str, Any]]] = {}
_PENDING_COUNTS_VERSION = 0
_PATH_CACHE_HOME: Path | None = None
_PATH_CACHE_VALUE: Path | None = None


def _path() -> Path:
    global _PATH_CACHE_HOME, _PATH_CACHE_VALUE
    home = ba_home()
    if _PATH_CACHE_HOME == home and _PATH_CACHE_VALUE is not None:
        return _PATH_CACHE_VALUE
    root = home / "user_inputs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "requests.json"
    _PATH_CACHE_HOME = home
    _PATH_CACHE_VALUE = path
    return path


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


def _rebuild_counts_locked(data: dict[str, Any], path: Path | None = None) -> None:
    global _COUNTS_LOADED_PATH, _PENDING_COUNTS_VERSION
    previous = dict(_PENDING_COUNTS_BY_SESSION)
    _PENDING_COUNTS_BY_SESSION.clear()
    _PENDING_REQUESTS_BY_SESSION.clear()
    for req in data.get("requests", {}).values():
        if not isinstance(req, dict) or req.get("status") != "pending":
            continue
        sid = str(req.get("app_session_id") or "")
        if sid:
            _PENDING_COUNTS_BY_SESSION[sid] = _PENDING_COUNTS_BY_SESSION.get(sid, 0) + 1
            _PENDING_REQUESTS_BY_SESSION.setdefault(sid, []).append(_public(req))
    _COUNTS_LOADED_PATH = path or _path()
    if _PENDING_COUNTS_BY_SESSION != previous:
        _PENDING_COUNTS_VERSION += 1


def _ensure_counts_locked() -> None:
    if _COUNTS_LOADED_PATH is not None:
        return
    path = _path()
    _rebuild_counts_locked(_read_locked(), path)


def _adjust_pending_count_locked(app_session_id: Any, delta: int) -> None:
    global _PENDING_COUNTS_VERSION
    sid = str(app_session_id or "")
    if not sid:
        return
    previous = _PENDING_COUNTS_BY_SESSION.get(sid, 0)
    next_count = _PENDING_COUNTS_BY_SESSION.get(sid, 0) + delta
    if next_count > 0:
        _PENDING_COUNTS_BY_SESSION[sid] = next_count
    else:
        _PENDING_COUNTS_BY_SESSION.pop(sid, None)
        next_count = 0
    if next_count != previous:
        _PENDING_COUNTS_VERSION += 1


def _add_pending_public_locked(req: dict[str, Any]) -> None:
    sid = str(req.get("app_session_id") or "")
    if not sid or req.get("status") != "pending":
        return
    public_req = _public(req)
    rows = _PENDING_REQUESTS_BY_SESSION.setdefault(sid, [])
    rows[:] = [row for row in rows if row.get("request_id") != public_req["request_id"]]
    rows.append(public_req)


def _remove_pending_public_locked(req: dict[str, Any]) -> None:
    sid = str(req.get("app_session_id") or "")
    if not sid:
        return
    request_id = req.get("request_id")
    rows = _PENDING_REQUESTS_BY_SESSION.get(sid)
    if not rows:
        return
    rows[:] = [row for row in rows if row.get("request_id") != request_id]
    if not rows:
        _PENDING_REQUESTS_BY_SESSION.pop(sid, None)


def _write_locked(data: dict[str, Any]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _public(req: dict[str, Any]) -> dict[str, Any]:
    public = {
        "request_id": req["request_id"],
        "app_session_id": req["app_session_id"],
        "kind": req["kind"],
        "status": req["status"],
        "created_at": req["created_at"],
        "expires_at": req.get("expires_at"),
        "resolved_at": req.get("resolved_at"),
    }
    if req["kind"] == "approval":
        public["prompt"] = req["prompt"]
    else:
        public["questions"] = req["questions"]
    return public


def _pending_equivalent_locked(
    data: dict[str, Any],
    *,
    app_session_id: str,
    kind: str,
    questions: list[dict[str, Any]],
    prompt: str,
) -> dict[str, Any] | None:
    for req in data.get("requests", {}).values():
        if (
            isinstance(req, dict)
            and req.get("status") == "pending"
            and req.get("app_session_id") == app_session_id
            and req.get("kind") == kind
            and req.get("questions") == questions
            and req.get("prompt") == prompt
        ):
            return req
    return None


def _new_request(
    *,
    app_session_id: str,
    kind: str,
    questions: list[dict[str, Any]],
    prompt: str,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    now = _now()
    return {
        "request_id": uuid.uuid4().hex,
        "app_session_id": app_session_id,
        "kind": kind,
        "questions": questions,
        "prompt": prompt,
        "status": "pending",
        "response": {},
        "created_at": now,
        "expires_at": now + timeout_seconds if timeout_seconds else None,
        "resolved_at": None,
    }


def create_request(
    *,
    app_session_id: str,
    kind: str = "input",
    questions: list[dict[str, Any]],
    prompt: str = "",
    timeout_seconds: float | None,
) -> dict[str, Any]:
    req = _new_request(
        app_session_id=app_session_id,
        kind=kind,
        questions=questions,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
    )
    with _LOCK:
        data = _read_locked()
        _ensure_counts_locked()
        data["requests"][req["request_id"]] = req
        _write_locked(data)
        _adjust_pending_count_locked(app_session_id, 1)
        _add_pending_public_locked(req)
    return _public(req)


def create_or_get_pending_request(
    *,
    app_session_id: str,
    kind: str = "input",
    questions: list[dict[str, Any]],
    prompt: str = "",
    timeout_seconds: float | None,
) -> tuple[dict[str, Any], bool]:
    with _LOCK:
        data = _read_locked()
        _ensure_counts_locked()
        existing = _pending_equivalent_locked(
            data,
            app_session_id=app_session_id,
            kind=kind,
            questions=questions,
            prompt=prompt,
        )
        if existing is not None:
            return _public(existing), False
        req = _new_request(
            app_session_id=app_session_id,
            kind=kind,
            questions=questions,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
        data["requests"][req["request_id"]] = req
        _write_locked(data)
        _adjust_pending_count_locked(app_session_id, 1)
        _add_pending_public_locked(req)
    return _public(req), True


def pending_for_session(app_session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        _ensure_counts_locked()
        return [dict(req) for req in _PENDING_REQUESTS_BY_SESSION.get(app_session_id, [])]


def pending_count_for_session(app_session_id: str) -> int:
    with _LOCK:
        _ensure_counts_locked()
        return _PENDING_COUNTS_BY_SESSION.get(app_session_id, 0)


def pending_counts_by_session() -> dict[str, int]:
    with _LOCK:
        _ensure_counts_locked()
        return dict(_PENDING_COUNTS_BY_SESSION)


def pending_counts_version() -> int:
    with _LOCK:
        _ensure_counts_locked()
        return _PENDING_COUNTS_VERSION


def pending_counts_version_loaded() -> int:
    with _LOCK:
        return _PENDING_COUNTS_VERSION


def get_request(request_id: str) -> dict[str, Any] | None:
    with _LOCK:
        req = _read_locked()["requests"].get(request_id)
        return dict(req) if isinstance(req, dict) else None


def resolve_request(request_id: str, response: dict[str, Any]) -> dict[str, Any] | None:
    return _complete_request(request_id, "resolved", response)


def cancel_request(request_id: str) -> dict[str, Any] | None:
    return _complete_request(request_id, "cancelled", {})


def expire_request(request_id: str) -> dict[str, Any] | None:
    return _complete_request(request_id, "expired", {})


def _complete_request(
    request_id: str,
    status: str,
    response: dict[str, Any],
) -> dict[str, Any] | None:
    with _LOCK:
        data = _read_locked()
        req = data["requests"].get(request_id)
        if not isinstance(req, dict):
            return None
        if req.get("status") != "pending":
            return dict(req)
        req["status"] = status
        req["response"] = dict(response)
        req["resolved_at"] = _now()
        _write_locked(data)
        _ensure_counts_locked()
        _adjust_pending_count_locked(req.get("app_session_id"), -1)
        _remove_pending_public_locked(req)
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
