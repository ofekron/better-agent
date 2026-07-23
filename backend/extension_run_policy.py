from __future__ import annotations

from typing import Optional

import config_store


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
    (e.g. a reviewer-preset session), never re-enable a globally disabled
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
