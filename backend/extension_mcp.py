from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


_MARKER_EXTENSION_ID = "BETTER_CLAUDE_EXTENSION_ID"
_MARKER_SERVER_NAME = "BETTER_CLAUDE_EXTENSION_MCP_SERVER"


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
