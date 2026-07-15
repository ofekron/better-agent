from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-core-ambient-home-")

import capabilities_mcp
import open_config_panel_mcp
import open_file_panel_mcp
import ambient_mcp_broker

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def tool_map(server):
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def main() -> None:
    ui = tool_map(open_file_panel_mcp.build_server())
    config = tool_map(open_config_panel_mcp.build_server())
    capabilities = tool_map(capabilities_mcp.build_server())
    assert "app_session_id" in ui["open_file_panel"].parameters["required"]
    assert "app_session_id" in ui["request_user_input"].parameters["required"]
    assert "app_session_id" in config["open_config_panel"].parameters["required"]
    assert "app_session_id" in capabilities["list_capabilities"].parameters["required"]
    assert open_file_panel_mcp.open_file_panel_response("", "panel", "/tmp/a")["success"] is False
    assert open_config_panel_mcp.open_config_panel_response("", "capability")["success"] is False

    seen = []
    capabilities_mcp._post_capabilities = lambda sid, payload: seen.append((sid, payload)) or {"success": True}
    result = asyncio.run(capabilities_mcp.build_server()._tool_manager.call_tool(
        "load_capability",
        {"app_session_id": "session-1", "capability_id": "ext:cap"},
    ))
    assert result["success"] is True
    assert seen == [(
        "session-1",
        {"action": "load", "capability_id": "ext:cap", "app_session_id": "session-1"},
    )]
    test_launcher_from_non_repo_cwd()
    print("PASS authenticated core ambient MCP schemas and calls")


def test_launcher_from_non_repo_cwd() -> None:
    asyncio.run(_launcher_from_non_repo_cwd())


async def _launcher_from_non_repo_cwd() -> None:
    broker = ambient_mcp_broker.AmbientMcpBroker()
    broker.start()
    try:
        launcher = Path(__file__).resolve().parents[1] / "core_ambient_mcp_launcher.py"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "BETTER_AGENT_HOME": os.environ["BETTER_AGENT_HOME"],
            "BETTER_CLAUDE_INTERNAL_TOKEN": "test-token",
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher), "ui"],
            env=env,
            cwd=tempfile.mkdtemp(prefix="ba-core-ambient-cwd-"),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} >= {"open_file_panel", "request_user_input"}
    finally:
        broker.stop()


if __name__ == "__main__":
    main()
