#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home
_test_home.isolate("ba-mcp-launch-")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import extension_mcp_launcher  # noqa: E402
import extension_store  # noqa: E402
import ambient_mcp_transport  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def main() -> int:
    os.environ["PATH"] = "/usr/bin"
    os.environ["PARENT_SECRET_SHOULD_NOT_LEAK"] = "secret"
    os.environ["BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID"] = "extension:ofek.extension:server"
    captured: dict[str, object] = {}

    class Broker:
        def close(self):
            captured["broker_closed"] = True

    broker = Broker()
    ambient_mcp_transport.connect = lambda: (broker, broker)  # type: ignore[assignment]
    ambient_mcp_transport.send_json = lambda stream, value: None  # type: ignore[assignment]
    ambient_mcp_transport.recv_json = lambda stream: {"credential": "ephemeral"}  # type: ignore[assignment]

    def resolve_native_mcp_server_config(**kwargs):
        return {
            "command": "server-bin",
            "args": ["--serve"],
            "env": {"EXTENSION_ENV": "ok"},
        }

    class Process:
        def wait(self):
            return 0

    def popen(args, env):
        captured["command"] = args[0]
        captured["args"] = args
        captured["env"] = env
        return Process()

    def execvpe(command, args, env):
        captured["command"] = command
        captured["args"] = args
        captured["env"] = env
        raise SystemExit(0)

    extension_store.resolve_native_mcp_server_config = resolve_native_mcp_server_config  # type: ignore[method-assign]
    os.execvpe = execvpe  # type: ignore[assignment]
    extension_mcp_launcher.subprocess.Popen = popen  # type: ignore[assignment]

    check(extension_mcp_launcher.main(["ofek.extension", "server"]) == 0, "launcher reached exec path")

    env = captured.get("env")
    check(isinstance(env, dict), "launcher passes explicit env")
    check(env.get("EXTENSION_ENV") == "ok", "launcher includes extension env")
    check(env.get("PATH") == "/usr/bin", "launcher preserves PATH only")
    check(env.get("PYTHONIOENCODING") == "utf-8", "launcher sets python encoding")
    check("PARENT_SECRET_SHOULD_NOT_LEAK" not in env, "launcher does not inherit parent secrets")
    check(env.get("BETTER_AGENT_INTERNAL_TOKEN") == "ephemeral", "launcher injects ephemeral credential")
    check(
        env.get("BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID") == "extension:ofek.extension:server",
        "ambient launcher forwards capability marker",
    )
    check(captured.get("broker_closed") is True, "launcher closes broker connection")

    captured.clear()
    os.environ["BETTER_CLAUDE_APP_SESSION_ID"] = "session-bound"
    check(extension_mcp_launcher.main(["ofek.extension", "server"]) == 0, "session launcher reached exec path")
    session_env = captured.get("env")
    check(isinstance(session_env, dict), "session launcher passes explicit env")
    check(
        "BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID" not in session_env,
        "session launcher does not forward ambient capability marker",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
