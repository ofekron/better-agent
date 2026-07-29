#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

def _record(counter: Path, event: str) -> None:
    with counter.open("a", encoding="utf-8") as handle:
        handle.write(event + "\n")


def _fixture_server(mode: str, counter: Path) -> int:
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            _record(counter, "initialize")
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _record(counter, "tools/list")
            if mode == "close":
                return 0
            result = {
                "tools": [] if mode == "empty" else [{
                    "name": "" if mode == "malformed" else f"{mode}_tool",
                    "description": "Fixture tool",
                    "inputSchema": {"type": "object", "properties": {}},
                }],
            }
        else:
            result = {}
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": result,
        }), flush=True)
    return 0


if len(sys.argv) == 4 and sys.argv[1] == "--fixture":
    raise SystemExit(_fixture_server(sys.argv[2], Path(sys.argv[3])))


BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import mcp_stdio_bridge
from mcp_tool_discovery import McpToolDiscovery
import runner
import runner_better_agent


def _config(mode: str, counter: Path) -> dict:
    return {
        "command": sys.executable,
        "args": [str(Path(__file__).resolve()), "--fixture", mode, str(counter)],
        "env": {},
    }


async def _assert_lifecycle(tmp: Path) -> None:
    counter = tmp / "events.log"
    discovery = McpToolDiscovery()
    healthy = _config("healthy", counter)
    first, second = await asyncio.gather(
        discovery.discover("fixture", healthy, mcp_stdio_bridge.mcp_list_tools),
        discovery.discover("fixture", healthy, mcp_stdio_bridge.mcp_list_tools),
    )
    assert first == second
    assert [tool["name"] for tool in first] == ["healthy_tool"]
    assert counter.read_text().splitlines() == ["initialize", "tools/list"]

    first[0]["name"] = "mutated"
    cached = await discovery.discover(
        "fixture",
        healthy,
        mcp_stdio_bridge.mcp_list_tools,
    )
    assert [tool["name"] for tool in cached] == ["healthy_tool"]
    assert counter.read_text().splitlines() == ["initialize", "tools/list"]

    empty = _config("empty", counter)
    assert await discovery.discover(
        "fixture",
        empty,
        mcp_stdio_bridge.mcp_list_tools,
    ) == []
    assert await discovery.discover(
        "fixture",
        empty,
        mcp_stdio_bridge.mcp_list_tools,
    ) == []
    assert counter.read_text().splitlines().count("initialize") == 3
    assert counter.read_text().splitlines().count("tools/list") == 3

    closed = _config("close", counter)
    failures = await asyncio.gather(
        discovery.discover("fixture", closed, mcp_stdio_bridge.mcp_list_tools),
        discovery.discover("fixture", closed, mcp_stdio_bridge.mcp_list_tools),
        return_exceptions=True,
    )
    assert len(failures) == 2
    assert all(
        isinstance(item, RuntimeError)
        and "MCP server closed stdout" in str(item)
        for item in failures
    )
    assert counter.read_text().splitlines().count("initialize") == 4
    assert counter.read_text().splitlines().count("tools/list") == 4

    for _ in range(2):
        try:
            await discovery.discover(
                "fixture",
                closed,
                mcp_stdio_bridge.mcp_list_tools,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("transport failure was cached as success")
    assert counter.read_text().splitlines().count("initialize") == 6

    discovery.invalidate("fixture")
    refreshed = await discovery.discover(
        "fixture",
        healthy,
        mcp_stdio_bridge.mcp_list_tools,
    )
    assert [tool["name"] for tool in refreshed] == ["healthy_tool"]
    assert counter.read_text().splitlines().count("initialize") == 7


async def _assert_provider_policies(tmp: Path) -> None:
    counter = tmp / "provider-events.log"
    healthy = {"healthy": _config("healthy", counter)}
    bridged = await runner._bridge_native_extension_mcp_servers(
        healthy,
        required_servers={"healthy"},
    )
    assert set(bridged) == {"healthy"}

    empty = {"empty": _config("empty", counter)}
    try:
        await runner._bridge_native_extension_mcp_servers(
            empty,
            required_servers={"empty"},
        )
    except RuntimeError as exc:
        assert str(exc) == "required extension MCP 'empty' advertised no tools"
    else:
        raise AssertionError("Claude bridge admitted required empty MCP")

    ambient = await runner._bridge_native_extension_mcp_servers(
        {"closed": _config("close", counter)},
        required_servers=set(),
    )
    assert ambient == {}

    schemas, handlers = await runner_better_agent._extension_mcp_tools_for_run(
        healthy,
        required_servers={"healthy"},
        used_names=set(),
    )
    assert [item["function"]["name"] for item in schemas] == [
        "mcp__healthy__healthy_tool",
    ]
    assert set(handlers) == {"mcp__healthy__healthy_tool"}

    try:
        await runner_better_agent._extension_mcp_tools_for_run(
            empty,
            required_servers={"empty"},
            used_names=set(),
        )
    except RuntimeError as exc:
        assert str(exc) == "required extension MCP 'empty' advertised no tools"
    else:
        raise AssertionError("OpenAI bridge admitted required empty MCP")

    malformed = {"malformed": _config("malformed", counter)}
    for bridge in ("claude", "openai"):
        try:
            if bridge == "claude":
                await runner._bridge_native_extension_mcp_servers(
                    malformed,
                    required_servers={"malformed"},
                )
            else:
                await runner_better_agent._extension_mcp_tools_for_run(
                    malformed,
                    required_servers={"malformed"},
                    used_names=set(),
                )
        except RuntimeError as exc:
            assert str(exc) == (
                "required extension MCP 'malformed' exposed no usable tools"
            )
        else:
            raise AssertionError(f"{bridge} admitted required malformed MCP")

    schemas, handlers = await runner_better_agent._extension_mcp_tools_for_run(
        {"closed": _config("close", counter)},
        required_servers=set(),
        used_names=set(),
    )
    assert schemas == []
    assert handlers == {}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mcp-discovery-test-") as raw:
        tmp = Path(raw)
        asyncio.run(_assert_lifecycle(tmp))
        asyncio.run(_assert_provider_policies(tmp))
    print("MCP tool discovery tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
