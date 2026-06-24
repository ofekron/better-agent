from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from better_agent_sdk import Client


def lock_ops_response(
    key: str,
    release: bool = False,
    holder_token: str = "",
) -> dict[str, Any]:
    key = (key or "").strip()
    if not key:
        return {"success": False, "error": "key_required"}
    try:
        return Client().lock_ops(
            key,
            release=release,
            holder_token=holder_token,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def build_server() -> FastMCP:
    server = FastMCP("better-agent-coordination")

    @server.tool()
    def lock_ops(
        key: str,
        release: bool = False,
        holder_token: str = "",
    ) -> dict[str, Any]:
        """Acquire or release a 3-minute coordination lock for a key."""
        return lock_ops_response(key, release, holder_token)

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
