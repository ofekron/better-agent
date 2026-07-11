"""Scoped per-call tokens for runtime IPC ops (plan Phase 6).

Layer 2 of IPC auth. The transport HMAC (`ipc.token`) only proves
same-home locality; every frame additionally carries a token mapping
to a kind + scope set, deny-by-default. The transport token doubles as
the admin credential (all scopes) so first-party local clients keep a
single file; session-scoped tokens (native adoption, agent-kind
clients) are minted here and can only act on their own session — they
can never shut the runtime down, submit into foreign sessions, or
enumerate other sessions.

Registry: `ba_home()/runtime/tokens.json`, 0600, token HASHES only —
raw tokens are returned once at mint time and never persisted.
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

READ = "read"
WRITE = "write"
CONTROL = "control"
ALL_SCOPES = (READ, WRITE, CONTROL)

_REGISTRY_NAME = "tokens.json"
_LOCK = threading.Lock()


def registry_path() -> Path:
    return runtime_ownership.runtime_dir() / _REGISTRY_NAME


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


def mint(kind: str, scopes: list[str], *, session_id: Optional[str] = None) -> str:
    if not kind or not scopes or any(s not in ALL_SCOPES for s in scopes):
        raise ValueError(f"invalid token spec: kind={kind!r} scopes={scopes!r}")
    raw = secrets.token_hex(32)
    with _LOCK:
        registry = _load()
        registry[_hash(raw)] = {
            "kind": kind,
            "scopes": list(scopes),
            **({"session_id": session_id} if session_id else {}),
        }
        _save(registry)
    return raw


def revoke(raw: str) -> bool:
    with _LOCK:
        registry = _load()
        removed = registry.pop(_hash(raw), None) is not None
        if removed:
            _save(registry)
        return removed


class TokenResolver:
    """Per-server resolver; the transport token resolves to admin."""

    def __init__(self, admin_token: str) -> None:
        self._admin_token = admin_token

    def resolve(self, raw: object) -> Optional[dict[str, Any]]:
        if not isinstance(raw, str) or not raw:
            return None
        if secrets.compare_digest(raw, self._admin_token):
            return {"kind": "admin", "scopes": list(ALL_SCOPES)}
        return _load().get(_hash(raw))
