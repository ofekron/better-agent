"""Disk-backed store for worker-node registration requests.

When a brand-new worker-node (one that is NOT declared in topology.yaml
and NOT yet in `node_registry_store`) dials primary's `/api/node/connect`,
primary writes a pending-registration record here and blocks the node's
WS handshake until the logged-in user clicks Approve or Deny in the UI.

This mirrors `stores/pending_approvals.py` (fresh-worker approvals) so
the behaviour is familiar:

  - On-disk so a primary restart doesn't silently drop an in-flight
    request (the node is still dialing/retrying and will re-request).
  - Frontend refreshes rehydrate the popup via
    `GET /api/pending_nodes`.
  - Multi-tab "double approve" is idempotent — the second approve/deny
    call sees `status != "pending"` and is a no-op.

Storage: one JSON file per node at
~/.better-claude/pending_nodes/<node_id>.json with shape:

    {
        "node_id": str,
        "address": str,             # node's self-reported address (metadata)
        "cwd_roots": [str, ...],    # absolute paths the node exposes
        "secret_hash": str,         # argon2 hash of the node's secret,
                                    #   handed to node_registry_store on approve
        "fingerprint": str,         # short sha256 prefix of the secret, for display
        "status": "pending" | "approved" | "denied",
        "created_at": iso,
        "expires_at": iso,          # EXPIRY_HOURS after created_at
        "resolved_at": iso | None,
    }

fcntl-based file locking serializes status transitions so two tabs
trying to approve the same node don't race. Files older than
PRUNE_AFTER_DAYS are pruned at backend startup regardless of status.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import portable_lock
from paths import ba_home

logger = logging.getLogger(__name__)


EXPIRY_HOURS = 24
PRUNE_AFTER_DAYS = 7

# Same id charset as topology node ids — never let a caller-controlled
# string escape the directory.
_ID_RE = re.compile(r"[A-Za-z0-9_\-.]{1,64}")
_cache_lock = threading.Lock()
_cache_loaded = False
_pending_by_node: dict[str, dict] = {}
_cache_version = 0


def _bump_version_locked() -> None:
    global _cache_version
    _cache_version += 1


def _copy_record(record: dict) -> dict:
    copied = dict(record)
    cwd_roots = copied.get("cwd_roots")
    if isinstance(cwd_roots, list):
        copied["cwd_roots"] = list(cwd_roots)
    return copied


def _is_pending_active(record: dict, now: datetime | None = None) -> bool:
    if record.get("status") != "pending":
        return False
    expires_at = record.get("expires_at")
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) >= (now or datetime.now())
    except (TypeError, ValueError):
        return True


def _dir() -> Path:
    return ba_home() / "pending_nodes"


def _now() -> str:
    return datetime.now().isoformat()


def _path(node_id: str) -> Path:
    if not _ID_RE.fullmatch(node_id or ""):
        raise ValueError(f"invalid node_id: {node_id!r}")
    return _dir() / f"{node_id}.json"


def create(
    *,
    node_id: str,
    address: str,
    cwd_roots: list[str],
    secret_hash: str,
    fingerprint: str,
) -> dict:
    """Persist a new pending-registration record (overwriting any prior
    record for the same node_id — a re-dial supersedes a stale request).
    Returns the record."""
    _dir().mkdir(parents=True, exist_ok=True)
    now_dt = datetime.now()
    record = {
        "node_id": node_id,
        "address": address,
        "cwd_roots": list(cwd_roots or []),
        "secret_hash": secret_hash,
        "fingerprint": fingerprint,
        "status": "pending",
        "created_at": now_dt.isoformat(),
        "expires_at": (now_dt + timedelta(hours=EXPIRY_HOURS)).isoformat(),
        "resolved_at": None,
    }
    # Overwrite on re-dial: the node retries with the same node_id; the
    # latest request is the one a human should act on.
    _path(node_id).write_text(json.dumps(record, indent=2), encoding="utf-8")
    with _cache_lock:
        _pending_by_node[node_id] = _copy_record(record)
        _bump_version_locked()
    return _copy_record(record)


def version() -> int:
    with _cache_lock:
        return _cache_version


def get(node_id: str) -> Optional[dict]:
    try:
        path = _path(node_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_pending() -> list[dict]:
    """All records still in `pending` status, oldest first. Used by the
    REST `GET /api/pending_nodes` endpoint for popup rehydration."""
    global _cache_loaded
    with _cache_lock:
        now = datetime.now()
        if _cache_loaded:
            expired = [
                node_id
                for node_id, rec in _pending_by_node.items()
                if not _is_pending_active(rec, now)
            ]
            for node_id in expired:
                _pending_by_node.pop(node_id, None)
            if expired:
                _bump_version_locked()
            return sorted(
                (_copy_record(rec) for rec in _pending_by_node.values()),
                key=lambda r: r.get("created_at", ""),
            )
        if not _dir().exists():
            changed = bool(_pending_by_node)
            _pending_by_node.clear()
            if changed:
                _bump_version_locked()
            _cache_loaded = True
            return []
        out: list[dict] = []
        _pending_by_node.clear()
        for path in _dir().glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not _is_pending_active(rec, now):
                continue
            node_id = rec.get("node_id")
            if isinstance(node_id, str):
                _pending_by_node[node_id] = _copy_record(rec)
                out.append(_copy_record(rec))
        out.sort(key=lambda r: r.get("created_at", ""))
        _cache_loaded = True
        return out


def _transition_locked(node_id: str, new_status: str) -> tuple[Optional[dict], str]:
    """Atomically transition pending → approved|denied.

    Returns `(record_or_None, reason)` where reason is one of
    "ok" | "missing" | "already_resolved" | "expired". fcntl exclusive
    lock serializes concurrent approve/deny calls from two tabs."""
    if new_status not in ("approved", "denied"):
        raise ValueError("new_status must be 'approved' or 'denied'")
    try:
        path = _path(node_id)
    except ValueError:
        return None, "missing"
    if not path.exists():
        return None, "missing"

    fd = os.open(str(path), os.O_RDWR)
    try:
        portable_lock.lock_ex(fd)
        try:
            with os.fdopen(fd, "r+", closefd=False, encoding="utf-8") as f:
                f.seek(0)
                try:
                    rec = json.loads(f.read())
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
                f.seek(0)
                f.truncate()
                f.write(json.dumps(rec, indent=2))
                with _cache_lock:
                    _pending_by_node.pop(node_id, None)
                    _bump_version_locked()
                return rec, "ok"
        finally:
            portable_lock.unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def approve(node_id: str) -> tuple[Optional[dict], str]:
    return _transition_locked(node_id, "approved")


def deny(node_id: str) -> tuple[Optional[dict], str]:
    return _transition_locked(node_id, "denied")


def delete(node_id: str) -> bool:
    try:
        path = _path(node_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
        with _cache_lock:
            if _pending_by_node.pop(node_id, None) is not None:
                _bump_version_locked()
        return True
    except OSError:
        return False


def prune_old(max_age_days: int = PRUNE_AFTER_DAYS) -> int:
    """Delete records older than `max_age_days` regardless of status.
    Called at backend startup. Returns count deleted."""
    if not _dir().exists():
        return 0
    cutoff_ts = (datetime.now() - timedelta(days=max_age_days)).timestamp()
    deleted = 0
    for path in _dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                deleted += 1
                with _cache_lock:
                    if _pending_by_node.pop(path.stem, None) is not None:
                        _bump_version_locked()
        except OSError:
            continue
    return deleted
