from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pcs_paths

pcs_paths.ensure_on_path()
from provider_config_sync_backend import api as _pcs  # noqa: E402


_MCP_CAPABILITY_ID = "mcp"
_MARKER_EXTENSION_ID = "BETTER_CLAUDE_EXTENSION_ID"
_MARKER_SERVER_NAME = "BETTER_CLAUDE_EXTENSION_MCP_SERVER"


def reconcile_native_mcp_servers(records: list[dict[str, Any]]) -> int:
    _configure_pcs()
    active: dict[str, tuple[str, dict[str, Any]]] = {}
    for record in records:
        manifest = record.get("manifest") or {}
        extension_id = str(manifest.get("id") or "").strip()
        if not extension_id:
            continue
        for item in (manifest.get("entrypoints") or {}).get("mcp") or []:
            if not isinstance(item, dict):
                continue
            server_name = str(item.get("replaces_builtin") or item.get("name") or "").strip()
            item_name = str(item.get("name") or "").strip()
            if not server_name or not item_name:
                continue
            active[server_name] = (extension_id, _launcher_server_item(extension_id, item_name))

    try:
        capability, current, exists = _pcs._current_unified_for_tool("", _MCP_CAPABILITY_ID, "global")
    except Exception:
        return 0
    content = _pcs._mcp_tool_content(current, exists)
    servers = dict(content.get("mcpServers") or {})

    changed = 0
    for name, raw in list(servers.items()):
        marker = _marker(raw)
        if not marker:
            continue
        if active.get(name, ("",))[0] == marker:
            continue
        servers.pop(name, None)
        changed += 1

    for name, (extension_id, item) in active.items():
        marker = _marker(servers.get(name))
        if marker == extension_id:
            continue
        if name in servers and marker != extension_id:
            continue
        servers[name] = item
        changed += 1

    if changed == 0:
        _sync_specific_entries(capability, active)
        return 0

    next_content = json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n"
    unified = capability["unified"]
    _pcs._write_entry_if_unchanged(unified, _pcs._expected_content(current, exists), next_content)
    _sync_specific_entries(capability, active)
    return changed


def _configure_pcs() -> None:
    import config_store
    import project_store
    from paths import ba_home, encode_cwd

    _pcs.configure(
        provider_records=lambda: config_store.list_provider_metadata(),
        project_records=lambda: project_store.list_projects(),
        sync_home=ba_home,
        encode_project_cwd=encode_cwd,
    )


def _sync_specific_entries(
    capability: dict[str, Any],
    active: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    for entry in capability.get("specifics") or []:
        if not entry.get("writable"):
            continue
        current, exists = _pcs._read_entry_current(entry)
        content = _pcs._mcp_tool_content(current, exists)
        servers = dict(content.get("mcpServers") or {})
        changed = False
        for name, raw in list(servers.items()):
            marker = _marker(raw)
            if not marker:
                continue
            if active.get(name, ("",))[0] == marker:
                continue
            servers.pop(name, None)
            changed = True
        for name, (extension_id, item) in active.items():
            marker = _marker(servers.get(name))
            if marker == extension_id:
                continue
            if name in servers and marker != extension_id:
                continue
            servers[name] = item
            changed = True
        if not changed:
            continue
        next_content = json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n"
        _pcs._write_entry_if_unchanged(entry, _pcs._expected_content(current, exists), next_content)


def _marker(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    env = raw.get("env")
    if not isinstance(env, dict):
        return ""
    extension_id = str(env.get(_MARKER_EXTENSION_ID) or "").strip()
    server_name = str(env.get(_MARKER_SERVER_NAME) or "").strip()
    return extension_id if extension_id and server_name else ""


def _launcher_server_item(extension_id: str, server_name: str) -> dict[str, Any]:
    command, args = _launcher_command(extension_id, server_name)
    return launcher_server_item(extension_id, server_name, command=command, args=args)


def launcher_server_item(
    extension_id: str,
    server_name: str,
    *,
    command: str | None = None,
    args: list[str] | None = None,
) -> dict[str, Any]:
    if command is None or args is None:
        command, args = _launcher_command(extension_id, server_name)
    return {
        "command": command,
        "args": args,
        "env": {
            _MARKER_EXTENSION_ID: extension_id,
            _MARKER_SERVER_NAME: server_name,
        },
    }


def _launcher_command(extension_id: str, server_name: str) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, ["--extension-mcp", extension_id, server_name]
    script = Path(__file__).with_name("extension_mcp_launcher.py")
    return sys.executable, [str(script), extension_id, server_name]
