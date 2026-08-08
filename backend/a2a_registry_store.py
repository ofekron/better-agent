"""Disk-backed registry of user-registered outbound A2A remote agents.

One JSON file per agent at `<bc_home()>/a2a_registry/<id>.json`:

    {
        "schema_version": 1,
        "id": str,
        "name": str,
        "base_url": str,
        "auth_header_name": str,      # "" if the agent needs no auth
        "auth_secret": str,           # "" if none; NEVER returned by redact()
        "agent_card": dict,           # last-fetched/validated snapshot
        "created_at": iso,
        "last_probed_at": iso | None,
        "last_probe_ok": bool | None,
        "last_probe_error": str | None,
    }

`auth_secret` is a credential Better Agent must present outbound on
every call — unlike `node_registry_store`'s one-way argon2 hash, it must
be recoverable, so it's stored in the per-record file (0o700, the
`json_store.write_json` default — the same protection every other
bc_home() secret-bearing store in this repo relies on). `redact()` is
the ONLY view callers may hand to a REST response, a WS payload, a log
line, or a subagent — it never includes `auth_secret`.

Disposable store (per CLAUDE.md schema-evolution rule): a mismatched
`schema_version` is treated as absent — wipe `a2a_registry/` to reset.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import bc_home

SCHEMA_VERSION = 1

_ID_RE = re.compile(r"a2a_[A-Za-z0-9]{1,64}")
_cache_lock = threading.Lock()
_cache_loaded = False
_records_by_id: dict[str, dict] = {}
_records_ordered: list[dict] = []
_generation = 0


def _dir() -> Path:
    return bc_home() / "a2a_registry"


def _path(agent_id: str) -> Path:
    if not _ID_RE.fullmatch(agent_id or ""):
        raise ValueError(f"invalid a2a agent id: {agent_id!r}")
    return _dir() / f"{agent_id}.json"


def _version_path() -> Path:
    return _dir() / ".version"


def _copy_record(record: dict) -> dict:
    copied = dict(record)
    if isinstance(copied.get("agent_card"), dict):
        copied["agent_card"] = dict(copied["agent_card"])
    return copied


def _rebuild_cache_locked() -> None:
    global _cache_loaded, _records_by_id, _records_ordered
    records: list[dict] = []
    paths = list(_dir().glob("*.json")) if _dir().exists() else []
    for path in paths:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("schema_version") == SCHEMA_VERSION:
            records.append(rec)
    records.sort(key=lambda r: r.get("created_at", ""))
    _records_by_id = {
        rec["id"]: _copy_record(rec) for rec in records if isinstance(rec.get("id"), str)
    }
    _records_ordered = [_copy_record(rec) for rec in records]
    _cache_loaded = True


def _ensure_cache_locked() -> None:
    if not _cache_loaded:
        _rebuild_cache_locked()


def _read_generation_locked() -> int:
    path = _version_path()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    generation = data.get("generation") if isinstance(data, dict) else None
    return generation if isinstance(generation, int) and generation >= 0 else 0


def _bump_generation_locked() -> None:
    global _generation
    _generation = max(_generation, _read_generation_locked()) + 1
    write_json(_version_path(), {"generation": _generation})


def _sync_generation_locked() -> None:
    global _cache_loaded, _generation
    generation = _read_generation_locked()
    if generation != _generation:
        _generation = generation
        _cache_loaded = False


def _set_cached_record_locked(record: dict) -> None:
    agent_id = record.get("id")
    if not isinstance(agent_id, str):
        return
    _records_by_id[agent_id] = _copy_record(record)
    _records_ordered[:] = sorted(
        (_copy_record(rec) for rec in _records_by_id.values()),
        key=lambda r: r.get("created_at", ""),
    )


def version_token() -> tuple[int, int, int]:
    with _cache_lock:
        _sync_generation_locked()
        return (_generation, 0, 0)


def add(
    *,
    name: str,
    base_url: str,
    auth_header_name: str,
    auth_secret: str,
    agent_card: dict,
) -> dict:
    agent_id = f"a2a_{uuid.uuid4().hex[:16]}"
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": agent_id,
        "name": name,
        "base_url": base_url,
        "auth_header_name": auth_header_name,
        "auth_secret": auth_secret,
        "agent_card": agent_card,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_probed_at": None,
        "last_probe_ok": None,
        "last_probe_error": None,
    }
    write_json(_path(agent_id), record)
    with _cache_lock:
        if _cache_loaded:
            _set_cached_record_locked(record)
        _bump_generation_locked()
    return record


def get(agent_id: str) -> Optional[dict]:
    try:
        path = _path(agent_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict) or rec.get("schema_version") != SCHEMA_VERSION:
        return None
    return rec


def list_all() -> list[dict]:
    with _cache_lock:
        _sync_generation_locked()
        _ensure_cache_locked()
        return [_copy_record(rec) for rec in _records_ordered]


def remove(agent_id: str) -> bool:
    try:
        path = _path(agent_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    with _cache_lock:
        if _cache_loaded:
            _records_by_id.pop(agent_id, None)
            _records_ordered[:] = sorted(
                (_copy_record(rec) for rec in _records_by_id.values()),
                key=lambda r: r.get("created_at", ""),
            )
        _bump_generation_locked()
    return True


def update_probe_result(
    agent_id: str,
    *,
    ok: bool,
    error: Optional[str],
    agent_card: Optional[dict] = None,
) -> Optional[dict]:
    record = get(agent_id)
    if record is None:
        return None
    record["last_probed_at"] = datetime.now(timezone.utc).isoformat()
    record["last_probe_ok"] = ok
    record["last_probe_error"] = error
    if agent_card is not None:
        record["agent_card"] = agent_card
    write_json(_path(agent_id), record)
    with _cache_lock:
        if _cache_loaded:
            _set_cached_record_locked(record)
        _bump_generation_locked()
    return record


def redact(record: dict) -> dict:
    """The ONLY view of a registry record allowed to leave the process
    boundary (REST response, WS payload, log line, subagent). Never
    includes `auth_secret`."""
    redacted = {k: v for k, v in record.items() if k != "auth_secret"}
    redacted["has_auth_secret"] = bool(record.get("auth_secret"))
    return redacted
