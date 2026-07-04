"""Codex CLI runner — detached per-run executable.

Spawned by `CodexProvider.start_run` as a subprocess with
`start_new_session=True`. Handles one Codex CLI run via
Codex app-server. It records Codex's native rollout path and turn
completion state; the backend tails the native rollout directly.

Life of a run:
  1. Backend creates run dir, writes input.json.
  2. Backend spawns `python runner_codex.py --run-dir <path>` detached.
  3. This script reads input.json, spawns `codex --json --yolo ...`.
  4. On thread.started event: captures thread_id, writes state.json.
  5. Backend tails Codex's native rollout JSONL directly.
  6. On turn.completed/turn.failed: writes complete.json and exits.

Cancel sentinel: backend writes `run_dir/cancel`, runner terminates the
codex subprocess.
"""

import argparse
import asyncio
import base64
import http.client
import json
import logging
import os
import sys
import time
import uuid
from tool_approval_client import request_tool_approval
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import re

from i18n import t
from builtin_mcp_config import native_mcp_runtime_env, with_builtin_mcp_servers
from capability_contexts import prepend_capability_context
from continuation import normalize_context_overflow_error
from codex_usage import token_usage_from_codex_usage
from loopback_http import raise_loopback_http_error
from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN,
    normalize_ask_mode,
)
import chat_store
import extension_store
from runs_dir import atomic_write_json
from env_compat import get_env
from orchestration_tool_descriptions import (
    ASK_DESCRIPTION as _ASK_DESCRIPTION,
    CHAT_DESCRIPTION as _CHAT_DESCRIPTION,
    CREATE_CHAT_DESCRIPTION as _CREATE_CHAT_DESCRIPTION,
    CREATE_SESSION_DESCRIPTION as _CREATE_SESSION_DESCRIPTION,
    CREATE_SUB_SESSION_DESCRIPTION as _CREATE_SUB_SESSION_DESCRIPTION,
    CREATE_WORKER_DESCRIPTION as _CREATE_WORKER_DESCRIPTION,
    DELETE_CHAT_DESCRIPTION as _DELETE_CHAT_DESCRIPTION,
    DELEGATE_TASK_DESCRIPTION as _DELEGATE_TASK_DESCRIPTION,
    ENSURE_NAMED_WORKER_DESCRIPTION as _ENSURE_NAMED_WORKER_DESCRIPTION,
    LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION as _LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION,
    MSSG_DESCRIPTION as _MSSG_DESCRIPTION,
)
from orchestration_tool_schemas import (
    DELEGATE_TASK_INPUT_SCHEMA as _DELEGATE_TASK_INPUT_SCHEMA,
    ENSURE_NAMED_WORKER_INPUT_SCHEMA as _ENSURE_NAMED_WORKER_INPUT_SCHEMA,
    LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA as _LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA,
)
from paths import ba_home
from provider_catalog_mcp import available_provider_models_response
from provider_run_config import symlink_home_overlay, toml_literal, write_skill_tree
from runtime_skills import materialize_runtime_skills
from proc_control import process_control as _process_control

APP_SERVER_REQUEST_TIMEOUT_S = 45.0
DELEGATE_HTTP_TIMEOUT_S = 24 * 60 * 60

_CODEX_SANDBOX_TO_TYPE = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}

_token_cache = {"mtime": 0.0, "token": None}


def _load_internal_token() -> Optional[str]:
    try:
        path = ba_home() / "internal_token"
        st = path.stat()
        if _token_cache["token"] is not None and _token_cache["mtime"] == st.st_mtime:
            return _token_cache["token"]
        token = path.read_text(encoding="utf-8").strip()
        _token_cache["mtime"] = st.st_mtime
        _token_cache["token"] = token or None
        return _token_cache["token"]
    except Exception:
        _token_cache["mtime"] = 0.0
        _token_cache["token"] = None
        return None


def _codex_sandbox_policy(sandbox: str = "danger-full-access") -> dict[str, str]:
    return {"type": _CODEX_SANDBOX_TO_TYPE.get(sandbox, "dangerFullAccess")}


def _codex_approval_policy(permission: Optional[dict]) -> str:
    return (permission or {}).get("approval") or "never"


def _codex_sandbox_mode(permission: Optional[dict]) -> str:
    return (permission or {}).get("sandbox") or "danger-full-access"


def _codex_approval_summary(method: str, params: dict) -> dict:
    """Friendly, size-capped summary of a codex approval request for the card."""
    if method in ("execCommandApproval", "item/commandExecution/requestApproval"):
        cmd = params.get("command") or []
        text = " ".join(str(c) for c in cmd)[:800]
        return {"tool": "shell", "input": {"command": text, "cwd": params.get("cwd", "")}}
    changes = params.get("fileChanges") or {}
    paths = list(changes.keys()) if isinstance(changes, dict) else []
    return {"tool": "edit", "input": {"files": paths[:50]}}


def _codex_runner_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {**inputs, "provider_kind": "codex"}


def _codex_thread_config(provider_run_config: Optional[dict[str, Any]]) -> dict[str, Any]:
    thread_config: dict[str, Any] = {}
    mcp_servers = (provider_run_config or {}).get("mcp_servers") or {}
    if mcp_servers:
        thread_config["mcpServers"] = mcp_servers
    return thread_config


def _codex_thread_capability_params(
    *,
    dynamic_tools: Optional[list[dict]],
    provider_run_config: Optional[dict[str, Any]],
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if dynamic_tools:
        params["dynamicTools"] = dynamic_tools
    thread_config = _codex_thread_config(provider_run_config)
    if thread_config:
        params["config"] = thread_config
    return params


def _codex_config_overrides(
    run_dir: Path,
    provider_run_config: dict,
) -> list[str]:
    overrides: list[str] = []
    mcp_servers = provider_run_config.get("mcp_servers") or {}
    if mcp_servers:
        overrides.append(f"mcp_servers={toml_literal(mcp_servers)}")

    return overrides


def _materialize_codex_run_home(
    run_dir: Path,
    provider_run_config: dict,
    *,
    cwd: str,
    bare_config: bool = False,
) -> dict[str, str]:
    real_home = Path.home()
    overlay_home = run_dir / "codex-home"
    symlink_home_overlay(real_home, overlay_home, skip={".codex", ".agents"})
    symlink_home_overlay(real_home / ".agents", overlay_home / ".agents", skip={"skills"})

    real_codex_home = Path(os.environ.get("CODEX_HOME") or real_home / ".codex").expanduser()
    overlay_codex_home = overlay_home / ".codex"
    if real_codex_home.exists() and not overlay_codex_home.exists() and not overlay_codex_home.is_symlink():
        os.symlink(real_codex_home, overlay_codex_home, target_is_directory=real_codex_home.is_dir())

    skills_root = overlay_home / ".agents" / "skills"
    materialize_runtime_skills(skills_root, cwd, bare_config=bare_config)

    skills = provider_run_config.get("skills") or {}
    if skills:
        write_skill_tree(skills_root, skills)

    env = {"HOME": str(overlay_home)}
    if overlay_codex_home.exists() or overlay_codex_home.is_symlink():
        env["CODEX_HOME"] = str(overlay_codex_home)
    return env


def _context_strategy_config_overrides(inputs: dict) -> list[str]:
    if inputs.get("context_strategy") != "continuation":
        return []
    return [
        "model_auto_compact_token_limit=999999999",
        'model_auto_compact_token_limit_scope="total"',
    ]


_CODEX_NATIVE_TOOL_NAMES = frozenset({
    "request_user_input",
})

_KNOWN_MCP_SERVER_TOOL_NAMES: dict[str, frozenset[str]] = {
    "ui": frozenset({"open_file_panel", "request_user_input"}),
    "open-config-panel": frozenset({"open_config_panel"}),
}


def _codex_existing_tool_names(provider_run_config: Optional[dict[str, Any]]) -> set[str]:
    names = set(_CODEX_NATIVE_TOOL_NAMES)
    mcp_servers = (provider_run_config or {}).get("mcp_servers") or {}
    for server_name, server_config in mcp_servers.items():
        names.update(_KNOWN_MCP_SERVER_TOOL_NAMES.get(str(server_name), ()))
        if not isinstance(server_config, dict):
            continue
        for tool_name in server_config.get("tool_names") or ():
            if isinstance(tool_name, str) and tool_name:
                names.add(tool_name)
    return names


def _add_dynamic_tool(
    dynamic_tools: list[dict],
    tool_handlers: dict[str, Any],
    tool: dict,
    handler: Any,
    *,
    existing_tool_names: set[str],
) -> bool:
    name = str(tool.get("name") or "").strip()
    if not name:
        raise ValueError("dynamic tool is missing a name")
    if name in existing_tool_names:
        return False
    if any(existing.get("name") == name for existing in dynamic_tools):
        raise ValueError(f"dynamic tool {name!r} is already registered")
    dynamic_tools.append(tool)
    tool_handlers[name] = handler
    existing_tool_names.add(name)
    return True


_CREATE_WORKER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "worker_description": {"type": "string"},
        "justification": {"type": "string"},
        "orchestration_mode": {"type": "string", "enum": ["team", "native"]},
        "node_id": {"type": "string"},
    },
    "required": ["worker_description", "justification", "orchestration_mode"],
    "additionalProperties": False,
}

_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "The shared team chat to read/post."},
        "message": {"type": "string", "description": "Optional non-empty message to append; empty = read-only."},
    },
    "required": ["chat_id"],
    "additionalProperties": False,
}

_CREATE_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "Unique id for the new chat."},
        "name": {"type": "string", "description": "Optional human-readable name."},
    },
    "required": ["chat_id"],
    "additionalProperties": False,
}

_DELETE_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "The chat to delete permanently."},
    },
    "required": ["chat_id"],
    "additionalProperties": False,
}

_OPEN_FILE_PANEL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["panel", "inline"]},
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "selected_start": {"type": "integer"},
        "selected_end": {"type": "integer"},
    },
    "required": ["mode", "path"],
    "additionalProperties": False,
}

_OPEN_FILE_PANEL_DESCRIPTION = (
    "Show the user a specific file location in the Better Agent UI. Use "
    "mode='panel' to open a persistent side panel, or mode='inline' to attach "
    "an inline viewer to the tool call."
)

_START_FILE_DISCUSSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "line": {"type": "integer"},
        "title": {"type": "string"},
    },
    "required": ["file_path", "line"],
    "additionalProperties": False,
}

_START_FILE_DISCUSSION_DESCRIPTION = (
    "Start an inline discussion attached to a specific line in file edit mode."
)

_MSSG_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {"type": "string"},
        "target_worker_id": {"type": "string"},
        "target_worker_pool": {"type": "string"},
        "pool_affinity_key": {"type": "string"},
        "message": {"type": "string"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
        "collapse_key": {"type": "string"},
        "collapse_policy": {"type": "string", "enum": ["take_latest"]},
    },
    "required": ["message"],
    "additionalProperties": False,
}

_CREATE_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "orchestration_mode": {
            "type": "string",
            "enum": ["native", "team"],
            "description": (
                "'native' (default) = a plain session that does work directly. "
                "'team' = a sub-coordinator for complex tasks that need their "
                "own delegation loop."
            ),
        },
        "node_id": {"type": "string"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_CREATE_SUB_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "node_id": {"type": "string"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
    },
    "required": [],
    "additionalProperties": False,
}

_ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {"type": "string"},
        "target_worker_id": {"type": "string"},
        "target_worker_pool": {"type": "string"},
        "pool_affinity_key": {"type": "string"},
        "message": {"type": "string"},
        "run_mode": {"type": "string", "enum": ["direct", "fork"]},
        "mode": {
            "type": "string",
            "enum": [
                ASK_MODE_WAIT_AND_GRAB_LAST_MSSG_IN_TURN,
                ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
            ],
        },
        "worker_description": {"type": "string"},
        "worker_registry_cwd": {"type": "string"},
        "ephemeral": {"type": "boolean"},
        "provider_id": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
    },
    "required": ["message"],
    "additionalProperties": False,
}


_DISABLEABLE_BUILTIN_TOOLS = frozenset({
    "ask",
    "create_session",
    "create_sub_session",
    "delegate_task",
    "ensure_named_worker",
    "list_available_provider_models",
    "mssg",
})


def _disabled_builtin_tools(inputs: dict) -> set[str]:
    raw = inputs.get("disabled_builtin_tools")
    if not isinstance(raw, list):
        return set()
    return {
        str(item).strip()
        for item in raw
        if str(item or "").strip() in _DISABLEABLE_BUILTIN_TOOLS
    }


def _dynamic_tool_text_result(text: str, *, success: bool) -> dict:
    return {
        "contentItems": [{"type": "inputText", "text": text}],
        "success": success,
    }


def _dynamic_tool_json_result(result: dict, *, success: bool) -> dict:
    return _dynamic_tool_text_result(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        success=success,
    )


def _post_loopback_sync(
    payload: dict,
    *,
    backend_url: str,
    internal_token: str,
    url_path: str = "/api/internal/ask-fork",
    timeout_s: float = DELEGATE_HTTP_TIMEOUT_S,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    deadline = time.monotonic() + timeout_s
    backoff = 1.0
    tried_live_token_after_forbidden = False

    def _request_once(token: str) -> dict:
        req = urllib.request.Request(
            backend_url.rstrip("/") + url_path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": token,
            },
        )
        remaining = max(1.0, deadline - time.monotonic())
        with urllib.request.urlopen(req, timeout=remaining) as resp:
            raw = resp.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(t("runner.delegate_non_json", e=str(e), raw=repr(raw[:200])))

    while True:
        try:
            return _request_once(internal_token)
        except urllib.error.HTTPError as e:
            live_token = _load_internal_token()
            if (
                e.code == 403
                and live_token
                and live_token != internal_token
                and not tried_live_token_after_forbidden
            ):
                tried_live_token_after_forbidden = True
                try:
                    return _request_once(live_token)
                except urllib.error.HTTPError:
                    raise e
            if e.code != 403:
                raise_loopback_http_error(e)
            raise
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ConnectionError,
            TimeoutError,
        ) as e:
            if time.monotonic() >= deadline:
                raise
            reason = getattr(e, "reason", e)
            logger.warning(
                "loopback POST %s failed (%s); retrying in %.1fs",
                url_path,
                reason,
                backoff,
            )
            time.sleep(min(backoff, max(0.5, deadline - time.monotonic())))
            backoff = min(backoff * 2, 60.0)


def _build_create_worker_dynamic_tool() -> dict:
    return {
        "name": "create_worker",
        "description": _CREATE_WORKER_DESCRIPTION,
        "inputSchema": _CREATE_WORKER_INPUT_SCHEMA,
    }


def _build_create_worker_tool_handler(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
    model: Optional[str],
    cwd: str,
):
    async def create_worker(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result(
                "create_worker arguments must be an object",
                success=False,
            )
        worker_description = args.get("worker_description") or ""
        justification = args.get("justification") or ""
        orchestration_mode = args.get("orchestration_mode") or ""
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        if not worker_description or not justification or not orchestration_mode:
            return _dynamic_tool_text_result(
                "worker_description, justification and orchestration_mode are required",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "worker_description": worker_description,
                    "justification": justification,
                    "orchestration_mode": orchestration_mode,
                    "cwd": cwd,
                    "client_request_id": f"cw_{uuid.uuid4().hex[:10]}",
                    "node_id": node_id,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/create-worker",
            )
        except Exception as e:
            logger.exception("create_worker dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_worker failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return create_worker


def _build_ensure_named_worker_dynamic_tool() -> dict:
    return {
        "name": "ensure_named_worker",
        "description": _ENSURE_NAMED_WORKER_DESCRIPTION,
        "inputSchema": _ENSURE_NAMED_WORKER_INPUT_SCHEMA,
    }


def _build_ensure_named_worker_tool_handler(
    *,
    cwd: str,
    backend_url: str,
    internal_token: str,
):
    async def ensure_named_worker(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result(
                "ensure_named_worker arguments must be an object",
                success=False,
            )
        name = str(args.get("name") or "").strip()
        worker_cwd = str(args.get("cwd") or cwd or "").strip()
        orchestration_mode = str(args.get("orchestration_mode") or "").strip()
        if not name or not orchestration_mode:
            return _dynamic_tool_text_result(
                "name and orchestration_mode are required",
                success=False,
            )
        if not worker_cwd:
            return _dynamic_tool_text_result("cwd is required", success=False)
        if orchestration_mode == "manager":
            orchestration_mode = "team"
        if orchestration_mode not in ("team", "native"):
            return _dynamic_tool_text_result(
                "orchestration_mode must be 'team' or 'native'",
                success=False,
            )
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        spec = {
            "role_key": name,
            "description": args.get("description") or f"worker:{name}",
            "orchestration_mode": orchestration_mode,
            "provision_prompt": args.get("provision_prompt"),
            "provider_id": args.get("provider_id"),
            "model": args.get("model"),
            "reasoning_effort": args.get("reasoning_effort"),
            "node_id": node_id,
            "tags": [name],
        }
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {"cwd": worker_cwd, "workers": [spec]},
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/workers/provision",
            )
        except Exception as e:
            logger.exception("ensure_named_worker dynamic tool handler failed")
            return _dynamic_tool_text_result(f"ensure_named_worker failed: {e}", success=False)
        workers = (result or {}).get("workers") or []
        if not workers:
            return _dynamic_tool_text_result(
                "ensure_named_worker provision returned no worker",
                success=False,
            )
        worker = workers[0]
        return _dynamic_tool_json_result(
            {
                "agent_session_id": worker.get("agent_session_id"),
                "name": worker.get("name"),
                "created": bool(worker.get("created")),
                "orchestration_mode": worker.get("orchestration_mode"),
                "registry_cwd": worker.get("registry_cwd") or worker.get("cwd"),
            },
            success=True,
        )

    return ensure_named_worker


def _build_open_file_panel_dynamic_tool() -> dict:
    return {
        "name": "open_file_panel",
        "description": _OPEN_FILE_PANEL_DESCRIPTION,
        "inputSchema": _OPEN_FILE_PANEL_INPUT_SCHEMA,
    }


def _build_start_file_discussion_dynamic_tool() -> dict:
    return {
        "name": "start_file_discussion",
        "description": _START_FILE_DISCUSSION_DESCRIPTION,
        "inputSchema": _START_FILE_DISCUSSION_INPUT_SCHEMA,
    }


def _build_mssg_dynamic_tool() -> dict:
    return {
        "name": "mssg",
        "description": _MSSG_DESCRIPTION,
        "inputSchema": _MSSG_INPUT_SCHEMA,
    }


def _build_ask_dynamic_tool() -> dict:
    return {
        "name": "ask",
        "description": _ASK_DESCRIPTION,
        "inputSchema": _ASK_INPUT_SCHEMA,
    }


def _build_list_available_provider_models_dynamic_tool() -> dict:
    return {
        "name": "list_available_provider_models",
        "description": _LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION,
        "inputSchema": _LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA,
    }


def _build_delegate_task_dynamic_tool() -> dict:
    return {
        "name": "delegate_task",
        "description": _DELEGATE_TASK_DESCRIPTION,
        "inputSchema": _DELEGATE_TASK_INPUT_SCHEMA,
    }


def _build_create_session_dynamic_tool() -> dict:
    return {
        "name": "create_session",
        "description": _CREATE_SESSION_DESCRIPTION,
        "inputSchema": _CREATE_SESSION_INPUT_SCHEMA,
    }


def _build_create_sub_session_dynamic_tool() -> dict:
    return {
        "name": "create_sub_session",
        "description": _CREATE_SUB_SESSION_DESCRIPTION,
        "inputSchema": _CREATE_SUB_SESSION_INPUT_SCHEMA,
    }


def _build_chat_dynamic_tool() -> dict:
    return {
        "name": "chat",
        "description": _CHAT_DESCRIPTION,
        "inputSchema": _CHAT_INPUT_SCHEMA,
    }


def _build_create_chat_dynamic_tool() -> dict:
    return {
        "name": "create_chat",
        "description": _CREATE_CHAT_DESCRIPTION,
        "inputSchema": _CREATE_CHAT_INPUT_SCHEMA,
    }


def _build_delete_chat_dynamic_tool() -> dict:
    return {
        "name": "delete_chat",
        "description": _DELETE_CHAT_DESCRIPTION,
        "inputSchema": _DELETE_CHAT_INPUT_SCHEMA,
    }


def _build_mssg_tool_handler(
    *,
    sender_session_id: str,
    backend_url: str,
    internal_token: str,
):
    async def mssg(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("mssg arguments must be an object", success=False)
        target_session_id = str(args.get("target_session_id") or "").strip()
        target_worker_id = str(args.get("target_worker_id") or "").strip()
        target_worker_pool = str(args.get("target_worker_pool") or "").strip()
        pool_affinity_key = str(args.get("pool_affinity_key") or "").strip()
        message = str(args.get("message") or "").strip()
        if (not target_session_id and not target_worker_id and not target_worker_pool) or not message:
            return _dynamic_tool_text_result(
                "one target and message are required",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": sender_session_id,
                    "target_session_id": target_session_id,
                    "target_worker_id": target_worker_id,
                    "target_worker_pool": target_worker_pool,
                    "pool_affinity_key": pool_affinity_key,
                    "message": message,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "collapse_key": str(args.get("collapse_key") or "").strip(),
                    "collapse_policy": str(args.get("collapse_policy") or "").strip(),
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/mssg",
                timeout_s=30.0,
            )
        except Exception as e:
            logger.exception("mssg dynamic tool handler failed")
            return _dynamic_tool_text_result(f"mssg failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return mssg



def _build_chat_tool_handler(*, sender_session_id: str):
    async def chat(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("chat arguments must be an object", success=False)
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return _dynamic_tool_text_result("chat_id is required", success=False)
        message = str(args.get("message") or "")
        try:
            result = await asyncio.to_thread(
                chat_store.post_and_read,
                chat_id=chat_id,
                reader_id=sender_session_id,
                message=message,
            )
        except Exception as e:
            logger.exception("chat dynamic tool handler failed")
            return _dynamic_tool_text_result(f"chat failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return chat


def _build_create_chat_tool_handler(*, sender_session_id: str):
    async def create_chat(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("create_chat arguments must be an object", success=False)
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return _dynamic_tool_text_result("chat_id is required", success=False)
        name = str(args.get("name") or "").strip()
        try:
            result = await asyncio.to_thread(
                chat_store.create_chat,
                chat_id=chat_id,
                created_by=sender_session_id,
                name=name,
            )
        except Exception as e:
            logger.exception("create_chat dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_chat failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return create_chat


def _build_delete_chat_tool_handler():
    async def delete_chat(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("delete_chat arguments must be an object", success=False)
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return _dynamic_tool_text_result("chat_id is required", success=False)
        try:
            existed = await asyncio.to_thread(chat_store.delete_chat, chat_id)
        except Exception as e:
            logger.exception("delete_chat dynamic tool handler failed")
            return _dynamic_tool_text_result(f"delete_chat failed: {e}", success=False)
        return _dynamic_tool_json_result({"chat_id": chat_id, "deleted": existed}, success=True)

    return delete_chat


def _build_list_available_provider_models_tool_handler():
    async def list_available_provider_models(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result(
                "list_available_provider_models arguments must be an object",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                available_provider_models_response,
                str(args.get("provider") or ""),
                str(args.get("model") or ""),
                str(args.get("reasoning_effort") or ""),
            )
        except Exception as e:
            logger.exception("list_available_provider_models dynamic tool handler failed")
            return _dynamic_tool_text_result(
                f"list_available_provider_models failed: {e}",
                success=False,
            )
        return _dynamic_tool_json_result(result, success=True)

    return list_available_provider_models


def _build_delegate_task_tool_handler(
    *,
    sender_session_id: str,
    cwd: str,
    model: Optional[str],
    backend_url: str,
    internal_token: str,
):
    async def delegate_task(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("delegate_task arguments must be an object", success=False)
        task = str(args.get("task") or "").strip()
        if not task:
            return _dynamic_tool_text_result("task is required", success=False)
        target = args.get("target_session_id")
        if target in ("", "null"):
            target = None
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": sender_session_id,
                    "task": task,
                    "target_session_id": target,
                    "cwd": cwd,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "sub_session": args.get("sub_session") is not False,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/delegate-task",
                timeout_s=DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("delegate_task dynamic tool handler failed")
            return _dynamic_tool_text_result(f"delegate_task failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return delegate_task


def _build_create_session_tool_handler(
    *,
    sender_session_id: str,
    cwd: str,
    model: Optional[str],
    backend_url: str,
    internal_token: str,
):
    async def create_session(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("create_session arguments must be an object", success=False)
        name = str(args.get("name") or "").strip()
        if not name:
            return _dynamic_tool_text_result("name is required", success=False)
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": sender_session_id,
                    "name": name,
                    "cwd": cwd,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "orchestration_mode": args.get("orchestration_mode") or "native",
                    "node_id": node_id,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/create-session",
                timeout_s=30.0,
            )
        except Exception as e:
            logger.exception("create_session dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_session failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return create_session


def _build_create_sub_session_tool_handler(
    *,
    sender_session_id: str,
    cwd: str,
    model: Optional[str],
    backend_url: str,
    internal_token: str,
):
    async def create_sub_session(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result(
                "create_sub_session arguments must be an object",
                success=False,
            )
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "sender_session_id": sender_session_id,
                    "description": str(args.get("description") or "").strip(),
                    "cwd": cwd,
                    "provider_id": str(args.get("provider_id") or "").strip() or None,
                    "model": str(args.get("model") or "").strip(),
                    "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                    "node_id": node_id,
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/create-sub-session",
                timeout_s=30.0,
            )
        except Exception as e:
            logger.exception("create_sub_session dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_sub_session failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return create_sub_session


def _build_ask_tool_handler(
    *,
    sender_session_id: str,
    app_session_id: str,
    model: Optional[str],
    cwd: str,
    backend_url: str,
    internal_token: str,
):
    async def ask(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("ask arguments must be an object", success=False)
        target_session_id = str(args.get("target_session_id") or "").strip()
        target_worker_id = str(args.get("target_worker_id") or "").strip()
        target_worker_pool = str(args.get("target_worker_pool") or "").strip()
        pool_affinity_key = str(args.get("pool_affinity_key") or "").strip()
        message = str(args.get("message") or "").strip()
        run_mode = str(args.get("run_mode") or "direct").strip() or "direct"
        try:
            mode = normalize_ask_mode(args.get("mode"))
        except ValueError as exc:
            return _dynamic_tool_text_result(str(exc), success=False)
        if (not target_session_id and not target_worker_id and not target_worker_pool) or not message:
            return _dynamic_tool_text_result(
                "one target and message are required",
                success=False,
            )
        if run_mode not in ("direct", "fork"):
            return _dynamic_tool_text_result("run_mode must be 'direct' or 'fork'", success=False)
        if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC and run_mode == "fork":
            return _dynamic_tool_text_result("async ask mode requires run_mode='direct'", success=False)
        ephemeral = bool(args.get("ephemeral"))
        if ephemeral and run_mode != "fork":
            return _dynamic_tool_text_result(
                "ephemeral is only valid for run_mode='fork'",
                success=False,
            )

        if run_mode == "fork":
            if not target_session_id:
                return _dynamic_tool_text_result("run_mode='fork' requires target_session_id", success=False)
            # Fork reuses the delegation engine (per-(caller, session) branch +
            # byte-offset outcome). ask is the model-facing name; the fork
            # execution path stays single-source in run_delegation.
            worker_description = str(args.get("worker_description") or "").strip()
            worker_registry_cwd = args.get("worker_registry_cwd")
            if worker_registry_cwd in ("", "null"):
                worker_registry_cwd = None
            client_delegation_id = f"del_{uuid.uuid4().hex[:10]}"
            payload = {
                "app_session_id": app_session_id,
                "instructions": message,
                "worker_session_id": target_session_id,
                "worker_description": worker_description,
                "provider_id": str(args.get("provider_id") or "").strip() or None,
                "model": str(args.get("model") or "").strip() or model,
                "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
                "cwd": cwd,
                "client_delegation_id": client_delegation_id,
                "run_mode": "fork",
                "worker_registry_cwd": worker_registry_cwd,
                "ephemeral": ephemeral,
            }
            url_path = "/api/internal/ask-fork"
        else:
            ask_id = f"ask_{uuid.uuid4().hex[:10]}"
            payload = {
                "sender_session_id": sender_session_id,
                "target_session_id": target_session_id,
                "target_worker_id": target_worker_id,
                "target_worker_pool": target_worker_pool,
                "pool_affinity_key": pool_affinity_key,
                "message": message,
                "ask_id": ask_id,
                "mode": mode,
                "provider_id": str(args.get("provider_id") or "").strip() or None,
                "model": str(args.get("model") or "").strip(),
                "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
            }
            url_path = "/api/internal/ask"

        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                payload,
                backend_url=backend_url,
                internal_token=internal_token,
                url_path=url_path,
                timeout_s=30.0 if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC else DELEGATE_HTTP_TIMEOUT_S,
            )
        except Exception as e:
            logger.exception("ask dynamic tool handler failed")
            return _dynamic_tool_text_result(f"ask failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return ask


def _build_open_file_panel_tool_handler(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
):
    async def open_file_panel(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result(
                "open_file_panel arguments must be an object",
                success=False,
            )
        mode = str(args.get("mode") or "").strip()
        path = str(args.get("path") or "").strip()
        if mode not in ("panel", "inline") or not path:
            return _dynamic_tool_text_result(
                "`mode` (panel|inline) and `path` are required",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "mode": mode,
                    "path": path,
                    "start_line": args.get("start_line"),
                    "end_line": args.get("end_line"),
                    "selected_start": args.get("selected_start"),
                    "selected_end": args.get("selected_end"),
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/open-file-panel",
                timeout_s=10.0,
            )
        except Exception as e:
            logger.exception("open_file_panel dynamic tool handler failed")
            return _dynamic_tool_text_result(f"open_file_panel failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return open_file_panel


def _build_start_file_discussion_tool_handler(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
):
    async def start_file_discussion(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result(
                "start_file_discussion arguments must be an object",
                success=False,
            )
        file_path = str(args.get("file_path") or "").strip()
        line = args.get("line")
        if not file_path or not isinstance(line, int) or line < 1:
            return _dynamic_tool_text_result(
                "`file_path` and `line >= 1` are required",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                {
                    "app_session_id": app_session_id,
                    "file_path": file_path,
                    "line": line,
                    "title": args.get("title") or "",
                },
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/file-editor/start-discussion",
                timeout_s=10.0,
            )
        except Exception as e:
            logger.exception("start_file_discussion dynamic tool handler failed")
            return _dynamic_tool_text_result(f"start_file_discussion failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return start_file_discussion


def _build_dynamic_tool_set(
    *,
    mode: str,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
    mssg_sender_session_id: str,
    cwd: str,
    model: Optional[str],
    open_file_panel_enabled: bool,
    file_editing_mode: bool,
    team_orchestration_enabled: bool,
    disabled_builtin_tools: set[str],
    existing_tool_names: set[str],
) -> tuple[list[dict], dict[str, Any]]:
    dynamic_tools: list[dict] = []
    tool_handlers: dict[str, Any] = {}
    if mode == "manager" and team_orchestration_enabled:
        if not app_session_id or not backend_url or not internal_token:
            raise RuntimeError(t("runner.manager_mode_missing_fields"))
        _add_dynamic_tool(
            dynamic_tools,
            tool_handlers,
            _build_create_worker_dynamic_tool(),
            _build_create_worker_tool_handler(
                app_session_id=app_session_id,
                backend_url=backend_url,
                internal_token=internal_token,
                model=model,
                cwd=cwd,
            ),
            existing_tool_names=existing_tool_names,
        )
    if open_file_panel_enabled:
        if not app_session_id or not backend_url or not internal_token:
            raise RuntimeError("open-file-panel requires app_session_id, backend_url, and internal_token")
        _add_dynamic_tool(
            dynamic_tools,
            tool_handlers,
            _build_open_file_panel_dynamic_tool(),
            _build_open_file_panel_tool_handler(
                app_session_id=app_session_id,
                backend_url=backend_url,
                internal_token=internal_token,
            ),
            existing_tool_names=existing_tool_names,
        )
        if file_editing_mode:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_start_file_discussion_dynamic_tool(),
                _build_start_file_discussion_tool_handler(
                    app_session_id=app_session_id,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
    if mssg_sender_session_id and backend_url and internal_token:
        if "mssg" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_mssg_dynamic_tool(),
                _build_mssg_tool_handler(
                    sender_session_id=mssg_sender_session_id,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
        if "ask" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_ask_dynamic_tool(),
                _build_ask_tool_handler(
                    sender_session_id=mssg_sender_session_id,
                    app_session_id=app_session_id or "",
                    model=model,
                    cwd=cwd,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
        if "ensure_named_worker" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_ensure_named_worker_dynamic_tool(),
                _build_ensure_named_worker_tool_handler(
                    cwd=cwd,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
        if "list_available_provider_models" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_list_available_provider_models_dynamic_tool(),
                _build_list_available_provider_models_tool_handler(),
                existing_tool_names=existing_tool_names,
            )
        if "chat" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_chat_dynamic_tool(),
                _build_chat_tool_handler(sender_session_id=mssg_sender_session_id),
                existing_tool_names=existing_tool_names,
            )
        if "create_chat" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_create_chat_dynamic_tool(),
                _build_create_chat_tool_handler(sender_session_id=mssg_sender_session_id),
                existing_tool_names=existing_tool_names,
            )
        if "delete_chat" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_delete_chat_dynamic_tool(),
                _build_delete_chat_tool_handler(),
                existing_tool_names=existing_tool_names,
            )
    if app_session_id and backend_url and internal_token:
        if "delegate_task" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_delegate_task_dynamic_tool(),
                _build_delegate_task_tool_handler(
                    sender_session_id=app_session_id,
                    cwd=cwd,
                    model=model,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
        if "create_session" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_create_session_dynamic_tool(),
                _build_create_session_tool_handler(
                    sender_session_id=app_session_id,
                    cwd=cwd,
                    model=model,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
        if "create_sub_session" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_create_sub_session_dynamic_tool(),
                _build_create_sub_session_tool_handler(
                    sender_session_id=app_session_id,
                    cwd=cwd,
                    model=model,
                    backend_url=backend_url,
                    internal_token=internal_token,
                ),
                existing_tool_names=existing_tool_names,
            )
    return dynamic_tools, tool_handlers


class _MappedNotificationStream:
    def __init__(self, queue: asyncio.Queue[Optional[bytes]]) -> None:
        self._queue = queue

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class _AppServerProcess:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        run_dir: Path,
        tool_handlers: Optional[dict[str, Any]] = None,
        approval_ctx: Optional[dict] = None,
    ) -> None:
        self._proc = proc
        self.pid = proc.pid
        self.stdin = proc.stdin
        self.stderr = proc.stderr
        self.returncode = proc.returncode
        self._run_dir = run_dir
        # When non-empty, interactive tool/command approvals round-trip to the
        # backend. Empty under approval_policy="never" (no approvals asked).
        self._approval_ctx = approval_ctx or {}
        self._responses: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._mapped: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self.stdout = _MappedNotificationStream(self._mapped)
        self._tool_handlers = tool_handlers or {}
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self._reader_task = asyncio.create_task(self._read_messages())
        self._steer_task = asyncio.create_task(self._watch_steer_inbox())

    async def request(
        self,
        method: str,
        params: dict,
        *,
        timeout_s: float = APP_SERVER_REQUEST_TIMEOUT_S,
    ) -> dict:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params})
        try:
            response = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as e:
            self._responses.pop(request_id, None)
            raise TimeoutError(f"codex app-server request timed out: {method}") from e
        if response.get("error"):
            raise RuntimeError(response["error"].get("message") or str(response["error"]))
        return response.get("result") or {}

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"method": method, "params": params})

    async def _send(self, message: dict) -> None:
        if self.stdin is None:
            raise RuntimeError("codex app-server stdin is closed")
        self.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.stdin.drain()

    @staticmethod
    def _is_closed_send_error(error: BaseException) -> bool:
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return True
        return isinstance(error, RuntimeError) and "stdin is closed" in str(error)

    async def _try_send_response(self, message: dict) -> bool:
        try:
            await self._send(message)
            return True
        except Exception as e:
            if self._is_closed_send_error(e):
                return False
            raise

    async def _read_messages(self) -> None:
        pending_terminal: Optional[dict] = None
        try:
            assert self._proc.stdout is not None
            while True:
                if pending_terminal is None:
                    raw = await self._proc.stdout.readline()
                else:
                    try:
                        raw = await asyncio.wait_for(
                            self._proc.stdout.readline(),
                            timeout=0.35,
                        )
                    except asyncio.TimeoutError:
                        await self._mapped.put(
                            (json.dumps(pending_terminal) + "\n").encode("utf-8")
                        )
                        pending_terminal = None
                        continue
                if not raw:
                    break
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is not None and request_id in self._responses:
                    future = self._responses.pop(request_id)
                    if not future.done():
                        future.set_result(message)
                    continue
                if request_id is not None:
                    handled = await self._handle_server_request(message)
                    if handled:
                        continue
                mapped = self._map_notification(message)
                if mapped is not None:
                    if mapped.get("type") in ("turn.completed", "turn.failed"):
                        pending_terminal = mapped
                        continue
                    await self._mapped.put((json.dumps(mapped) + "\n").encode("utf-8"))
        finally:
            if pending_terminal is not None:
                await self._mapped.put(
                    (json.dumps(pending_terminal) + "\n").encode("utf-8")
                )
            await self._mapped.put(None)

    async def _handle_server_request(self, message: dict) -> bool:
        method = message.get("method")
        # Codex app-server asks the client to approve a command/file-change
        # when approval_policy != "never". Round-trip to the backend →
        # frontend and reply with the user's decision. No ctx (never policy)
        # → not handled here (won't be asked).
        if method in (
            "execCommandApproval",
            "applyPatchApproval",
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            # Fire-and-forget: the reply is sent via self._send when the
            # decision lands. Awaiting here would block the single stdout
            # reader for up to the approval timeout, stalling all other
            # responses/notifications (and turn completion).
            asyncio.create_task(self._handle_codex_approval(message))
            return True
        if method != "item/tool/call":
            return False
        request_id = message.get("id")
        params = message.get("params") or {}
        tool_name = params.get("tool")
        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            await self._try_send_response({
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"unknown dynamic tool: {tool_name}",
                },
            })
            return True
        try:
            result = await handler(params)
        except Exception as e:
            await self._try_send_response({
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": str(e),
                },
            })
            return True
        await self._try_send_response({"id": request_id, "result": result})
        return True

    async def _handle_codex_approval(self, message: dict) -> None:
        """Reply to a codex approval ServerRequest with the user's decision.
        No approval_ctx (approval_policy=never) → deny (fail-closed). Any
        error still sends a denial so codex never hangs waiting for a reply
        that will never come (fail-closed)."""
        request_id = message.get("id")
        try:
            params = message.get("params") or {}
            method = message.get("method")
            summary = _codex_approval_summary(method, params)
            ctx = self._approval_ctx
            approved = False
            if ctx:
                approved = await asyncio.to_thread(
                    request_tool_approval,
                    backend_url=ctx.get("backend_url", ""),
                    internal_token=ctx.get("internal_token", ""),
                    app_session_id=ctx.get("app_session_id", ""),
                    run_id=ctx.get("run_id", ""),
                    provider_kind="codex",
                    tool_name=summary.get("tool", ""),
                    summary=summary,
                )
            decision = "approved" if approved else "denied"
        except Exception:
            logging.getLogger("runner_codex").exception("codex approval handler failed (denying)")
            decision = "denied"
        try:
            await self._try_send_response({
                "id": request_id,
                "result": {"decision": decision},
            })
        except Exception:
            logging.getLogger("runner_codex").exception("codex approval response send failed")

    def _map_notification(self, message: dict) -> Optional[dict]:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "thread/started":
            thread = params.get("thread") or {}
            self.thread_id = thread.get("id")
            return {"type": "thread.started", "thread_id": self.thread_id}
        if method == "turn/started":
            turn = params.get("turn") or {}
            self.turn_id = turn.get("id")
            return {"type": "turn.started", "turn_id": self.turn_id}
        if method == "turn/completed":
            turn = params.get("turn") or {}
            self.turn_id = None
            status = turn.get("status")
            if status == "completed":
                return {"type": "turn.completed", "usage": turn.get("usage") or {}}
            return {
                "type": "turn.failed",
                "error": {"message": str(turn.get("error") or status or "turn failed")},
            }
        if method in ("item/started", "item/updated", "item/completed"):
            return None
        if method == "error":
            error = params.get("error") or {}
            return {"type": "error", "message": error.get("message") or str(error)}
        return None

    async def _watch_steer_inbox(self) -> None:
        inbox = self._run_dir / "steer.jsonl"
        offset = 0
        while self._proc.returncode is None:
            try:
                if inbox.exists():
                    with inbox.open(encoding="utf-8") as f:
                        f.seek(offset)
                        while line := f.readline():
                            payload = json.loads(line)
                            if not self.thread_id or not self.turn_id:
                                raise RuntimeError("steer inbox consumed without active turn")
                            await self.request("turn/steer", {
                                "threadId": self.thread_id,
                                "expectedTurnId": self.turn_id,
                                "input": build_codex_steer_input(self._run_dir, payload),
                            })
                            offset = f.tell()
            except Exception:
                logging.getLogger("runner_codex").exception("codex steering failed")
            await asyncio.sleep(0.05)

    async def wait(self) -> int:
        code = await self._proc.wait()
        self.returncode = code
        self._steer_task.cancel()
        try:
            await asyncio.wait_for(self._reader_task, timeout=2.0)
        except asyncio.TimeoutError:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        except Exception:
            logging.getLogger("runner_codex").exception("codex app-server reader failed")
        return code


def _build_app_server_argv(
    codex_bin: str,
    profile: Optional[str],
    config_overrides: Optional[list[str]] = None,
) -> list[str]:
    """Build the `codex app-server` invocation.

    `profile` selects a codex profile (e.g. `fugu`) via `-p`, so providers
    that differ only by config.toml profile reuse the single `codex` binary
    instead of shipping a launcher. Global flags precede the subcommand.
    """
    argv = [codex_bin]
    if profile:
        argv += ["-p", profile]
    for override in config_overrides or []:
        argv += ["-c", override]
    argv.append("app-server")
    return argv


async def _start_app_server(
    codex_bin: str,
    *,
    run_dir: Path,
    cwd: str,
    model: Optional[str],
    reasoning_effort: Optional[str],
    session_id: Optional[str],
    fork: bool = False,
    turn_input: list[dict],
    dynamic_tools: Optional[list[dict]] = None,
    tool_handlers: Optional[dict[str, Any]] = None,
    provider_run_config: Optional[dict[str, Any]] = None,
    config_overrides: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    approval_policy: str = "never",
    sandbox: str = "danger-full-access",
    approval_ctx: Optional[dict] = None,
    profile: Optional[str] = None,
) -> _AppServerProcess:
    argv = _build_app_server_argv(codex_bin, profile, config_overrides)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **_process_control().detach_spawn_kwargs(),
        limit=16 * 1024 * 1024,
    )
    client = _AppServerProcess(proc, run_dir, tool_handlers=tool_handlers, approval_ctx=approval_ctx)
    try:
        await client.request("initialize", {
            "clientInfo": {
                "name": "better_agent",
                "title": "Better Agent",
                "version": "1",
            },
            "capabilities": {
                "experimentalApi": True,
            },
        })
        await client.notify("initialized", {})
        if session_id:
            capability_params = _codex_thread_capability_params(
                dynamic_tools=dynamic_tools,
                provider_run_config=provider_run_config,
            )
            if fork:
                # Fork branches the parent's rollout into a new, isolated
                # thread (`thread/fork`). The parent must have a persisted
                # rollout or codex returns "no rollout found".
                result = await client.request(
                    "thread/fork",
                    {"threadId": session_id, **capability_params},
                )
            else:
                result = await client.request(
                    "thread/resume",
                    {"threadId": session_id, **capability_params},
                )
        else:
            thread_start_params = {
                "cwd": cwd,
                "model": model,
                "approvalPolicy": approval_policy,
                "sandboxPolicy": _codex_sandbox_policy(sandbox),
                **_codex_thread_capability_params(
                    dynamic_tools=dynamic_tools,
                    provider_run_config=provider_run_config,
                ),
            }
            result = await client.request("thread/start", thread_start_params)
        thread = result.get("thread") or {}
        client.thread_id = thread.get("id") or session_id
        await client._mapped.put((json.dumps({
            "type": "thread.started",
            "thread_id": client.thread_id,
        }) + "\n").encode("utf-8"))
        await client.request("turn/start", {
            "threadId": client.thread_id,
            "input": turn_input,
            "cwd": cwd,
            "model": model,
            "effort": reasoning_effort,
            "approvalPolicy": approval_policy,
            "sandboxPolicy": _codex_sandbox_policy(sandbox),
        })
    except Exception:
        if proc.returncode is None:
            _process_control().signal_stop(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                _process_control().force_kill(proc.pid)
                await proc.wait()
        raise
    return client

logger = logging.getLogger(__name__)


def _resolve_codex_cli(inputs: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Find the codex CLI binary.

    Honors `inputs["codex_binary"]` (defaults to `codex`). Provider-specific
    selection that differs only by config.toml profile is handled separately
    via `inputs["codex_profile"]`, so every provider reuses the one binary.
    """
    from cli_paths import resolve_cli_binary

    binary = (inputs or {}).get("codex_binary") or "codex"
    resolved = resolve_cli_binary(binary)
    if os.name == "nt" and binary == "codex" and resolved:
        npm_dir = Path(resolved).parent
        vendor_root = npm_dir / "node_modules" / "@openai" / "codex" / "node_modules"
        if vendor_root.is_dir():
            for candidate in sorted(vendor_root.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe")):
                if candidate.is_file():
                    return str(candidate)
    return resolved


def _materialize_image_attachments(run_dir: Path, images: list) -> list[Path]:
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        ext = img["media_type"].split("/")[-1].replace("jpeg", "jpg")
        fpath = att_dir / f"attachment_{i}.{ext}"
        fpath.write_bytes(base64.b64decode(img["data"]))
        paths.append(fpath)
    return paths


def build_codex_turn_input(run_dir: Path, prompt: str, images: list) -> list[dict]:
    turn_input: list[dict] = []
    if prompt:
        turn_input.append({"type": "text", "text": prompt, "text_elements": []})
    for path in _materialize_image_attachments(run_dir, images) if images else []:
        turn_input.append({"type": "localImage", "path": str(path)})
    return turn_input


def _rollout_terminal_state(rollout_path: Optional[str]) -> tuple[Optional[bool], dict]:
    if not rollout_path:
        return None, {}
    path = Path(rollout_path)
    if not path.exists():
        return None, {}
    usage: dict = {}
    terminal: Optional[bool] = None
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    payload = item.get("payload") or {}
                    if item.get("type") != "event_msg" or not isinstance(payload, dict):
                        continue
                    payload_type = payload.get("type")
                    if payload_type == "token_count":
                        info = payload.get("info") or {}
                        usage = token_usage_from_codex_usage(
                            info.get("total_token_usage") if isinstance(info, dict) else info
                        ) or usage
                    elif payload_type == "task_complete":
                        terminal = True
                    elif payload_type in ("task_failed", "turn_failed"):
                        terminal = False
                except Exception:
                    continue
    except OSError:
        return None, usage
    return terminal, usage


async def _wait_rollout_terminal_state(
    rollout_path: Optional[str],
    *,
    timeout: float = 20.0,
    poll_interval: float = 0.25,
) -> tuple[Optional[bool], dict]:
    deadline = time.monotonic() + timeout
    last_usage: dict = {}
    while True:
        terminal, usage = _rollout_terminal_state(rollout_path)
        if usage:
            last_usage = usage
        if terminal is not None:
            return terminal, usage or last_usage
        if time.monotonic() >= deadline:
            return None, last_usage
        await asyncio.sleep(poll_interval)


def build_codex_steer_input(run_dir: Path, payload: dict) -> list[dict]:
    return build_codex_turn_input(
        run_dir,
        payload.get("prompt") or "",
        payload.get("images") or [],
    )


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _stable_uuid(namespace: str, key: str) -> str:
    """Deterministic UUID from (namespace, key). Same inputs → same
    UUID across re-emissions so `apply_event` REPLACEs the render-tree
    node in place instead of appending a card per update. `namespace`
    (the codex thread_id) keeps the reused `item_0` id from colliding
    across turns/sessions."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{key}"))


def _stable_payload_key(payload: dict) -> str:
    item_id = payload.get("id") or payload.get("call_id")
    if isinstance(item_id, str) and item_id:
        return item_id
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _response_item_uuid(parent_uuid: str, payload: dict, suffix: str = "") -> str:
    payload_type = payload.get("type") or "unknown"
    return _stable_uuid(
        parent_uuid,
        f"response_item:{payload_type}:{_stable_payload_key(payload)}{suffix}",
    )


def _file_size(path: Optional[Path]) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ============================================================================
# Tool name mapping — Codex → Claude
# ============================================================================
_TOOL_NAME_MAP = {
    "command_execution": "Bash",
    "file_change": "Edit",
    "mcp_tool_call": "MCP",
}

_CODEX_AGENT_TOOL_NAMES = {
    "spawn_agent",
    "spawn_agents",
    "spawn_agents_on_csv",
    "multi_agent.spawn_agent",
    "multi_agent_v1.spawn_agent",
}


# ============================================================================
# Event normalization — Codex ThreadEvent → Claude jsonl shape
# ============================================================================

def _normalize_agent_message(
    item: dict, parent_uuid: str, *, event_uuid: Optional[str] = None,
) -> dict:
    text = item.get("text", "")
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "codex",
        },
        "uuid": event_uuid or _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, item)


def _normalize_reasoning(
    item: dict, parent_uuid: str, *, event_uuid: Optional[str] = None,
) -> dict:
    text = item.get("text", "")
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": "codex",
        },
        "uuid": event_uuid or _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, item)


def _normalize_command_started(item: dict, parent_uuid: str) -> dict:
    command = item.get("command", "")
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id", _new_uuid()),
                "name": "Bash",
                "input": {"command": command},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_command_completed(item: dict, parent_uuid: str) -> dict:
    output = item.get("aggregated_output", "")
    exit_code = item.get("exit_code")
    status = item.get("status", "completed")
    content = output
    if status == "failed" and exit_code is not None and exit_code != 0:
        content = f"Error: exit code {exit_code}\n{output}"
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": content or "",
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_file_change(item: dict, parent_uuid: str) -> dict:
    changes = item.get("changes", [])
    status = item.get("status", "completed")
    parts = []
    for change in changes:
        path = change.get("path", "")
        kind = change.get("kind", "update")
        if kind == "delete":
            parts.append(f"Delete: {path}")
        elif kind == "add":
            parts.append(f"Add: {path}")
        else:
            parts.append(f"Update: {path}")
    description = "\n".join(parts)
    tool_name = "Edit" if status != "failed" else "Edit"
    result = description if status == "completed" else f"Failed: {description}"
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id", _new_uuid()),
                "name": tool_name,
                "input": {"description": description},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": result,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_mcp_tool_started(item: dict, parent_uuid: str) -> dict:
    tool_name = item.get("tool", "unknown")
    server = item.get("server", "")
    arguments = item.get("arguments", {})
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id", _new_uuid()),
                "name": f"mcp__{server}__{tool_name}" if server else tool_name,
                "input": arguments if isinstance(arguments, dict) else {"value": arguments},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_mcp_tool_completed(item: dict, parent_uuid: str) -> dict:
    error = item.get("error")
    result_data = item.get("result")
    content = ""
    if error:
        if isinstance(error, dict):
            content = f"Error: {error.get('message', str(error))}"
        else:
            content = f"Error: {error}"
    elif result_data:
        result_content = result_data.get("content", [])
        if isinstance(result_content, list):
            texts = []
            for c in result_content:
                if isinstance(c, dict):
                    texts.append(c.get("text", json.dumps(c)))
                else:
                    texts.append(str(c))
            content = "\n".join(texts)
        else:
            content = str(result_data)
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _collab_agent_description(item: dict) -> str:
    prompt = item.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    receivers = item.get("receiverThreadIds")
    if isinstance(receivers, list) and receivers:
        return f"{item.get('tool') or 'subagent'}: {', '.join(str(r) for r in receivers)}"
    return str(item.get("tool") or "subagent")


def _normalize_collab_agent_started(item: dict, parent_uuid: str) -> dict:
    description = _collab_agent_description(item)
    input_data = {
        "subagent_type": str(item.get("tool") or "default"),
        "description": description,
        "prompt": item.get("prompt") or description,
    }
    if item.get("model"):
        input_data["model"] = item["model"]
    if item.get("reasoningEffort"):
        input_data["reasoning_effort"] = item["reasoningEffort"]
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id") or _new_uuid(),
                "name": "Agent",
                "input": input_data,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_collab_agent_completed(item: dict, parent_uuid: str) -> dict:
    states = item.get("agentsStates")
    lines: list[str] = []
    if isinstance(states, dict):
        for thread_id, state in states.items():
            if not isinstance(state, dict):
                continue
            status = state.get("status")
            message = state.get("message")
            text = " ".join(str(v) for v in (status, message) if v)
            if text:
                lines.append(f"{thread_id}: {text}")
    content = "\n".join(lines) or str(item.get("status") or "completed")
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_web_search(item: dict, parent_uuid: str, tool_id: Optional[str] = None) -> dict:
    query = item.get("query", "")
    action = item.get("action", "")
    tool_use_id = tool_id or item.get("id") or _new_uuid()
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "WebSearch",
                "input": {"query": query, "action": action},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _web_search_result_text(item: dict) -> str:
    result_data = None
    for key in ("result", "results", "output", "content"):
        if key in item:
            result_data = item.get(key)
            break
    if not result_data:
        return ""

    if isinstance(result_data, str):
        return result_data.strip()

    if isinstance(result_data, list):
        parts = []
        for entry in result_data:
            if isinstance(entry, dict):
                title = entry.get("title") or entry.get("name") or ""
                url = entry.get("url") or entry.get("link") or ""
                snippet = (
                    entry.get("snippet")
                    or entry.get("text")
                    or entry.get("content")
                    or entry.get("summary")
                    or ""
                )
                line = " - ".join(str(v) for v in (title, url, snippet) if v)
                if line:
                    parts.append(line)
            elif entry is not None:
                parts.append(str(entry))
        return "\n".join(parts).strip()

    if isinstance(result_data, dict):
        content = result_data.get("content")
        if isinstance(content, list):
            texts = []
            for entry in content:
                if isinstance(entry, dict):
                    text = entry.get("text") or entry.get("content")
                    if text:
                        texts.append(str(text))
                elif entry is not None:
                    texts.append(str(entry))
            if texts:
                return "\n".join(texts).strip()
        for key in ("text", "snippet", "summary", "content"):
            value = result_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(result_data, ensure_ascii=False)

    return str(result_data).strip()


def _normalize_web_search_result(
    item: dict,
    parent_uuid: str,
    tool_id: Optional[str] = None,
) -> Optional[dict]:
    content = _web_search_result_text(item)
    if not content:
        return None
    tool_use_id = tool_id or item.get("id") or ""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_web_search_events(item: dict, parent_uuid: str) -> list[dict]:
    tool_use_id = item.get("id") or _new_uuid()
    tool_use = _normalize_web_search(item, parent_uuid, tool_use_id)
    tool_result = _normalize_web_search_result(item, tool_use["uuid"], tool_use_id)
    return [tool_use] + ([tool_result] if tool_result else [])


def _json_obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    if value is None:
        return {}
    return {"value": value}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _parent_tool_use_id(payload: dict) -> str:
    for key in (
        "parent_tool_use_id",
        "parentToolUseId",
        "parent_call_id",
        "parentCallId",
        "parent_item_id",
        "parentItemId",
        "parent_id",
        "parentId",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _with_parent_tool_use_id(event: dict, payload: dict) -> dict:
    parent_tool_use_id = _parent_tool_use_id(payload)
    if parent_tool_use_id:
        event["parent_tool_use_id"] = parent_tool_use_id
    return event


def _attach_collab_parent_from_thread(
    item: dict,
    collab_thread_parents: dict[str, str],
) -> dict:
    item_thread_id = item.get("threadId")
    if (
        isinstance(item_thread_id, str)
        and item_thread_id in collab_thread_parents
        and not _parent_tool_use_id(item)
    ):
        return {**item, "parentToolUseId": collab_thread_parents[item_thread_id]}
    return item


def _remember_collab_receivers(item: dict, collab_thread_parents: dict[str, str]) -> None:
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return
    receivers = item.get("receiverThreadIds")
    if not isinstance(receivers, list):
        return
    for receiver in receivers:
        if isinstance(receiver, str) and receiver:
            collab_thread_parents[receiver] = item_id


def _normalize_agent_args(args: dict) -> dict:
    prompt = _first_text(
        args.get("prompt"),
        args.get("message"),
        args.get("task"),
        args.get("description"),
    )
    description = _first_text(
        args.get("description"),
        args.get("task"),
        args.get("prompt"),
        args.get("message"),
    )
    subagent_type = _first_text(
        args.get("agent_type"),
        args.get("subagent_type"),
        args.get("type"),
        args.get("name"),
    ) or "default"
    normalized = dict(args)
    normalized["subagent_type"] = subagent_type
    if description:
        normalized["description"] = description
    if prompt:
        normalized["prompt"] = prompt
    return normalized


def _response_text_content(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _response_input_text_content(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _extract_subagent_notification(text: str) -> Optional[dict]:
    start = text.find("<subagent_notification>")
    end = text.find("</subagent_notification>")
    if start < 0 or end < 0 or end <= start:
        return None
    raw = text[start + len("<subagent_notification>"):end].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_subagent_notification(payload: dict, parent_uuid: str) -> Optional[dict]:
    notification = _extract_subagent_notification(_response_input_text_content(payload))
    if notification is None:
        return None
    agent_path = notification.get("agent_path")
    status = notification.get("status")
    content = status
    if not isinstance(content, str):
        content = json.dumps(status, ensure_ascii=False, default=str)
    return _with_parent_tool_use_id({
        "type": "user",
        "codex_subagent_id": str(agent_path or ""),
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": str(agent_path or ""),
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_response_message(payload: dict, parent_uuid: str) -> Optional[dict]:
    # Native Codex session files include developer/user history as
    # response_item.message records. Better Agent already owns user
    # message scaffolds, so only assistant output becomes render events.
    if payload.get("role") != "assistant":
        return _normalize_subagent_notification(payload, parent_uuid)
    text = _response_text_content(payload)
    if not text:
        return None
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "codex",
        },
        "uuid": _response_item_uuid(parent_uuid, payload),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_response_reasoning(payload: dict, parent_uuid: str) -> Optional[dict]:
    summary = payload.get("summary")
    if not isinstance(summary, list) or not summary:
        # Encrypted-only or absent reasoning has no renderable content.
        return None
    parts: list[str] = []
    for block in summary:
        if isinstance(block, dict):
            text = block.get("text") or block.get("summary")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    text = "\n".join(p for p in parts if p)
    if not text:
        return None
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": "codex",
        },
        "uuid": _response_item_uuid(parent_uuid, payload),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _native_payload_label(event_type: str, payload: dict) -> str:
    payload_type = payload.get("type")
    return f"{event_type}.{payload_type}" if payload_type else event_type


def _native_payload_text(event_type: str, payload: Any) -> str:
    try:
        body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except TypeError:
        body = str(payload)
    return f"Codex native {_native_payload_label(event_type, payload if isinstance(payload, dict) else {})}\n\n```json\n{body}\n```"


def _normalize_native_payload(event_type: str, payload: Any, parent_uuid: str) -> dict:
    event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": _native_payload_text(event_type, payload)}],
            "model": "codex",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }
    if isinstance(payload, dict):
        return _with_parent_tool_use_id(event, payload)
    return event


def _normalize_event_msg_text(payload: dict, parent_uuid: str, text: str) -> dict:
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "codex",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_event_msg_reasoning(payload: dict, parent_uuid: str, text: str) -> dict:
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": "codex",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_event_msg_patch_apply_end(payload: dict, parent_uuid: str) -> dict:
    output = payload.get("stdout") or payload.get("stderr") or ""
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, default=str)
    if payload.get("success") is False and output:
        output = f"Patch failed\n{output}"
    elif not output:
        output = "Patch applied" if payload.get("success") else "Patch finished"
    event, _ = _normalize_response_tool_result(
        {
            "type": "custom_tool_call_output",
            "call_id": payload.get("call_id") or payload.get("id") or "",
            "output": output,
        },
        parent_uuid,
    )
    return event


def _duration_text(duration_ms: object) -> Optional[str]:
    if not isinstance(duration_ms, int) or duration_ms < 1000:
        return None
    total_seconds = round(duration_ms / 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _event_msg_notice_text(payload: dict) -> str:
    payload_type = payload.get("type")
    if payload_type == "context_compacted":
        return "Context compacted"
    if payload_type == "turn_aborted":
        reason = payload.get("reason")
        text = "Turn interrupted" if reason == "interrupted" else "Turn aborted"
        duration = _duration_text(payload.get("duration_ms"))
        return f"{text} after {duration}" if duration else text
    return "Codex event"


def _normalize_event_msg_notice(payload: dict, parent_uuid: str) -> dict:
    return _with_parent_tool_use_id({
        "type": "lifecycle_notice",
        "data": {
            "kind": payload.get("type") or "codex_event",
            "message": _event_msg_notice_text(payload),
            "reason": payload.get("reason"),
            "duration_ms": payload.get("duration_ms"),
            "timestamp": datetime.now().isoformat(),
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
    }, payload)


def _normalize_response_tool_call(payload: dict, parent_uuid: str) -> tuple[dict, str]:
    tool_use_id = payload.get("call_id") or payload.get("id") or _new_uuid()
    payload_type = payload.get("type")
    name = payload.get("name") or "unknown"
    args = _json_obj(payload.get("arguments", payload.get("input")))

    if payload_type == "tool_search_call":
        name = "tool_search_tool"

    if name == "exec_command":
        name = "Bash"
        if "cmd" in args and "command" not in args:
            args = {**args, "command": args["cmd"]}
    elif name in _CODEX_AGENT_TOOL_NAMES:
        name = "Agent"
        args = _normalize_agent_args(args)
    elif name == "update_plan":
        # Codex's native planning tool: `plan: [{step, status}]`. Map to
        # Claude's TodoWrite shape so the Todos extension reconstructs it as
        # `current_todos` — same boundary-normalization pattern as
        # `_normalize_todo_list` (Codex stream item) and Gemini's
        # update_topic→TodoWrite rename. Status vocabulary is identical to
        # TodoWrite (pending/in_progress/completed), so it passes through.
        name, args = "TodoWrite", _codex_update_plan_to_todos(args)

    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": name,
                "input": args,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload), tool_use_id


def _codex_update_plan_to_todos(args: dict) -> dict:
    """Build a Claude `TodoWrite` input (`{"todos": [...]}`) from a Codex
    `update_plan` tool_call payload (`{"plan": [{"step","status"}]}`).

    `step` → `content`, `status` → `status` (Codex already uses the same
    pending/in_progress/completed vocabulary as TodoWrite). The optional
    `explanation` has no TodoWrite slot and is dropped.
    """
    raw_plan = args.get("plan") if isinstance(args, dict) else None
    todos: list[dict] = []
    if isinstance(raw_plan, list):
        for entry in raw_plan:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if not isinstance(status, str) or not status:
                status = "pending"
            todos.append({
                "content": str(entry.get("step", "") or ""),
                "status": status,
                "activeForm": None,
            })
    return {"todos": todos}


def _normalize_response_tool_result(payload: dict, parent_uuid: str) -> tuple[dict, str]:
    tool_use_id = payload.get("call_id") or payload.get("id") or ""
    output: Any = payload.get("output", "")
    if output == "":
        for key in ("result", "content", "tools"):
            if key in payload:
                output = payload.get(key)
                break
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    return _with_parent_tool_use_id({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": output,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload), tool_use_id

def _normalize_item(payload: dict, parent_uuid: str, provider: Optional[Any] = None) -> dict:
    # ... inside payload_type checks for tool outputs ...
    if payload_type in (
        "function_call_output",
        "custom_tool_call_output",
        "tool_search_output",
    ):
        event, _ = _normalize_response_tool_result(payload, parent_uuid)
        if provider:
            return provider.format_tool_result(event["tool_use_id"], event["content"])
        return event
    # ...


def _web_search_item_from_payload(payload: dict) -> dict:
    action = payload.get("action") or {}
    query = payload.get("query") or ""
    if not query and isinstance(action, dict):
        query = action.get("query") or action.get("url") or ""
    return {
        "id": payload.get("call_id") or payload.get("id") or _new_uuid(),
        "query": query,
        "action": action,
    }


def _web_search_dedupe_key(item: dict) -> str:
    return json.dumps(
        {"query": item.get("query", ""), "action": item.get("action", "")},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _normalize_response_item_event(payload: dict, parent_uuid: str) -> Optional[dict]:
    payload_type = payload.get("type")
    if payload_type == "message":
        return _normalize_response_message(payload, parent_uuid)
    if payload_type == "reasoning":
        return _normalize_response_reasoning(payload, parent_uuid)
    if payload_type in ("function_call", "custom_tool_call", "tool_search_call"):
        event, _ = _normalize_response_tool_call(payload, parent_uuid)
        event["uuid"] = _response_item_uuid(parent_uuid, payload, ":tool_use")
        return event
    if payload_type in (
        "function_call_output",
        "custom_tool_call_output",
        "tool_search_output",
    ):
        event, _ = _normalize_response_tool_result(payload, parent_uuid)
        event["uuid"] = _response_item_uuid(parent_uuid, payload, ":tool_result")
        return event
    if payload_type == "web_search_call":
        event = _normalize_web_search(_web_search_item_from_payload(payload), parent_uuid)
        event["uuid"] = _response_item_uuid(parent_uuid, payload, ":web_search")
        return event
    event = _normalize_native_payload("response_item", payload, parent_uuid)
    event["uuid"] = _response_item_uuid(parent_uuid, payload, ":native")
    return event


def _normalize_error_item(item: dict, parent_uuid: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"Error: {item.get('message', 'unknown error')}"}],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
        "isStreamError": True,
    }


def _normalize_todo_list(
    item: dict, parent_uuid: str, event_uuid: str,
) -> Optional[dict]:
    """Normalize a Codex `todo_list` stream item to a Claude-shaped
    `TodoWrite` tool_use event (full-list snapshot → REPLACE semantics).

    Codex emits a single todo_list item (stable `id`, e.g. `item_0`)
    that mutates in place across item.started → item.updated →
    item.completed; each emission carries the WHOLE list with a binary
    `completed` flag per entry. Mapping to Claude's `TodoWrite` todos[]
    lets the Todos extension REPLACE `current_todos` on every emission.
    `event_uuid` is stable per (run, item id) so `apply_event` REPLACEs
    the render-tree node rather than appending a card per update.

    Status: Codex's stream has no `in_progress` — the FIRST
    not-completed entry is surfaced as `in_progress` (matches Codex's
    own TUI active-step rendering and the Gemini in_progress heuristic);
    remaining not-completed → `pending`; completed → `completed`.
    """
    raw_items = item.get("items")
    if not isinstance(raw_items, list):
        return None
    todos: list[dict] = []
    in_progress_assigned = False
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        if entry.get("completed"):
            status = "completed"
        elif not in_progress_assigned:
            status = "in_progress"
            in_progress_assigned = True
        else:
            status = "pending"
        todos.append({
            "content": entry.get("text") or "",
            "status": status,
            "activeForm": None,
        })
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id") or _new_uuid(),
                "name": "TodoWrite",
                "input": {"todos": todos},
            }],
        },
        "uuid": event_uuid,
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


# ============================================================================
# Network error detection
# ============================================================================
_NETWORK_ERROR_PATTERN = re.compile(
    r"(?:"
    r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|"
    r"ENOTFOUND|EAI_NONAME|getaddrinfo|could not resolve|"
    r"socket hang up|network error|"
    r"connect ETIMEDOUT|connect ECONNREFUSED|"
    r"TLS handshake|SSL handshake|"
    r"HTTP 50[23]|HTTP 429|"
    r"rate.?limit|overloaded|temporarily unavailable|"
    r"service unavailable|bad gateway"
    r")",
    re.IGNORECASE,
)

def _is_network_error_message(msg: str) -> bool:
    return bool(_NETWORK_ERROR_PATTERN.search(msg))


def _sum_usage(a: Optional[dict], b: Optional[dict]) -> dict:
    out: dict[str, int] = {}
    for d in ((a or {}), (b or {})):
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)):
                out[k] = int(out.get(k, 0) + int(v))
    return out


def _prepend_capability_context(prompt: str, inputs: dict) -> str:
    return prepend_capability_context(prompt, inputs)


# ============================================================================
# Main async runner
# ============================================================================
async def _run(run_dir: Path, inputs: dict) -> int:
    log = logging.getLogger("runner_codex")

    mode = inputs.get("mode", "native")
    if mode == "team":
        mode = "manager"
    if mode not in ("native", "manager"):
        _fail(run_dir, f"mode must be 'native' or 'team', got {mode!r}")
        return 1
    prompt = inputs.get("prompt") or ""
    images = inputs.get("images") or []
    bare_config = bool(inputs.get("bare_config"))
    cwd = inputs.get("cwd")
    if not cwd:
        _fail(run_dir, "missing required field: cwd")
        return 1
    if not prompt and not images:
        _fail(run_dir, "missing required field: prompt")
        return 1
    prompt = _prepend_capability_context(prompt, inputs)

    model = inputs.get("model")
    reasoning_effort = inputs.get("reasoning_effort")
    permission = inputs.get("permission") or {}
    session_id = inputs.get("session_id")
    fork = bool(inputs.get("fork"))
    app_session_id = inputs.get("app_session_id") or ""
    runner_inputs = _codex_runner_inputs(inputs)
    provider_run_config = with_builtin_mcp_servers(
        runner_inputs,
        inputs.get("provider_run_config") or {},
    )
    run_env = os.environ.copy()
    run_env.update(native_mcp_runtime_env(runner_inputs))
    run_env.update(_materialize_codex_run_home(
        run_dir,
        provider_run_config,
        cwd=cwd,
        bare_config=bare_config,
    ))
    backend_url = inputs.get("backend_url") or get_env(
        "BETTER_CLAUDE_BACKEND_URL",
        "http://localhost:8000",
    )
    internal_token = inputs.get("internal_token") or ""
    mssg_sender_session_id = str(
        inputs.get("mssg_sender_session_id") or app_session_id or ""
    ).strip()
    continuation_chain = inputs.get("continuation_chain") or []
    disabled_builtin_tools = _disabled_builtin_tools(inputs)
    team_orchestration_enabled = extension_store.is_extension_runtime_ready(
        extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
    )
    existing_tool_names = _codex_existing_tool_names(provider_run_config)
    open_file_panel_enabled = bool(inputs.get("open_file_panel_enabled"))
    file_editing_mode = inputs.get("working_mode") == "file_editing"
    try:
        dynamic_tools, tool_handlers = _build_dynamic_tool_set(
            mode=mode,
            app_session_id=app_session_id,
            backend_url=backend_url,
            internal_token=internal_token,
            mssg_sender_session_id=mssg_sender_session_id,
            cwd=cwd,
            model=model,
            open_file_panel_enabled=open_file_panel_enabled,
            file_editing_mode=file_editing_mode,
            team_orchestration_enabled=team_orchestration_enabled,
            disabled_builtin_tools=disabled_builtin_tools,
            existing_tool_names=existing_tool_names,
        )
    except RuntimeError as exc:
        _fail(run_dir, str(exc))
        return 1

    codex_bin = _resolve_codex_cli(inputs)
    if not codex_bin:
        _fail(run_dir, "codex CLI not found on PATH")
        return 1

    turn_input = build_codex_turn_input(run_dir, prompt, images)
    from codex_native import resolve_rollout_path
    initial_rollout_path = resolve_rollout_path(session_id or "")
    initial_byte_offset = _file_size(initial_rollout_path)

    state: dict = {
        "run_id": run_dir.name,
        "mode": mode,
        "runner_pid": os.getpid(),
        "app_session_id": app_session_id,
        "started_at": datetime.now().isoformat(),
        "session_id": session_id,
        "jsonl_path": str(initial_rollout_path) if initial_rollout_path else None,
        "rollout_path": str(initial_rollout_path) if initial_rollout_path else None,
        "pre_query_byte_offset": initial_byte_offset,
        "complete": False,
    }
    state_path = run_dir / "state.json"

    _retry_backoff = 2.0
    _accumulated_usage: dict = {}
    _cancel_path = run_dir / "cancel"

    async def _retry_sleep(seconds: float) -> None:
        import time as _time
        deadline = _time.monotonic() + seconds
        while _time.monotonic() < deadline:
            if _cancel_path.exists():
                raise asyncio.CancelledError()
            await asyncio.sleep(min(0.5, deadline - _time.monotonic()))

    while True:
        discovered_sid: Optional[str] = None
        total_usage: dict = {}
        success = False
        error: Optional[str] = None
        cancelled = False
        turn_completed_seen = False
        interrupt_timeout_task: Optional[asyncio.Task] = None

        state["session_id"] = session_id
        state["jsonl_path"] = str(initial_rollout_path) if initial_rollout_path else None
        state["rollout_path"] = str(initial_rollout_path) if initial_rollout_path else None
        state["pre_query_byte_offset"] = initial_byte_offset
        state["complete"] = False

        try:
            proc = await _start_app_server(
                codex_bin,
                run_dir=run_dir,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                session_id=session_id,
                fork=fork,
                turn_input=turn_input,
                dynamic_tools=dynamic_tools,
                tool_handlers=tool_handlers,
                provider_run_config=provider_run_config,
                config_overrides=[
                    *list((inputs or {}).get("codex_config_overrides") or []),
                    *_codex_config_overrides(run_dir, provider_run_config),
                    *_context_strategy_config_overrides(inputs),
                ],
                env=run_env,
                approval_policy=_codex_approval_policy(permission),
                sandbox=_codex_sandbox_mode(permission),
                approval_ctx=(
                    {
                        "backend_url": backend_url,
                        "internal_token": internal_token,
                        "app_session_id": app_session_id,
                        "run_id": run_dir.name,
                    }
                    if _codex_approval_policy(permission) != "never"
                    and backend_url and internal_token and app_session_id
                    else None
                ),
                profile=(inputs or {}).get("codex_profile") or None,
            )

            cancel_seen = asyncio.Event()
            interrupt_terminal_seen = asyncio.Event()

            async def _drain_stderr() -> None:
                try:
                    with (run_dir / "codex_stderr.log").open("ab") as f:
                        while True:
                            chunk = await proc.stderr.read(8192)
                            if not chunk:
                                return
                            f.write(chunk)
                            f.flush()
                except Exception:
                    log.exception("codex stderr drain failed")

            stderr_task = asyncio.create_task(_drain_stderr())

            async def _cancel_watcher() -> None:
                nonlocal cancelled, interrupt_timeout_task
                while not cancel_seen.is_set():
                    if _cancel_path.exists():
                        cancelled = True
                        log.info("cancel sentinel seen, interrupting codex turn")
                        if proc.thread_id and proc.turn_id:
                            try:
                                await proc.request("turn/interrupt", {
                                    "threadId": proc.thread_id,
                                    "turnId": proc.turn_id,
                                })
                            except Exception:
                                log.exception("codex turn/interrupt failed")

                            async def _force_stop_if_interrupt_hangs() -> None:
                                try:
                                    await asyncio.wait_for(
                                        interrupt_terminal_seen.wait(), timeout=15.0,
                                    )
                                except asyncio.TimeoutError:
                                    if proc.returncode is None:
                                        _process_control().signal_stop(proc.pid)

                            interrupt_timeout_task = asyncio.create_task(
                                _force_stop_if_interrupt_hangs()
                            )
                        elif proc.returncode is None:
                            _process_control().signal_stop(proc.pid)
                        cancel_seen.set()
                        return
                    try:
                        await asyncio.wait_for(cancel_seen.wait(), timeout=0.15)
                    except asyncio.TimeoutError:
                        pass

            cancel_task = asyncio.create_task(_cancel_watcher())

            try:
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        raw_event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = raw_event.get("type")

                    if event_type == "thread.started":
                        thread_id = raw_event.get("thread_id")
                        if thread_id:
                            discovered_sid = thread_id
                            from codex_native import resolve_rollout_path_polled
                            rollout_path = await resolve_rollout_path_polled(thread_id)
                            state["session_id"] = thread_id
                            state["jsonl_path"] = str(rollout_path) if rollout_path else None
                            state["rollout_path"] = str(rollout_path) if rollout_path else None
                            if not initial_byte_offset:
                                state["pre_query_byte_offset"] = _file_size(rollout_path)
                            atomic_write_json(state_path, state)
                        continue

                    if event_type == "turn.started":
                        state["turn_id"] = raw_event.get("turn_id")
                        atomic_write_json(state_path, state)
                        continue

                    if event_type == "turn.completed":
                        state["turn_id"] = None
                        atomic_write_json(state_path, state)
                        turn_completed_seen = True
                        success = True
                        error = None
                        total_usage = token_usage_from_codex_usage(raw_event.get("usage")) or {}
                        break

                    if event_type == "turn.failed":
                        state["turn_id"] = None
                        atomic_write_json(state_path, state)
                        turn_completed_seen = True
                        err_data = raw_event.get("error", {})
                        error = (
                            normalize_context_overflow_error(err_data.get("message"))
                            or err_data.get("message", "turn failed")
                        )
                        break

                    if event_type == "error":
                        new_err = raw_event.get("message", "")
                        if new_err and not error:
                            error = normalize_context_overflow_error(new_err) or new_err
                        continue

            finally:
                cancel_seen.set()
                interrupt_terminal_seen.set()
                if interrupt_timeout_task is not None and not interrupt_timeout_task.done():
                    interrupt_timeout_task.cancel()
                    try:
                        await interrupt_timeout_task
                    except asyncio.CancelledError:
                        pass
                if not cancel_task.done():
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass

            if proc.returncode is None:
                _process_control().signal_stop(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                _process_control().force_kill(proc.pid)
                await proc.wait()

            try:
                await asyncio.wait_for(stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stderr_task.cancel()

            if not turn_completed_seen and not cancelled:
                rollout_path = state.get("rollout_path")
                if not rollout_path:
                    thread_id_for_rollout = discovered_sid or state.get("session_id") or proc.thread_id
                    if thread_id_for_rollout:
                        try:
                            from codex_native import resolve_rollout_path_polled
                            resolved_rollout_path = await resolve_rollout_path_polled(
                                thread_id_for_rollout,
                                timeout=5.0,
                            )
                            rollout_path = str(resolved_rollout_path) if resolved_rollout_path else None
                            if rollout_path:
                                state["jsonl_path"] = rollout_path
                                state["rollout_path"] = rollout_path
                                atomic_write_json(state_path, state)
                        except Exception:
                            log.exception("failed to resolve Codex rollout path after stdout closed")
                rollout_terminal, rollout_usage = await _wait_rollout_terminal_state(
                    rollout_path,
                    timeout=60.0,
                )
                if rollout_terminal is True:
                    turn_completed_seen = True
                    success = True
                    error = None
                    if rollout_usage:
                        total_usage = rollout_usage
                elif rollout_terminal is False and not error:
                    turn_completed_seen = True
                    error = "Codex rollout reported turn failure"

            if proc.returncode != 0 and not error and not cancelled:
                try:
                    stderr_log = run_dir / "codex_stderr.log"
                    if stderr_log.exists():
                        lines = stderr_log.read_text(encoding="utf-8").splitlines()
                        if lines:
                            for line in reversed(lines):
                                if line.strip():
                                    error = normalize_context_overflow_error(line.strip()) or line.strip()
                                    break
                    if not error:
                        error = f"Codex CLI exited with code {proc.returncode}"
                except Exception as e:
                    log.error("failed to extract error from stderr: %s", e)
                    error = f"Codex CLI exited with code {proc.returncode}"

            if not turn_completed_seen and not error and not cancelled:
                base = "Codex CLI exited without completing a turn"
                error = base

            if not cancelled:
                rollout_terminal, rollout_usage = _rollout_terminal_state(state.get("rollout_path"))
                if rollout_terminal is True:
                    turn_completed_seen = True
                    success = True
                    error = None
                    if rollout_usage:
                        total_usage = rollout_usage

        except asyncio.CancelledError:
            error = "cancelled"
        except Exception as e:
            log.exception("Codex runner failed")
            error = f"{type(e).__name__}: {e}"

        # Network retry check
        if error and not cancelled and _is_network_error_message(error):
            if total_usage:
                _accumulated_usage = _sum_usage(_accumulated_usage, total_usage)
            log.warning("codex network error, retry %.1fs: %s", _retry_backoff, error)
            await _retry_sleep(_retry_backoff)
            _retry_backoff = min(_retry_backoff * 2, 60.0)
            continue

        total_usage = _sum_usage(_accumulated_usage, total_usage)
        _retry_backoff = 2.0
        break

    if cancelled and not error:
        error = "cancelled"

    final_success = success and not cancelled and not error

    complete = {
        "success": final_success,
        "session_id": discovered_sid,
        "error": error,
        "token_usage": total_usage or None,
        "finished_at": datetime.now().isoformat(),
    }
    try:
        (run_dir / "complete.json").write_text(json.dumps(complete, indent=2), encoding="utf-8")
    except Exception:
        log.exception("failed to write complete.json")

    state["complete"] = True
    state["finished_at"] = complete["finished_at"]
    if discovered_sid and not state.get("session_id"):
        state["session_id"] = discovered_sid
    try:
        atomic_write_json(state_path, state)
    except Exception:
        log.exception("failed to finalize state.json")

    return 0 if final_success else 1


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_codex fatal: %s", error)
    try:
        (run_dir / "complete.json").write_text(json.dumps({
            "success": False,
            "session_id": None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("failed to write error complete.json")


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_codex %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("runner_codex").info("starting for run_dir=%s", run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as e:
        _fail(run_dir, f"failed to read input.json: {e}")
        return 1

    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as e:
        logger.exception("runner_codex top-level failure")
        _fail(run_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
