from __future__ import annotations

import copy
from typing import Any, Optional

import config_store
from provider_run_config import merge_provider_run_configs


def normalize_disabled_builtin_extensions(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return list(dict.fromkeys(str(item).strip() for item in value if str(item or "").strip()))


def normalize_extra_mcp_servers(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item or "").strip()))


def extra_mcp_servers_for_run(
    *,
    session_record: dict | None,
    worker_record: dict | None = None,
) -> list[str]:
    """Session-scoped opt-in of globally default-off extension MCP servers."""
    for record in (worker_record, session_record):
        if isinstance(record, dict) and record.get("extra_mcp_servers"):
            return normalize_extra_mcp_servers(record.get("extra_mcp_servers"))
    return []


def disabled_builtin_tools_for_run(
    *,
    session_record: dict | None,
    worker_record: dict | None = None,
) -> list[str]:
    """Union of the global disabled builtin tools and any session/worker
    record's own `disabled_builtin_tools`. Records can only ADD disables
    (e.g. a reviewer harness profile), never re-enable a globally disabled
    tool — safety guards fail closed."""
    disabled = set(config_store.get_disabled_builtin_tools())
    for record in (worker_record, session_record):
        if not isinstance(record, dict):
            continue
        raw = record.get("disabled_builtin_tools")
        if isinstance(raw, list):
            disabled.update(
                str(item).strip()
                for item in raw
                if str(item or "").strip() in config_store.DISABLEABLE_BUILTIN_TOOLS
            )
    return sorted(disabled)


def disabled_runtime_skills_for_run(
    *,
    session_record: dict | None,
    worker_record: dict | None = None,
) -> list[str]:
    """Per-session runtime-skill exclusion. Entries are skill names; the
    single entry "*" disables every runtime skill for the session."""
    disabled: set[str] = set()
    for record in (worker_record, session_record):
        if isinstance(record, dict) and isinstance(
            record.get("disabled_runtime_skills"), list
        ):
            disabled.update(
                str(item).strip()
                for item in record["disabled_runtime_skills"]
                if str(item or "").strip()
            )
    return sorted(disabled)


def disabled_builtin_extensions_for_run(
    explicit: Optional[list[str]],
    *,
    session_record: dict | None,
    worker_record: dict | None = None,
) -> list[str]:
    if explicit is not None:
        return normalize_disabled_builtin_extensions(explicit) or []

    for record in (worker_record, session_record):
        if (
            isinstance(record, dict)
            and "disabled_builtin_extensions" in record
            and record.get("disabled_builtin_extensions") is not None
        ):
            normalized = normalize_disabled_builtin_extensions(
                record.get("disabled_builtin_extensions")
            )
            return normalized or []

    return config_store.get_disabled_builtin_extensions()


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


def _effective_profile_record(
    session_record: dict | None,
    worker_record: dict | None,
) -> dict[str, Any]:
    if isinstance(worker_record, dict) and worker_record:
        return worker_record
    return session_record if isinstance(session_record, dict) else {}


def _provider_capability_contexts(value: object, provider_kind: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    if not any(isinstance(item, dict) and isinstance(item.get("outputs"), list) for item in value):
        return copy.deepcopy(value)
    from capability_contexts import provider_capability_contexts

    return provider_capability_contexts(value, provider_kind)


def resolve_extension_run_policy(
    *,
    resolved_harness_run_config: dict | None,
    session_record: dict | None,
    worker_record: dict | None = None,
    provider_kind: str,
    provider_run_config: dict | None,
    capability_contexts: list[dict] | None,
    disabled_builtin_extensions: Optional[list[str]],
) -> dict[str, Any]:
    profile_record = _effective_profile_record(session_record, worker_record)
    snapshot_supplied = (
        isinstance(resolved_harness_run_config, dict)
        and bool(resolved_harness_run_config.get("profile_id"))
    )
    snapshot = (
        copy.deepcopy(resolved_harness_run_config)
        if snapshot_supplied
        else None
    )
    if snapshot is None:
        import harness_profile_resolver

        snapshot = harness_profile_resolver.resolve_for_session(
            profile_record,
            turn_capability_contexts=capability_contexts,
        ) or {}

    explicit_mcp_servers = extra_mcp_servers_for_run(
        session_record=session_record,
        worker_record=worker_record,
    )
    effective_mcp_servers = _merge_names(
        snapshot.get("extra_mcp_servers"),
        explicit_mcp_servers,
    )
    effective_disabled_extensions = _merge_names(
        snapshot.get("disabled_builtin_extensions"),
        disabled_builtin_extensions_for_run(
            disabled_builtin_extensions,
            session_record=session_record,
            worker_record=worker_record,
        ),
    )
    effective_disabled_tools = _merge_names(
        snapshot.get("disabled_builtin_tools"),
        disabled_builtin_tools_for_run(
            session_record=session_record,
            worker_record=worker_record,
        ),
    )
    effective_disabled_runtime_skills = _merge_names(
        snapshot.get("disabled_runtime_skills"),
        disabled_runtime_skills_for_run(
            session_record=session_record,
            worker_record=worker_record,
        ),
    )
    effective_provider_run_config = merge_provider_run_configs(
        snapshot.get("provider_run_config"),
        provider_run_config,
    )
    context_source = (
        capability_contexts
        if snapshot_supplied and isinstance(capability_contexts, list) and capability_contexts
        else snapshot.get("capability_contexts")
    )
    effective_contexts = _provider_capability_contexts(context_source, provider_kind)
    effective_capability_ids = _merge_names(
        snapshot.get("active_capability_ids"),
        profile_record.get("active_capability_ids"),
    )

    snapshot["bare_config"] = bool(snapshot.get("bare_config") or profile_record.get("bare_config"))
    snapshot["capability_contexts"] = copy.deepcopy(effective_contexts)
    snapshot["provider_run_config"] = copy.deepcopy(effective_provider_run_config)
    snapshot["extra_mcp_servers"] = list(effective_mcp_servers)
    snapshot["active_capability_ids"] = list(effective_capability_ids)
    snapshot["disabled_builtin_extensions"] = list(effective_disabled_extensions)
    snapshot["disabled_builtin_tools"] = list(effective_disabled_tools)
    snapshot["disabled_runtime_skills"] = list(effective_disabled_runtime_skills)
    launcher_projection = copy.deepcopy(snapshot.get("launcher_projection") or {})
    launcher_projection["disabled_builtin_extensions"] = list(effective_disabled_extensions)
    launcher_projection["disabled_builtin_tools"] = list(effective_disabled_tools)
    launcher_projection["disabled_runtime_skills"] = list(effective_disabled_runtime_skills)
    launcher_projection["active_capability_ids"] = list(effective_capability_ids)
    snapshot["launcher_projection"] = launcher_projection

    return {
        "bare_config": snapshot["bare_config"],
        "capability_contexts": effective_contexts,
        "provider_run_config": effective_provider_run_config,
        "extra_mcp_servers": effective_mcp_servers,
        "active_capability_ids": effective_capability_ids,
        "disabled_builtin_extensions": effective_disabled_extensions,
        "disabled_builtin_tools": effective_disabled_tools,
        "disabled_runtime_skills": effective_disabled_runtime_skills,
        "resolved_harness_run_config": snapshot,
    }
