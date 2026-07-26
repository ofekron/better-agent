from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ARTIFACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "sdk"
    / "better_agent_sdk"
    / "marketplace_protocol"
    / "v1"
    / "protocol.json"
)


def _artifact_bytes() -> bytes:
    return ARTIFACT_PATH.read_bytes()


PROTOCOL_HASH = hashlib.sha256(_artifact_bytes()).hexdigest()
PROTOCOL = json.loads(_artifact_bytes())

if PROTOCOL.get("name") != "better-agent-marketplace" or PROTOCOL.get("version") != 1:
    raise RuntimeError("unsupported Marketplace protocol artifact")

PATTERNS = {
    name: re.compile(pattern)
    for name, pattern in PROTOCOL["identifiers"].items()
}
ALLOWED_ACTIONS = frozenset(PROTOCOL["actions"])
FORBIDDEN_ACTION_FIELDS = frozenset(PROTOCOL["forbidden_action_fields"])
TERMINAL_ACTION_STATES = frozenset(
    state
    for state, transitions in PROTOCOL["action_transitions"].items()
    if not transitions
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_identifier(kind: str, value: object) -> str:
    if not isinstance(value, str) or not PATTERNS[kind].fullmatch(value):
        raise ValueError(f"invalid Marketplace {kind}")
    return value


def require_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not PATTERNS["sha256"].fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256")
    return value
