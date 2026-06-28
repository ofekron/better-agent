"""Disk-backed registry of APPROVED worker-nodes (primary-side).

EVERY node — whether pre-declared in `topology.yaml` or discovered
dynamically — authenticates the same way: it generates a random secret
once (or pins one via `BETTER_CLAUDE_NODE_TOKEN` on the node), presents
it on every dial, and the FIRST time an authenticated human approves it
we persist `argon2(secret)` here. On every later reconnect we verify
the presented secret against that hash. There is NO shared token.

`topology.yaml` is the MANIFEST (allowlist of permitted ids + each
declared node's `cwd_roots` policy). It is deliberately separate from
this store (it is repo-checked-in and must not carry per-deployment
secrets) and from `node_store.py` (which holds only transient live-WS
state, no authority). The per-node secret hashes live HERE, never in
topology.yaml.

Trust model (trust-on-first-approve, à la SSH known_hosts): a third
party can't impersonate a node_id without its secret, and no shared
secret is copied between machines — so one compromised node can't
impersonate another.

Storage: one JSON file per node at
~/.better-claude/node_registry/<node_id>.json:

    {
        "schema_version": 1,
        "node_id": str,
        "address": str,
        "cwd_roots": [str, ...],
        "secret_hash": str,        # argon2 hash of the node's secret
        "approved_at": iso,
    }

Schema migrations are NOT supported (per CLAUDE.md): a mismatched
`schema_version` is treated as absent — wipe `node_registry/` to reset.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from json_store import write_json
from paths import ba_home
from topology import NodeSpec

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_ID_RE = re.compile(r"[A-Za-z0-9_\-.]{1,64}")
_ph = PasswordHasher()


def _dir() -> Path:
    return ba_home() / "node_registry"


def _path(node_id: str) -> Path:
    if not _ID_RE.fullmatch(node_id or ""):
        raise ValueError(f"invalid node_id: {node_id!r}")
    return _dir() / f"{node_id}.json"


def hash_secret(secret: str) -> str:
    """argon2 hash of a node secret. Exposed so the registration flow
    can hash once (at request time) and reuse the hash on approve."""
    return _ph.hash(secret)


def add(
    *,
    node_id: str,
    address: str,
    cwd_roots: list[str],
    secret_hash: str,
) -> dict:
    """Persist an approved node. Overwrites any prior record for the
    same id (re-approval rotates the secret hash). Returns the record.

    Written atomically (tmp + os.replace) via the canonical store writer:
    these records hold argon2 secret hashes, so a crash mid-write must not
    leave a truncated/half-written authority file."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "node_id": node_id,
        "address": address,
        "cwd_roots": list(cwd_roots or []),
        "secret_hash": secret_hash,
        "approved_at": datetime.now().isoformat(),
    }
    write_json(_path(node_id), record)
    return record


def get(node_id: str) -> Optional[dict]:
    """Load one approved-node record, or None if absent / malformed /
    wrong schema version."""
    try:
        path = _path(node_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict) or rec.get("schema_version") != SCHEMA_VERSION:
        logger.warning(
            "node_registry: %s has unexpected schema; ignoring (wipe %s to reset)",
            path, _dir(),
        )
        return None
    return rec


def list_all() -> list[dict]:
    if not _dir().exists():
        return []
    out: list[dict] = []
    for path in _dir().glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("schema_version") == SCHEMA_VERSION:
            out.append(rec)
    out.sort(key=lambda r: r.get("approved_at", ""))
    return out


def remove(node_id: str) -> bool:
    """Revoke an approved node. Returns True if a record was removed."""
    try:
        path = _path(node_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def verify_secret(node_id: str, secret: str) -> bool:
    """Constant-time-ish (argon2 verify) check of a presented secret
    against the stored hash. False if node unknown or secret wrong."""
    rec = get(node_id)
    if not rec:
        return False
    stored = rec.get("secret_hash") or ""
    try:
        return bool(_ph.verify(stored, secret))
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def to_spec(node_id: str) -> Optional[NodeSpec]:
    """Project an approved-node record into a NodeSpec so the rest of
    the system (node_store.register, snapshots) treats dynamic nodes
    exactly like topology-declared ones."""
    rec = get(node_id)
    if not rec:
        return None
    return NodeSpec(
        id=node_id,
        role="worker_node",
        address=rec.get("address") or "",
        cwd_roots=tuple(rec.get("cwd_roots") or ()),
    )
