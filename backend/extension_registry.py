from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import extension_store


@dataclass(frozen=True)
class BuiltinMcpExtension:
    extension_id: str
    name: str
    mcp_server: str
    interacts_with_user: bool
    bare_allowed: bool
    requires_backend_auth: bool
    predicate: Callable[[dict], bool]


BUILTIN_MCP_EXTENSIONS: tuple[BuiltinMcpExtension, ...] = ()


def _disabled_extension_ids(inputs: dict) -> set[str]:
    raw = inputs.get("disabled_builtin_extensions")
    if not isinstance(raw, list):
        return set()
    known = {item.extension_id for item in BUILTIN_MCP_EXTENSIONS}
    return {
        extension_id
        for extension_id in (str(item or "").strip() for item in raw)
        if extension_id in known
    }


def active_builtin_mcp_extensions(inputs: dict, *, interacts_with_user: bool, bare: bool) -> list[BuiltinMcpExtension]:
    disabled = _disabled_extension_ids(inputs)
    active: list[BuiltinMcpExtension] = []
    for extension in BUILTIN_MCP_EXTENSIONS:
        if not extension_store.is_extension_runtime_ready(extension.extension_id):
            continue
        if extension.extension_id in disabled:
            continue
        if bare and not extension.bare_allowed:
            continue
        if extension.interacts_with_user and not interacts_with_user:
            continue
        if not extension.interacts_with_user and bare and not extension.bare_allowed:
            continue
        if extension.predicate(inputs):
            active.append(extension)
    return active


# Built-in MCP servers added directly in builtin_mcp_config (not via the
# active_builtin_mcp_extensions loop), listed here so Settings can show and
# toggle them per extension.
_SUPPLEMENTAL_BUILTIN_MCP_SERVERS: dict[str, tuple[tuple[str, str], ...]] = {}


def builtin_mcp_servers_by_extension() -> dict[str, list[tuple[str, str]]]:
    """extension_id → [(server_name, label)] for every built-in MCP server."""
    result: dict[str, list[tuple[str, str]]] = {}
    for ext in BUILTIN_MCP_EXTENSIONS:
        result.setdefault(ext.extension_id, []).append((ext.mcp_server, ext.name))
    for ext_id, servers in _SUPPLEMENTAL_BUILTIN_MCP_SERVERS.items():
        result.setdefault(ext_id, []).extend(servers)
    return result
