from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
from typing import Any

from env_compat import get_env
from paths import bc_home

_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MAINTAINED_EXTENSION_GROUPS = frozenset(
    {
        "better-agent-coordination",
        "better-agent-marketplace",
        "better-agent-provider-config-sync",
        "better-agent-session-bridge",
        "better-agent-session-control",
    }
)
_CORE_SCRIPTS = {
    "capabilities": "capabilities_mcp.py",
    "communicate": "communicate_mcp.py",
    "open-config-panel": "open_config_panel_mcp.py",
    "ui": "open_file_panel_mcp.py",
}


def install_launcher() -> Path:
    directory = bc_home() / "runtime" / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    command = (
        [sys.executable, "--operation-cli"]
        if getattr(sys, "frozen", False)
        else [sys.executable, str(Path(__file__).resolve())]
    )
    _write_launcher(
        directory / "better-agent-cli",
        "#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n',
        executable=True,
    )
    _write_launcher(
        directory / "better-agent-cli.cmd",
        "@echo off\r\n" + subprocess.list2cmdline(command) + " %*\r\n",
        executable=False,
    )
    return directory


def available_configs() -> dict[str, dict[str, Any]]:
    configs = {
        group: _core_config(script)
        for group, script in _CORE_SCRIPTS.items()
    }
    import extension_store

    inputs = {
        "app_session_id": get_env("BETTER_CLAUDE_APP_SESSION_ID"),
        "backend_url": get_env("BETTER_CLAUDE_BACKEND_URL"),
        "internal_token": get_env("BETTER_CLAUDE_INTERNAL_TOKEN"),
        "cwd": get_env("BETTER_CLAUDE_CWD"),
        "model": get_env("BETTER_CLAUDE_MODEL"),
        "provider_id": get_env("BETTER_CLAUDE_PROVIDER_ID"),
        "bare_config": get_env("BETTER_CLAUDE_BARE_CONFIG") == "1",
        "open_file_panel_enabled": get_env("BETTER_CLAUDE_USER_FACING") == "1",
        "active_capability_ids": [
            item
            for item in get_env("BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS").split(",")
            if item
        ],
        "disabled_builtin_extensions": [
            item
            for item in get_env("BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS").split(",")
            if item
        ],
    }
    extension_configs = extension_store.runtime_mcp_server_configs(
        inputs,
        user_facing=bool(inputs["open_file_panel_enabled"]),
        bare=bool(inputs["bare_config"]),
    )
    configs.update(
        {
            group: config
            for group, config in extension_configs.items()
            if group in _MAINTAINED_EXTENSION_GROUPS
        }
    )
    return configs


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    configs = available_configs()
    if args == ["--list"]:
        print(json.dumps({"groups": sorted(configs)}, separators=(",", ":")))
        return 0
    if not args:
        raise SystemExit("usage: better-agent-cli --list | GROUP OPERATION INPUT")
    group = args.pop(0)
    if not _GROUP_RE.fullmatch(group):
        raise SystemExit("invalid operation group")
    config = configs.get(group)
    if config is None:
        raise SystemExit(f"operation group is unavailable: {group}")
    return _exec_config(config, args)


def _core_config(script: str) -> dict[str, Any]:
    flag = {
        "capabilities_mcp.py": "--capabilities-mcp",
        "communicate_mcp.py": "--communicate-mcp",
        "open_config_panel_mcp.py": "--open-config-panel-mcp",
        "open_file_panel_mcp.py": "--open-file-panel-mcp",
    }[script]
    args = [flag] if getattr(sys, "frozen", False) else [str(Path(__file__).with_name(script))]
    return {"command": sys.executable, "args": args, "env": {}}


def _exec_config(config: dict[str, Any], args: list[str]) -> int:
    command = str(config.get("command") or "").strip()
    configured_args = [str(item) for item in config.get("args") or []]
    if not command:
        raise SystemExit("operation group has no executable")
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in (config.get("env") or {}).items()})
    argv = [command, *configured_args, "cli", *args]
    os.execvpe(command, argv, env)
    raise AssertionError("execvpe returned")


def _write_launcher(path: Path, content: str, *, executable: bool) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except FileNotFoundError:
        pass
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        if executable:
            temp.chmod(0o700)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
