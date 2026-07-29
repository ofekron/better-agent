from __future__ import annotations

import json
from pathlib import Path
import os
from typing import Any

import extension_store
import installation_profile
import harness_run_projection
from env_compat import dual_env_many, get_env


def _with_sdk_pythonpath(env: dict[str, Any]) -> dict[str, Any]:
    sdk = Path(__file__).resolve().parents[1] / "sdk"
    if not sdk.is_dir():
        return env
    existing = os.environ.get("PYTHONPATH", "")
    return {
        **env,
        "PYTHONPATH": str(sdk) + (os.pathsep + existing if existing else ""),
    }


def _open_file_panel_server_config(
    env: dict[str, Any],
    *,
    include_tool_metadata: bool,
) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("open_file_panel_mcp.py")
    tools = [
        "open_file_panel",
        "request_user_approval",
        "request_user_input",
    ]
    if env.get("BETTER_CLAUDE_FILE_EDITING") == "1":
        tools.append("start_file_discussion")
    config = {
        "command": sys.executable,
        "args": [str(script)],
        "env": env,
    }
    if include_tool_metadata:
        config["tool_names"] = sorted(tools)
    return config


def _open_config_panel_server_config(
    env: dict[str, Any],
    *,
    include_tool_metadata: bool,
) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("open_config_panel_mcp.py")
    config = {
        "command": sys.executable,
        "args": [str(script)],
        "env": env,
    }
    if include_tool_metadata:
        config["tool_names"] = ["open_config_panel"]
    return config


def _capabilities_server_config(
    env: dict[str, Any],
    *,
    include_tool_metadata: bool,
) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("capabilities_mcp.py")
    config = {
        "command": sys.executable,
        "env": env,
    }
    if include_tool_metadata:
        config["tool_names"] = [
            "list_capabilities",
            "load_capability",
            "release_capability",
        ]
    if getattr(sys, "frozen", False):
        return {**config, "args": ["--capabilities-mcp"]}
    return {**config, "args": [str(script)]}


def _communicate_server_config(
    env: dict[str, Any],
    *,
    include_tool_metadata: bool,
) -> dict[str, Any]:
    import sys
    script = Path(__file__).with_name("communicate_mcp.py")
    config = {
        "command": sys.executable,
        "env": env,
    }
    if include_tool_metadata:
        config["tool_names"] = [
            "chat",
            "create_chat",
            "create_session",
            "create_sub_session",
            "create_worker",
            "delegate_task",
            "delete_chat",
            "ensure_named_worker",
            "inbox",
            "list_available_provider_models",
            "mssg",
            "ask",
            "read_chat_history",
            "read_inbox_history",
            "set_chat_sender_policy",
            "stop_turn",
        ]
    if getattr(sys, "frozen", False):
        return {**config, "args": ["--communicate-mcp"]}
    return {**config, "args": [str(script)]}


def _runtime_broker_env(runtime_broker: Any) -> dict[str, Any]:
    return {
        "BETTER_AGENT_RUNTIME_BROKER": runtime_broker,
        "BETTER_CLAUDE_RUNTIME_BROKER": runtime_broker,
    }


def with_builtin_mcp_servers(
    inputs: dict,
    provider_run_config: dict,
    *,
    runtime_broker: Any = None,
    integrations_enabled: bool | None = None,
    runtime_mcp_servers: dict[str, dict[str, Any]] | None = None,
    launcher_mcp_servers: dict[str, dict[str, Any]] | None = None,
    include_tool_metadata: bool = False,
) -> dict:
    enabled = (
        installation_profile.integrations_enabled()
        if integrations_enabled is None
        else integrations_enabled
    )
    if not enabled:
        return provider_run_config
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
    broker = (
        get_env("BETTER_CLAUDE_RUNTIME_BROKER").strip()
        if runtime_broker is None
        else runtime_broker
    )
    if broker and (
        type(broker) is not str
        and broker != {"kind": "runner_operation_broker"}
    ):
        raise ValueError("runtime broker reference is invalid")
    cwd = str(inputs.get("cwd") or "")
    model = str(inputs.get("model") or "")
    provider_id = str(inputs.get("provider_id") or "").strip()
    provider_kind = str(inputs.get("provider_kind") or "").strip().lower()
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("user_facing")) and not bare

    base_env = _with_sdk_pythonpath({
        **dual_env_many({
            "BETTER_CLAUDE_BACKEND_URL": backend_url,
            "BETTER_CLAUDE_APP_SESSION_ID": app_session_id,
            "BETTER_CLAUDE_CWD": cwd,
            "BETTER_CLAUDE_MODEL": model,
            "BETTER_CLAUDE_PROVIDER_ID": provider_id,
            "BETTER_CLAUDE_FILE_EDITING": (
                "1"
                if inputs.get("working_mode") == "file_editing"
                else "0"
            ),
        }),
        **_runtime_broker_env(broker),
    })
    if user_facing and app_session_id and broker:
        import provider_manifest
        _spec = provider_manifest.spec_for(provider_kind)
        if _spec is None or _spec.hosts_ui_mcp:
            servers["ui"] = _open_file_panel_server_config(
                base_env,
                include_tool_metadata=include_tool_metadata,
            )
        servers["open-config-panel"] = _open_config_panel_server_config(
            base_env,
            include_tool_metadata=include_tool_metadata,
        )

    # Capability management — let the model scope its own session (load/release/
    # list scoped capabilities). Internal, non-bare sessions only; bare sessions
    # are deliberately capability-stripped. Independent of user_facing so
    # headless/worker turns can self-scope too (matches runner.py's Claude path).
    if app_session_id and broker and not bare:
        cap_env = _with_sdk_pythonpath({
            **dual_env_many({
                "BETTER_CLAUDE_BACKEND_URL": backend_url,
                "BETTER_CLAUDE_APP_SESSION_ID": app_session_id,
                "BETTER_CLAUDE_BARE_CONFIG": "0",
            }),
            **_runtime_broker_env(broker),
        })
        servers["capabilities"] = _capabilities_server_config(
            cap_env,
            include_tool_metadata=include_tool_metadata,
        )

    # Team-coordination tools for CLI-backed providers whose vendor CLI has
    # no in-process function-calling hook of its own (unlike Codex/openai,
    # which already implement this same tool set natively — registering it
    # here too would just duplicate their existing tools). Scoped to actual
    # team-mode turns specifically, not merely team_orchestration_enabled,
    # so native-mode tool availability for these providers is unchanged.
    if (
        app_session_id
        and broker
        and not bare
        and provider_kind in ("agy", "copilot")
        and str(inputs.get("mode") or "") == "team"
        and bool(inputs.get("team_orchestration_enabled"))
    ):
        mssg_sender_session_id = str(
            inputs.get("mssg_sender_session_id") or app_session_id
        ).strip()
        communicate_env = _with_sdk_pythonpath({
            **dual_env_many({
                "BETTER_CLAUDE_BACKEND_URL": backend_url,
                "BETTER_CLAUDE_CWD": cwd,
                "BETTER_CLAUDE_MODEL": model,
                "BETTER_CLAUDE_MSSG_SENDER_SESSION_ID": mssg_sender_session_id,
            }),
            **_runtime_broker_env(broker),
        })
        servers["communicate"] = _communicate_server_config(
            communicate_env,
            include_tool_metadata=include_tool_metadata,
        )

    runtime_servers = (
        extension_store.runtime_mcp_server_configs(
            inputs,
            user_facing=bool(user_facing and app_session_id),
            bare=bare,
        )
        if runtime_mcp_servers is None
        else runtime_mcp_servers
    )
    for name, server_config in runtime_servers.items():
        if extension_store.is_reserved_mcp_server_name(name):
            servers[name] = server_config
            continue
        servers.setdefault(name, server_config)

    launcher_servers = (
        extension_store.native_mcp_launcher_server_configs(
            inputs,
            user_facing=bool(user_facing and app_session_id),
            bare=bare,
        )
        if launcher_mcp_servers is None
        else launcher_mcp_servers
    )
    for name, server_config in launcher_servers.items():
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
    runtime_broker = get_env("BETTER_CLAUDE_RUNTIME_BROKER").strip()
    cwd = str(inputs.get("cwd") or "")
    model = str(inputs.get("model") or "")
    provider_id = str(inputs.get("provider_id") or "").strip()
    provisioned_tool_profile = str(inputs.get("provisioned_tool_profile") or "").strip()
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("user_facing")) and not bare
    disabled_extensions = [
        str(item).strip()
        for item in inputs.get("disabled_builtin_extensions") or []
        if str(item or "").strip()
    ]
    active_capability_ids = [
        str(item).strip()
        for item in inputs.get("active_capability_ids") or []
        if str(item or "").strip()
    ]
    return dual_env_many({
        "BETTER_CLAUDE_BACKEND_URL": backend_url,
        "BETTER_CLAUDE_RUNTIME_BROKER": runtime_broker,
        "BETTER_CLAUDE_APP_SESSION_ID": app_session_id,
        "BETTER_CLAUDE_CWD": cwd,
        "BETTER_CLAUDE_MODEL": model,
        "BETTER_CLAUDE_PROVIDER_ID": provider_id,
        "BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE": provisioned_tool_profile,
        "BETTER_CLAUDE_BARE_CONFIG": "1" if bare else "0",
        "BETTER_CLAUDE_USER_FACING": "1" if user_facing else "0",
        "BETTER_CLAUDE_FILE_EDITING": "1" if inputs.get("working_mode") == "file_editing" else "0",
        "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS": ",".join(disabled_extensions),
        "BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS": ",".join(active_capability_ids),
        "BETTER_CLAUDE_HARNESS_LAUNCHER_PROJECTION": json.dumps(
            harness_run_projection.launcher_projection(inputs),
            separators=(",", ":"),
            sort_keys=True,
        ),
    })
