from __future__ import annotations

import os
import subprocess
import sys

from env_compat import get_env
import extension_store


def _runtime_inputs() -> dict:
    return {
        "backend_url": get_env("BETTER_CLAUDE_BACKEND_URL"),
        "internal_token": "",
        "app_session_id": get_env("BETTER_CLAUDE_APP_SESSION_ID"),
        "cwd": get_env("BETTER_CLAUDE_CWD"),
        "model": get_env("BETTER_CLAUDE_MODEL"),
        "provider_id": get_env("BETTER_CLAUDE_PROVIDER_ID"),
        "mode": get_env("BETTER_CLAUDE_MODE"),
        "working_mode": get_env("BETTER_CLAUDE_WORKING_MODE"),
        "provisioned_tool_profile": get_env("BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE"),
        "bare_config": get_env("BETTER_CLAUDE_BARE_CONFIG") == "1",
        "open_file_panel_enabled": get_env("BETTER_CLAUDE_INTERACTS_WITH_USER") == "1",
        "disabled_builtin_extensions": [
            item
            for item in get_env("BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS").split(",")
            if item
        ],
        # Threaded from `_native_mcp_launcher_env` so a capability-gated MCP
        # (predicate `contains: {active_capability_ids: ...}`) re-resolves the
        # same way it was built; without this the launcher predicate fails
        # closed and the server refuses to start (`extension MCP unavailable`).
        "active_capability_ids": [
            item
            for item in get_env("BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS").split(",")
            if item
        ],
        "extension_mcp_launcher_context": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: extension_mcp_launcher.py <extension-id> <server-name>", file=sys.stderr)
        return 2
    extension_id, server_name = args
    broker_connection = broker_stream = None
    credential = ""
    if not get_env("BETTER_CLAUDE_APP_SESSION_ID"):
        try:
            import ambient_mcp_transport
            broker_connection, broker_stream = ambient_mcp_transport.connect()
            ambient_mcp_transport.send_json(broker_stream, {
                "extension_id": extension_id,
                "server_name": server_name,
                "provider_id": get_env("BETTER_CLAUDE_PROVIDER_ID") or "ambient",
                "cwd": get_env("BETTER_CLAUDE_CWD"),
                "pid": os.getpid(),
            })
            grant = ambient_mcp_transport.recv_json(broker_stream)
            credential = str(grant.get("credential") or "")
            if not credential:
                raise ConnectionError("broker returned no credential")
        except (ConnectionError, OSError, ValueError) as exc:
            print(f"ambient MCP authentication unavailable: {exc}", file=sys.stderr)
            return 1
    config = extension_store.resolve_native_mcp_server_config(
        extension_id=extension_id,
        server_name=server_name,
        inputs=_runtime_inputs(),
    )
    if not config:
        if broker_connection is not None:
            broker_connection.close()
        print(f"extension MCP unavailable: {extension_id}/{server_name}", file=sys.stderr)
        return 1
    command = str(config.get("command") or "").strip()
    if not command:
        if broker_connection is not None:
            broker_connection.close()
        print("extension MCP resolved without a command", file=sys.stderr)
        return 1
    exec_args = [command, *[str(arg) for arg in config.get("args") or []]]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
    if credential:
        env["BETTER_AGENT_INTERNAL_TOKEN"] = credential
        env["BETTER_CLAUDE_INTERNAL_TOKEN"] = credential
        ambient_capability_id = os.environ.get("BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID", "").strip()
        if ambient_capability_id:
            env["BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID"] = ambient_capability_id
    process = subprocess.Popen(exec_args, env=env)
    try:
        return process.wait()
    finally:
        if broker_stream is not None and broker_stream is not broker_connection:
            broker_stream.close()
        if broker_connection is not None:
            broker_connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
