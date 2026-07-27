from __future__ import annotations

import copy
from typing import Any

from provider_run_config import merge_provider_run_configs


def _merge_names(*values: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(name)
    return merged


def apply_to_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    snapshot = inputs.get("resolved_harness_run_config")
    if not is_active(snapshot):
        return inputs
    out = dict(inputs)
    out["bare_config"] = bool(snapshot.get("bare_config"))
    out["capability_contexts"] = list(snapshot.get("capability_contexts") or [])
    out["provider_run_config"] = merge_provider_run_configs(
        snapshot.get("provider_run_config"),
        inputs.get("provider_run_config"),
    )
    out["extra_mcp_servers"] = _merge_names(
        snapshot.get("extra_mcp_servers"),
        inputs.get("extra_mcp_servers"),
    )
    out["active_capability_ids"] = _merge_names(
        snapshot.get("active_capability_ids"),
        inputs.get("active_capability_ids"),
    )
    out["disabled_builtin_extensions"] = _merge_names(
        snapshot.get("disabled_builtin_extensions"),
        inputs.get("disabled_builtin_extensions"),
    )
    out["disabled_builtin_tools"] = _merge_names(
        snapshot.get("disabled_builtin_tools"),
        inputs.get("disabled_builtin_tools"),
    )
    out["disabled_runtime_skills"] = _merge_names(
        snapshot.get("disabled_runtime_skills"),
        inputs.get("disabled_runtime_skills"),
    )
    return out


def is_active(snapshot: Any) -> bool:
    return isinstance(snapshot, dict) and bool(snapshot.get("profile_id"))


def launcher_projection(inputs: dict[str, Any]) -> dict[str, Any]:
    snapshot = inputs.get("resolved_harness_run_config")
    if not isinstance(snapshot, dict):
        return {}
    projection = snapshot.get("launcher_projection")
    return copy.deepcopy(projection) if isinstance(projection, dict) else {}


def selected_mcp_servers(inputs: dict[str, Any], extension_id: str) -> set[str]:
    projection = launcher_projection(inputs)
    raw = projection.get("extension_mcp_servers") if projection else None
    if not isinstance(raw, dict):
        return set()
    names = raw.get(extension_id)
    if not isinstance(names, list):
        return set()
    return {str(item).strip() for item in names if str(item or "").strip()}


def selected_extension_ids(inputs: dict[str, Any]) -> set[str]:
    projection = launcher_projection(inputs)
    raw = projection.get("extension_revisions") if projection else None
    if not isinstance(raw, dict):
        return set()
    return {str(item).strip() for item in raw if str(item or "").strip()}
