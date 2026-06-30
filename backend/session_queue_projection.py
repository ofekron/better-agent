from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from paths import ba_home

_lock = threading.Lock()
_loaded = False
_records: dict[str, dict[str, Any]] = {}

_SIDECAR_JSON_SUFFIXES = (".summary.json", ".drafts.json")


def _projection_dir() -> Path:
    return ba_home() / "queue_recovery_projection"


def _record_path(session_id: str) -> Path:
    return _projection_dir() / f"{session_id}.json"


def _sessions_dir() -> Path:
    return ba_home() / "sessions"


def _is_session_json(path: Path) -> bool:
    return path.name.endswith(".json") and not path.name.endswith(_SIDECAR_JSON_SUFFIXES)


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _records.clear()
    projection_dir = _projection_dir()
    if projection_dir.is_dir():
        for path in projection_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sid = record.get("id") if isinstance(record, dict) else None
            if isinstance(sid, str):
                _records[sid] = copy.deepcopy(record)
    _loaded = True


def _write_record_locked(record: dict[str, Any]) -> None:
    session_id = record["id"]
    path = _record_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{session_id}.", suffix=".json.tmp", dir=path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _user_message_projection(messages: Iterable[Any]) -> dict[str, Any]:
    client_ids: list[str] = []
    lifecycle_ids: list[str] = []
    user_messages: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        user_messages.append(copy.deepcopy(msg))
        client_id = msg.get("client_id")
        if isinstance(client_id, str) and client_id:
            client_ids.append(client_id)
        lifecycle_id = msg.get("lifecycle_msg_id")
        if isinstance(lifecycle_id, str) and lifecycle_id:
            lifecycle_ids.append(lifecycle_id)
    return {
        "user_messages": user_messages,
        "user_client_ids": client_ids,
        "user_lifecycle_msg_ids": lifecycle_ids,
    }


def _queued_prompt_is_pending(
    prompt: dict[str, Any],
    user_client_ids: set[str],
    user_lifecycle_ids: set[str],
) -> bool:
    client_id = prompt.get("client_id")
    if isinstance(client_id, str) and client_id and client_id in user_client_ids:
        return False
    lifecycle_id = prompt.get("lifecycle_msg_id")
    if (
        isinstance(lifecycle_id, str)
        and lifecycle_id
        and lifecycle_id in user_lifecycle_ids
    ):
        return False
    return True


def project_session(session: dict[str, Any]) -> Optional[dict[str, Any]]:
    sid = session.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    user_projection = _user_message_projection(session.get("messages") or [])
    user_client_ids = set(user_projection["user_client_ids"])
    user_lifecycle_ids = set(user_projection["user_lifecycle_msg_ids"])
    queued = [
        copy.deepcopy(prompt)
        for prompt in (session.get("queued_prompts") or [])
        if isinstance(prompt, dict)
        and _queued_prompt_is_pending(prompt, user_client_ids, user_lifecycle_ids)
    ]
    return {
        "id": sid,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "queued_prompts": queued,
        **user_projection,
    }


def upsert_from_session(session: dict[str, Any]) -> None:
    record = project_session(session)
    if record is None:
        return
    upsert_record(record)


def upsert_record(record: dict[str, Any]) -> None:
    if not isinstance(record.get("id"), str) or not record["id"]:
        return
    with _lock:
        _load_locked()
        if _records.get(record["id"]) == record:
            return
        _records[record["id"]] = record
        _write_record_locked(record)


def get(session_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        _load_locked()
        record = _records.get(session_id)
        return copy.deepcopy(record) if record is not None else None


def queued_prompts(session_id: str) -> list[dict[str, Any]]:
    record = get(session_id)
    if not record:
        return []
    return [
        prompt for prompt in record.get("queued_prompts") or []
        if isinstance(prompt, dict)
    ]


def _walk_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for fork in node.get("forks") or []:
        if isinstance(fork, dict):
            yield from _walk_nodes(fork)


def rebuild_from_disk() -> int:
    rebuilt: dict[str, dict[str, Any]] = {}
    sessions_dir = _sessions_dir()
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*.json"):
            if not _is_session_json(path):
                continue
            try:
                root = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(root, dict):
                continue
            for node in _walk_nodes(root):
                record = project_session(node)
                if record is not None:
                    rebuilt[record["id"]] = record
    with _lock:
        _load_locked()
        projection_dir = _projection_dir()
        if projection_dir.is_dir():
            for path in projection_dir.glob("*.json"):
                try:
                    path.unlink()
                except OSError:
                    pass
        _records.clear()
        _records.update(rebuilt)
        for record in _records.values():
            _write_record_locked(record)
    return len(rebuilt)
