"""Disk-backed approval store for fresh-worker delegation requests.

When the manager calls `delegate(worker_session_id=null, ...)`, we
write a pending approval record and block the delegate tool's HTTP
loopback until the user clicks Approve or Deny in the UI. The store
lives on disk so:

  - Backend restarts don't lose the in-flight approval (the detached
    runner is still blocking on its HTTP call).
  - Frontend refreshes can rehydrate the inline approval card via
    `GET /api/pending_approvals?cwd=...`.
  - Multi-tab "double approve" is idempotent — the second
    approve/deny call sees `status != "pending"` and is a no-op.

Storage: one JSON file per delegation at
~/.better-claude/pending_approvals/<delegation_id>.json with shape:

    {
        "delegation_id": str,
        "app_session_id": str,         # caller Better Agent session id
        "cwd": str,
        "justification": str,
        "proposed_description": str,
        "proposed_orchestration_mode": "manager" | "native",
        "instructions_preview": str,
        "model": str,
        "status": "pending" | "approved" | "denied",
        "created_at": iso,
        "expires_at": iso,             # 24h after created_at
        "resolved_at": iso | None,
        "approved_description": str | None,
        "approved_orchestration_mode": str | None,
    }

Files older than 7 days are pruned at backend startup regardless of
status. fcntl-based file locking serializes status transitions so two
tabs trying to approve the same request don't race.
"""

import portable_lock
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from paths import ba_home
import perf


def _dir() -> Path:
    return ba_home() / "pending_approvals"


EXPIRY_HOURS = 24
PRUNE_AFTER_DAYS = 7
_pending_cache_lock = threading.Lock()
_pending_cache: tuple[int, list[dict]] | None = None
_pending_cache_version = 0


def _now() -> str:
    return datetime.now().isoformat()


def _path(delegation_id: str) -> Path:
    # Strict id format — never let a caller-controlled string escape the dir.
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", delegation_id):
        raise ValueError(f"invalid delegation_id: {delegation_id!r}")
    return _dir() / f"{delegation_id}.json"


def _invalidate_pending_cache() -> None:
    global _pending_cache, _pending_cache_version
    with _pending_cache_lock:
        _pending_cache = None
        _pending_cache_version += 1


def _pending_snapshot() -> list[dict]:
    global _pending_cache
    while True:
        with _pending_cache_lock:
            version = _pending_cache_version
            cached = _pending_cache
            if cached is not None and cached[0] == version:
                return [dict(item) for item in cached[1]]
        if not _dir().exists():
            records: list[dict] = []
        else:
            records = []
            for path in _dir().glob("*.json"):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if rec.get("status") == "pending":
                    records.append(rec)
            records.sort(key=lambda r: r.get("created_at", ""))
        with _pending_cache_lock:
            if version != _pending_cache_version:
                continue
            _pending_cache = (version, [dict(item) for item in records])
            return records


@perf.timed_fn("store.approval.create")
def create(
    *,
    delegation_id: str,
    app_session_id: str,
    cwd: str,
    justification: str,
    proposed_description: str,
    proposed_orchestration_mode: str,
    instructions_preview: str,
    model: str,
    node_id: str = "primary",
) -> dict:
    """Persist a new pending-approval record. Returns the record.

    `node_id` is the worker-node the spawned worker will run on if the
    user approves. Surfaced to the frontend so the approval card can
    show "spawn worker on linux-box" instead of just "spawn worker".
    Defaults to "primary" so single-machine deployments stay unchanged.
    Field is purely additive — old records without it read as "primary"
    via `.get("node_id") or "primary"` at callsites."""
    if proposed_orchestration_mode == "manager":
        proposed_orchestration_mode = "team"
    if proposed_orchestration_mode not in ("team", "native"):
        raise ValueError(
            f"proposed_orchestration_mode must be 'team' or 'native', got "
            f"{proposed_orchestration_mode!r}"
        )
    _dir().mkdir(parents=True, exist_ok=True)
    now_dt = datetime.now()
    record = {
        "delegation_id": delegation_id,
        "app_session_id": app_session_id,
        "cwd": cwd,
        "justification": justification,
        "proposed_description": proposed_description,
        "proposed_orchestration_mode": proposed_orchestration_mode,
        "instructions_preview": instructions_preview[:2000],
        "model": model,
        "node_id": node_id,
        "status": "pending",
        "created_at": now_dt.isoformat(),
        "expires_at": (now_dt + timedelta(hours=EXPIRY_HOURS)).isoformat(),
        "resolved_at": None,
        "approved_description": None,
        "approved_orchestration_mode": None,
    }
    path = _path(delegation_id)
    if path.exists():
        raise ValueError(f"approval already exists for delegation_id={delegation_id!r}")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    _invalidate_pending_cache()
    return record


def get(delegation_id: str) -> Optional[dict]:
    try:
        path = _path(delegation_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_pending(*, cwd: Optional[str] = None) -> list[dict]:
    """All pending approvals, optionally filtered by cwd. Used by the
    REST `GET /api/pending_approvals?cwd=...` endpoint for WS-reconnect
    rehydration."""
    records = _pending_snapshot()
    if cwd is None:
        return records
    return [rec for rec in records if rec.get("cwd") == cwd]


@perf.timed_fn("store.approval.transition")
def _transition_locked(
    delegation_id: str,
    new_status: str,
    *,
    description: Optional[str] = None,
    orchestration_mode: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """Atomically transition a record from `pending` to `new_status`.

    Returns `(record_or_None, reason)`. `reason` is one of:
      - "ok": transition succeeded; record is the updated record
      - "missing": no such record on disk
      - "already_resolved": status was already `approved` or `denied`
                            (idempotent — second call is a no-op)
      - "expired": record's expires_at is in the past

    fcntl exclusive lock serializes concurrent approve/deny calls from
    two tabs so they don't both succeed and double-spawn.
    """
    if new_status not in ("approved", "denied"):
        raise ValueError(f"new_status must be 'approved' or 'denied'")
    try:
        path = _path(delegation_id)
    except ValueError:
        return None, "missing"
    if not path.exists():
        return None, "missing"

    # Open r+ so we can rewrite in place under the lock.
    fd = os.open(str(path), os.O_RDWR)
    try:
        portable_lock.lock_ex(fd)
        try:
            with os.fdopen(fd, "r+", closefd=False, encoding="utf-8") as f:
                f.seek(0)
                raw = f.read()
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    return None, "missing"

                if rec.get("status") != "pending":
                    return rec, "already_resolved"

                expires_at = rec.get("expires_at")
                if expires_at:
                    try:
                        if datetime.fromisoformat(expires_at) < datetime.now():
                            return rec, "expired"
                    except ValueError:
                        pass

                rec["status"] = new_status
                rec["resolved_at"] = _now()
                if new_status == "approved":
                    if description is not None:
                        rec["approved_description"] = description
                    else:
                        rec["approved_description"] = rec.get("proposed_description")
                    if orchestration_mode is not None:
                        if orchestration_mode == "manager":
                            orchestration_mode = "team"
                        if orchestration_mode not in ("team", "native"):
                            raise ValueError(
                                f"orchestration_mode must be team|native, got "
                                f"{orchestration_mode!r}"
                            )
                        rec["approved_orchestration_mode"] = orchestration_mode
                    else:
                        rec["approved_orchestration_mode"] = rec.get(
                            "proposed_orchestration_mode"
                        )

                f.seek(0)
                f.truncate()
                f.write(json.dumps(rec, indent=2))
                _invalidate_pending_cache()
                return rec, "ok"
        finally:
            portable_lock.unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def approve(
    delegation_id: str,
    *,
    description: Optional[str] = None,
    orchestration_mode: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    return _transition_locked(
        delegation_id,
        "approved",
        description=description,
        orchestration_mode=orchestration_mode,
    )


def deny(delegation_id: str) -> tuple[Optional[dict], str]:
    return _transition_locked(delegation_id, "denied")


def delete(delegation_id: str) -> bool:
    """Delete an approval record (regardless of status). Used by
    cancellation paths. Returns True if a record was removed."""
    try:
        path = _path(delegation_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
        _invalidate_pending_cache()
        return True
    except OSError:
        return False


def prune_old(max_age_days: int = PRUNE_AFTER_DAYS) -> int:
    """Delete approvals older than `max_age_days` regardless of status.
    Called at backend startup. Returns count deleted."""
    if not _dir().exists():
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    cutoff_ts = cutoff.timestamp()
    deleted = 0
    for path in _dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                deleted += 1
        except OSError:
            continue
    if deleted:
        _invalidate_pending_cache()
    return deleted
