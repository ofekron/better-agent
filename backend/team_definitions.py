from __future__ import annotations

from typing import Any

import extension_store


class TeamDefinitionError(ValueError):
    pass


def source_by_id(source_id: str) -> dict[str, Any]:
    sid = str(source_id or "").strip()
    for source in extension_store.team_definition_sources():
        if source["source_id"] == sid or source["name"] == sid:
            return source
    raise TeamDefinitionError("team definition source not found")


def build_plan(
    *,
    source_id: str,
    profile: str,
    team_instance_id: str,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = source_by_id(source_id)
    definition = source["definition"]
    validate_definition(definition)
    profile_name = str(profile or "").strip()
    profiles = definition.get("profiles") or {}
    selected_profile = profiles.get(profile_name)
    if not isinstance(selected_profile, dict):
        raise TeamDefinitionError("profile not found")
    vars_map = {str(k): str(v) for k, v in (variables or {}).items()}
    manager = _resolve_value(definition["manager"], vars_map)
    workers = _workers_by_id(definition)
    active_ids = _profile_ids(selected_profile.get("activate"), workers)
    finalize_ids = _profile_ids(selected_profile.get("finalize_with"), workers)
    active_specs = [
        _worker_provision_spec(workers[worker_id], team_instance_id, vars_map)
        for worker_id in active_ids
    ]
    finalize_specs = [
        _worker_provision_spec(workers[worker_id], team_instance_id, vars_map)
        for worker_id in finalize_ids
    ]
    return {
        "source_id": source["source_id"],
        "extension_id": source["extension_id"],
        "name": definition["name"],
        "profile": profile_name,
        "team_instance_id": team_instance_id,
        "manager": manager,
        "activate": active_specs,
        "finalize_with": finalize_specs,
    }


def validate_definition(definition: Any) -> None:
    if not isinstance(definition, dict):
        raise TeamDefinitionError("team definition must be an object")
    if definition.get("schema_version") != 1:
        raise TeamDefinitionError("team definition schema_version must be 1")
    name = str(definition.get("name") or "").strip()
    if not name:
        raise TeamDefinitionError("team definition name is required")
    manager = definition.get("manager")
    if not isinstance(manager, dict):
        raise TeamDefinitionError("team definition manager is required")
    if str(manager.get("orchestration_mode") or "native") != "native":
        raise TeamDefinitionError("team manager must be native")
    catalog = definition.get("catalog") or {}
    if not isinstance(catalog, dict):
        raise TeamDefinitionError("team catalog must be an object")
    workers = catalog.get("workers") or []
    if not isinstance(workers, list):
        raise TeamDefinitionError("catalog.workers must be a list")
    seen: set[str] = set()
    for worker in workers:
        if not isinstance(worker, dict):
            raise TeamDefinitionError("catalog workers must be objects")
        worker_id = str(worker.get("id") or "").strip()
        if not worker_id:
            raise TeamDefinitionError("worker id is required")
        if worker_id in seen:
            raise TeamDefinitionError(f"duplicate worker id: {worker_id}")
        seen.add(worker_id)
        if str(worker.get("type") or "worker") != "worker":
            raise TeamDefinitionError("catalog workers must have type worker")
        if not str(worker.get("role_key") or "").strip():
            raise TeamDefinitionError(f"worker role_key is required: {worker_id}")
    profiles = definition.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise TeamDefinitionError("profiles must be an object")
    worker_ids = set(seen)
    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise TeamDefinitionError(f"profile must be an object: {profile_name}")
        _profile_ids(profile.get("activate"), worker_ids)
        _profile_ids(profile.get("finalize_with"), worker_ids)


def _workers_by_id(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(worker["id"]): worker
        for worker in definition.get("catalog", {}).get("workers", [])
        if isinstance(worker, dict)
    }


def _profile_ids(raw: Any, workers: dict[str, Any] | set[str]) -> list[str]:
    worker_ids = set(workers)
    if raw in (None, ""):
        return []
    if raw == "*":
        return sorted(worker_ids)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise TeamDefinitionError("profile worker lists must be string lists or '*'")
    unknown = [item for item in raw if item not in worker_ids]
    if unknown:
        raise TeamDefinitionError(f"profile references unknown worker: {', '.join(unknown)}")
    return list(raw)


def _worker_provision_spec(worker: dict[str, Any], team_instance_id: str, variables: dict[str, str]) -> dict[str, Any]:
    resolved = _resolve_value(worker, variables)
    worker_id = str(resolved["id"])
    return {
        "member_id": worker_id,
        "team_instance_id": team_instance_id,
        "role_key": str(resolved.get("role_key") or worker_id),
        "role": str(resolved.get("role_key") or worker_id),
        "description": str(resolved.get("description") or worker_id),
        "orchestration_mode": str(resolved.get("orchestration_mode") or "native"),
        "provider_id": str(resolved.get("provider_id") or ""),
        "model": str(resolved.get("model") or ""),
        "reasoning_effort": str(resolved.get("reasoning_effort") or ""),
        "node_id": str(resolved.get("node_id") or ""),
        "run_mode": str(resolved.get("run_mode") or ""),
        "cwd": str(resolved.get("cwd") or ""),
        "prompt_ref": str(resolved.get("prompt_ref") or ""),
        "provision_prompt": str(resolved.get("provision_prompt") or ""),
        "bare_config": resolved.get("bare_config") is True,
        "capability_contexts": resolved.get("capability_contexts") or [],
    }


def _resolve_value(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        current = value
        for key, replacement in variables.items():
            current = current.replace(f"${key}", replacement)
        return current
    if isinstance(value, list):
        return [_resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, variables) for key, item in value.items()}
    return value
