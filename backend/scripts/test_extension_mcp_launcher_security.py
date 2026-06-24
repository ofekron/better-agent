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


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def main() -> int:
    os.environ["PATH"] = "/usr/bin"
    os.environ["PARENT_SECRET_SHOULD_NOT_LEAK"] = "secret"
    captured: dict[str, object] = {}

    def resolve_native_mcp_server_config(**kwargs):
        return {
            "command": "server-bin",
            "args": ["--serve"],
            "env": {"EXTENSION_ENV": "ok"},
        }

    def execvpe(command, args, env):
        captured["command"] = command
        captured["args"] = args
        captured["env"] = env
        raise SystemExit(0)

    extension_store.resolve_native_mcp_server_config = resolve_native_mcp_server_config  # type: ignore[method-assign]
    os.execvpe = execvpe  # type: ignore[assignment]

    try:
        extension_mcp_launcher.main(["ofek.extension", "server"])
    except SystemExit as exc:
        check(exc.code == 0, "launcher reached exec path")

    env = captured.get("env")
    check(isinstance(env, dict), "launcher passes explicit env")
    check(env.get("EXTENSION_ENV") == "ok", "launcher includes extension env")
    check(env.get("PATH") == "/usr/bin", "launcher preserves PATH only")
    check(env.get("PYTHONIOENCODING") == "utf-8", "launcher sets python encoding")
    check("PARENT_SECRET_SHOULD_NOT_LEAK" not in env, "launcher does not inherit parent secrets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
