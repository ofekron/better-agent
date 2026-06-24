from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from better_agent_sdk import Client

_TIMEOUT = 60.0


def marketplace_action(action: str, **payload: Any) -> dict[str, Any]:
    try:
        return Client().call_internal(
            "/api/internal/marketplace",
            {"action": action, **payload},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def build_server() -> FastMCP:
    server = FastMCP("better-agent-marketplace")

    @server.tool()
    def search_extensions(query: str = "", limit: int = 20) -> dict[str, Any]:
        """Search the Better Agent marketplace extension catalog."""
        return marketplace_action("search", query=query, limit=limit)

    @server.tool()
    def list_installed_extensions() -> dict[str, Any]:
        """List currently installed Better Agent extensions."""
        return marketplace_action("list_installed")

    @server.tool()
    def get_installed_extension(extension_id: str) -> dict[str, Any]:
        """Inspect one installed Better Agent extension."""
        return marketplace_action("get_installed", extension_id=extension_id)

    @server.tool()
    def install_extension(extension_id: str, entitlement_token: str = "") -> dict[str, Any]:
        """Install an extension from the marketplace catalog by extension id.

        Installing IS consenting: it grants the extension the permissions it
        declares in its manifest (`permissions`). Review them before installing.
        The returned record echoes `manifest.permissions` and the recorded
        `consent`. Permissions like `filesystem`, `network`, `secrets`, and
        `internal_loopback` let the extension run code with your privileges —
        only install extensions you trust.
        """
        return marketplace_action(
            "install",
            extension_id=extension_id,
            entitlement_token=entitlement_token,
        )

    @server.tool()
    def set_extension_enabled(extension_id: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable an installed extension."""
        return marketplace_action("set_enabled", extension_id=extension_id, enabled=enabled)

    @server.tool()
    def uninstall_extension(extension_id: str) -> dict[str, Any]:
        """Uninstall an installed extension."""
        return marketplace_action("uninstall", extension_id=extension_id)

    @server.tool()
    def update_installed_extensions() -> dict[str, Any]:
        """Update installed extensions that have refreshable sources."""
        return marketplace_action("update")

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
