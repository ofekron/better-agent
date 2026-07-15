from __future__ import annotations

from pathlib import Path
from typing import Any

import extension_store
from env_compat import better_agent_runtime_env, dual_env_many, get_env


def _open_file_panel_server_config(env: dict[str, str]) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("open_file_panel_mcp.py")
    return {
        "command": sys.executable,
        "args": [str(script)],
        "env": env,
    }


def _open_config_panel_server_config(env: dict[str, str]) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("open_config_panel_mcp.py")
    return {
        "command": sys.executable,
        "args": [str(script)],
        "env": env,
    }


def _capabilities_server_config(env: dict[str, str]) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("capabilities_mcp.py")
    if getattr(sys, "frozen", False):
        return {"command": sys.executable, "args": ["--capabilities-mcp"], "env": env}
    return {"command": sys.executable, "args": [str(script)], "env": env}


def with_builtin_mcp_servers(inputs: dict, provider_run_config: dict) -> dict:
    config = {
        **provider_run_config,
        "mcp_servers": dict(provider_run_config.get("mcp_servers") or {}),
    }
    servers = config["mcp_servers"]

    app_session_id = str(inputs.get("app_session_id") or "").strip()
    backend_url = str(
        inputs.get("backend_url")
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    internal_token = str(inputs.get("internal_token") or "").strip()
    cwd = str(inputs.get("cwd") or "")
    model = str(inputs.get("model") or "")
    provider_id = str(inputs.get("provider_id") or "").strip()
    provider_kind = str(inputs.get("provider_kind") or "").strip().lower()
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("open_file_panel_enabled")) and not bare

    base_env = {
        **better_agent_runtime_env(),
        **dual_env_many(
            {
                "BETTER_CLAUDE_BACKEND_URL": backend_url,
                "BETTER_CLAUDE_INTERNAL_TOKEN": internal_token,
                "BETTER_CLAUDE_APP_SESSION_ID": app_session_id,
                "BETTER_CLAUDE_CWD": cwd,
                "BETTER_CLAUDE_MODEL": model,
                "BETTER_CLAUDE_PROVIDER_ID": provider_id,
                "BETTER_CLAUDE_FILE_EDITING": "1"
                if inputs.get("working_mode") == "file_editing"
                else "0",
            }
        ),
    }
    if user_facing and app_session_id and backend_url and internal_token:
        import provider_manifest
        _spec = provider_manifest.spec_for(provider_kind)
        if _spec is None or _spec.hosts_ui_mcp:
            servers["ui"] = _open_file_panel_server_config(base_env)
        servers["open-config-panel"] = _open_config_panel_server_config(base_env)

    # Capability management — let the model scope its own session (load/release/
    # list scoped capabilities). Internal, non-bare sessions only; bare sessions
    # are deliberately capability-stripped. Independent of user_facing so
    # headless/worker turns can self-scope too (matches runner.py's Claude path).
    if app_session_id and backend_url and internal_token and not bare:
        cap_env = {
            **better_agent_runtime_env(),
            **dual_env_many(
                {
                    "BETTER_CLAUDE_BACKEND_URL": backend_url,
                    "BETTER_CLAUDE_INTERNAL_TOKEN": internal_token,
                    "BETTER_CLAUDE_APP_SESSION_ID": app_session_id,
                    "BETTER_CLAUDE_BARE_CONFIG": "0",
                }
            ),
        }
        servers["capabilities"] = _capabilities_server_config(cap_env)

    for name, server_config in extension_store.runtime_mcp_server_configs(
        inputs,
        user_facing=bool(user_facing and app_session_id),
        bare=bare,
    ).items():
        if extension_store.is_reserved_mcp_server_name(name):
            servers[name] = server_config
            continue
        servers.setdefault(name, server_config)

    for name, server_config in extension_store.native_mcp_launcher_server_configs(
        inputs,
        user_facing=bool(user_facing and app_session_id),
        bare=bare,
    ).items():
        if extension_store.is_reserved_mcp_server_name(name):
            servers[name] = server_config
            continue
        servers.setdefault(name, server_config)

    return config


def native_mcp_runtime_env(inputs: dict) -> dict[str, str]:
    app_session_id = str(inputs.get("app_session_id") or "").strip()
    backend_url = str(
        inputs.get("backend_url")
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    internal_token = str(inputs.get("internal_token") or "").strip()
    cwd = str(inputs.get("cwd") or "")
    model = str(inputs.get("model") or "")
    provider_id = str(inputs.get("provider_id") or "").strip()
    provisioned_tool_profile = str(inputs.get("provisioned_tool_profile") or "").strip()
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("open_file_panel_enabled")) and not bare
    disabled_extensions = [
        str(item).strip()
        for item in inputs.get("disabled_builtin_extensions") or []
        if str(item or "").strip()
    ]
    return {
        **better_agent_runtime_env(),
        **dual_env_many(
            {
                "BETTER_CLAUDE_BACKEND_URL": backend_url,
                "BETTER_CLAUDE_INTERNAL_TOKEN": internal_token,
                "BETTER_CLAUDE_APP_SESSION_ID": app_session_id,
                "BETTER_CLAUDE_CWD": cwd,
                "BETTER_CLAUDE_MODEL": model,
                "BETTER_CLAUDE_PROVIDER_ID": provider_id,
                "BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE": provisioned_tool_profile,
                "BETTER_CLAUDE_BARE_CONFIG": "1" if bare else "0",
                "BETTER_CLAUDE_USER_FACING": "1" if user_facing else "0",
                "BETTER_CLAUDE_FILE_EDITING": "1"
                if inputs.get("working_mode") == "file_editing"
                else "0",
                "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS": ",".join(disabled_extensions),
            }
        ),
    }
