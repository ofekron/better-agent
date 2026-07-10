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
from codex_normalize import (
    _codex_primary_final_answer_text,
    _codex_primary_assistant_text,
    _codex_terminal_state,
    _file_size,
)
from codex_usage import token_usage_from_codex_usage
from runner_guard import (
    GHOST_RETRY_BACKOFF_S,
    GHOST_RETRY_MAX,
    apply_ghost_completion_guard,
    should_retry_ghost,
)
from loopback_http import raise_loopback_http_error
from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
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
    SET_CHAT_SENDER_POLICY_DESCRIPTION as _SET_CHAT_SENDER_POLICY_DESCRIPTION,
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
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES

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
        "history_mode": {
            "type": "string",
            "enum": ["unread_history", "caught_up"],
            "description": (
                "Optional first-read override. unread_history treats existing "
                "messages as unseen; caught_up starts at the current chat head. "
                "Ignored after this session already has a chat cursor."
            ),
        },
    },
    "required": ["chat_id"],
    "additionalProperties": False,
}

_READ_CHAT_HISTORY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "The shared team chat to inspect."},
        "limit": {"type": "integer", "description": "Maximum messages to return, clamped to 1..200. Defaults to 50."},
        "before_seq": {"type": ["integer", "null"], "description": "Return messages older than this sequence. Omit for newest history."},
    },
    "required": ["chat_id"],
    "additionalProperties": False,
}

_READ_CHAT_HISTORY_DESCRIPTION = (
    "Read shared chat history without changing your unread cursor. Use this when "
    "you need older context but do not want those messages marked as seen."
)

_CREATE_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "Unique id for the new chat."},
        "name": {"type": "string", "description": "Optional human-readable name."},
        "new_readers_see_history": {
            "type": "boolean",
            "description": (
                "Whether sessions with no chat cursor see existing messages as unread "
                "on first read. Defaults to true."
            ),
        },
        "sender_policy": {
            "type": "string",
            "enum": ["open", "allowlist", "disallowlist"],
            "description": (
                "Who may post. open allows everyone, allowlist allows only sender_ids, "
                "disallowlist blocks sender_ids. Defaults to open."
            ),
        },
        "sender_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Session ids used by allowlist or disallowlist sender policy.",
        },
    },
    "required": ["chat_id"],
    "additionalProperties": False,
}

_SET_CHAT_SENDER_POLICY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "Chat whose sender policy will change."},
        "sender_policy": {
            "type": "string",
            "enum": ["open", "allowlist", "disallowlist"],
            "description": "open allows everyone; allowlist allows only sender_ids; disallowlist blocks sender_ids.",
        },
        "sender_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Session ids used by allowlist or disallowlist sender policy.",
        },
    },
    "required": ["chat_id", "sender_policy"],
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
                ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
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
    "chat",
    "create_chat",
    "create_session",
    "create_sub_session",
    "delete_chat",
    "delegate_task",
    "ensure_named_worker",
    "list_available_provider_models",
    "mssg",
    "read_chat_history",
    "set_chat_sender_policy",
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


def _build_set_chat_sender_policy_dynamic_tool() -> dict:
    return {
        "name": "set_chat_sender_policy",
        "description": _SET_CHAT_SENDER_POLICY_DESCRIPTION,
        "inputSchema": _SET_CHAT_SENDER_POLICY_INPUT_SCHEMA,
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
        history_mode = str(args.get("history_mode") or "").strip()
        try:
            result = await asyncio.to_thread(
                chat_store.post_and_read,
                chat_id=chat_id,
                reader_id=sender_session_id,
                message=message,
                history_mode=history_mode,
            )
        except Exception as e:
            logger.exception("chat dynamic tool handler failed")
            return _dynamic_tool_text_result(f"chat failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return chat


def _build_read_chat_history_dynamic_tool() -> dict:
    return {
        "name": "read_chat_history",
        "description": _READ_CHAT_HISTORY_DESCRIPTION,
        "inputSchema": _READ_CHAT_HISTORY_INPUT_SCHEMA,
    }


def _build_read_chat_history_tool_handler():
    async def read_chat_history(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("read_chat_history arguments must be an object", success=False)
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return _dynamic_tool_text_result("chat_id is required", success=False)
        try:
            result = await asyncio.to_thread(
                chat_store.read_history,
                chat_id=chat_id,
                limit=int(args.get("limit") or 50),
                before_seq=args.get("before_seq"),
            )
        except Exception as e:
            logger.exception("read_chat_history dynamic tool handler failed")
            return _dynamic_tool_text_result(f"read_chat_history failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return read_chat_history


def _build_create_chat_tool_handler(*, sender_session_id: str):
    async def create_chat(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("create_chat arguments must be an object", success=False)
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return _dynamic_tool_text_result("chat_id is required", success=False)
        name = str(args.get("name") or "").strip()
        new_readers_see_history = args.get("new_readers_see_history", True)
        sender_policy = str(args.get("sender_policy") or "").strip()
        sender_ids = args.get("sender_ids")
        try:
            result = await asyncio.to_thread(
                chat_store.create_chat,
                chat_id=chat_id,
                created_by=sender_session_id,
                name=name,
                new_readers_see_history=new_readers_see_history,
                sender_policy=sender_policy,
                sender_ids=sender_ids,
            )
        except Exception as e:
            logger.exception("create_chat dynamic tool handler failed")
            return _dynamic_tool_text_result(f"create_chat failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return create_chat


def _build_set_chat_sender_policy_tool_handler(*, sender_session_id: str):
    async def set_chat_sender_policy(params: dict) -> dict:
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _dynamic_tool_text_result("set_chat_sender_policy arguments must be an object", success=False)
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return _dynamic_tool_text_result("chat_id is required", success=False)
        sender_policy = str(args.get("sender_policy") or "").strip()
        try:
            result = await asyncio.to_thread(
                chat_store.set_sender_policy,
                chat_id=chat_id,
                owner_id=sender_session_id,
                sender_policy=sender_policy,
                sender_ids=args.get("sender_ids"),
            )
        except Exception as e:
            logger.exception("set_chat_sender_policy dynamic tool handler failed")
            return _dynamic_tool_text_result(f"set_chat_sender_policy failed: {e}", success=False)
        is_error = bool(result.get("error")) or result.get("success") is False
        return _dynamic_tool_json_result(result, success=not is_error)

    return set_chat_sender_policy


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
        if "read_chat_history" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_read_chat_history_dynamic_tool(),
                _build_read_chat_history_tool_handler(),
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
        if "set_chat_sender_policy" not in disabled_builtin_tools:
            _add_dynamic_tool(
                dynamic_tools,
                tool_handlers,
                _build_set_chat_sender_policy_dynamic_tool(),
                _build_set_chat_sender_policy_tool_handler(sender_session_id=mssg_sender_session_id),
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
        self._send_lock = asyncio.Lock()
        self._server_request_tasks: set[asyncio.Task] = set()
        self._mapped: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self.stdout = _MappedNotificationStream(self._mapped)
        self._tool_handlers = tool_handlers or {}
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self._reader_task = asyncio.create_task(self._read_messages())
        self._steer_task = asyncio.create_task(self._watch_steer_inbox())
        # Drain stderr from the moment the process spawns — i.e. before the
        # initialize/thread/turn handshake runs. Codex can emit a large burst of
        # startup warnings (plugin manifests, model catalog, MCP server startup)
        # to stderr; if nothing reads it during the handshake the OS pipe buffer
        # fills, codex blocks on the stderr write, and the handshake response
        # never arrives — surfacing as `codex app-server request timed out:
        # initialize`. Reusable by the turn loop via `_stderr_task`.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    def _server_request_done(self, task: asyncio.Task) -> None:
        self._server_request_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.getLogger("runner_codex").error(
                "codex server request task failed: %s", error,
            )

    async def _drain_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            with (self._run_dir / "codex_stderr.log").open("ab") as f:
                while True:
                    chunk = await self._proc.stderr.read(8192)
                    if not chunk:
                        return
                    f.write(chunk)
                    f.flush()
        except Exception:
            logging.getLogger("runner_codex").exception("codex stderr drain failed")

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

    async def close_input(self) -> None:
        if self.stdin is None:
            return
        self.stdin.close()
        wait_closed = getattr(self.stdin, "wait_closed", None)
        if wait_closed is not None:
            await wait_closed()

    async def _send(self, message: dict) -> None:
        async with self._send_lock:
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
                    if message.get("method") == "item/tool/call":
                        task = asyncio.create_task(self._handle_server_request(message))
                        self._server_request_tasks.add(task)
                        task.add_done_callback(self._server_request_done)
                        continue
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
        log = logging.getLogger("runner_codex")
        while self._proc.returncode is None:
            try:
                if inbox.exists():
                    with inbox.open(encoding="utf-8") as f:
                        f.seek(offset)
                        while line := f.readline():
                            line_end = f.tell()
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError:
                                if not line.endswith("\n"):
                                    break
                                log.warning("dropping malformed codex steer entry")
                                offset = line_end
                                continue
                            if not self.thread_id or not self.turn_id:
                                log.warning("dropping codex steer entry without active turn")
                                offset = line_end
                                continue
                            try:
                                await self.request("turn/steer", {
                                    "threadId": self.thread_id,
                                    "expectedTurnId": self.turn_id,
                                    "input": build_codex_steer_input(self._run_dir, payload),
                                })
                            except TimeoutError:
                                raise
                            except Exception:
                                log.exception("dropping failed codex steer entry")
                            offset = line_end
            except Exception:
                log.exception("codex steering failed")
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
        for task in tuple(self._server_request_tasks):
            task.cancel()
        if self._server_request_tasks:
            await asyncio.gather(*self._server_request_tasks, return_exceptions=True)
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
        limit=SUBPROCESS_LINE_LIMIT_BYTES,
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


def _scan_rollout_slice(
    path: Path, start: int, end: Optional[int],
) -> tuple[Optional[bool], dict, bool]:
    """Scan rollout bytes [start, end) and report the slice's terminal
    state, last cumulative token usage, and whether any non-empty
    `agent_message` was seen. `end=None` scans to EOF."""
    usage: dict = {}
    terminal: Optional[bool] = None
    assistant_seen = False
    with path.open("rb") as f:
        if start:
            f.seek(start)
        data = f.read() if end is None else f.read(max(0, end - start))
    for raw in data.splitlines():
        try:
            item = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            # start may land mid-line if the offset captured a partial
            # flush; the next iteration resumes on a boundary.
            continue
        try:
            payload = item.get("payload") or {}
            item_terminal = _codex_terminal_state(item)
            if item_terminal is not None:
                terminal = item_terminal
            if _codex_primary_assistant_text(item):
                assistant_seen = True
            if item.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            if payload_type == "token_count":
                info = payload.get("info") or {}
                usage = token_usage_from_codex_usage(
                    info.get("total_token_usage") if isinstance(info, dict) else info
                ) or usage
        except Exception:
            continue
    return terminal, usage, assistant_seen


def _rollout_usage_baseline(rollout_path: Optional[str], byte_offset: int) -> dict:
    """Last cumulative token usage strictly BEFORE `byte_offset` — the
    resumed session's prior-turn totals that this attempt's `token_count`
    events re-report."""
    if not rollout_path or byte_offset <= 0:
        return {}
    path = Path(rollout_path)
    if not path.exists():
        return {}
    try:
        _, usage, _ = _scan_rollout_slice(path, 0, byte_offset)
    except OSError:
        return {}
    return usage


def _usage_delta(cumulative: dict, baseline: dict) -> dict:
    """Per-key non-negative difference of two normalized usage dicts."""
    if not baseline:
        return cumulative
    return {
        key: max(0, value - baseline.get(key, 0))
        for key, value in cumulative.items()
        if isinstance(value, int)
    }


def _rollout_terminal_state(
    rollout_path: Optional[str],
    *,
    byte_offset: int = 0,
    usage_baseline: Optional[dict] = None,
) -> tuple[Optional[bool], dict, bool]:
    """Scan the rollout from `byte_offset` forward and report this slice's
    terminal state, PER-ATTEMPT token usage, and whether any non-empty
    `agent_message` was seen.

    The Codex rollout is CUMULATIVE across resumed turns on the same native
    session — prior turns' events (including `agent_message`, `task_complete`
    and `token_count` totals) sit before this run's `pre_query_byte_offset`.
    Scanning from byte 0 would let a prior turn's content set `assistant_seen`
    (neutering the ghost-completion guard) or surface a prior `task_complete`
    as this turn's terminal state. Callers pass `pre_query_byte_offset` so
    only THIS turn's events count.

    `token_count` events report usage cumulative across the SESSION, not the
    slice — a resumed attempt re-reports prior turns' totals even when it
    produced nothing, which would neuter the guard's zero-usage condition and
    overcount the turn's token_usage. The reported usage is therefore the
    delta against the last cumulative usage before `byte_offset`
    (`usage_baseline`; computed from the prefix when not supplied)."""
    if not rollout_path:
        return None, {}, False
    path = Path(rollout_path)
    if not path.exists():
        return None, {}, False
    usage: dict = {}
    terminal: Optional[bool] = None
    assistant_seen = False
    try:
        terminal, usage, assistant_seen = _scan_rollout_slice(path, byte_offset, None)
    except OSError:
        return None, usage, assistant_seen
    if usage and byte_offset:
        baseline = (
            usage_baseline if usage_baseline is not None
            else _rollout_usage_baseline(rollout_path, byte_offset)
        )
        usage = _usage_delta(usage, baseline)
    return terminal, usage, assistant_seen


async def _wait_rollout_terminal_state(
    rollout_path: Optional[str],
    *,
    byte_offset: int = 0,
    timeout: float = 20.0,
    poll_interval: float = 0.25,
) -> tuple[Optional[bool], dict, bool]:
    deadline = time.monotonic() + timeout
    last_usage: dict = {}
    last_assistant_seen = False
    # Prefix is immutable while polling — compute the resumed-session usage
    # baseline once instead of rescanning it every poll.
    usage_baseline = _rollout_usage_baseline(rollout_path, byte_offset)
    while True:
        terminal, usage, assistant_seen = _rollout_terminal_state(
            rollout_path, byte_offset=byte_offset, usage_baseline=usage_baseline,
        )
        if usage:
            last_usage = usage
        if assistant_seen:
            last_assistant_seen = True
        if terminal is not None:
            return terminal, usage or last_usage, assistant_seen or last_assistant_seen
        if time.monotonic() >= deadline:
            return None, last_usage, last_assistant_seen
        await asyncio.sleep(poll_interval)


async def _forward_rollout_terminal(
    proc: _AppServerProcess,
    rollout_path: str,
    *,
    byte_offset: int,
) -> None:
    while proc.returncode is None:
        terminal, usage, assistant_seen = await _wait_rollout_terminal_state(
            rollout_path,
            byte_offset=byte_offset,
            timeout=1.0,
        )
        if terminal is True:
            await proc._mapped.put((json.dumps({
                "type": "turn.completed",
                "usage": usage,
                "rollout_terminal": True,
                "assistant_seen": assistant_seen,
            }) + "\n").encode("utf-8"))
            return
        if terminal is False:
            await proc._mapped.put((json.dumps({
                "type": "turn.failed",
                "error": {"message": "Codex rollout reported turn failure"},
                "rollout_terminal": True,
            }) + "\n").encode("utf-8"))
            return


def _rollout_parent_final_seen(
    rollout_path: Optional[str],
    *,
    byte_offset: int = 0,
) -> bool:
    if not rollout_path:
        return False
    try:
        with Path(rollout_path).open("rb") as file:
            file.seek(byte_offset)
            rows = file.read().splitlines()
    except OSError:
        return False
    for raw in rows:
        try:
            item = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if _codex_primary_final_answer_text(item):
            return True
    return False


def _apply_parent_final_guard(
    *,
    success: bool,
    cancelled: bool,
    error: Optional[str],
    prompt: str,
    final_answer_seen: bool,
    result_seen: bool,
) -> tuple[bool, Optional[str]]:
    if not success or cancelled or error or not prompt or not result_seen:
        return success, error
    if final_answer_seen:
        return success, error
    return False, "parent_final_not_emitted"


def _rollout_attempt_boundary(
    session_id: Optional[str],
    rollout_path: Optional[Path],
) -> tuple[int, bool]:
    return _file_size(rollout_path), rollout_path is not None or not session_id


async def _settle_app_server_process(
    proc: _AppServerProcess,
    *,
    rollout_terminal_completion: bool,
    log: logging.Logger,
) -> None:
    if proc.returncode is None:
        if rollout_terminal_completion:
            try:
                await proc.close_input()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                log.debug("Codex app-server input was already closed", exc_info=True)
        else:
            _process_control().signal_stop(proc.pid)
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        if rollout_terminal_completion:
            log.warning(
                "Codex app-server remained alive after rollout completion; "
                "reaping completed infrastructure"
            )
        _process_control().force_kill(proc.pid)
        await proc.wait()


def build_codex_steer_input(run_dir: Path, payload: dict) -> list[dict]:
    return build_codex_turn_input(
        run_dir,
        payload.get("prompt") or "",
        payload.get("images") or [],
    )




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
        extension_store.extension_id_for_role('team-orchestration')
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
    from codex_native import resolve_rollout_path, resolve_rollout_path_polled
    initial_rollout_path = resolve_rollout_path(session_id or "")
    if session_id and initial_rollout_path is None:
        initial_rollout_path = await resolve_rollout_path_polled(session_id, timeout=5.0)
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
    _ghost_attempts = 0
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
        assistant_seen = False
        attempt_start_byte, attempt_boundary_known = _rollout_attempt_boundary(
            session_id, initial_rollout_path,
        )
        interrupt_timeout_task: Optional[asyncio.Task] = None
        rollout_terminal_task: Optional[asyncio.Task] = None
        rollout_terminal_completion = False

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

            # stderr has been draining since the app-server process spawned (see
            # `_AppServerProcess._drain_stderr`), which is before the handshake —
            # so a startup stderr flood can't deadlock the initialize/thread/turn
            # requests. Reuse that task for lifecycle cleanup below.
            stderr_task = proc._stderr_task

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
                            # Persist the CLI/app-server pid so restart recovery
                            # can re-attach to a still-running CLI whose wrapper
                            # died, instead of declaring the run dead.
                            state["cli_pid"] = proc.pid
                            if not initial_byte_offset:
                                state["pre_query_byte_offset"] = attempt_start_byte
                            # Per-attempt rollout boundary for the ghost
                            # guard: thread.started is the first event of
                            # THIS attempt, so the file size at this moment
                            # marks where this attempt's content begins.
                            # Captured every attempt (incl. network retries)
                            # so a failed attempt's partial events before a
                            # retry are excluded — distinct from
                            # pre_query_byte_offset, which is the whole-run
                            # start used by provider ingestion.
                            if rollout_path:
                                if not attempt_boundary_known:
                                    log.warning(
                                        "rollout boundary unavailable for resumed session %s; "
                                        "live terminal monitor disabled",
                                        session_id,
                                    )
                                else:
                                    rollout_terminal_task = asyncio.create_task(
                                        _forward_rollout_terminal(
                                            proc,
                                            str(rollout_path),
                                            byte_offset=attempt_start_byte,
                                        )
                                    )
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
                        assistant_seen = assistant_seen or bool(raw_event.get("assistant_seen"))
                        rollout_terminal_completion = bool(raw_event.get("rollout_terminal"))
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
                if rollout_terminal_task is not None and not rollout_terminal_task.done():
                    rollout_terminal_task.cancel()
                    try:
                        await rollout_terminal_task
                    except asyncio.CancelledError:
                        pass

            await _settle_app_server_process(
                proc,
                rollout_terminal_completion=rollout_terminal_completion,
                log=log,
            )

            try:
                await asyncio.wait_for(stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stderr_task.cancel()

            if not turn_completed_seen and not cancelled and attempt_boundary_known:
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
                rollout_terminal, rollout_usage, rollout_assistant = await _wait_rollout_terminal_state(
                    rollout_path,
                    byte_offset=attempt_start_byte or (state.get("pre_query_byte_offset") or 0),
                    timeout=60.0,
                )
                if rollout_assistant:
                    assistant_seen = True
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

            if not cancelled and attempt_boundary_known:
                rollout_terminal, rollout_usage, rollout_assistant = _rollout_terminal_state(
                    state.get("rollout_path"),
                    byte_offset=attempt_start_byte or (state.get("pre_query_byte_offset") or 0),
                )
                if rollout_assistant:
                    assistant_seen = True
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

        # Ghost-completion guard (parity with the Claude runner): a Codex
        # task_complete with no agent_message output for a non-empty prompt
        # and zero token usage is a provider ghost completion, not a real
        # success. Applied inside the loop so a prompt_not_executed result
        # can be retried — codex-cli intermittently swallows an empty/failed
        # upstream response as a normal task_complete, and a fresh attempt
        # usually succeeds. The attempt-scoped rollout scan
        # (attempt_start_byte) excludes the failed attempt's partial events.
        success, error = apply_ghost_completion_guard(
            success=success,
            cancelled=cancelled,
            error=error,
            prompt=prompt,
            assistant_seen=assistant_seen,
            total_usage=total_usage,
            result_seen=turn_completed_seen,
        )
        success, error = _apply_parent_final_guard(
            success=success,
            cancelled=cancelled,
            error=error,
            prompt=prompt,
            final_answer_seen=_rollout_parent_final_seen(
                state.get("rollout_path"),
                byte_offset=attempt_start_byte
                or (state.get("pre_query_byte_offset") or 0),
            ),
            result_seen=turn_completed_seen,
        )

        # Network retry check
        if error and not cancelled and _is_network_error_message(error):
            if total_usage:
                _accumulated_usage = _sum_usage(_accumulated_usage, total_usage)
            log.warning("codex network error, retry %.1fs: %s", _retry_backoff, error)
            await _retry_sleep(_retry_backoff)
            _retry_backoff = min(_retry_backoff * 2, 60.0)
            continue

        # Ghost-completion retry (bounded): prompt_not_executed is
        # transient — retry a few times before failing the turn.
        if should_retry_ghost(error, cancelled=cancelled, attempts=_ghost_attempts):
            _ghost_attempts += 1
            log.warning(
                "codex ghost completion (prompt_not_executed); "
                "retry %d/%d after %.1fs",
                _ghost_attempts, GHOST_RETRY_MAX, GHOST_RETRY_BACKOFF_S,
            )
            await _retry_sleep(GHOST_RETRY_BACKOFF_S)
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
