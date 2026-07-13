from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TypedDict

from json_store import write_json_durable
from paths import bc_home


SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_ROOT_KEYS = frozenset({"schema_version", "records"})
_RECORD_KEYS = frozenset({
    "parent_session_id",
    "target_session_id",
    "lifecycle_msg_id",
    "owner_lifecycle_msg_id",
    "started_at",
    "last_event_at",
})


class DetachedBackgroundRecord(TypedDict):
    parent_session_id: str
    target_session_id: str
    lifecycle_msg_id: str
    owner_lifecycle_msg_id: str | None
    started_at: str
    last_event_at: str


class DetachedBackgroundStoreError(ValueError):
    pass


def _path() -> Path:
    return bc_home() / "detached_background.json"


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DetachedBackgroundStoreError(f"{field} must be a non-empty string")
    return value


def _validate_record(
    raw: object,
    *,
    lifecycle_key: str,
) -> DetachedBackgroundRecord:
    if not isinstance(raw, dict) or set(raw) != _RECORD_KEYS:
        raise DetachedBackgroundStoreError(
            f"detached background record {lifecycle_key!r} has invalid shape"
        )
    record: DetachedBackgroundRecord = {
        "parent_session_id": _require_text(
            raw["parent_session_id"], "parent_session_id"
        ),
        "target_session_id": _require_text(
            raw["target_session_id"], "target_session_id"
        ),
        "lifecycle_msg_id": _require_text(
            raw["lifecycle_msg_id"], "lifecycle_msg_id"
        ),
        "owner_lifecycle_msg_id": (
            _require_text(raw["owner_lifecycle_msg_id"], "owner_lifecycle_msg_id")
            if raw["owner_lifecycle_msg_id"] is not None
            else None
        ),
        "started_at": _require_text(raw["started_at"], "started_at"),
        "last_event_at": _require_text(raw["last_event_at"], "last_event_at"),
    }
    if record["lifecycle_msg_id"] != lifecycle_key:
        raise DetachedBackgroundStoreError(
            f"detached background record key {lifecycle_key!r} does not match lifecycle_msg_id"
        )
    return record


def _read_locked() -> dict[str, DetachedBackgroundRecord]:
    path = _path()
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DetachedBackgroundStoreError(
            f"failed to parse detached background store: {exc}"
        ) from exc
    if not isinstance(state, dict) or set(state) != _ROOT_KEYS:
        raise DetachedBackgroundStoreError("detached background store has invalid shape")
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] != SCHEMA_VERSION
    ):
        raise DetachedBackgroundStoreError(
            "unsupported detached background store schema_version="
            f"{state['schema_version']!r}; expected {SCHEMA_VERSION}"
        )
    raw_records = state["records"]
    if not isinstance(raw_records, dict):
        raise DetachedBackgroundStoreError(
            "detached background store records must be an object"
        )
    records: dict[str, DetachedBackgroundRecord] = {}
    for lifecycle_key, raw in raw_records.items():
        lifecycle = _require_text(lifecycle_key, "record key")
        records[lifecycle] = _validate_record(raw, lifecycle_key=lifecycle)
    return records


def _write_locked(records: dict[str, DetachedBackgroundRecord]) -> None:
    write_json_durable(
        _path(),
        {"schema_version": SCHEMA_VERSION, "records": records},
    )


def load() -> dict[str, DetachedBackgroundRecord]:
    with _LOCK:
        return _read_locked()


def upsert(
    *,
    parent_session_id: str,
    target_session_id: str,
    lifecycle_msg_id: str,
    owner_lifecycle_msg_id: str | None,
    started_at: str,
    last_event_at: str,
) -> DetachedBackgroundRecord:
    record: DetachedBackgroundRecord = {
        "parent_session_id": _require_text(parent_session_id, "parent_session_id"),
        "target_session_id": _require_text(target_session_id, "target_session_id"),
        "lifecycle_msg_id": _require_text(lifecycle_msg_id, "lifecycle_msg_id"),
        "owner_lifecycle_msg_id": (
            _require_text(owner_lifecycle_msg_id, "owner_lifecycle_msg_id")
            if owner_lifecycle_msg_id is not None
            else None
        ),
        "started_at": _require_text(started_at, "started_at"),
        "last_event_at": _require_text(last_event_at, "last_event_at"),
    }
    with _LOCK:
        records = _read_locked()
        if records.get(record["lifecycle_msg_id"]) == record:
            return record
        records[record["lifecycle_msg_id"]] = record
        _write_locked(records)
    return record


def remove(
    lifecycle_msg_id: str,
    *,
    target_session_id: str | None = None,
) -> bool:
    lifecycle = _require_text(lifecycle_msg_id, "lifecycle_msg_id")
    target = (
        _require_text(target_session_id, "target_session_id")
        if target_session_id is not None
        else None
    )
    with _LOCK:
        records = _read_locked()
        record = records.get(lifecycle)
        if record is None or (
            target is not None and record["target_session_id"] != target
        ):
            return False
        records.pop(lifecycle)
        _write_locked(records)
        return True


def drop_sessions(session_ids: set[str]) -> set[str]:
    removed_sessions = {
        _require_text(session_id, "session_id") for session_id in session_ids
    }
    if not removed_sessions:
        return set()
    with _LOCK:
        records = _read_locked()
        removed_lifecycles = {
            lifecycle
            for lifecycle, record in records.items()
            if record["parent_session_id"] in removed_sessions
            or record["target_session_id"] in removed_sessions
        }
        if not removed_lifecycles:
            return set()
        for lifecycle in removed_lifecycles:
            records.pop(lifecycle)
        _write_locked(records)
        return removed_lifecycles
