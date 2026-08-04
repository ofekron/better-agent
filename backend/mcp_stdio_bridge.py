from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from execution_environment import isolated_subprocess_environment
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES

logger = logging.getLogger(__name__)

MCP_LIST_TIMEOUT_S = 8.0
MCP_CALL_TIMEOUT_S = 300.0
REQUIREMENTS_WAIT_TRUE_MCP_CALL_TIMEOUT_S = 1380.0
MCP_STDERR_LIMIT_BYTES = 4000


def mcp_subprocess_env(config: dict[str, Any]) -> dict[str, str]:
    raw_env = config.get("env") or {}
    if not isinstance(raw_env, dict):
        raise RuntimeError("MCP server config env must be an object")
    env = isolated_subprocess_environment(
        overrides={str(k): str(v) for k, v in raw_env.items()},
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _mcp_command_args(config: dict[str, Any]) -> tuple[str, list[str]]:
    command = str(config.get("command") or "").strip()
    if not command:
        raise RuntimeError("MCP server config missing command")
    raw_args = config.get("args") or []
    if not isinstance(raw_args, list):
        raise RuntimeError("MCP server config args must be an array")
    args: list[str] = []
    for arg in raw_args:
        if not isinstance(arg, str):
            raise RuntimeError("MCP server config args must be strings")
        args.append(arg)
    return command, args


async def mcp_json_request(
    config: dict[str, Any],
    method: str,
    params: dict[str, Any],
    *,
    timeout: float | None,
) -> dict[str, Any]:
    command, args = _mcp_command_args(config)
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=mcp_subprocess_env(config),
        limit=SUBPROCESS_LINE_LIMIT_BYTES,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(512)
            if not chunk:
                return
            if sum(len(item) for item in stderr_chunks) < MCP_STDERR_LIMIT_BYTES:
                stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(_drain_stderr())

    async def _stderr_excerpt() -> str:
        if proc.returncode is None:
            return ""
        try:
            await asyncio.wait_for(stderr_task, timeout=0.2)
        except asyncio.TimeoutError:
            pass
        except Exception:
            return ""
        data = b"".join(stderr_chunks)[:MCP_STDERR_LIMIT_BYTES]
        return data.decode("utf-8", "replace").strip()

    async def _send(payload: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read_response() -> dict[str, Any]:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        if not line:
            stderr = await _stderr_excerpt()
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"MCP server closed stdout{detail}")
        response = json.loads(line.decode("utf-8", "replace"))
        if response.get("error"):
            raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
        return response.get("result") or {}

    try:
        await _send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "better-agent-runner", "version": "1"},
            },
        })
        await _read_response()
        await _send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        await _send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        return await _read_response()
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        try:
            await asyncio.wait_for(stderr_task, timeout=0.2)
        except asyncio.TimeoutError:
            stderr_task.cancel()


async def mcp_list_tools(server_name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    result = await mcp_json_request(config, "tools/list", {}, timeout=MCP_LIST_TIMEOUT_S)
    tools = result.get("tools") or []
    return [item for item in tools if isinstance(item, dict)]


def mcp_call_timeout_s(config: dict[str, Any], tool_name: str, args: dict[str, Any]) -> float:
    configured_timeout = config.get("tool_timeout_sec")
    if (
        isinstance(configured_timeout, (int, float))
        and not isinstance(configured_timeout, bool)
        and configured_timeout > 0
    ):
        return float(configured_timeout)
    server_name = str(config.get("_server_name") or config.get("server_name") or "")
    if (
        server_name in {"get-requirements", "better-agent-requirements"}
        and tool_name == "fire_get_requirements"
        and args.get("wait") is True
    ):
        return REQUIREMENTS_WAIT_TRUE_MCP_CALL_TIMEOUT_S
    return MCP_CALL_TIMEOUT_S


async def mcp_call_tool(
    config: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    result = await mcp_json_request(
        config,
        "tools/call",
        {"name": tool_name, "arguments": args},
        timeout=mcp_call_timeout_s(config, tool_name, args),
    )
    if "content" not in result:
        text = (
            json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)
            if "structuredContent" in result
            else json.dumps(result, ensure_ascii=False)
        )
        result["content"] = [{"type": "text", "text": text}]
    if result.get("isError"):
        result["is_error"] = True
    return result
