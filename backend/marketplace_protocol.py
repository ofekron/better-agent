from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
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
    # Static fail-fast against a corrupted shipped artifact; unreachable at
    # runtime through the public API. Excluded, not faked with a reload.
    raise RuntimeError("unsupported Marketplace protocol artifact")  # pragma: no cover

PATTERNS = {
    name: re.compile(pattern)
    for name, pattern in PROTOCOL["identifiers"].items()
}
ALLOWED_ACTIONS = frozenset(PROTOCOL["actions"])
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


def _bounded_text(
    value: object,
    field: str,
    *,
    required: bool = True,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    limit = maximum or PROTOCOL["bounds"]["display_text_max"]
    if len(text) > limit or any(ord(character) < 32 for character in text):
        raise ValueError(f"{field} is invalid")
    return text


def _future_timestamp(value: object, field: str) -> str:
    text = _bounded_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
        raise ValueError(f"{field} is expired")
    return text


def validate_leased_action(value: object) -> dict[str, Any]:
    schema = PROTOCOL["leased_action"]
    if not isinstance(value, dict) or set(value) != set(schema["required"]):
        raise ValueError("leased action has an invalid shape")
    extension_value = value["extension"]
    extension_fields = {
        *schema["extension_required"],
        *schema["extension_optional"],
    }
    if (
        not isinstance(extension_value, dict)
        or not set(schema["extension_required"]) <= set(extension_value)
        or not set(extension_value) <= extension_fields
    ):
        raise ValueError("leased action extension has an invalid shape")

    action_id = require_identifier("action", value["action_id"])
    action_type = value["action_type"]
    if not isinstance(action_type, str) or action_type not in ALLOWED_ACTIONS:
        raise ValueError("leased action type is invalid")
    lease_capability = _bounded_text(value["lease_capability"], "lease_capability")
    lease_expires_at = _future_timestamp(
        value["lease_expires_at"],
        "lease_expires_at",
    )
    target_version = _bounded_text(
        value["target_version"],
        "target_version",
        required=False,
        maximum=128,
    )
    catalog_snapshot_sha256 = require_hash(
        value["catalog_snapshot_sha256"],
        "catalog_snapshot_sha256",
    )
    extension_id = require_identifier("extension", extension_value["id"])
    extension_name = _bounded_text(extension_value["name"], "extension.name")
    extension_version = (
        _bounded_text(
            extension_value["version"],
            "extension.version",
            maximum=128,
        )
        if "version" in extension_value
        else ""
    )
    publisher = (
        _bounded_text(extension_value["publisher"], "extension.publisher")
        if "publisher" in extension_value
        else ""
    )
    permission_delta_value = extension_value.get("permission_delta", [])
    if (
        not isinstance(permission_delta_value, list)
        or ("permission_delta" in extension_value and not permission_delta_value)
        or len(permission_delta_value) > PROTOCOL["bounds"]["permission_count_max"]
    ):
        raise ValueError("extension.permission_delta is invalid")
    permission_delta = [
        _bounded_text(permission, "extension.permission")
        for permission in permission_delta_value
    ]
    if action_type in {"install", "update"}:
        if not target_version or extension_version != target_version:
            raise ValueError("leased action target version is invalid")
    elif target_version:
        raise ValueError("leased action has an unexpected target version")

    return {
        "action_id": action_id,
        "action_type": action_type,
        "lease_capability": lease_capability,
        "lease_expires_at": lease_expires_at,
        "target_version": target_version,
        "catalog_snapshot_sha256": catalog_snapshot_sha256,
        "extension": {
            "id": extension_id,
            "name": extension_name,
            "version": extension_version,
            "publisher": publisher,
            "permission_delta": permission_delta,
        },
    }
