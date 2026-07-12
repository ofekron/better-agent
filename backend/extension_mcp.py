from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from provider_config_sync_backend import api as _pcs


_MARKER_EXTENSION_ID = "BETTER_CLAUDE_EXTENSION_ID"
_MARKER_SERVER_NAME = "BETTER_CLAUDE_EXTENSION_MCP_SERVER"
_MARKER_AMBIENT_CAPABILITY_ID = "BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID"


def reconcile_native_mcp_servers(records: list[dict[str, Any]]) -> int:
    _configure_pcs()
    import ambient_mcp_sources

    del records
    desired: dict[str, dict[str, Any]] = {}
    for capability in ambient_mcp_sources.capabilities():
        if not capability.available or capability.launcher is None:
            continue
        if capability.name in desired:
            raise ValueError(f"duplicate ambient MCP server name: {capability.name}")
        launcher = dict(capability.launcher)
        launcher["env"] = {
            **dict(launcher.get("env") or {}),
            _MARKER_AMBIENT_CAPABILITY_ID: capability.id,
        }
        desired[capability.name] = launcher
    result = _pcs.reconcile_global_mcp_servers(desired, owns_server=_is_owned_server)
    return len(result["changed"])


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


def _is_owned_server(_name: str, raw: Any) -> bool:
    if not isinstance(raw, dict) or not isinstance(raw.get("env"), dict):
        return False
    return bool(str(raw["env"].get(_MARKER_AMBIENT_CAPABILITY_ID) or "").strip())


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
