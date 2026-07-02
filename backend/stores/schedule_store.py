"""Durable schedule store — backend-owned replacement for the CLI's
in-process CronCreate/ScheduleWakeup timers.

One file at `ba_home()/schedules.json` holds every schedule. A schedule
is a prompt the backend fires into a session at `fire_at` through the
normal turn path (`coordinator.submit_prompt`), so scheduled turns are
durable across backend restarts and runner exits — unlike the CLI's
in-memory timers, which die with their process.

Schema migrations are NOT supported: on version mismatch we log loudly
and return an empty store. Wipe `schedules.json` to start fresh.
"""

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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

MAX_PER_SESSION = 20
MAX_PROMPT_LEN = 10_000
MIN_INTERVAL_SECONDS = 60
MAX_HORIZON = timedelta(days=365)

_lock = threading.Lock()
_data_cache: tuple[tuple[int, int], dict] | None = None


def _path() -> Path:
    return ba_home() / "schedules.json"


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "schedules": []}


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
            "schedule_store: failed to read %s (%s) — returning empty store. "
            "Delete the file to start fresh.", path, e,
        )
        return _empty()
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        logger.error(
            "schedule_store: unexpected shape/version at %s (expected %s, "
            "got %r) — returning empty store. Delete the file to start "
            "fresh.", path, SCHEMA_VERSION,
            raw.get("version") if isinstance(raw, dict) else type(raw).__name__,
        )
        return _empty()
    raw.setdefault("schedules", [])
    _data_cache = (fingerprint, copy.deepcopy(raw))
    return raw


def _write(data: dict) -> None:
    global _data_cache
    write_json(_path(), data)
    _data_cache = (_fingerprint(), copy.deepcopy(data))


def _parse_iso(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("fire_at must be an ISO-8601 string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("fire_at must be an ISO-8601 datetime")
    if dt.tzinfo is not None:
        raise ValueError("fire_at must be a naive local datetime")
    return dt


def create(
    *,
    app_session_id: str,
    prompt: str,
    kind: str,
    fire_at: Optional[str] = None,
    interval_seconds: Optional[int] = None,
) -> dict:
    """Validate and persist one schedule. Raises ValueError on any
    invalid input — callers surface the message to the tool/API caller.
    """
    if not isinstance(app_session_id, str) or not app_session_id:
        raise ValueError("app_session_id required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt required")
    if len(prompt) > MAX_PROMPT_LEN:
        raise ValueError(f"prompt exceeds {MAX_PROMPT_LEN} chars")
    if kind not in ("once", "recurring"):
        raise ValueError("kind must be 'once' or 'recurring'")

    now = datetime.now()
    if kind == "recurring":
        if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool):
            raise ValueError("interval_seconds (int) required for recurring")
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}")
        if timedelta(seconds=interval_seconds) > MAX_HORIZON:
            raise ValueError("interval exceeds max horizon")
        first_fire = (
            _parse_iso(fire_at) if fire_at else now + timedelta(seconds=interval_seconds)
        )
    else:
        if not fire_at:
            raise ValueError("fire_at required for kind='once'")
        first_fire = _parse_iso(fire_at)
        interval_seconds = None
    if first_fire > now + MAX_HORIZON:
        raise ValueError("fire_at exceeds max horizon")

    record = {
        "id": uuid.uuid4().hex[:12],
        "app_session_id": app_session_id,
        "prompt": prompt,
        "kind": kind,
        "fire_at": first_fire.isoformat(),
        "interval_seconds": interval_seconds,
        "created_at": now.isoformat(),
        "last_fired_at": None,
    }
    with _lock:
        data = _read()
        per_session = [
            s for s in data["schedules"]
            if s.get("app_session_id") == app_session_id
        ]
        if len(per_session) >= MAX_PER_SESSION:
            raise ValueError(
                f"session already has {MAX_PER_SESSION} schedules")
        data["schedules"].append(record)
        _write(data)
    return record


def list_all() -> list[dict]:
    """Every schedule across all sessions, sorted by next fire time."""
    with _lock:
        data = _read()
    return sorted(data["schedules"], key=lambda s: s.get("fire_at") or "")


def list_for_session(app_session_id: str) -> list[dict]:
    with _lock:
        data = _read()
    return [
        s for s in data["schedules"]
        if s.get("app_session_id") == app_session_id
    ]


def get(schedule_id: str) -> Optional[dict]:
    with _lock:
        data = _read()
    for s in data["schedules"]:
        if s.get("id") == schedule_id:
            return s
    return None


def delete(schedule_id: str) -> Optional[dict]:
    """Remove and return the schedule, or None if unknown."""
    with _lock:
        data = _read()
        for i, s in enumerate(data["schedules"]):
            if s.get("id") == schedule_id:
                removed = data["schedules"].pop(i)
                _write(data)
                return removed
    return None


def due(now: Optional[datetime] = None) -> list[dict]:
    """Schedules whose fire_at is in the past, oldest first."""
    now = now or datetime.now()
    with _lock:
        data = _read()
    out = []
    for s in data["schedules"]:
        try:
            if _parse_iso(s["fire_at"]) <= now:
                out.append(s)
        except (KeyError, ValueError, TypeError):
            logger.error("schedule_store: malformed record %r", s)
    out.sort(key=lambda s: s["fire_at"])
    return out


def mark_fired(schedule_id: str, now: Optional[datetime] = None) -> None:
    """once → delete; recurring → advance fire_at past `now` and stamp
    last_fired_at. Marking happens BEFORE the prompt is submitted so a
    crash mid-fire drops the firing (at-most-once) instead of
    double-firing on the next tick.
    """
    now = now or datetime.now()
    with _lock:
        data = _read()
        for i, s in enumerate(data["schedules"]):
            if s.get("id") != schedule_id:
                continue
            if s.get("kind") == "once":
                data["schedules"].pop(i)
            else:
                try:
                    interval_s = int(s["interval_seconds"])
                    if interval_s < 1:
                        raise ValueError("non-positive interval")
                    interval = timedelta(seconds=interval_s)
                    nxt = _parse_iso(s["fire_at"])
                except (KeyError, ValueError, TypeError):
                    # A malformed record would raise on EVERY tick and
                    # wedge the scheduler — drop it loudly instead.
                    logger.error(
                        "schedule_store: malformed recurring record %r — "
                        "dropping", s,
                    )
                    data["schedules"].pop(i)
                    _write(data)
                    return
                while nxt <= now:
                    nxt += interval
                s["fire_at"] = nxt.isoformat()
                s["last_fired_at"] = now.isoformat()
            _write(data)
            return
