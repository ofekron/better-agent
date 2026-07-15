from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from env_compat import better_agent_runtime_env, get_env


_MODULE_FILES = {
    "ui": "open_file_panel_mcp.py",
    "open-config-panel": "open_config_panel_mcp.py",
    "capabilities": "capabilities_mcp.py",
}


def _child_env(credential: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        **better_agent_runtime_env(),
        "BETTER_CLAUDE_BACKEND_URL": get_env("BETTER_CLAUDE_BACKEND_URL") or "http://localhost:8000",
        "BETTER_CLAUDE_INTERNAL_TOKEN": credential,
        "BETTER_AGENT_INTERNAL_TOKEN": credential,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in _MODULE_FILES:
        print("usage: core_ambient_mcp_launcher.py <ui|open-config-panel|capabilities>", file=sys.stderr)
        return 2
    server_name = args[0]
    import ambient_mcp_transport

    connection, stream = ambient_mcp_transport.connect()
    try:
        ambient_mcp_transport.send_json(stream, {
            "source_kind": "core",
            "server_name": server_name,
            "provider_id": get_env("BETTER_CLAUDE_PROVIDER_ID") or "ambient",
            "cwd": get_env("BETTER_CLAUDE_CWD"),
            "pid": os.getpid(),
        })
        grant = ambient_mcp_transport.recv_json(stream)
        credential = str(grant.get("credential") or "")
        if not credential:
            raise PermissionError("broker returned no credential")
        env = _child_env(credential)
        module_path = Path(__file__).with_name(_MODULE_FILES[server_name]).resolve()
        process = subprocess.Popen([sys.executable, str(module_path)], env=env)
        return process.wait()
    finally:
        if stream is not connection:
            stream.close()
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
