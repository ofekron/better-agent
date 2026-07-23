from __future__ import annotations

import sys

from better_agent_sdk.surfaces import proxy_fastmcp_tools, run_cli_from_fastmcp

_TOOLS = (
    "apply_provider_config_entry",
    "auto_sync_provider_config_entry",
    "create_provider_config_capability",
    "delete_provider_config_capability",
    "get_provider_config_state",
    "list_provider_config_capabilities",
    "list_provider_config_capability_picker",
    "list_provider_config_projects",
    "list_provider_config_worklist",
    "open_provider_config_sync_gui",
    "read_provider_config_entry",
    "remove_unified_capability_item",
    "restore_provider_config_entry",
    "transfer_provider_config_capability",
    "update_provider_config_auto_settings",
    "upsert_unified_capability_item",
    "write_provider_config_entry",
)
_OPERATIONS = {
    name: "provider_config_sync_tools_" + name
    for name in _TOOLS
}


def main() -> int:
    from provider_config_sync_backend.mcp_server import create_server

    server = create_server()
    if sys.argv[1:2] == ["cli"]:
        return run_cli_from_fastmcp(
            server,
            name="provider-config-sync",
            operations=_OPERATIONS,
        )
    if sys.argv[1:]:
        raise SystemExit("expected no arguments for MCP mode or 'cli' for CLI mode")
    proxy_fastmcp_tools(
        server,
        name="provider-config-sync",
        operations=_OPERATIONS,
    )
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
