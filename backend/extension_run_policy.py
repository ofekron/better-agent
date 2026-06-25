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
