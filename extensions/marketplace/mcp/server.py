from __future__ import annotations

from typing import Any

from better_agent_sdk import Client
from better_agent_sdk.surfaces import OperationSpec, build_mcp_server, run_mcp_or_cli

_TIMEOUT = 60.0
_ACTIONS = {
    "search": "search",
    "list_installed": "installed.list",
    "get_installed": "installed.get",
    "install": "install",
    "set_enabled": "enabled.set",
    "uninstall": "uninstall",
    "update": "update",
}


class MarketplaceClient:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client or Client()

    def invoke(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        capability_action = _ACTIONS.get(action)
        if capability_action is None:
            raise ValueError("unknown marketplace action")
        return self._client.invoke_capability(
            "marketplace", capability_action, payload, timeout=_TIMEOUT,
        )


def marketplace_action(action: str, **payload: Any) -> dict[str, Any]:
    try:
        return MarketplaceClient().invoke(action, payload)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def search_extensions(query: str = "", limit: int = 20) -> dict[str, Any]:
    """Search the Better Agent marketplace extension catalog."""
    return marketplace_action("search", query=query, limit=limit)


def list_installed_extensions() -> dict[str, Any]:
    """List currently installed Better Agent extensions."""
    return marketplace_action("list_installed")


def get_installed_extension(extension_id: str) -> dict[str, Any]:
    """Inspect one installed Better Agent extension."""
    return marketplace_action("get_installed", extension_id=extension_id)


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


def set_extension_enabled(extension_id: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable an installed extension."""
    return marketplace_action("set_enabled", extension_id=extension_id, enabled=enabled)


def uninstall_extension(extension_id: str) -> dict[str, Any]:
    """Uninstall an installed extension."""
    return marketplace_action("uninstall", extension_id=extension_id)


def update_installed_extensions() -> dict[str, Any]:
    """Update installed extensions that have refreshable sources."""
    return marketplace_action("update")


def _specs() -> tuple[OperationSpec, ...]:
    return (
        OperationSpec("search_extensions", search_extensions, operation="runtime_marketplace_search_extensions"),
        OperationSpec("list_installed_extensions", list_installed_extensions, operation="runtime_marketplace_list_installed_extensions"),
        OperationSpec("get_installed_extension", get_installed_extension, operation="runtime_marketplace_get_installed_extension"),
        OperationSpec(
            "install_extension",
            install_extension,
            sensitive=True,
            operation="runtime_marketplace_install_extension",
        ),
        OperationSpec("set_extension_enabled", set_extension_enabled, operation="runtime_marketplace_set_extension_enabled"),
        OperationSpec("uninstall_extension", uninstall_extension, operation="runtime_marketplace_uninstall_extension"),
        OperationSpec("update_installed_extensions", update_installed_extensions, operation="runtime_marketplace_update_installed_extensions"),
    )


def build_server():
    return build_mcp_server("better-agent-marketplace", _specs())


def main() -> int:
    return run_mcp_or_cli("better-agent-marketplace", _specs())


if __name__ == "__main__":
    raise SystemExit(main())
