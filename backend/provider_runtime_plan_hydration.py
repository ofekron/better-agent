from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError

# Placeholder for the per-run operation-broker address, unknowable before the
# runner starts its host; `hydrate_runner_operation_broker` swaps it for the
# real address runner-side. Any env value equal to this dict that survives to
# an actual spawn is a wiring bug and fails loudly (env values must be str).
RUNNER_OPERATION_BROKER_REF = {"kind": "runner_operation_broker"}


def _canonical_json_text(value: Any) -> str:
    """Deterministic JSON encoding shared by key derivation and value capture.

    `capture_runtime_hydration` stores a value under the key `hydration_key`
    computes from its reference, so both MUST serialize identically — any
    divergence breaks hydration lookups at apply time."""
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def hydration_key(reference: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json_text(reference).encode("utf-8"),
    ).hexdigest()


def capture_runtime_hydration(
    hydration: dict[str, str] | None,
    reference: Mapping[str, Any],
    value: Any,
) -> None:
    if hydration is None:
        return
    try:
        encoded = _canonical_json_text(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError(
            "runtime hydration value must be JSON-compatible",
        ) from exc
    key = hydration_key(reference)
    existing = hydration.get(key)
    if existing is not None and existing != encoded:
        raise ExecutionContractError("runtime hydration reference is ambiguous")
    hydration[key] = encoded


def _is_hydration_reference(value: Any) -> bool:
    if type(value) is not dict:
        return False
    if set(value) == {"kind", "extension_id"}:
        return (
            value["kind"] == "extension_identity"
            and type(value["extension_id"]) is str
            and bool(value["extension_id"])
        )
    if set(value) == {"kind", "path_sha256"}:
        return (
            value["kind"] == "runtime_value"
            and type(value["path_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["path_sha256"]) is not None
        )
    return (
        set(value) == {"kind", "extension_id", "key_sha256"}
        and value["kind"] == "extension_setting"
        and type(value["extension_id"]) is str
        and bool(value["extension_id"])
        and type(value["key_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", value["key_sha256"]) is not None
    )


def apply_runtime_hydration(
    value: Any,
    hydration: Mapping[str, str],
) -> Any:
    if _is_hydration_reference(value):
        encoded = hydration.get(hydration_key(value))
        if encoded is None:
            raise ExecutionContractError("runtime hydration value is unavailable")
        try:
            return json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ExecutionContractError(
                "runtime hydration value is invalid",
            ) from exc
    if type(value) is list:
        return [
            apply_runtime_hydration(item, hydration)
            for item in value
        ]
    if type(value) is not dict:
        return value
    hydrated: dict[str, Any] = {}
    for key, item in value.items():
        target = (
            key[:-4]
            if key.endswith("_ref") and _is_hydration_reference(item)
            else key
        )
        if target in hydrated:
            raise ExecutionContractError("runtime hydration key is ambiguous")
        hydrated[target] = apply_runtime_hydration(item, hydration)
    return hydrated


__all__ = [
    "RUNNER_OPERATION_BROKER_REF",
    "apply_runtime_hydration",
    "capture_runtime_hydration",
    "hydration_key",
]
