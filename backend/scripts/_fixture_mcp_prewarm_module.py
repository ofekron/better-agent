"""Lightweight FastMCP-ish stdio server fixture for mcp_prewarm tests.

Follows the same `build_server() -> FastMCP` convention every real
extension MCP server module follows (see
`better-agent-private/extensions/*/mcp/server.py`), kept trivial so
test runtime stays bounded. `MCP_PREWARM_FIXTURE_DELAY_SECONDS` lets a
test simulate a slow/hanging extension without a second fixture file.
"""

from __future__ import annotations

import os
import sys
import time
import types


def build_server():
    delay = float(os.environ.get("MCP_PREWARM_FIXTURE_DELAY_SECONDS", "0") or "0")
    if delay:
        time.sleep(delay)

    if os.environ.get("MCP_PREWARM_FIXTURE_LOW_LEVEL") == "1":
        from mcp.server import Server

        return Server("mcp-prewarm-low-level-fixture")

    from mcp.server.fastmcp import FastMCP

    server = FastMCP("mcp-prewarm-fixture")

    @server.tool()
    def ping(x: int) -> int:
        return x + 1

    if os.environ.get("MCP_PREWARM_FIXTURE_PATHS_COLLISION") == "1":
        collision = types.ModuleType("paths")
        collision.__file__ = "/fixture-extension/backend/paths.py"
        sys.modules["paths"] = collision

    return server
