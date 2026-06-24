"""Disk-backed consent store — the integrity boundary.

This mirrors ``stores/pending_approvals.py`` (fcntl-locked transitions,
24h expiry, 7-day prune, path-traversal-safe ids) but holds *credential
operation consents* instead of worker-spawn approvals.

The key invariant: the broker stores its OWN copy of the validated
descriptor here. Callers (Claude via the MCP tool) only ever pass a
``consent_id`` — they NEVER re-supply the descriptor at execute time. So
the operation that executes is byte-for-byte the one the user approved;
substitution is impossible because there is no caller-controlled descriptor
on the execute path.

No HMAC: the broker holds both the data and any key, so a seal would prove
nothing the broker-owned 0600 file doesn't already. Integrity rests on this
file being broker-write-only + the consent_id-only execute contract.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import portable_lock
from paths import ba_home

EXPIRY_HOURS = 24
PRUNE_AFTER_DAYS = 7

_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")


def _dir() -> Path:
    return ba_home() / "credential_broker" / "consents"


def _now() -> str:
    return datetime.now().isoformat()


def _path(consent_id: str) -> Path:
    if not _ID_RE.fullmatch(consent_id):
        raise ValueError(f"invalid consent_id: {consent_id!r}")
    return _dir() / f"{consent_id}.json"


def create(
    *,
    consent_id: str,
    app_session_id: str,
    provider_id: str,
    descriptor: dict,
    descriptor_hash: str,
    sink_public: dict,
) -> dict:
    """Persist a new pending consent. ``descriptor`` is the broker's frozen
    copy of the validated operation; ``sink_public`` is the display-safe
    computed-sink dict (no secret). ``secret_refs`` is None until the user
    binds secret values at approval time."""
    _dir().mkdir(parents=True, exist_ok=True)
    now_dt = datetime.now()
    record = {
        "consent_id": consent_id,
        "app_session_id": app_session_id,
        "provider_id": provider_id,
        "secret_ref": None,
        "secret_refs": None,
        "descriptor": descriptor,
        "descriptor_hash": descriptor_hash,
        "sink": sink_public,
        "status": "pending",  # pending | approved | denied | revoked
        "created_at": now_dt.isoformat(),
        "expires_at": (now_dt + timedelta(hours=EXPIRY_HOURS)).isoformat(),
        "resolved_at": None,
        "use_count": 0,
        "last_used_at": None,
    }
    path = _path(consent_id)
    if path.exists():
        raise ValueError(f"consent already exists for consent_id={consent_id!r}")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, json.dumps(record, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    return record


def get(consent_id: str) -> Optional[dict]:
    try:
        path = _path(consent_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def public_view(record: dict) -> dict:
    """Display-safe projection for REST/WS — drops the full descriptor's
    template strings (which could embed sink internals) but keeps the
    computed sink + status. Never contains the secret (descriptors never
    hold the value), but we still avoid leaking raw template internals to
    the UI beyond the computed sink."""
    descriptor = record.get("descriptor", {})
    secret_sources = descriptor.get("secret_sources") or {}
    return {
        "consent_id": record["consent_id"],
        "app_session_id": record["app_session_id"],
        "provider_id": record["provider_id"],
        "label": descriptor.get("label", ""),
        "sink": record.get("sink", {}),
        "status": record["status"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "use_count": record.get("use_count", 0),
        "secret_names": list(descriptor.get("secret_names") or ["secret"]),
        "secret_sources": {
            name: {
                "kind": source.get("kind"),
                "service": source.get("service"),
                "account": source.get("account"),
            }
            for name, source in secret_sources.items()
        },
    }


def list_pending(*, app_session_id: Optional[str] = None) -> list[dict]:
    if not _dir().exists():
        return []
    out: list[dict] = []
    for path in _dir().glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("status") != "pending":
            continue
        if app_session_id is not None and rec.get("app_session_id") != app_session_id:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("created_at", ""))
    return out


def _expired(rec: dict) -> bool:
    exp = rec.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) < datetime.now()
    except ValueError:
        return False


def _transition_locked(
    consent_id: str,
    new_status: str,
    *,
    secret_ref: Optional[str] = None,
    secret_refs: Optional[dict[str, str]] = None,
) -> tuple[Optional[dict], str]:
    """pending → approved|denied. On approve, binds ``secret_ref`` (the
    secret the user provided at approval). Returns (record, reason); reason ∈
    {ok, missing, already_resolved, expired}."""
    if new_status not in ("approved", "denied"):
        raise ValueError("new_status must be 'approved' or 'denied'")
    try:
        path = _path(consent_id)
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
                if _expired(rec):
                    return rec, "expired"
                rec["status"] = new_status
                rec["resolved_at"] = _now()
                if new_status == "approved":
                    rec["secret_ref"] = secret_ref
                    rec["secret_refs"] = secret_refs or (
                        {"secret": secret_ref} if secret_ref else None
                    )
                f.seek(0)
                f.truncate()
                f.write(json.dumps(rec, indent=2))
                return rec, "ok"
        finally:
            portable_lock.unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def approve(
    consent_id: str,
    *,
    secret_ref: Optional[str] = None,
    secret_refs: Optional[dict[str, str]] = None,
) -> tuple[Optional[dict], str]:
    """Approve a consent, binding the user-provided secret to it."""
    return _transition_locked(
        consent_id,
        "approved",
        secret_ref=secret_ref,
        secret_refs=secret_refs,
    )


def deny(consent_id: str) -> tuple[Optional[dict], str]:
    return _transition_locked(consent_id, "denied")


def revoke(consent_id: str) -> tuple[Optional[dict], str]:
    """Revoke a consent from any non-terminal state. After this, no further
    execute can acquire it. Returns (record, reason); reason ∈
    {ok, missing, already_revoked}."""
    try:
        path = _path(consent_id)
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
                if rec.get("status") == "revoked":
                    return rec, "already_revoked"
                rec["status"] = "revoked"
                rec["resolved_at"] = _now()
                f.seek(0)
                f.truncate()
                f.write(json.dumps(rec, indent=2))
                return rec, "ok"
        finally:
            portable_lock.unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_for_execute(consent_id: str) -> tuple[Optional[dict], str]:
    """Atomically verify a consent is usable and bump its use counter under
    the lock, returning the record. Reason ∈ {ok, missing, not_approved,
    revoked, expired}.

    The revoke check happens under the SAME exclusive lock as the use-count
    bump, so a concurrent ``revoke`` either commits before this call (→
    'revoked') or after it (the op runs once, then no further executes). No
    TOCTOU between the revoke decision and the execute decision.
    """
    try:
        path = _path(consent_id)
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
                status = rec.get("status")
                if status == "revoked":
                    return rec, "revoked"
                if status != "approved":
                    return rec, "not_approved"
                if _expired(rec):
                    return rec, "expired"
                if not rec.get("secret_refs") and not rec.get("secret_ref"):
                    return rec, "no_secret"
                rec["use_count"] = int(rec.get("use_count", 0)) + 1
                rec["last_used_at"] = _now()
                f.seek(0)
                f.truncate()
                f.write(json.dumps(rec, indent=2))
                return rec, "ok"
        finally:
            portable_lock.unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def delete(consent_id: str) -> bool:
    try:
        path = _path(consent_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def prune_old(max_age_days: int = PRUNE_AFTER_DAYS) -> int:
    if not _dir().exists():
        return 0
    cutoff_ts = (datetime.now() - timedelta(days=max_age_days)).timestamp()
    deleted = 0
    for path in _dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted
