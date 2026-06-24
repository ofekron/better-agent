"""Node-side persistent identity (id + secret) for dynamic registration.

A brand-new worker-node no longer needs to be pre-declared in
topology.yaml nor share a `BETTER_CLAUDE_NODE_TOKEN`. Instead it
generates a stable identity ONCE and presents it on every dial:

  - `node_id`  — defaults to the machine hostname (sanitized); the
                 human approving the node in the primary UI sees this.
  - `secret`   — a random token the node keeps to itself and presents
                 as the bearer credential. Primary stores `argon2(secret)`
                 the first time it approves the node, then verifies it
                 on every reconnect (trust-on-first-approve).
  - `cwd_roots`— absolute paths this node is willing to host work under.
  - `address`  — self-reported address (metadata; primary never dials
                 nodes, but it's shown in the approval popup).

Persisted at `ba_home()/node_identity.json`. Env vars override the
persisted/derived values (and are written back) so an operator can pin
any field:

  BETTER_CLAUDE_NODE_ID          — explicit node id
  BETTER_CLAUDE_NODE_TOKEN       — explicit secret (legacy var reused)
  BETTER_CLAUDE_NODE_CWD_ROOTS   — ':'-separated absolute paths
  BETTER_CLAUDE_NODE_ADDRESS     — explicit self-reported address
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from env_compat import get_env
from paths import ba_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_ID_SANITIZE = re.compile(r"[^A-Za-z0-9_\-.]+")


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    secret: str
    cwd_roots: tuple[str, ...]
    address: str


def _path() -> Path:
    return ba_home() / "node_identity.json"


def _default_node_id() -> str:
    raw = socket.gethostname() or "worker-node"
    # Drop a trailing ".local"/".lan" so the id reads cleanly.
    raw = re.sub(r"\.(local|lan)$", "", raw)
    sanitized = _ID_SANITIZE.sub("-", raw).strip("-.") or "worker-node"
    return sanitized.lower()[:64]


def _default_address() -> str:
    # Best-effort routable host; falls back to hostname. Purely metadata
    # for the approval popup — primary never dials nodes.
    host = socket.gethostname() or "worker-node"
    return host


def _env_cwd_roots() -> list[str]:
    raw = get_env("BETTER_CLAUDE_NODE_CWD_ROOTS").strip()
    if not raw:
        return []
    return [p for p in (s.strip() for s in raw.split(":")) if p.startswith("/")]


def _load_file() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {}
    return data


def load_or_create() -> NodeIdentity:
    """Resolve this node's identity, persisting any freshly generated or
    env-overridden fields back to disk. Idempotent across restarts."""
    data = _load_file()

    node_id = (
        get_env("BETTER_CLAUDE_NODE_ID")
        or data.get("node_id")
        or _default_node_id()
    )
    secret = (
        get_env("BETTER_CLAUDE_NODE_TOKEN")
        or data.get("secret")
        or secrets.token_hex(32)
    )
    cwd_roots = _env_cwd_roots() or list(data.get("cwd_roots") or [])
    address = (
        get_env("BETTER_CLAUDE_NODE_ADDRESS")
        or data.get("address")
        or _default_address()
    )

    out = {
        "schema_version": SCHEMA_VERSION,
        "node_id": node_id,
        "secret": secret,
        "cwd_roots": cwd_roots,
        "address": address,
    }
    if out != {**data, **out} or not _path().exists():
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(out, indent=2), encoding="utf-8")
            # Secret lives here in plaintext — lock the file down.
            os.chmod(_path(), 0o600)
        except OSError:
            logger.exception("node_identity: failed to persist %s", _path())

    return NodeIdentity(
        node_id=node_id,
        secret=secret,
        cwd_roots=tuple(cwd_roots),
        address=address,
    )
