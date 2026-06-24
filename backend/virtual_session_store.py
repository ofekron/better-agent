from __future__ import annotations

import json
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from paths import ba_home

_lock = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VIRTUAL_PREFIX = "virtual:"
_MAX_SYNTHETIC_MESSAGES = 500
_MAX_BACKING_SESSIONS = 25
_MAX_METADATA_BYTES = 64 * 1024
_cache_signature: tuple[int, int] | None = None
_cache_data: dict[str, Any] | None = None
_summary_cache_signature: tuple[int, int] | None = None
_summary_cache: list[dict[str, Any]] | None = None


def _path() -> Path:
    return ba_home() / "virtual_sessions.json"


def _now() -> str:
    return datetime.now().isoformat()


def _load() -> dict[str, Any]:
    global _cache_data, _cache_signature
    path = _path()
    try:
        st = path.stat()
    except OSError:
        _cache_signature = None
        _cache_data = None
        return {"version": 1, "sessions": {}}
    signature = (st.st_mtime_ns, st.st_size)
    if _cache_signature == signature and _cache_data is not None:
        return deepcopy(_cache_data)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cache_signature = None
        _cache_data = None
        return {"version": 1, "sessions": {}}
    if not isinstance(data, dict) or data.get("version") != 1:
        _cache_signature = None
        _cache_data = None
        return {"version": 1, "sessions": {}}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        data["sessions"] = {}
    _cache_signature = signature
    _cache_data = deepcopy(data)
    return deepcopy(data)


def _save(data: dict[str, Any]) -> None:
    global _cache_data, _cache_signature, _summary_cache, _summary_cache_signature
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    try:
        st = path.stat()
        _cache_signature = (st.st_mtime_ns, st.st_size)
        _cache_data = deepcopy(data)
    except OSError:
        _cache_signature = None
        _cache_data = None
    _summary_cache_signature = None
    _summary_cache = None


def _clean_extension_id(extension_id: str) -> str:
    extension_id = str(extension_id or "").strip()
    if not extension_id:
        raise ValueError("extension_id is required")
    if not _ID_RE.fullmatch(extension_id) or ":" in extension_id:
        raise ValueError("extension_id contains invalid characters")
    return extension_id


def _prefix(extension_id: str) -> str:
    return f"{_VIRTUAL_PREFIX}{extension_id}:"


def _is_valid_virtual_id(session_id: Any, extension_id: str | None = None) -> bool:
    raw = str(session_id or "").strip()
    if not _ID_RE.fullmatch(raw):
        return False
    parts = raw.split(":", 2)
    if len(parts) != 3 or parts[0] != "virtual":
        return False
    owner, suffix = parts[1], parts[2]
    if not owner or not suffix:
        return False
    try:
        _clean_extension_id(owner)
    except ValueError:
        return False
    if extension_id is None:
        return True
    return raw.startswith(_prefix(_clean_extension_id(extension_id)))


def _assert_not_real_session(session_id: str) -> None:
    import session_store
    if session_store.get_session(session_id) is not None:
        raise ValueError("virtual session id collides with a real session")


def _clean_id(extension_id: str, value: Any) -> str:
    extension_id = _clean_extension_id(extension_id)
    raw = str(value or "").strip()
    if not raw:
        return f"{_prefix(extension_id)}{uuid.uuid4().hex}"
    if not _ID_RE.fullmatch(raw):
        raise ValueError("virtual session id contains invalid characters")
    if not raw.startswith(_prefix(extension_id)):
        raise ValueError("virtual session id must use the extension namespace")
    _assert_not_real_session(raw)
    return raw


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _backing_session_ids(extension_id: str, value: Any) -> list[str]:
    ids = _strings(value)
    if len(ids) > _MAX_BACKING_SESSIONS:
        raise ValueError(f"virtual session backing sessions exceed {_MAX_BACKING_SESSIONS}")
    import extension_session_ownership
    for sid in ids:
        if not extension_session_ownership.is_owner(sid, extension_id):
            raise PermissionError("extension does not own backing session")
    return ids


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        msg = {
            "id": str(item.get("id") or uuid.uuid4().hex),
            "role": role,
            "content": str(item.get("content") or ""),
            "timestamp": str(item.get("timestamp") or _now()),
            "events": item.get("events") if isinstance(item.get("events"), list) else [],
            "isStreaming": bool(item.get("isStreaming", item.get("is_streaming", False))),
        }
        for key in (
            "client_id",
            "source",
            "lifecycle_msg_id",
            "backing_session_id",
            "backing_message_id",
            "ask_result",
            "chosen_session_id",
            "completed_at",
            "stopped_at",
            "interrupted_by_msg_id",
            "trace_id",
            "error",
            "errorText",
            "agent_message_uuid",
        ):
            if item.get(key) is not None:
                msg[key] = item[key]
        out.append(msg)
    return out


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    encoded = json.dumps(value, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds 64 KB")
    return value


def _synthetic_messages(payload: dict[str, Any], existing: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = payload.get(
        "synthetic_messages",
        payload.get("messages", (existing or {}).get("synthetic_messages", (existing or {}).get("messages") or [])),
    )
    messages = _messages(raw)
    if len(messages) > _MAX_SYNTHETIC_MESSAGES:
        raise ValueError(f"virtual session synthetic messages exceed {_MAX_SYNTHETIC_MESSAGES}")
    for message in messages:
        message["source"] = message.get("source") or "synthetic"
    return messages


def _stored_projection(extension_id: str, payload: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    now = _now()
    session_id = _clean_id(extension_id, payload.get("id") or (existing or {}).get("id"))
    synthetic_messages = _synthetic_messages(payload, existing)
    name = str(payload.get("name") or (existing or {}).get("name") or "Virtual Session").strip()
    cwd = str(payload.get("cwd") if payload.get("cwd") is not None else (existing or {}).get("cwd") or "")
    model = str(payload.get("model") if payload.get("model") is not None else (existing or {}).get("model") or "")
    provider_id = payload.get("provider_id", (existing or {}).get("provider_id"))
    node_id = str(payload.get("node_id") if payload.get("node_id") is not None else (existing or {}).get("node_id") or "primary")
    backing_session_ids = _backing_session_ids(
        extension_id,
        payload.get("backing_session_ids", (existing or {}).get("backing_session_ids") or [])
    )
    metadata = _metadata(payload.get("metadata", (existing or {}).get("metadata") or {}))
    created_at = str((existing or {}).get("created_at") or payload.get("created_at") or now)
    return {
        "id": session_id,
        "name": name or "Virtual Session",
        "cwd": cwd,
        "model": model,
        "provider_id": provider_id if isinstance(provider_id, str) else None,
        "node_id": node_id or "primary",
        "created_at": created_at,
        "updated_at": now,
        "synthetic_messages": synthetic_messages,
        "message_count": len(synthetic_messages),
        "orchestration_mode": "virtual",
        "source": "extension",
        "kind": "user",
        "virtual": True,
        "extension_id": extension_id,
        "backing_session_ids": backing_session_ids,
        "metadata": metadata,
        "is_running": False,
        "unread_count": 0,
        "monitoring_state": "idle",
    }


def _backing_messages(extension_id: str, session_ids: list[str]) -> list[dict[str, Any]]:
    from session_manager import manager as session_manager
    import extension_session_ownership
    out: list[dict[str, Any]] = []
    for sid in session_ids:
        if not extension_session_ownership.is_owner(sid, extension_id):
            continue
        session = session_manager.get_lite(sid)
        if not session:
            continue
        for message in session.get("messages") or []:
            if not isinstance(message, dict):
                continue
            copied = deepcopy(message)
            copied["backing_session_id"] = sid
            copied["backing_message_id"] = copied.get("id")
            out.append(copied)
    return out


def _materialize(stored: dict[str, Any], *, include_messages: bool) -> dict[str, Any]:
    session = deepcopy(stored)
    synthetic = _messages(session.pop("synthetic_messages", []) or [])
    backing = _backing_messages(
        str(session.get("extension_id") or ""),
        _strings(session.get("backing_session_ids") or []),
    )
    messages = backing + synthetic
    messages.sort(key=lambda message: str(message.get("timestamp") or ""))
    session["message_count"] = len(messages)
    session["max_seq_by_sid"] = {}
    session["pagination"] = {
        "total_messages": len(messages),
        "oldest_loaded_seq": 1 if messages else None,
        "has_older": False,
    }
    if include_messages:
        session["messages"] = messages
    else:
        session.pop("messages", None)
    return session


def list_all() -> list[dict[str, Any]]:
    global _summary_cache, _summary_cache_signature
    with _lock:
        data = _load()
        signature = _cache_signature
        if (
            signature is not None
            and _summary_cache_signature == signature
            and _summary_cache is not None
        ):
            return deepcopy(_summary_cache)
        sessions = data.get("sessions") or {}
        out: list[dict[str, Any]] = []
        for session in sessions.values():
            if not (
                isinstance(session, dict)
                and _is_valid_virtual_id(session.get("id"), session.get("extension_id"))
            ):
                continue
            summary = dict(session)
            summary.pop("synthetic_messages", None)
            summary.pop("messages", None)
            out.append(summary)
        out.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
        if signature is not None:
            _summary_cache_signature = signature
            _summary_cache = deepcopy(out)
        return deepcopy(out)


def get(session_id: str) -> dict[str, Any] | None:
    if not _is_valid_virtual_id(session_id):
        return None
    with _lock:
        session = (_load().get("sessions") or {}).get(session_id)
        if not isinstance(session, dict):
            return None
        if not _is_valid_virtual_id(session.get("id"), session.get("extension_id")):
            return None
        stored = deepcopy(session)
    return _materialize(stored, include_messages=True)


def upsert(extension_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    extension_id = _clean_extension_id(extension_id)
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        session_id = _clean_id(extension_id, payload.get("id"))
        existing = sessions.get(session_id)
        if isinstance(existing, dict) and existing.get("extension_id") != extension_id:
            raise PermissionError("extension does not own this virtual session")
        session = _stored_projection(extension_id, {**payload, "id": session_id}, existing if isinstance(existing, dict) else None)
        sessions[session_id] = session
        _save(data)
        stored = deepcopy(session)
    return _materialize(stored, include_messages=True)


def delete(extension_id: str, session_id: str) -> bool:
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        existing = sessions.get(session_id)
        if not isinstance(existing, dict):
            return False
        if existing.get("extension_id") != extension_id:
            raise PermissionError("extension does not own this virtual session")
        del sessions[session_id]
        _save(data)
        return True


def replace_messages(extension_id: str, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        existing = sessions.get(session_id)
        if not isinstance(existing, dict):
            raise KeyError("virtual session not found")
        if existing.get("extension_id") != extension_id:
            raise PermissionError("extension does not own this virtual session")
        session = _stored_projection(extension_id, {**existing, "synthetic_messages": messages}, existing)
        sessions[session_id] = session
        _save(data)
        stored = deepcopy(session)
    return _materialize(stored, include_messages=True)


def append_message(extension_id: str, session_id: str, message: dict[str, Any]) -> dict[str, Any]:
    parsed = _messages([message])
    if not parsed:
        raise ValueError("message role must be 'user' or 'assistant'")
    parsed[0]["source"] = parsed[0].get("source") or "synthetic"
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        existing = sessions.get(session_id)
        if not isinstance(existing, dict):
            raise KeyError("virtual session not found")
        if existing.get("extension_id") != extension_id:
            raise PermissionError("extension does not own this virtual session")
        messages = _messages(existing.get("synthetic_messages", existing.get("messages") or []))
        if len(messages) >= _MAX_SYNTHETIC_MESSAGES:
            raise ValueError(f"virtual session synthetic messages exceed {_MAX_SYNTHETIC_MESSAGES}")
        messages.extend(parsed)
        session = _stored_projection(extension_id, {**existing, "synthetic_messages": messages}, existing)
        sessions[session_id] = session
        _save(data)
    return deepcopy(parsed[0])


def update_message_fields(
    extension_id: str,
    session_id: str,
    message_id: str,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        existing = sessions.get(session_id)
        if not isinstance(existing, dict):
            raise KeyError("virtual session not found")
        if existing.get("extension_id") != extension_id:
            raise PermissionError("extension does not own this virtual session")
        messages = _messages(existing.get("synthetic_messages", existing.get("messages") or []))
        for message in messages:
            if not isinstance(message, dict) or message.get("id") != message_id:
                continue
            for key, value in fields.items():
                if value is None:
                    message.pop(key, None)
                else:
                    message[key] = value
            session = _stored_projection(extension_id, {**existing, "synthetic_messages": messages}, existing)
            sessions[session_id] = session
            _save(data)
            return deepcopy(message)
    return None
