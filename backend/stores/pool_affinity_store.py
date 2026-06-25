from __future__ import annotations

import threading
from datetime import datetime, timezone

from json_store import write_json
from paths import ba_home


_SCHEMA_VERSION = 1
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path():
    return ba_home() / "workers" / "pool_affinities.json"


def _empty() -> dict:
    return {"version": _SCHEMA_VERSION, "bindings": {}}


def _read() -> dict:
    path = _path()
    if not path.exists():
        return _empty()
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
        return _empty()
    bindings = raw.setdefault("bindings", {})
    if not isinstance(bindings, dict):
        raw["bindings"] = {}
    return raw


def _binding_key(sender_session_id: str, affinity_key: str) -> str:
    return f"{sender_session_id}\0{affinity_key}"


def get_binding(tag: str, sender_session_id: str, affinity_key: str) -> str:
    clean_tag = str(tag or "").strip()
    clean_sender = str(sender_session_id or "").strip()
    clean_key = str(affinity_key or "").strip()
    if not clean_tag or not clean_sender or not clean_key:
        return ""
    with _lock:
        row = (_read().get("bindings") or {}).get(clean_tag, {}).get(
            _binding_key(clean_sender, clean_key),
            {},
        )
    if not isinstance(row, dict):
        return ""
    return str(row.get("worker_session_id") or "").strip()


def bind(tag: str, sender_session_id: str, affinity_key: str, worker_session_id: str) -> None:
    clean_tag = str(tag or "").strip()
    clean_sender = str(sender_session_id or "").strip()
    clean_key = str(affinity_key or "").strip()
    clean_worker = str(worker_session_id or "").strip()
    if not clean_tag or not clean_sender or not clean_key or not clean_worker:
        return
    with _lock:
        raw = _read()
        by_tag = raw.setdefault("bindings", {}).setdefault(clean_tag, {})
        key = _binding_key(clean_sender, clean_key)
        created_at = by_tag.get(key, {}).get("created_at") or _now()
        by_tag[key] = {
            "sender_session_id": clean_sender,
            "affinity_key": clean_key,
            "worker_session_id": clean_worker,
            "created_at": created_at,
            "updated_at": _now(),
        }
        write_json(_path(), raw)


def clear_binding(tag: str, sender_session_id: str, affinity_key: str) -> None:
    clean_tag = str(tag or "").strip()
    clean_sender = str(sender_session_id or "").strip()
    clean_key = str(affinity_key or "").strip()
    if not clean_tag or not clean_sender or not clean_key:
        return
    with _lock:
        raw = _read()
        by_tag = raw.get("bindings", {}).get(clean_tag)
        if not isinstance(by_tag, dict):
            return
        by_tag.pop(_binding_key(clean_sender, clean_key), None)
        if not by_tag:
            raw.get("bindings", {}).pop(clean_tag, None)
        write_json(_path(), raw)
