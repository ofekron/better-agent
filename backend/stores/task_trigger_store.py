"""Durable registry of task triggers — the "when" side of a Task.

The Scheduler ticks this store each loop alongside `schedule_store`. A
trigger record says: "when this condition holds, launch task T via
`task_runner.launch_task`". One loop, typed sources: session-prompt timers
live in `schedule_store`; task triggers live here. No overlap in ownership.

Kinds:
  - schedule_once:     fire at `fire_at`, launch once, then delete.
  - schedule_recurring: launch every `interval_seconds`.
  - script:            run `detector` command every `poll_interval_seconds`;
                       launch on exit 0 (never on non-zero), advancing the
                       poll window either way so a failing detector backs off.

Schema migrations are NOT supported: on version mismatch we log loudly and
return an empty store. Wipe `task_triggers.json` to start fresh.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import ba_home

from stores import task_store

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_lock = threading.RLock()
_data_cache: tuple[tuple[int, int], dict] | None = None


def _path() -> Path:
    return ba_home() / "task_triggers.json"


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "triggers": []}


def _fingerprint() -> tuple[int, int]:
    try:
        st = _path().stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _read() -> dict:
    global _data_cache
    path = _path()
    if not path.exists():
        return _empty()
    fingerprint = _fingerprint()
    cached = _data_cache
    if cached is not None and cached[0] == fingerprint:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(
            "task_trigger_store: failed to read %s (%s) - returning empty store. "
            "Delete the file to start fresh.", path, e,
        )
        return _empty()
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        logger.error(
            "task_trigger_store: unexpected shape/version at %s (expected %s, "
            "got %r) - returning empty store. Delete the file to start fresh.",
            path, SCHEMA_VERSION,
            raw.get("version") if isinstance(raw, dict) else type(raw).__name__,
        )
        return _empty()
    raw.setdefault("triggers", [])
    if not isinstance(raw["triggers"], list):
        return _empty()
    _data_cache = (fingerprint, copy.deepcopy(raw))
    return raw


def _write(data: dict) -> None:
    global _data_cache
    write_json(_path(), data)
    _data_cache = (_fingerprint(), copy.deepcopy(data))


def _parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("fire_at must be an ISO-8601 datetime")
    if dt.tzinfo is not None:
        raise ValueError("fire_at must be a naive local datetime")
    return dt


def unregister_task(task_id: str) -> None:
    """Drop every trigger owned by `task_id` (on update/delete)."""
    with _lock:
        data = _read()
        before = len(data["triggers"])
        data["triggers"] = [t for t in data["triggers"] if t.get("task_id") != task_id]
        if len(data["triggers"]) != before:
            _write(data)


def register_for_task(task: dict) -> list[dict]:
    """Rebuild the trigger set for a task from its `trigger` field. Manual and
    api kinds produce no records (manual = UI button only; api = external REST
    call, not yet active). Returns the created records."""
    task_id = task.get("id")
    if not task_id:
        return []
    unregister_task(task_id)
    trigger = task.get("trigger") or {"kind": "manual", "config": {}}
    kind = trigger.get("kind") or "manual"
    cfg = trigger.get("config") or {}
    now = datetime.now()
    created: list[dict] = []

    def _add(**fields) -> None:
        rec = {
            "id": uuid.uuid4().hex[:12],
            "task_id": task_id,
            "task_cwd": task.get("cwd") or "",
            "task_node_id": task.get("node_id") or "primary",
            "created_at": now.isoformat(),
            "last_fired_at": None,
            **fields,
        }
        created.append(rec)

    if kind == "schedule":
        mode = cfg.get("mode", "once")
        if mode == "once":
            fire_at = _parse_iso(cfg["fire_at"])
            _add(kind="schedule_once", fire_at=fire_at.isoformat(), interval_seconds=None)
        else:
            interval = int(cfg["interval_seconds"])
            first = _parse_iso(cfg["fire_at"]) if cfg.get("fire_at") else now + timedelta(seconds=interval)
            _add(kind="schedule_recurring", fire_at=first.isoformat(), interval_seconds=interval)
    elif kind == "script":
        interval = int(cfg.get("poll_interval_seconds", 300))
        _add(
            kind="script",
            fire_at=(now + timedelta(seconds=interval)).isoformat(),
            interval_seconds=interval,
            detector=cfg["detector"],
        )
    # manual / api: no records.

    if created:
        with _lock:
            data = _read()
            data["triggers"].extend(created)
            _write(data)
    return [dict(c) for c in created]


def get(trigger_id: str) -> Optional[dict]:
    with _lock:
        data = _read()
    for t in data["triggers"]:
        if t.get("id") == trigger_id:
            return dict(t)
    return None


def due(now: Optional[datetime] = None) -> list[dict]:
    """Triggers whose fire_at is in the past, oldest first."""
    now = now or datetime.now()
    with _lock:
        data = _read()
    out = []
    for t in data["triggers"]:
        try:
            if _parse_iso(t["fire_at"]) <= now:
                out.append(t)
        except (KeyError, ValueError, TypeError):
            logger.error("task_trigger_store: malformed record %r", t)
    out.sort(key=lambda t: t["fire_at"])
    return out


def mark_fired(trigger_id: str, now: Optional[datetime] = None) -> None:
    """schedule_once → delete; everything else → advance fire_at past `now` by
    its interval and stamp last_fired_at. Marking before launch gives
    at-most-once on crash."""
    now = now or datetime.now()
    with _lock:
        data = _read()
        for i, t in enumerate(data["triggers"]):
            if t.get("id") != trigger_id:
                continue
            if t.get("kind") == "schedule_once":
                data["triggers"].pop(i)
                _write(data)
                return
            try:
                interval = timedelta(seconds=int(t["interval_seconds"]))
                nxt = _parse_iso(t["fire_at"])
            except (KeyError, ValueError, TypeError):
                logger.error("task_trigger_store: malformed record %r - dropping", t)
                data["triggers"].pop(i)
                _write(data)
                return
            while nxt <= now:
                nxt += interval
            t["fire_at"] = nxt.isoformat()
            t["last_fired_at"] = now.isoformat()
            _write(data)
            return


def list_for_task(task_id: str) -> list[dict]:
    with _lock:
        data = _read()
    return [dict(t) for t in data["triggers"] if t.get("task_id") == task_id]


def drop_task_references(task_id: str) -> None:
    unregister_task(task_id)
