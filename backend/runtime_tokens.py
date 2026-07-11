"""Scoped per-call tokens for runtime IPC ops (plan Phase 6).

Two independent credentials, so "can connect" is NOT "is admin":

  1. Transport connect-secret (`ipc.token`, in runtime_ipc) — the HMAC
     authkey. Proves same-home locality only. Holding it lets a peer
     complete the handshake; it grants NO op authority on its own.
  2. Per-call authority token — carried in every frame, resolved here
     against a 0600 hash-only registry, deny-by-default. The admin
     token (all scopes) is minted at server start into `admin.token`;
     session-scoped tokens (native adoption, agent clients) are minted
     on demand and may only touch ops naming their own session.

A client given only a scoped token therefore cannot escalate even
though it shares the connect-secret: the scoped token is not admin in
the registry, and the connect-secret is not an authority token at all.
(Residual: same-uid processes can read every 0600 file — that is the
OS trust boundary. The scoped layer is least-privilege for clients we
deliberately hand a narrow token, e.g. sandboxed native MCPs; it is
not a defense against an unconstrained same-uid peer.)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Optional

import runtime_ownership
from bff_runtime_contract import (
    BFF_SERVICE_SCOPE,
    BFF_SERVICE_TOKEN_KIND,
    BFF_SERVICE_TOKEN_NAME,
)

READ = "read"
WRITE = "write"
CONTROL = "control"
ALL_SCOPES = (READ, WRITE, CONTROL, BFF_SERVICE_SCOPE)

_REGISTRY_NAME = "tokens.json"
_ADMIN_TOKEN_NAME = "admin.token"
_LOCK = threading.Lock()


def registry_path() -> Path:
    return runtime_ownership.runtime_dir() / _REGISTRY_NAME


def admin_token_path() -> Path:
    return runtime_ownership.runtime_dir() / _ADMIN_TOKEN_NAME


def bff_service_token_path() -> Path:
    return runtime_ownership.runtime_dir() / BFF_SERVICE_TOKEN_NAME


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load() -> dict[str, Any]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(registry: dict[str, Any]) -> None:
    runtime_ownership.ensure_runtime_dir()
    path = registry_path()
    path.write_text(json.dumps(registry, indent=1, sort_keys=True), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _register(raw: str, record: dict[str, Any]) -> None:
    with _LOCK:
        registry = _load()
        registry[_hash(raw)] = record
        _save(registry)


def mint(kind: str, scopes: list[str], *, session_id: Optional[str] = None) -> str:
    if not kind or not scopes or any(s not in ALL_SCOPES for s in scopes):
        raise ValueError(f"invalid token spec: kind={kind!r} scopes={scopes!r}")
    raw = secrets.token_hex(32)
    _register(raw, {
        "kind": kind,
        "scopes": list(scopes),
        **({"session_id": session_id} if session_id else {}),
    })
    return raw


def revoke(raw: str) -> bool:
    with _LOCK:
        registry = _load()
        removed = registry.pop(_hash(raw), None) is not None
        if removed:
            _save(registry)
        return removed


def ensure_admin_token() -> str:
    """Server-side: mint the admin token once and persist it 0600.
    Idempotent — an existing valid admin.token is reused so restarts do
    not invalidate first-party clients mid-flight."""
    with _LOCK:
        path = admin_token_path()
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        registry = _load()
        if existing and _hash(existing) in registry:
            return existing
        raw = secrets.token_hex(32)
        registry[_hash(raw)] = {"kind": "admin", "scopes": list(ALL_SCOPES)}
        _save(registry)
        runtime_ownership.ensure_runtime_dir()
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        if os.name != "nt":
            path.chmod(0o600)
        return raw


def ensure_bff_service_token() -> str:
    with _LOCK:
        path = bff_service_token_path()
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        registry = _load()
        existing_record = registry.get(_hash(existing)) if existing else None
        if (
            isinstance(existing_record, dict)
            and existing_record.get("kind") == BFF_SERVICE_TOKEN_KIND
            and existing_record.get("scopes") == [BFF_SERVICE_SCOPE]
        ):
            return existing
        raw = secrets.token_hex(32)
        registry[_hash(raw)] = {
            "kind": BFF_SERVICE_TOKEN_KIND,
            "scopes": [BFF_SERVICE_SCOPE],
        }
        _save(registry)
        runtime_ownership.ensure_runtime_dir()
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        if os.name != "nt":
            path.chmod(0o600)
        return raw


def read_admin_token_or_empty() -> str:
    """Client-side: the first-party admin authority token, or "" when
    absent (ping needs no authority; other ops then fail closed)."""
    try:
        return admin_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class TokenResolver:
    """Registry-only resolver. Unknown tokens (including the bare
    transport connect-secret) resolve to None → denied."""

    def resolve(self, raw: object) -> Optional[dict[str, Any]]:
        if not isinstance(raw, str) or not raw:
            return None
        return _load().get(_hash(raw))
