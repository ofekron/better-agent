"""Unified detached per-run executable.

Spawned by `ClaudeProvider.start_run` as a subprocess with
`start_new_session=True`. Handles exactly one claude CLI run (native or
manager mode) via `claude_agent_sdk.ClaudeSDKClient`. Claude CLI itself
(spawned by the SDK) writes its own session jsonl at
`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`, and that file is
the source of truth — the backend tails it directly. This runner's only
job on the data path is to (a) spawn the SDK, (b) keep it alive until
the turn completes, (c) write a tiny `state.json` so the backend knows
where claude's jsonl lives and can tail it, and (d) write
`complete.json` when done.

Life of a run:
  1. Backend creates `~/.better-claude/runs/<run_id>/` and writes
     `input.json` with all inputs.
  2. Backend spawns `python runner.py --run-dir <path>` detached
     (start_new_session=True, stdin=DEVNULL, stdout/stderr → log files).
  3. This script writes `pid` (its own pid), reads `input.json`, builds
     `ClaudeAgentOptions` (with an in-process delegate MCP server for
     manager mode), connects the SDK, and calls `client.query(prompt)`.
  4. On `SystemMessage(subtype="init")`, captures the claude session_id,
     computes `jsonl_path`, and writes `state.json` atomically. The
     backend polls `state.json` to start its FileTailer.
  5. Iterates `client.receive_response()` until `ResultMessage` arrives
     (or cancel sentinel triggers `client.interrupt()`).
  6. Writes `complete.json` with success/error/session_id/token_usage
     and sets `state.json.complete = true`. Exits.

Cancel sentinel: backend writes an empty file `run_dir/cancel` to
request cancel. A background asyncio task polls for it every ~150ms and
calls `client.interrupt()` on sight.
"""

import argparse
import asyncio
import contextvars
import json
import logging
import os
import sys
import time
import http.client
import threading
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import chat_store
import extension_store
from activity_state import transition_activity
from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    normalize_ask_mode,
)
from env_compat import get_env
from loopback_http import raise_loopback_http_error
from trace_collector import aggregate_claude_turn_usage
from user_input_contract import USER_INPUT_MAX_QUESTIONS, build_request_user_input_schema
from user_input_identity import logical_request_id as user_input_logical_request_id
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
    SESSION_ORGANIZATION_INPUT_PROPERTIES as _SESSION_ORGANIZATION_INPUT_PROPERTIES,
)
from provider_catalog_mcp import available_provider_models_response

# internal_token mtime-cache. The in-process MCP server callbacks
# capture `internal_token` in a closure at spawn time — risky once a
# runner outlives a token rotation: captured closures would keep using
# the stale value and start 403-ing.
#
# `_load_internal_token()` re-reads `ba_home()/internal_token` per
# MCP call but caches on mtime so steady-state cost is one stat() per
# call, not one read(). Closures fall back to their captured value if
# the file read fails (e.g. unset BETTER_CLAUDE_HOME, fs error).
_token_cache: dict = {"token": None, "mtime": 0.0}
def _load_internal_token() -> Optional[str]:
    try:
        from paths import ba_home as _ba_home
        path = _ba_home() / "internal_token"
        st = path.stat()
        if _token_cache["mtime"] != st.st_mtime:
            _token_cache["token"] = path.read_text(encoding="utf-8").strip()
            _token_cache["mtime"] = st.st_mtime
        return _token_cache["token"]
    except OSError:
        return None
    except Exception:
        return None
from i18n import t
from continuation import normalize_context_overflow_error
from provider_run_config import write_skill_tree
from reasoning_effort import claude_sdk_effort
from runner_guard import apply_ghost_completion_guard
from runtime_skills import (
    CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME,
    materialize_runtime_skills,
)


def _claude_cache_env() -> dict[str, str]:
    """CLI subprocess env enabling the 1-hour prompt-cache TTL.

    ENABLE_PROMPT_CACHING_1H short-circuits the CLI's remote feature gate
    so every request carries cache_control ttl:"1h" plus the
    extended-cache-ttl beta header, keeping the stable prefix (system
    prompt + MCP tool defs + skills) cached across idle gaps longer than
    the 5-minute default TTL. An inherited FORCE_PROMPT_CACHING_5M is
    checked first by the CLI and remains a deliberate shell-level opt-out;
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS or a HIPAA org disables the
    beta header independently.
    """
    return {"ENABLE_PROMPT_CACHING_1H": "1"}


def _resolve_claude_cli() -> Optional[str]:
    from cli_paths import resolve_cli_binary

    resolved = resolve_cli_binary("claude")
    if os.name == "nt" and resolved:
        path = Path(resolved)
        npm_dir = path.parent
        packaged_exe = (
            npm_dir
            / "node_modules"
            / "@anthropic-ai"
            / "claude-code"
            / "bin"
            / "claude.exe"
        )
        if packaged_exe.is_file():
            return str(packaged_exe)
    return resolved

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from paths import encode_cwd
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES
from tool_approval_client import describe_tool_call as _describe_tool_call
from tool_approval_client import request_tool_approval
from prompt_templates import render_prompt

logger = logging.getLogger(__name__)


async def _deny_background_tool_use(hook_input, tool_use_id, context):
    """PreToolUse backstop for the no-background policy (see
    runs_dir.BACKGROUND_WORK_TOOLS): deny any tool input that still
    requests background execution or a remote (inherently background)
    sandbox, whatever the tool. The CLI's native
    CLAUDE_CODE_DISABLE_BACKGROUND_TASKS switch already strips these from
    the tool schemas — this hook covers future CLI schema changes."""
    tool_input = (hook_input or {}).get("tool_input") or {}
    wants_bg = bool(tool_input.get("run_in_background"))
    wants_remote = str(tool_input.get("isolation") or "") == "remote"
    if not wants_bg and not wants_remote:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Background execution is disabled — run this in the "
                "foreground and wait for it to complete."
            ),
        }
    }


def _background_policy_hooks() -> dict:
    """Hook set enforcing the no-background policy on every tool call
    (matcher None = all tools)."""
    return {
        "PreToolUse": [
            HookMatcher(matcher=None, hooks=[_deny_background_tool_use]),
        ],
    }


_RESPONSE_NO_PROGRESS_TIMEOUT_S = 0
_RESPONSE_ACTIVITY_POLL_S = 1.0
_MCP_LIST_TIMEOUT_S = 8.0
_MCP_CALL_TIMEOUT_S = 300.0
_REQUIREMENTS_WAIT_TRUE_MCP_CALL_TIMEOUT_S = 1380.0
# An in-flight tool call counts as response progress: the CLI is silent while
# a tool executes, so the no-progress watchdog must not read a long MCP/Bash
# call as a wedged CLI. The backstop must exceed the longest declared tool
# budget (delegate / browser-test MCP tools run up to 24h); the CLI's own tool
# timeouts eventually emit an error tool_result, which clears the entry.
_TOOL_CALL_BUSY_BACKSTOP_S = 25 * 60 * 60
_TOOL_CALL_BUSY_WARN_S = 30 * 60


def _block_type(block: object) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return type(block).__name__


def _block_field(block: object, field: str) -> Optional[str]:
    value = block.get(field) if isinstance(block, dict) else getattr(block, field, None)
    return value if isinstance(value, str) and value else None


class _OutstandingToolCalls:
    """Tool calls of the current turn with a tool_use seen and no tool_result
    yet. Scoped per `_run_one_turn` so a discarded interrupted tail cannot
    leak stale entries into the next turn."""

    def __init__(self) -> None:
        self._started: dict[str, float] = {}
        self._warned: set[str] = set()

    def apply(self, msg: object) -> None:
        if isinstance(msg, AssistantMessage):
            for block in (msg.content or []):
                if _block_type(block) in ("ToolUseBlock", "tool_use"):
                    tool_id = _block_field(block, "id")
                    if tool_id:
                        self._started.setdefault(tool_id, time.monotonic())
            return
        if isinstance(msg, UserMessage):
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                return
            for block in content:
                if _block_type(block) in ("ToolResultBlock", "tool_result"):
                    tool_id = _block_field(block, "tool_use_id")
                    if tool_id:
                        self._started.pop(tool_id, None)
                        self._warned.discard(tool_id)

    def busy(self, log: logging.Logger) -> bool:
        now = time.monotonic()
        active = False
        for tool_id, started in self._started.items():
            age = now - started
            if age >= _TOOL_CALL_BUSY_BACKSTOP_S:
                continue
            if age >= _TOOL_CALL_BUSY_WARN_S and tool_id not in self._warned:
                self._warned.add(tool_id)
                log.warning(
                    "tool call %s still outstanding after %.0fs — watchdog held open",
                    tool_id, age,
                )
            active = True
        return active


class _RunnerActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_progress_at = time.monotonic()

    def mark(self) -> None:
        with self._lock:
            self._last_progress_at = time.monotonic()

    def last_progress_at(self) -> float:
        with self._lock:
            return self._last_progress_at


_BackgroundActivityProbe = Callable[[], Awaitable[bool]]


_runner_activity_var: contextvars.ContextVar[Optional[_RunnerActivity]] = (
    contextvars.ContextVar("runner_activity", default=None)
)
_active_runner_activity_lock = threading.Lock()
_active_runner_activity: Optional[_RunnerActivity] = None


def _set_active_runner_activity(activity: Optional[_RunnerActivity]) -> None:
    global _active_runner_activity
    with _active_runner_activity_lock:
        _active_runner_activity = activity


def _mark_runner_activity() -> None:
    activity = _runner_activity_var.get()
    if activity is None:
        with _active_runner_activity_lock:
            activity = _active_runner_activity
    if activity is not None:
        activity.mark()


def _mcp_subprocess_env(config: dict[str, Any]) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
    return env


async def _mcp_json_request(
    config: dict[str, Any],
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    command = str(config.get("command") or "").strip()
    if not command:
        raise RuntimeError("MCP server config missing command")
    proc = await asyncio.create_subprocess_exec(
        command,
        *[str(arg) for arg in config.get("args") or []],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_mcp_subprocess_env(config),
        limit=SUBPROCESS_LINE_LIMIT_BYTES,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    async def _send(payload: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read_response() -> dict[str, Any]:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("MCP server closed stdout")
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


async def _mcp_list_tools(server_name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        result = await _mcp_json_request(config, "tools/list", {}, timeout=_MCP_LIST_TIMEOUT_S)
    except Exception:
        logger.warning("extension MCP %s tools/list failed", server_name, exc_info=True)
        return []
    tools = result.get("tools") or []
    return [item for item in tools if isinstance(item, dict)]


def _mcp_call_timeout_s(config: dict[str, Any], tool_name: str, args: dict[str, Any]) -> float:
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
        return _REQUIREMENTS_WAIT_TRUE_MCP_CALL_TIMEOUT_S
    return _MCP_CALL_TIMEOUT_S


async def _mcp_call_tool(
    config: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    result = await _mcp_json_request(
        config,
        "tools/call",
        {"name": tool_name, "arguments": args},
        timeout=_mcp_call_timeout_s(config, tool_name, args),
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


async def _bridge_native_extension_mcp_servers(
    inputs: dict[str, Any],
    *,
    user_facing: bool,
    bare: bool,
) -> dict[str, dict[str, Any]]:
    configs = extension_store.native_mcp_server_configs(
        inputs,
        user_facing=user_facing,
        bare=bare,
    )
    bridged: dict[str, dict[str, Any]] = {}
    tool_lists = await asyncio.gather(*(
        _mcp_list_tools(server_name, config)
        for server_name, config in configs.items()
    ))
    for (server_name, config), tools in zip(configs.items(), tool_lists):
        sdk_tools = []
        for item in tools:
            raw_tool_name = str(item.get("name") or "").strip()
            if not raw_tool_name:
                continue
            input_schema = item.get("inputSchema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}

            bridged_config = {**config, "_server_name": server_name}

            async def _handler(args: dict[str, Any], *, _config=bridged_config, _tool_name=raw_tool_name) -> dict[str, Any]:
                return await _mcp_call_tool(_config, _tool_name, args)

            sdk_tools.append(tool(
                raw_tool_name,
                str(item.get("description") or f"{server_name} MCP tool {raw_tool_name}"),
                input_schema,
            )(_handler))
        if sdk_tools:
            bridged[server_name] = create_sdk_mcp_server(
                name=server_name,
                version="1.0.0",
                tools=sdk_tools,
            )
    return bridged


class ResponseNoProgressError(RuntimeError):
    pass


def _resolve_claude_config_dir(raw: str) -> Path:
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return Path.home() / ".claude"


def _materialize_claude_skill_plugin(
    run_dir: Path,
    cwd: str,
    provider_run_config: dict,
    *,
    bare_config: bool,
) -> Optional[dict[str, str]]:
    if bare_config:
        return None
    plugin_dir = run_dir / "claude-runtime-skills-plugin"
    skills_root = plugin_dir / "skills"
    count = materialize_runtime_skills(skills_root, cwd, bare_config=bare_config)

    configured_skills = provider_run_config.get("skills") or {}
    if configured_skills:
        write_skill_tree(skills_root, configured_skills)
        count += len(configured_skills)

    if count == 0:
        return None

    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({
            "name": CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME,
            "description": "Run-local Better Agent skills for this turn.",
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"type": "local", "path": str(plugin_dir)}


# ============================================================================
# Create-worker tool schema & description
# ============================================================================
_CREATE_WORKER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "worker_description": {
            "type": "string",
            "description": (
                "Short (3-8 word) description of what this worker handles. "
                "Becomes the new Better Agent session's name, user-editable at approval time."
            ),
        },
        "justification": {
            "type": "string",
            "description": (
                "1-3 sentences "
                "explaining why none of the existing <known_workers> fit "
                "and a fresh worker is needed. Shown to the user verbatim "
                "in the approval card."
            ),
        },
        "orchestration_mode": {
            "type": "string",
            "enum": ["team", "native"],
            "description": (
                "Orchestration "
                "mode for the new worker Better Agent session. 'native' = a plain "
                "claude session (does work directly, simplest). 'team' "
                "= a sub-coordinator that can itself delegate to workers "
                "(rarely needed). User can override at approval time."
            ),
        },
        "node_id": {
            "type": ["string", "null"],
            "description": (
                "OPTIONAL: which worker-node should host this worker. "
                "Defaults to the session's node_id (= 'primary' for "
                "single-machine deployments). Set this only when "
                "targeting a different machine than the session's "
                "default — available node_ids appear in <known_workers>."
            ),
        },
        "cwd": {
            "type": ["string", "null"],
            "description": (
                "OPTIONAL: working directory for the new worker session. "
                "Defaults to (inherits) the creating session's cwd; set it "
                "only to target a different project root."
            ),
        },
        **_SESSION_ORGANIZATION_INPUT_PROPERTIES,
    },
    "required": ["worker_description", "justification", "orchestration_mode"],
}

_MSSG_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {
            "type": "string",
            "description": "Better Agent session_id of the target session.",
        },
        "target_worker_id": {
            "type": "string",
            "description": "Registered worker id, equal to that worker's agent_session_id.",
        },
        "target_worker_pool": {
            "type": "string",
            "description": "Worker-pool tag. The backend routes to an idle worker in that pool.",
        },
        "pool_affinity_key": {
            "type": "string",
            "description": "Optional thread key for target_worker_pool; repeat it to route back to the same pool worker.",
        },
        "message": {
            "type": "string",
            "description": "Message to enqueue for the target session.",
        },
        "provider_id": {
            "type": ["string", "null"],
            "description": "OPTIONAL — provider for this continuation turn.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "OPTIONAL — model for this continuation turn.",
        },
        "reasoning_effort": {
            "type": ["string", "null"],
            "description": "OPTIONAL — reasoning effort for this continuation turn.",
        },
        "collapse_key": {
            "type": "string",
            "description": "Optional key for coalescing pending mssg work on the target session.",
        },
        "collapse_policy": {
            "type": "string",
            "enum": ["take_latest"],
            "description": "When collapse_key is set, take_latest keeps one pending message and replaces it with the newest body.",
        },
    },
    "required": ["message"],
}


_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {
            "type": "string",
            "description": "The shared team chat to read/post.",
        },
        "message": {
            "type": "string",
            "description": (
                "Optional non-empty message to append (stamped with your id). "
                "Empty/whitespace means read-only: just return new messages."
            ),
        },
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
}

_READ_CHAT_HISTORY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "The shared team chat to inspect."},
        "limit": {
            "type": "integer",
            "description": "Maximum messages to return, clamped to 1..200. Defaults to 50.",
        },
        "before_seq": {
            "type": ["integer", "null"],
            "description": "Return messages older than this sequence. Omit for newest history.",
        },
    },
    "required": ["chat_id"],
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
}

_DELETE_CHAT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "The chat to delete permanently."},
    },
    "required": ["chat_id"],
}

_CREATE_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Name / short description for the new session.",
        },
        "orchestration_mode": {
            "type": "string",
            "enum": ["native", "team"],
            "description": (
                "'native' (default) = a plain session that does work directly. "
                "'team' = a sub-coordinator for complex tasks that need their "
                "own delegation loop."
            ),
        },
        "node_id": {
            "type": "string",
            "description": "Optional node to host the session (default: primary).",
        },
        "provider_id": {
            "type": ["string", "null"],
            "description": "OPTIONAL — provider for the new session. Defaults to the creating session's provider.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "OPTIONAL — model for the new session. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": ["string", "null"],
            "description": "OPTIONAL — reasoning effort for the new session. Defaults to the creating session's effort.",
        },
        "cwd": {
            "type": ["string", "null"],
            "description": "OPTIONAL — working directory for the new session. Defaults to (inherits) the creating session's cwd.",
        },
        **_SESSION_ORGANIZATION_INPUT_PROPERTIES,
    },
    "required": ["name"],
}


_CREATE_SUB_SESSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "Optional short label for the hidden sub-session.",
        },
        "node_id": {
            "type": ["string", "null"],
            "description": "Optional node to host the sub-session.",
        },
        "provider_id": {
            "type": ["string", "null"],
            "description": "OPTIONAL — provider for the sub-session. Defaults to the creating session's provider.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "OPTIONAL — model for the sub-session. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": ["string", "null"],
            "description": "OPTIONAL — reasoning effort for the sub-session. Defaults to the creating session's effort.",
        },
        "cwd": {
            "type": ["string", "null"],
            "description": "OPTIONAL — working directory for the sub-session. Defaults to (inherits) the creating session's cwd.",
        },
        **_SESSION_ORGANIZATION_INPUT_PROPERTIES,
    },
    "required": [],
}


_ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {
            "type": "string",
            "description": (
                "Better Agent session_id of the team member to message. In "
                "fork mode this is the session to branch from."
            ),
        },
        "target_worker_id": {
            "type": "string",
            "description": "Registered worker id, equal to that worker's agent_session_id. Direct mode only.",
        },
        "target_worker_pool": {
            "type": "string",
            "description": "Worker-pool tag. The backend routes direct mode to an idle worker in that pool.",
        },
        "pool_affinity_key": {
            "type": "string",
            "description": "Optional thread key for target_worker_pool; repeat it to route back to the same pool worker.",
        },
        "message": {
            "type": "string",
            "description": "Message / full task instructions for the session.",
        },
        "run_mode": {
            "type": "string",
            "enum": ["direct", "fork"],
            "description": (
                "'direct' (default) runs the requested session. 'fork' "
                "branches an existing session for isolated review/check work; "
                "do not use fork for brand-new sessions."
            ),
        },
        "mode": {
            "type": "string",
            "enum": [
                ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
                ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
            ],
            "description": (
                "wait_and_grab_last_assistant_mssg_in_turn waits and returns the reply; "
                "continue_and_expect_mssg_back_async returns after enqueue and "
                "expects a later mssg back."
            ),
        },
        "worker_description": {
            "type": "string",
            "description": (
                "Optional short label for the session in run_mode='fork'."
            ),
        },
        "worker_registry_cwd": {
            "type": ["string", "null"],
            "description": (
                "For fork: copy the worker's registry_cwd exactly when the "
                "worker is registered under a different project cwd."
            ),
        },
        "ephemeral": {
            "type": "boolean",
            "description": (
                "Only for run_mode='fork': use a fresh temporary fork and "
                "delete its Better Agent session after the call."
            ),
        },
        "provider_id": {
            "type": ["string", "null"],
            "description": "OPTIONAL — provider for this continuation/fork turn.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "OPTIONAL — model for this continuation/fork turn.",
        },
        "reasoning_effort": {
            "type": ["string", "null"],
            "description": "OPTIONAL — reasoning effort for this continuation/fork turn.",
        },
    },
    "required": ["message"],
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


# HTTP timeout for the delegate loopback. Long because the manager may
# request a fresh worker that requires user approval, and the user may
# walk away — 24h gives the runner room to wait without prematurely
# returning is_error to the model.
_DELEGATE_HTTP_TIMEOUT = 24 * 60 * 60  # 24h in seconds


# ============================================================================
# Open-file-panel tool schema & description (active session, any mode)
# ============================================================================
_OPEN_FILE_PANEL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["panel", "inline"],
            "description": (
                "'inline' = embed an editable, scrollable view of the "
                "file directly inside THIS message, initially scrolled "
                "to the chosen lines (use this to point the user at "
                "specific code in context). 'panel' = open the file as "
                "a tab in the user's side file-panel area (use this for "
                "files the user should keep around / switch between)."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Absolute path (or path relative to the session cwd) of "
                "the file to open."
            ),
        },
        "start_line": {
            "type": "integer",
            "description": "1-based first line to scroll into view.",
        },
        "end_line": {
            "type": "integer",
            "description": "1-based last line of the focused range.",
        },
        "selected_start": {
            "type": "integer",
            "description": "1-based first line to highlight as selected.",
        },
        "selected_end": {
            "type": "integer",
            "description": "1-based last line of the selected range.",
        },
    },
    "required": ["mode", "path"],
}

_OPEN_FILE_PANEL_DESCRIPTION = (
    "Show the user a specific location in a file — this is a "
    "communication tool, not a file opener. Use it when you want to "
    "draw the user's attention to code you're discussing, a bug you "
    "found, or a change you made. Pick `mode`: 'inline' embeds an "
    "editable/scrollable file view inside this message (best for "
    "pointing at specific code you're discussing right now); 'panel' "
    "opens it as a tab in the side file-panel area (best for files the "
    "user should keep around or compare). Optionally pass "
    "start_line/end_line to control the initial scroll + (inline) "
    "initial size, and selected_start/selected_end to highlight a "
    "range. Returns immediately; it does not block."
)


_REQUEST_USER_INPUT_SCHEMA: dict[str, Any] = build_request_user_input_schema()

_REQUEST_USER_INPUT_DESCRIPTION = (
    "Ask the user a bounded question and wait for their answer. Use this "
    "only when you cannot continue safely without user input. Pass one "
    f"question or a batch of up to {USER_INPUT_MAX_QUESTIONS} questions. "
    "Each question can include up to three suggested options; if no option "
    "fits, the user can answer in free text. Returns a map of question id "
    "to answer string."
)

_START_FILE_DISCUSSION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "line": {"type": "integer"},
        "title": {"type": "string"},
    },
    "required": ["file_path", "line"],
}

_START_FILE_DISCUSSION_DESCRIPTION = (
    "Start an inline discussion attached to a specific line in a file "
    "currently open in file edit mode. Use this only when you want the "
    "conversation to happen beside that line instead of in the main chat."
)


# Short — opening a panel is a fast state mutation, not a long job.
_OPEN_FILE_PANEL_HTTP_TIMEOUT = 60


# ============================================================================
# Atomic state.json writes
# ============================================================================
def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to `path` atomically (tmp + rename) to prevent readers
    from observing a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        from runs_dir import _append_run_state_ledger
        _append_run_state_ledger(path, data)
    except Exception:
        logger.exception("failed to append run-state ledger")


# ============================================================================
# Delegate tool builder (manager mode only)
# ============================================================================
def _tool_success_result(result: dict) -> dict:
    """Common @tool success-return: JSON-pretty-printed payload as text
    content. Dicts are errors only when they explicitly carry an error
    or `success: false`; tool-specific success payloads are not required
    to include a `success` field."""
    is_error = False
    if isinstance(result, dict):
        is_error = bool(result.get("error")) or result.get("success") is False
    return {
        "content": [
            {"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))},
        ],
        "is_error": is_error,
    }


def _tool_error_response(prefix: str, exc: BaseException) -> dict:
    """Common @tool error-return for tools whose error messages are
    plain f-strings (NOT i18n). Dispatches on `exc` type:
      - HTTPError: log warning, message includes status + body preview
      - URLError:  log warning, message includes reason
      - other:     log.exception (uses live sys.exc_info from caller's
                   except block), message is "<prefix> error: <exc>"

    INVARIANT: must be called FROM WITHIN an `except` block so the
    `logger.exception` fallback sees the live traceback. Delegate uses
    i18n strings and does NOT use this helper — keep it untouched.
    """
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        msg = f"{prefix} HTTP {exc.code}: {exc.reason} {body}"
        logger.warning(msg)
    elif isinstance(exc, urllib.error.URLError):
        msg = f"{prefix} connection error: {exc.reason}"
        logger.warning(msg)
    else:
        logger.exception("%s tool handler failed", prefix)
        msg = f"{prefix} error: {exc}"
    return {
        "content": [{"type": "text", "text": msg}],
        "is_error": True,
    }


def _is_network_error(exc: BaseException) -> bool:
    """Check if an exception is a transient network error that warrants retry."""
    if isinstance(exc, CLINotFoundError):
        return False
    # CLIConnectionError and ProcessError (CLI exit) should NOT be retried
    # infinitely in the runner's loop. Bubbling them up to the
    # orchestrator allows for proper UI feedback ('Retrying in Ns...')
    # via the orchestrator's transient retry loop.
    if isinstance(exc, (CLIConnectionError, ProcessError)):
        return False
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return True
    return False


def _post_loopback_sync(
    payload: dict,
    *,
    backend_url: str,
    internal_token: str,
    url_path: str,
    timeout: float,
    non_json_t_key: str,
    log_prefix: str,
    backoff_cap: float,
    recover: Optional[Callable[[], Optional[dict]]] = None,
) -> dict:
    """Shared retry loop for the runner's loopback POSTs into the
    backend (delegate, open-file-panel). Retries on
    transient connection loss with exponential backoff. HTTPError
    responses are terminal and re-raised. If `recover` returns a dict
    after a connection loss, that durable result is returned instead of
    retrying. `log_prefix` is interpolated into the retry warning.
    `non_json_t_key` is the i18n key for the "response body did not
    parse" RuntimeError each tool uses.

    INVARIANT: matches the inlined retry loop each `_post_*_sync`
    previously implemented — same headers, same JSON envelope, same
    deadline/backoff math, same exception classification.
    """
    import time
    body = json.dumps(payload).encode("utf-8")
    deadline = time.monotonic() + timeout
    backoff = 1.0
    tried_live_token_after_forbidden = False

    def _request_once(token: str) -> dict:
        req = urllib.request.Request(
            url=backend_url.rstrip("/") + url_path,
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
            raise RuntimeError(
                t(non_json_t_key, e=str(e), raw=repr(raw[:200]))
            )

    while True:
        try:
            _mark_runner_activity()
            return _request_once(internal_token)
        except urllib.error.HTTPError as e:
            _mark_runner_activity()
            live_token = _load_internal_token()
            if (
                e.code == 403
                and live_token
                and live_token != internal_token
                and not tried_live_token_after_forbidden
            ):
                tried_live_token_after_forbidden = True
                try:
                    _mark_runner_activity()
                    return _request_once(live_token)
                except urllib.error.HTTPError:
                    _mark_runner_activity()
                    raise e
            if e.code != 403:
                raise_loopback_http_error(e)
            raise
        except (urllib.error.URLError, http.client.RemoteDisconnected) as e:
            _mark_runner_activity()
            recovered = recover() if recover is not None else None
            if recovered is not None:
                return recovered
            if time.monotonic() >= deadline:
                raise
            reason = getattr(e, "reason", e)
            logger.warning(
                "%s URLError (%s); retrying in %.1fs",
                log_prefix, reason, backoff,
            )
            time.sleep(min(backoff, max(0.5, deadline - time.monotonic())))
            backoff = min(backoff * 2, backoff_cap)


def _byte_size_if_exists(path: Optional[str]) -> int:
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _recover_ask_result(ask_id: str) -> Optional[dict]:
    """Restart re-attach for the `ask` tool: if the target turn already
    completed and its result was persisted to ask_status_store (shared disk),
    return it so the runner's URLError-retry resolves without re-POSTing.
    Mirrors `_recover_delegate_result`.
    """
    try:
        import ask_status_store
        status = ask_status_store.read_status(ask_id)
    except Exception:
        logger.exception("ask status recovery read failed")
        return None
    if not status:
        return None
    result = status.get("result")
    return result if isinstance(result, dict) else None


def _recover_delegate_result(client_delegation_id: str) -> Optional[dict]:
    try:
        import delegation_status_store
        status = delegation_status_store.read_status(client_delegation_id)
    except Exception:
        logger.exception("delegate status recovery read failed")
        return None
    if not status:
        return None

    result = status.get("result")
    if isinstance(result, dict):
        return result

    run_dir = status.get("provider_run_dir")
    if not run_dir:
        return None
    try:
        from runs_dir import read_best_complete
        complete = read_best_complete(Path(run_dir))
    except Exception:
        logger.exception("delegate run complete recovery failed")
        return None
    if not complete:
        return None

    jsonl_path = status.get("jsonl_path")
    total_bytes_now = _byte_size_if_exists(jsonl_path)
    return {
        "success": bool(complete.get("success")),
        "error": complete.get("error"),
        "worker_session_id": status.get("worker_agent_session_id"),
        "worker_description": status.get("worker_description") or "",
        "fork_agent_sid": status.get("fork_agent_sid") or complete.get("session_id"),
        "run_mode": status.get("run_mode") or "fork",
        "jsonl_path": jsonl_path,
        "new_byte_offset": int(status.get("new_byte_offset") or 1),
        "total_bytes_now": total_bytes_now,
        "token_usage": complete.get("token_usage"),
    }


def _resolve_tool_cwd(args: dict[str, Any], inherited_cwd: str) -> str:
    """cwd override-or-inherit: use the caller-supplied cwd if provided,
    otherwise inherit the creating session's cwd."""
    return str(args.get("cwd") or "").strip() or inherited_cwd


def _build_create_worker_tool(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
    model: Optional[str],
    cwd: str,
):
    def _post_create_worker_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/create-worker",
            timeout=_DELEGATE_HTTP_TIMEOUT,
            non_json_t_key="runner.delegate_non_json",
            log_prefix="create-worker POST",
            backoff_cap=60.0,
        )

    @tool("create_worker", _CREATE_WORKER_DESCRIPTION, _CREATE_WORKER_INPUT_SCHEMA)
    async def create_worker(args: dict[str, Any]) -> dict[str, Any]:
        worker_description = args.get("worker_description") or ""
        justification = args.get("justification") or ""
        orchestration_mode = args.get("orchestration_mode") or ""
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        if not worker_description or not justification or not orchestration_mode:
            return {
                "content": [{
                    "type": "text",
                    "text": "worker_description, justification and orchestration_mode are required",
                }],
                "is_error": True,
            }
        import uuid as _uuid
        payload = {
            "app_session_id": app_session_id,
            "worker_description": worker_description,
            "justification": justification,
            "orchestration_mode": orchestration_mode,
            "cwd": _resolve_tool_cwd(args, cwd),
            "client_request_id": f"cw_{_uuid.uuid4().hex[:10]}",
            "node_id": node_id,
            "folder_id": args.get("folder_id"),
            "tag_ids": args.get("tag_ids") or [],
        }
        try:
            result = await asyncio.to_thread(_post_create_worker_sync, payload)
        except Exception as e:
            return _tool_error_response("create_worker", e)
        return _tool_success_result(result)

    return create_worker


def _build_ensure_named_worker_tool(
    *,
    cwd: str,
    backend_url: str,
    internal_token: str,
):
    def _post_ensure_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/workers/provision",
            timeout=_DELEGATE_HTTP_TIMEOUT,
            non_json_t_key="runner.delegate_non_json",
            log_prefix="ensure-named-worker POST",
            backoff_cap=60.0,
        )

    @tool("ensure_named_worker", _ENSURE_NAMED_WORKER_DESCRIPTION, _ENSURE_NAMED_WORKER_INPUT_SCHEMA)
    async def ensure_named_worker(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        worker_cwd = _resolve_tool_cwd(args, cwd)
        orchestration_mode = str(args.get("orchestration_mode") or "").strip()
        if not name or not orchestration_mode:
            return {
                "content": [{"type": "text", "text": "name and orchestration_mode are required"}],
                "is_error": True,
            }
        if orchestration_mode == "manager":
            orchestration_mode = "team"
        if orchestration_mode not in ("team", "native"):
            return {
                "content": [{"type": "text", "text": "orchestration_mode must be 'team' or 'native'"}],
                "is_error": True,
            }
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
            "folder_id": args.get("folder_id"),
            "tag_ids": args.get("tag_ids") or [],
        }
        payload = {"cwd": worker_cwd, "workers": [spec]}
        try:
            result = await asyncio.to_thread(_post_ensure_sync, payload)
        except Exception as e:
            return _tool_error_response("ensure_named_worker", e)
        workers = (result or {}).get("workers") or []
        if not workers:
            return _tool_error_response(
                "ensure_named_worker",
                RuntimeError("provision returned no worker"),
            )
        worker = workers[0]
        return _tool_success_result({
            "agent_session_id": worker.get("agent_session_id"),
            "name": worker.get("name"),
            "created": bool(worker.get("created")),
            "orchestration_mode": worker.get("orchestration_mode"),
            "registry_cwd": worker.get("registry_cwd") or worker.get("cwd"),
        })

    return ensure_named_worker


# ============================================================================
# mssg tool builder (team session messaging)
# ============================================================================
def _build_mssg_tool(
    *,
    sender_session_id: str,
    backend_url: str,
    internal_token: str,
):
    def _post_mssg_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/mssg",
            timeout=30,
            non_json_t_key="runner.mssg_non_json",
            log_prefix="mssg POST",
            backoff_cap=5.0,
        )

    @tool("mssg", _MSSG_DESCRIPTION, _MSSG_INPUT_SCHEMA)
    async def mssg(args: dict[str, Any]) -> dict[str, Any]:
        target_session_id = str(args.get("target_session_id") or "").strip()
        target_worker_id = str(args.get("target_worker_id") or "").strip()
        target_worker_pool = str(args.get("target_worker_pool") or "").strip()
        pool_affinity_key = str(args.get("pool_affinity_key") or "").strip()
        message = str(args.get("message") or "").strip()
        if (not target_session_id and not target_worker_id and not target_worker_pool) or not message:
            return {
                "content": [{
                    "type": "text",
                    "text": "one target and message are required",
                }],
                "is_error": True,
            }
        payload = {
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
        }
        try:
            result = await asyncio.to_thread(_post_mssg_sync, payload)
        except Exception as e:
            return _tool_error_response("mssg", e)
        return _tool_success_result(result)

    return mssg


def _build_chat_tool(*, sender_session_id: str):
    @tool("chat", _CHAT_DESCRIPTION, _CHAT_INPUT_SCHEMA)
    async def chat(args: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return {
                "content": [{"type": "text", "text": "chat_id is required"}],
                "is_error": True,
            }
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
            return _tool_error_response("chat", e)
        return _tool_success_result(result)

    return chat


def _build_read_chat_history_tool():
    @tool("read_chat_history", _READ_CHAT_HISTORY_DESCRIPTION, _READ_CHAT_HISTORY_INPUT_SCHEMA)
    async def read_chat_history(args: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return {
                "content": [{"type": "text", "text": "chat_id is required"}],
                "is_error": True,
            }
        try:
            result = await asyncio.to_thread(
                chat_store.read_history,
                chat_id=chat_id,
                limit=int(args.get("limit") or 50),
                before_seq=args.get("before_seq"),
            )
        except Exception as e:
            return _tool_error_response("read_chat_history", e)
        return _tool_success_result(result)

    return read_chat_history


def _build_list_available_provider_models_tool():
    @tool(
        "list_available_provider_models",
        _LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION,
        _LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA,
    )
    async def list_available_provider_models(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                available_provider_models_response,
                str(args.get("provider") or ""),
                str(args.get("model") or ""),
                str(args.get("reasoning_effort") or ""),
            )
        except Exception as e:
            return _tool_error_response("list_available_provider_models", e)
        return _tool_success_result(result)

    return list_available_provider_models


def _build_create_chat_tool(*, sender_session_id: str):
    @tool("create_chat", _CREATE_CHAT_DESCRIPTION, _CREATE_CHAT_INPUT_SCHEMA)
    async def create_chat(args: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return {
                "content": [{"type": "text", "text": "chat_id is required"}],
                "is_error": True,
            }
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
            return _tool_error_response("create_chat", e)
        return _tool_success_result(result)

    return create_chat


def _build_set_chat_sender_policy_tool(*, sender_session_id: str):
    @tool("set_chat_sender_policy", _SET_CHAT_SENDER_POLICY_DESCRIPTION, _SET_CHAT_SENDER_POLICY_INPUT_SCHEMA)
    async def set_chat_sender_policy(args: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return {
                "content": [{"type": "text", "text": "chat_id is required"}],
                "is_error": True,
            }
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
            return _tool_error_response("set_chat_sender_policy", e)
        return _tool_success_result(result)

    return set_chat_sender_policy


def _build_delete_chat_tool():
    @tool("delete_chat", _DELETE_CHAT_DESCRIPTION, _DELETE_CHAT_INPUT_SCHEMA)
    async def delete_chat(args: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(args.get("chat_id") or "").strip()
        if not chat_id:
            return {
                "content": [{"type": "text", "text": "chat_id is required"}],
                "is_error": True,
            }
        try:
            existed = await asyncio.to_thread(chat_store.delete_chat, chat_id)
        except Exception as e:
            return _tool_error_response("delete_chat", e)
        return _tool_success_result({"chat_id": chat_id, "deleted": existed})

    return delete_chat


def _build_delegate_task_tool(
    *,
    sender_session_id: str,
    cwd: str,
    model: Optional[str],
    backend_url: str,
    internal_token: str,
):
    def _post_delegate_task_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/delegate-task",
            timeout=_DELEGATE_HTTP_TIMEOUT,  # approval modes can block long
            non_json_t_key="runner.mssg_non_json",
            log_prefix="delegate_task POST",
            backoff_cap=5.0,
        )

    @tool("delegate_task", _DELEGATE_TASK_DESCRIPTION, _DELEGATE_TASK_INPUT_SCHEMA)
    async def delegate_task(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args.get("task") or "").strip()
        if not task:
            return {
                "content": [{"type": "text", "text": "task is required"}],
                "is_error": True,
            }
        target = args.get("target_session_id")
        if target in ("", "null"):
            target = None
        payload = {
            "sender_session_id": sender_session_id,
            "task": task,
            "target_session_id": target,
            "cwd": _resolve_tool_cwd(args, cwd),
            "provider_id": str(args.get("provider_id") or "").strip() or None,
            "model": str(args.get("model") or "").strip(),
            "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
            "sub_session": args.get("sub_session") is not False,
            "folder_id": args.get("folder_id"),
            "tag_ids": args.get("tag_ids") or [],
        }
        try:
            result = await asyncio.to_thread(_post_delegate_task_sync, payload)
        except Exception as e:
            return _tool_error_response("delegate_task", e)
        return _tool_success_result(result)

    return delegate_task


_CAPABILITY_ID_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "capability_id": {
            "type": "string",
            "description": "Full capability id, e.g. 'ofek.testape:testape'.",
        }
    },
    "required": ["capability_id"],
}
_CAPABILITY_HTTP_TIMEOUT = 30.0


def _build_capability_tools(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
):
    """Better Agent runtime-capability management. Lets the model scope its own
    session — load a capability (its MCP + skill become available next turn),
    release it, or list what's loadable. Core owns the write; these tools POST
    over the internal loopback."""
    url_path = f"/api/internal/sessions/{app_session_id}/capabilities"

    def _post(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path=url_path,
            timeout=_CAPABILITY_HTTP_TIMEOUT,
            non_json_t_key="runner.mssg_non_json",
            log_prefix="capabilities POST",
            backoff_cap=5.0,
        )

    async def _run_action(name: str, payload: dict) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(_post, payload)
        except Exception as e:
            return _tool_error_response(name, e)
        return _tool_success_result(result)

    @tool(
        "list_capabilities",
        "List the scoped capabilities loadable in this session and which are active.",
        {"type": "object", "properties": {}},
    )
    async def list_capabilities(args: dict[str, Any]) -> dict[str, Any]:
        return await _run_action("list_capabilities", {"action": "list"})

    @tool(
        "load_capability",
        "Load a scoped capability into this session. Its MCP + skill become "
        "available on the next turn.",
        _CAPABILITY_ID_INPUT_SCHEMA,
    )
    async def load_capability(args: dict[str, Any]) -> dict[str, Any]:
        capability_id = str(args.get("capability_id") or "").strip()
        if not capability_id:
            return {
                "content": [{"type": "text", "text": "capability_id is required"}],
                "is_error": True,
            }
        return await _run_action(
            "load_capability", {"action": "load", "capability_id": capability_id}
        )

    @tool(
        "release_capability",
        "Release a previously loaded capability from this session.",
        _CAPABILITY_ID_INPUT_SCHEMA,
    )
    async def release_capability(args: dict[str, Any]) -> dict[str, Any]:
        capability_id = str(args.get("capability_id") or "").strip()
        if not capability_id:
            return {
                "content": [{"type": "text", "text": "capability_id is required"}],
                "is_error": True,
            }
        return await _run_action(
            "release_capability", {"action": "release", "capability_id": capability_id}
        )

    return [list_capabilities, load_capability, release_capability]


def _build_create_session_tool(
    *,
    sender_session_id: str,
    cwd: str,
    model: Optional[str],
    backend_url: str,
    internal_token: str,
):
    def _post_create_session_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/create-session",
            timeout=30,
            non_json_t_key="runner.delegate_non_json",
            log_prefix="create-session POST",
            backoff_cap=5.0,
        )

    @tool("create_session", _CREATE_SESSION_DESCRIPTION, _CREATE_SESSION_INPUT_SCHEMA)
    async def create_session(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return {
                "content": [{"type": "text", "text": "name is required"}],
                "is_error": True,
            }
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        payload = {
            "sender_session_id": sender_session_id,
            "name": name,
            "cwd": _resolve_tool_cwd(args, cwd),
            "provider_id": str(args.get("provider_id") or "").strip() or None,
            "model": str(args.get("model") or "").strip(),
            "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
            "orchestration_mode": args.get("orchestration_mode") or "native",
            "node_id": node_id,
            "folder_id": args.get("folder_id"),
            "tag_ids": args.get("tag_ids") or [],
        }
        try:
            result = await asyncio.to_thread(_post_create_session_sync, payload)
        except Exception as e:
            return _tool_error_response("create_session", e)
        return _tool_success_result(result)

    return create_session


def _build_create_sub_session_tool(
    *,
    sender_session_id: str,
    cwd: str,
    model: Optional[str],
    backend_url: str,
    internal_token: str,
):
    def _post_create_sub_session_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/create-sub-session",
            timeout=30,
            non_json_t_key="runner.delegate_non_json",
            log_prefix="create-sub-session POST",
            backoff_cap=5.0,
        )

    @tool("create_sub_session", _CREATE_SUB_SESSION_DESCRIPTION, _CREATE_SUB_SESSION_INPUT_SCHEMA)
    async def create_sub_session(args: dict[str, Any]) -> dict[str, Any]:
        node_id = args.get("node_id")
        if node_id in ("", "null"):
            node_id = None
        payload = {
            "sender_session_id": sender_session_id,
            "description": str(args.get("description") or "").strip(),
            "cwd": _resolve_tool_cwd(args, cwd),
            "provider_id": str(args.get("provider_id") or "").strip() or None,
            "model": str(args.get("model") or "").strip(),
            "reasoning_effort": str(args.get("reasoning_effort") or "").strip() or None,
            "node_id": node_id,
            "folder_id": args.get("folder_id"),
            "tag_ids": args.get("tag_ids") or [],
        }
        try:
            result = await asyncio.to_thread(_post_create_sub_session_sync, payload)
        except Exception as e:
            return _tool_error_response("create_sub_session", e)
        return _tool_success_result(result)

    return create_sub_session


def _build_ask_tool(
    *,
    sender_session_id: str,
    app_session_id: str,
    model: Optional[str],
    cwd: str,
    backend_url: str,
    internal_token: str,
):
    @tool("ask", _ASK_DESCRIPTION, _ASK_INPUT_SCHEMA)
    async def ask(args: dict[str, Any]) -> dict[str, Any]:
        target_session_id = str(args.get("target_session_id") or "").strip()
        target_worker_id = str(args.get("target_worker_id") or "").strip()
        target_worker_pool = str(args.get("target_worker_pool") or "").strip()
        pool_affinity_key = str(args.get("pool_affinity_key") or "").strip()
        message = str(args.get("message") or "").strip()
        run_mode = str(args.get("run_mode") or "direct").strip() or "direct"
        try:
            mode = normalize_ask_mode(args.get("mode"))
        except ValueError as exc:
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "is_error": True,
            }
        if (not target_session_id and not target_worker_id and not target_worker_pool) or not message:
            return {
                "content": [{
                    "type": "text",
                    "text": "one target and message are required",
                }],
                "is_error": True,
            }
        if run_mode not in ("direct", "fork"):
            return {
                "content": [{"type": "text", "text": "run_mode must be 'direct' or 'fork'"}],
                "is_error": True,
            }
        if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC and run_mode == "fork":
            return {
                "content": [{"type": "text", "text": "async ask mode requires run_mode='direct'"}],
                "is_error": True,
            }
        ephemeral = bool(args.get("ephemeral"))
        if ephemeral and run_mode != "fork":
            return {
                "content": [{
                    "type": "text",
                    "text": "ephemeral is only valid for run_mode='fork'",
                }],
                "is_error": True,
            }

        if run_mode == "fork":
            if not target_session_id:
                return {
                    "content": [{"type": "text", "text": "run_mode='fork' requires target_session_id"}],
                    "is_error": True,
                }
            # Fork reuses the delegation engine (per-(caller, session) branch +
            # structured jsonl-offset outcome). ask is the model-facing name;
            # the fork execution path stays single-source in run_delegation.
            worker_description = str(args.get("worker_description") or "").strip()
            worker_registry_cwd = args.get("worker_registry_cwd")
            if worker_registry_cwd in ("", "null"):
                worker_registry_cwd = None
            import uuid as _duuid
            client_delegation_id = f"del_{_duuid.uuid4().hex[:10]}"
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

            def _post_fork_sync() -> dict:
                return _post_loopback_sync(
                    payload,
                    backend_url=backend_url,
                    internal_token=internal_token,
                    url_path="/api/internal/ask-fork",
                    timeout=_DELEGATE_HTTP_TIMEOUT,
                    non_json_t_key="runner.delegate_non_json",
                    log_prefix="ask(fork) POST",
                    backoff_cap=60.0,
                    recover=lambda: _recover_delegate_result(client_delegation_id),
                )

            try:
                result = await asyncio.to_thread(_post_fork_sync)
            except Exception as e:
                return _tool_error_response("ask", e)
            return _tool_success_result(result)

        # direct: team message on the target's real session, wait for reply.
        import uuid as _uuid
        ask_id = f"ask_{_uuid.uuid4().hex[:10]}"
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

        def _post_ask_sync() -> dict:
            return _post_loopback_sync(
                payload,
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/ask",
                timeout=30 if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC else _DELEGATE_HTTP_TIMEOUT,
                non_json_t_key="runner.mssg_non_json",
                log_prefix="ask POST",
                backoff_cap=60.0,
                recover=(None if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC else lambda: _recover_ask_result(ask_id)),
            )

        try:
            result = await asyncio.to_thread(_post_ask_sync)
        except Exception as e:
            return _tool_error_response("ask", e)
        return _tool_success_result(result)

    return ask


# ============================================================================
# Open-file-panel tool builder (active session, any orchestration mode)
# ============================================================================
def _build_open_file_panel_tool(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
):
    """Build an in-process SDK MCP tool that opens a file in the user's
    UI. POSTs to /api/internal/open-file-panel. Mirrors the browser-harness
    tool's loopback pattern but is fast (no long block)."""

    def _post_open_file_panel_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/open-file-panel",
            timeout=_OPEN_FILE_PANEL_HTTP_TIMEOUT,
            non_json_t_key="runner.open_file_panel_non_json",
            log_prefix="open-file-panel POST",
            backoff_cap=10.0,
        )

    @tool("open_file_panel", _OPEN_FILE_PANEL_DESCRIPTION, _OPEN_FILE_PANEL_INPUT_SCHEMA)
    async def open_file_panel(args: dict[str, Any]) -> dict[str, Any]:
        mode = args.get("mode") or ""
        path = (args.get("path") or "").strip()
        if mode not in ("panel", "inline") or not path:
            return {
                "content": [{
                    "type": "text",
                    "text": "`mode` (panel|inline) and `path` are required",
                }],
                "is_error": True,
            }

        payload = {
            "app_session_id": app_session_id,
            "mode": mode,
            "path": path,
            "start_line": args.get("start_line"),
            "end_line": args.get("end_line"),
            "selected_start": args.get("selected_start"),
            "selected_end": args.get("selected_end"),
        }

        try:
            result = await asyncio.to_thread(_post_open_file_panel_sync, payload)
        except Exception as e:
            return _tool_error_response("open-file-panel", e)
        return _tool_success_result(result)

    return open_file_panel


def _build_request_user_input_tool(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
    run_id: str,
):
    def _post_request_user_input_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/user-input/request",
            timeout=_DELEGATE_HTTP_TIMEOUT,
            non_json_t_key="runner.open_file_panel_non_json",
            log_prefix="request-user-input POST",
            backoff_cap=60.0,
        )

    @tool("request_user_input", _REQUEST_USER_INPUT_DESCRIPTION, _REQUEST_USER_INPUT_SCHEMA)
    async def request_user_input(args: dict[str, Any]) -> dict[str, Any]:
        questions = args.get("questions")
        if not isinstance(questions, list) or not questions:
            return {
                "content": [{"type": "text", "text": "`questions` must be a non-empty array"}],
                "is_error": True,
            }
        payload = {
            "app_session_id": app_session_id,
            "questions": questions,
            "timeout_seconds": args.get("timeout_seconds"),
            "logical_request_id": user_input_logical_request_id("claude", run_id, questions),
        }
        try:
            result = await asyncio.to_thread(_post_request_user_input_sync, payload)
        except Exception as e:
            return _tool_error_response("request-user-input", e)
        return _tool_success_result(result)

    return request_user_input


def _build_start_file_discussion_tool(
    *,
    app_session_id: str,
    backend_url: str,
    internal_token: str,
):
    def _post_start_file_discussion_sync(payload: dict) -> dict:
        return _post_loopback_sync(
            payload,
            backend_url=backend_url,
            internal_token=internal_token,
            url_path="/api/internal/file-editor/start-discussion",
            timeout=_OPEN_FILE_PANEL_HTTP_TIMEOUT,
            non_json_t_key="runner.open_file_panel_non_json",
            log_prefix="start-file-discussion POST",
            backoff_cap=10.0,
        )

    @tool("start_file_discussion", _START_FILE_DISCUSSION_DESCRIPTION, _START_FILE_DISCUSSION_INPUT_SCHEMA)
    async def start_file_discussion(args: dict[str, Any]) -> dict[str, Any]:
        file_path = (args.get("file_path") or "").strip()
        line = args.get("line")
        if not file_path or not isinstance(line, int) or line < 1:
            return {
                "content": [{"type": "text", "text": "`file_path` and `line >= 1` are required"}],
                "is_error": True,
            }
        try:
            result = await asyncio.to_thread(_post_start_file_discussion_sync, {
                "app_session_id": app_session_id,
                "file_path": file_path,
                "line": line,
                "title": args.get("title") or "",
            })
        except Exception as e:
            return _tool_error_response("start-file-discussion", e)
        return _tool_success_result(result)

    return start_file_discussion


# ============================================================================
# Session picker tool. `propose_sessions` stamps the inline session picker
# (`ask_result`) on the CALLING session's in-flight assistant message via
# /api/internal/ask-propose. The Ask flow stamps it directly server-side
# (session_search.propose_sessions); this MCP tool is the agent-facing
# wrapper for any native session, registered on the session-bridge server.
# ============================================================================
# Mirrors session_search.ASK_SINGLETON_ID. Local literal (not an import) so
# the runner subprocess doesn't drag session_search's import graph.
_ASK_SINGLETON_ID = "virtual:ofek-dev.ask:ask"

_PROPOSE_SESSIONS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Session ids to offer in the picker (parent dir "
                           "names of the matched events.jsonl), most relevant "
                           "first (≤5). Empty if nothing is relevant.",
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence on why these match.",
        },
    },
    "required": ["session_ids"],
}
_PROPOSE_SESSIONS_DESCRIPTION = (
    "Present the chosen sessions to the user as an inline picker in this "
    "session. Call after `search_sessions` when you want the user to pick a "
    "target. Pass an empty list for 'create new'."
)


def _build_propose_sessions_tool(
    *, app_session_id: str, backend_url: str, internal_token: str,
):
    """The `propose_sessions` MCP tool — stamps the picker on the calling
    session's in-flight assistant message."""

    @tool(
        "propose_sessions",
        _PROPOSE_SESSIONS_DESCRIPTION,
        _PROPOSE_SESSIONS_INPUT_SCHEMA,
    )
    async def propose_sessions(args: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "caller_sid": app_session_id,
            "session_ids": args.get("session_ids") or [],
            "reasoning": args.get("reasoning") or "",
        }
        try:
            result = await asyncio.to_thread(
                _post_loopback_sync,
                payload,
                backend_url=backend_url,
                internal_token=internal_token,
                url_path="/api/internal/ask-propose",
                timeout=10.0,
                non_json_t_key="runner.open_file_panel_non_json",
                log_prefix="ask POST /api/internal/ask-propose",
                backoff_cap=10.0,
            )
        except Exception as e:
            return _tool_error_response("ask-propose", e)
        return _tool_success_result(result)

    return propose_sessions


def _message_id(message: object) -> Optional[str]:
    if isinstance(message, dict):
        value = message.get("message_id") or message.get("id")
    else:
        value = getattr(message, "message_id", None) or getattr(message, "id", None)
    return str(value) if value else None


def _context_overflow_error(stop_reason: Optional[str]) -> Optional[str]:
    return normalize_context_overflow_error(stop_reason)


# ============================================================================
# Prompt composition
# ============================================================================


def _compose_prompt_text(prompt: str, files: list, log: logging.Logger) -> str:
    """Final prompt text sent to the CLI: file-attachment preamble + prompt."""
    if not files:
        return prompt
    file_sections: list[str] = []
    for f in files:
        try:
            raw = base64.b64decode(f.get("data", ""))
            name = f.get("name", "unknown")
        except Exception:
            log.warning("Skipping malformed file attachment: %s", f.get("name", "?"))
            continue
        try:
            text = raw.decode("utf-8")
            file_sections.append(
                f"<file name=\"{name}\">\n{text}\n</file>"
            )
        except UnicodeDecodeError:
            file_sections.append(
                f"<file name=\"{name}\">[binary file, {f.get('size', len(raw))} bytes]</file>"
            )
    file_preamble = "\n\n".join(file_sections)
    return f"{file_preamble}\n\n{prompt}" if prompt else file_preamble



# ============================================================================
# Runner lifecycle primitives — heartbeat
# ============================================================================


async def _heartbeat_writer(
    run_dir: Path,
    current_turn_holder: list,
    shutdown_event: asyncio.Event,
    *,
    interval_s: float = 5.0,
) -> None:
    """Refresh `runs/<run_id>/runner_alive` every interval.

    The file payload includes `current_turn_id` (None when idle between
    turns) so backend recovery can distinguish "runner intentionally idle
    waiting for next prompt" from "runner crashed mid-turn." Stops on
    `shutdown_event.set()`. Errors are logged but never propagate.
    """
    from runs_dir import atomic_write_json, runner_alive_path

    alive_path = runner_alive_path(run_dir)
    while not shutdown_event.is_set():
        try:
            atomic_write_json(alive_path, {
                "pid": os.getpid(),
                "current_turn_id": current_turn_holder[0],
                "timestamp": datetime.now().isoformat(),
            })
        except Exception:
            logger.exception("heartbeat write failed")
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=interval_s,
            )
        except asyncio.TimeoutError:
            pass


def _persist_activity_state(
    state: dict,
    state_path: Path,
    *,
    foreground_status: Optional[str] = None,
    background_work_ids: Optional[set[str]] = None,
) -> bool:
    next_state = transition_activity(
        state,
        foreground_status=foreground_status,
        background_work_ids=background_work_ids,
    )
    if next_state is None:
        return False
    _atomic_write_json(state_path, next_state)
    state.clear()
    state.update(next_state)
    return True


def _apply_task_message(msg: object, tasks: set[str]) -> bool:
    """Fold one SDK message into the in-flight background-subagent set.

    `TaskStartedMessage` adds the task; a terminal `TaskNotificationMessage`
    (completed/failed/stopped) removes it. Anything else is ignored."""
    before = frozenset(tasks)
    if isinstance(msg, TaskStartedMessage):
        tid = getattr(msg, "task_id", None)
        if tid:
            tasks.add(tid)
    elif isinstance(msg, TaskNotificationMessage):
        tid = getattr(msg, "task_id", None)
        if tid:
            tasks.discard(tid)
    return before != tasks


async def _background_response_activity_active(
    *,
    outstanding_tasks: set[str],
    process_controller: object,
    log: logging.Logger,
) -> bool:
    if outstanding_tasks:
        return True
    try:
        return bool(
            await asyncio.to_thread(
                process_controller.has_detached_descendants,
                os.getpid(),
                frozenset(),
            )
        )
    except Exception:
        log.exception("runner background activity check failed")
        return False



# ============================================================================
# Per-turn helper — drives ONE turn on an already-connected SDK client
# ============================================================================
async def _drain_until_result(
    resp_iter, log: logging.Logger, timeout_s: float = 15.0,
) -> bool:
    """Settle barrier after an interrupt.

    `client.interrupt()` only awaits the CLI's control-response ACK — the
    CLI is still winding the interrupted turn down and will emit its tail
    (tool aborts) plus a terminating ``ResultMessage``. Consume and DISCARD
    those so the client is fully settled before the runner proceeds to
    its completion path. Skipping this leaves the interrupted turn's tail
    unread in the stream.

    Bounded by ``timeout_s`` so a CLI that never terminates can't hang the
    runner. Returns True if the stream settled (ResultMessage or natural
    end), False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "interrupt settle drain timed out after %.1fs", timeout_s,
            )
            return False
        try:
            msg = await asyncio.wait_for(
                resp_iter.__anext__(), timeout=remaining,
            )
        except asyncio.TimeoutError:
            log.warning(
                "interrupt settle drain timed out after %.1fs", timeout_s,
            )
            return False
        except StopAsyncIteration:
            return True
        if isinstance(msg, ResultMessage):
            return True


async def _receive_response_message(
    resp_iter,
    *,
    timeout_s: float,
    activity: Optional[_RunnerActivity] = None,
    background_activity: Optional[_BackgroundActivityProbe] = None,
) -> object:
    if timeout_s <= 0:
        return await resp_iter.__anext__()

    receive_task = asyncio.create_task(resp_iter.__anext__())
    static_started_at = time.monotonic()
    try:
        while True:
            if background_activity is not None and await background_activity():
                if activity is not None:
                    activity.mark()
                else:
                    static_started_at = time.monotonic()
            last_progress_at = (
                activity.last_progress_at() if activity is not None else static_started_at
            )
            remaining = timeout_s - (time.monotonic() - last_progress_at)
            if remaining <= 0:
                raise ResponseNoProgressError(
                    f"Claude runner made no response progress for {timeout_s:.0f}s"
                )
            done, _pending = await asyncio.wait(
                {receive_task},
                timeout=min(_RESPONSE_ACTIVITY_POLL_S, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                if activity is not None:
                    activity.mark()
                return receive_task.result()
    except BaseException:
        if not receive_task.done():
            receive_task.cancel()
            with suppress(BaseException):
                await receive_task
        raise


def _jsonl_byte_offset_after_lines(path: Path, line_count: int) -> Optional[int]:
    if line_count <= 0:
        return 0
    try:
        with path.open("rb") as f:
            for _ in range(line_count):
                if f.readline() == b"":
                    return None
            return f.tell()
    except OSError:
        return None


def _fork_prefix_byte_offset(path: Path, line_count: int, log: logging.Logger) -> int:
    deadline = time.monotonic() + 2.0
    while True:
        offset = _jsonl_byte_offset_after_lines(path, line_count)
        if offset is not None:
            return offset
        if time.monotonic() >= deadline:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            log.warning(
                "fork prefix boundary unavailable for %s lines in %s; starting at EOF %d",
                line_count,
                path,
                size,
            )
            return size
        time.sleep(0.05)


def _fork_parent_line_count(provider_run_config: object) -> int:
    if not isinstance(provider_run_config, dict):
        return 0
    try:
        return int(provider_run_config.get("fork_parent_line_count") or 0)
    except (TypeError, ValueError):
        return 0


async def _run_one_turn(
    *,
    client: ClaudeSDKClient,
    prompt: str,
    images: list,
    files: list,
    run_dir: Path,
    turn_id: str,
    pre_query_byte_offset: int,
    state: dict,
    state_path: Path,
    cwd: str,
    claude_config_dir: Path,
    log: logging.Logger,
    cancel_path: Optional[Path] = None,
    interactive_permissions: bool = False,
    current_turn_holder: Optional[list] = None,
    no_progress_timeout_s: float = _RESPONSE_NO_PROGRESS_TIMEOUT_S,
    fork_parent_line_count: int = 0,
) -> dict:
    """Execute one turn against an already-connected `ClaudeSDKClient`.

    Side-effects:
    - Writes `runs/<run_id>/turns/<turn_id>/start.json` BEFORE the query
      so crash recovery can identify the in-flight turn.
    - Writes `runs/<run_id>/turns/<turn_id>/complete.json` at turn end —
      `runs_dir.read_best_complete` salvages it when the runner dies in
      the window before the run-level complete.json lands.
    - Mutates the caller's `state` dict (sid discovery → state.session_id
      / state.jsonl_path) and writes the run-level `state.json` on
      discovery.

    Watches `run_dir/cancel` (run-level sentinel).

    Does NOT manage `client.connect()` / `client.disconnect()` — caller
    owns the SDK client lifecycle.
    """
    from runs_dir import atomic_write_json, turn_dir

    turn_d = turn_dir(run_dir, turn_id)
    try:
        turn_d.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("failed to mkdir turn dir %s", turn_d)

    # Per-turn start.json — recovery key. pre_query_byte_offset is the
    # jsonl-baseline this turn started against.
    start_payload = {
        "turn_id": turn_id,
        "pre_query_byte_offset": pre_query_byte_offset,
        "started_at": datetime.now().isoformat(),
    }
    try:
        atomic_write_json(turn_d / "start.json", start_payload)
    except Exception:
        logger.exception("failed to write turn start.json")

    discovered_sid: Optional[str] = None
    total_usage: dict = {}
    assistant_usage_snapshots: list[tuple[Optional[str], object]] = []
    result_usage: object = None
    success = False
    error: Optional[str] = None
    cancelled = False
    sdk_output_parts: list[str] = []
    # Last text block of the last PRIMARY assistant message (subagent
    # messages carry parent_tool_use_id and live in their own jsonl).
    # Stamped into complete.json as `final_assistant_text` so the
    # backend's tailer drain can verify the CLI flushed the turn's
    # final text line before firing `complete`.
    final_assistant_text: Optional[str] = None
    context_window: Optional[int] = None
    last_stop_reason: Optional[str] = None
    result_seen = False
    assistant_seen = False
    # Tool names the model called this turn — surfaced in complete.json
    # for the UI/telemetry. (Reap is decided by live background processes,
    # not tool names.)
    used_tools: set[str] = set()
    # In-flight subagent tasks (`TaskStarted`/`TaskNotification` bookends).
    # Feeds the no-progress watchdog's activity probe: a turn is not stuck
    # while a subagent is still running.
    outstanding_tasks: set[str] = set()

    # Cancel sentinel watcher: polls for `cancel_path` every ~150ms
    # and calls client.interrupt() on sight. Default = run-level
    # `run_dir/cancel` (today's behavior). Persistent `_main_loop`
    # passes `turn_dir(run_dir, turn_id)/cancel` per turn so cancel is
    # turn-scoped — cancelling turn N does NOT abort turn N+1.
    cancel_seen = asyncio.Event()
    if cancel_path is None:
        cancel_path = run_dir / "cancel"

    # `cancelled` mutated by the watcher coroutine via list-cell (avoids
    # `nonlocal` since the helper is at module scope, not nested).
    cancelled_cell = [False]

    async def _cancel_watcher() -> None:
        while not cancel_seen.is_set():
            if cancel_path.exists():
                cancelled_cell[0] = True
                log.info("cancel sentinel seen, calling client.interrupt()")
                try:
                    await client.interrupt()
                except Exception:
                    logger.exception("client.interrupt() failed")
                cancel_seen.set()
                return
            try:
                await asyncio.wait_for(cancel_seen.wait(), timeout=0.15)
            except asyncio.TimeoutError:
                pass

    from proc_control import process_control

    response_activity_process_controller = process_control()
    outstanding_tool_calls = _OutstandingToolCalls()

    async def _background_activity_probe() -> bool:
        if outstanding_tool_calls.busy(log):
            return True
        return await _background_response_activity_active(
            outstanding_tasks=outstanding_tasks,
            process_controller=response_activity_process_controller,
            log=log,
        )

    watcher_task: Optional[asyncio.Task] = None
    activity = _RunnerActivity()
    activity_token = _runner_activity_var.set(activity)
    _set_active_runner_activity(activity)

    try:
        if current_turn_holder is not None:
            current_turn_holder[0] = turn_id
        # Inject file contents into the prompt for non-image attachments.
        prompt = _compose_prompt_text(prompt, files, log)

        if images:
            content: list[dict] = []
            for img in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img["media_type"],
                        "data": img["data"],
                    },
                })
            # Image-only messages: skip the text item so the model
            # doesn't see a synthetic placeholder.
            if prompt:
                content.append({"type": "text", "text": prompt})

            async def _multimodal_msg():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "parent_tool_use_id": None,
                }

            await client.query(_multimodal_msg())
        elif interactive_permissions:
            # can_use_tool set → SDK requires an async-iterable prompt.
            _text = prompt

            async def _text_msg():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": _text}]},
                    "parent_tool_use_id": None,
                }

            await client.query(_text_msg())
        else:
            await client.query(prompt)

        watcher_task = asyncio.create_task(_cancel_watcher())

        resp_iter = client.receive_response()
        while True:
            try:
                msg = await _receive_response_message(
                    resp_iter,
                    timeout_s=no_progress_timeout_s,
                    activity=activity,
                    background_activity=_background_activity_probe,
                )
            except StopAsyncIteration:
                break
            outstanding_tool_calls.apply(msg)
            if cancelled_cell[0]:
                # Interrupt landed. Don't process this turn's tail — but DO
                # drain it to the terminating ResultMessage so the client
                # is fully settled before the completion path runs. See
                # _drain_until_result.
                if not isinstance(msg, ResultMessage):
                    await _drain_until_result(resp_iter, log)
                break

            if isinstance(msg, SystemMessage):
                # Track in-flight subagents for the no-progress watchdog's
                # activity probe. No-ops for non-Task system messages.
                if _apply_task_message(msg, outstanding_tasks):
                    try:
                        _persist_activity_state(
                            state,
                            state_path,
                            background_work_ids=outstanding_tasks,
                        )
                    except Exception:
                        logger.exception("failed to persist background task activity")
                data = msg.data or {}
                if data.get("subtype") == "init":
                    sid = data.get("session_id")
                    if sid and sid != discovered_sid:
                        discovered_sid = sid
                        jsonl_path = (
                            claude_config_dir / "projects"
                            / encode_cwd(cwd) / f"{sid}.jsonl"
                        )
                        if fork_parent_line_count > 0:
                            pre_query_byte_offset = _fork_prefix_byte_offset(
                                jsonl_path,
                                fork_parent_line_count,
                                log,
                            )
                        state["session_id"] = sid
                        state["jsonl_path"] = str(jsonl_path)
                        state["pre_query_byte_offset"] = pre_query_byte_offset
                        try:
                            state["pre_query_jsonl_inode"] = jsonl_path.stat().st_ino
                        except OSError:
                            state["pre_query_jsonl_inode"] = None
                        # Persist the provider CLI's pid so restart recovery can
                        # tell a still-running CLI (whose wrapper died) from a
                        # genuinely dead run and re-attach instead of declaring
                        # it failed. SDK-internal; best-effort.
                        try:
                            _cli_proc = client._transport._process
                            _cli_pid = getattr(_cli_proc, "pid", None)
                            state["cli_pid"] = int(_cli_pid) if _cli_pid else None
                        except Exception:
                            state["cli_pid"] = None
                        try:
                            _atomic_write_json(state_path, state)
                            log.info("state.json written: session_id=%s", sid)
                        except Exception:
                            logger.exception("failed to write state.json")

            elif isinstance(msg, AssistantMessage):
                assistant_seen = True
                # Capture assistant text as fallback for when the CLI
                # doesn't write a session jsonl (e.g. API credentials).
                # ALSO record tool names (surfaced in complete.json).
                msg_texts: list[str] = []
                for block in (msg.content or []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        t_ = block.get("text")
                        if isinstance(t_, str) and t_:
                            msg_texts.append(t_)
                    elif hasattr(block, "text") and block.text:
                        msg_texts.append(block.text)
                    # Tool-use block — record tool name for lazy decision.
                    if _block_type(block) in ("ToolUseBlock", "tool_use"):
                        tname = _block_field(block, "name")
                        if tname:
                            used_tools.add(tname)
                sdk_output_parts.extend(msg_texts)
                if msg_texts and not getattr(msg, "parent_tool_use_id", None):
                    final_assistant_text = msg_texts[-1]
                usage = getattr(msg, "usage", None)
                if usage:
                    assistant_usage_snapshots.append((_message_id(msg), usage))
                # API-level error surfaced by the SDK on the assistant
                # message (e.g. `rate_limit`, `auth`, etc.). Capture it
                # NOW — Z.AI's `ResultMessage.subtype` mislabels these
                # as "success" despite `is_error=True`, so we'd lose the
                # real classification if we waited for the result frame.
                # The machine label itself can be wrong: the CLI stamps
                # Z.AI context-window overflows as `max_output_tokens`
                # while the human-readable text says "context window
                # limit" — so the error message TEXT wins over the label
                # when it matches a context-overflow phrase. Otherwise
                # turn_manager's overflow→continuation gate never fires.
                msg_error = getattr(msg, "error", None)
                if msg_error and not error:
                    error = (
                        normalize_context_overflow_error(" ".join(msg_texts))
                        or str(msg_error)
                    )
                sr = getattr(msg, "stop_reason", None)
                if sr:
                    last_stop_reason = sr

            elif isinstance(msg, ResultMessage):
                result_seen = True
                success = not msg.is_error
                rsr = getattr(msg, "stop_reason", None)
                if rsr:
                    last_stop_reason = rsr
                if msg.is_error and not error:
                    # Same label-vs-text rule as the assistant-message
                    # capture: an overflow phrase in the result text wins
                    # over the CLI's generic subtype.
                    error = (
                        normalize_context_overflow_error(msg.result)
                        or msg.subtype
                        or "error"
                    )
                if msg.result and not sdk_output_parts:
                    sdk_output_parts.append(msg.result)
                # Z.AI returns subtype="unknown" for timeouts — the real
                # error text is in the result string. Reclassify so the
                # orchestrator's transient-error retry can match it.
                if error and error in ("unknown", "error") and msg.result:
                    rl = msg.result.lower()
                    if "timed out" in rl or "timeout" in rl:
                        error = "timeout"
                usage = getattr(msg, "usage", None)
                if usage:
                    result_usage = usage
                # Extract context window from model usage metadata.
                # model_usage is {"model_name": {contextWindow, ...}, ...}
                mu = getattr(msg, "model_usage", None)
                if mu:
                    for model_info in (mu.values() if isinstance(mu, dict) else []):
                        cw = model_info.get("contextWindow") if isinstance(model_info, dict) else None
                        if cw:
                            context_window = cw
                            break
                break

        if result_seen:
            try:
                await resp_iter.__anext__()
            except StopAsyncIteration:
                pass
            except Exception as e:
                if _is_network_error(e):
                    raise
                logger.exception("SDK response stream failed while closing")
                success = False
                if not error:
                    error = f"{type(e).__name__}: {e}"

    except asyncio.CancelledError:
        cancelled_cell[0] = True
        error = t("runner.cancelled")
    except ResponseNoProgressError as e:
        log.warning("%s", e)
        error = f"{type(e).__name__}: {e}"
    except Exception as e:
        if _is_network_error(e):
            raise
        logger.exception("SDK run failed")
        error = f"{type(e).__name__}: {e}"
    finally:
        _set_active_runner_activity(None)
        _runner_activity_var.reset(activity_token)
        if current_turn_holder is not None:
            current_turn_holder[0] = None
        cancel_seen.set()
        if watcher_task and not watcher_task.done():
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    cancelled = cancelled_cell[0]
    if cancelled and not error:
        error = t("runner.cancelled")

    total_usage = (
        aggregate_claude_turn_usage(assistant_usage_snapshots, result_usage)
        or {}
    )

    # Ghost-completion guard: when a second --resume CLI is spawned while
    # a previous instance still holds the session, the CLI cross-process
    # enqueues the prompt into the live instance and returns a zero-token
    # "success" ResultMessage with NO assistant output — the prompt was
    # never executed by THIS run. Shared with the Codex runner so both
    # providers fail closed identically instead of binding a fake reply.
    success, error = apply_ghost_completion_guard(
        success=success,
        cancelled=cancelled,
        error=error,
        prompt=prompt,
        assistant_seen=assistant_seen,
        total_usage=total_usage,
        result_seen=result_seen,
    )

    overflow_error = _context_overflow_error(last_stop_reason)
    if overflow_error:
        success = False
        if not error:
            error = overflow_error

    # Bounded-graceful turn-stop: when the cancel sentinel fired, sweep
    # any setsid'd `run_in_background` shells the CLI spawned under us
    # before this turn ends. The backend's old `cancel_run` killpg
    # tower included this sweep; under the soft path the runner owns
    # it (CLI + same-pgroup descendants are skipped — pgroup match).
    # Runs ONCE after `_drain_until_result` returns; CLI close happens
    # later via SDK `disconnect()` and is pid-targeted.
    if cancelled:
        try:
            from proc_control import process_control
            swept = process_control().kill_detached_descendant_groups(
                os.getpid(),
            )
            if swept:
                log.info("runner bg-sweep: signalled %d detached group(s)", swept)
        except Exception:
            logger.exception("runner bg-sweep failed")

    final_success = success and not cancelled and not error

    foreground_status = "completed" if final_success else "failed"
    if cancelled:
        foreground_status = "cancelled"
    try:
        _persist_activity_state(
            state,
            state_path,
            foreground_status=foreground_status,
            background_work_ids=outstanding_tasks,
        )
    except Exception:
        logger.exception("failed to persist foreground terminal activity")

    # Per-turn complete.json — written alongside the run-level one (the
    # caller writes that); `runs_dir.read_best_complete` salvages it when
    # the runner dies before the run-level write lands.
    turn_complete_payload = {
        "success": final_success,
        "session_id": discovered_sid,
        "error": error,
        "token_usage": total_usage or None,
        "context_window": context_window,
        "finished_at": datetime.now().isoformat(),
        "sdk_output": " ".join(sdk_output_parts).strip() or None,
        "final_assistant_text": final_assistant_text,
        "turn_id": turn_id,
        "used_tools": sorted(used_tools),
    }
    try:
        atomic_write_json(turn_d / "complete.json", turn_complete_payload)
    except Exception:
        logger.exception("failed to write turn complete.json")

    return {
        "success": success,
        "cancelled": cancelled,
        "error": error,
        "discovered_sid": discovered_sid,
        "total_usage": total_usage,
        "context_window": context_window,
        "sdk_output_parts": sdk_output_parts,
        "final_assistant_text": final_assistant_text,
        "final_success": final_success,
        "used_tools": used_tools,
    }


# ============================================================================
# Main async runner
# ============================================================================
async def _run(run_dir: Path, inputs: dict) -> int:
    log = logging.getLogger("runner")

    mode = inputs.get("mode")
    if mode not in ("native", "manager"):
        _fail(run_dir, t("runner.invalid_mode", mode=mode))
        return 1

    # working_mode tags ephemeral sessions (e.g. "search_worker"). Used to
    # keep workers OFF the session-bridge tools so a worker can't recurse
    # via `search_sessions`.
    working_mode = inputs.get("working_mode")

    prompt = inputs.get("prompt") or ""
    images = inputs.get("images") or []
    files = inputs.get("files") or []
    cwd = inputs.get("cwd")
    if (not prompt and not images and not files) or not cwd:
        _fail(run_dir, t("runner.missing_fields"))
        return 1

    model = inputs.get("model")
    reasoning_effort = claude_sdk_effort(inputs.get("reasoning_effort"))
    permission_mode = (inputs.get("permission") or {}).get("mode") or "bypassPermissions"
    session_id = inputs.get("session_id")
    if session_id == "null":
        session_id = None
    disallowed_tools = inputs.get("disallowed_tools") or [
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
    ]
    # Fail closed: the backend strips the CLI's in-process timer tools
    # (replaced by its durable scheduler). A claude with live timer
    # tools could start a turn of its own, racing a fresh --resume
    # instance on the shared session jsonl. If the backend didn't strip
    # them, refuse to spawn.
    from runs_dir import TIMER_TOOLS as _TIMER_TOOLS
    _missing_timer_strips = [
        name for name in _TIMER_TOOLS if name not in disallowed_tools
    ]
    if _missing_timer_strips:
        _fail(
            run_dir,
            "input.json disallowed_tools is missing timer tools "
            f"{_missing_timer_strips} — refusing to spawn",
        )
        return 1
    # Fail closed: background execution is forbidden on every run (see
    # runs_dir.BACKGROUND_WORK_TOOLS). Refuse to spawn if the backend
    # didn't strip the background-interaction tools, and re-assert the
    # CLI's native disable switches regardless of the spawner's env so a
    # misconfigured caller can't re-enable backgrounding.
    from runs_dir import (
        AUTO_BACKGROUND_ENV as _AUTO_BG_ENV,
        BACKGROUND_TASKS_DISABLE_ENV as _BG_DISABLE_ENV,
        BACKGROUND_WORK_TOOLS as _BG_TOOLS,
        BG_EXIT_HANDOFF_DISABLE_ENV as _BG_HANDOFF_ENV,
    )
    _missing_bg_strips = [
        name for name in _BG_TOOLS if name not in disallowed_tools
    ]
    if _missing_bg_strips:
        _fail(
            run_dir,
            "input.json disallowed_tools is missing background tools "
            f"{_missing_bg_strips} — refusing to spawn",
        )
        return 1
    os.environ[_BG_DISABLE_ENV] = "1"
    os.environ[_BG_HANDOFF_ENV] = "1"
    os.environ.pop(_AUTO_BG_ENV, None)
    # `is None` (not `or`): an explicit empty list means "load NO setting
    # sources" (bare / supervised isolation). `or` would collapse [] back to
    # the default and silently re-enable user/project CLAUDE.md + settings.
    _ss = inputs.get("setting_sources")
    setting_sources = ["user", "project", "local"] if _ss is None else _ss

    # Resolve the Claude config directory (respects CLAUDE_CONFIG_DIR env).
    _cfg_dir_raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    _claude_config_dir = _resolve_claude_config_dir(_cfg_dir_raw)

    # Build MCP server config
    mcp_servers: dict = {}
    app_session_id = inputs.get("app_session_id")
    backend_url = inputs.get("backend_url")
    internal_token = inputs.get("internal_token")
    disabled_builtin_tools = _disabled_builtin_tools(inputs)
    # Bare (TestApe-isolated) sessions are capability-stripped and also
    # suppress user-facing extras later in MCP setup, so compute this before
    # any MCP-server gating checks use it.
    _bare = bool(inputs.get("bare_config", False))
    mssg_sender_session_id = (
        inputs.get("mssg_sender_session_id") or app_session_id
    )

    team_orchestration_enabled = extension_store.is_extension_runtime_ready(
        extension_store.extension_id_for_role('team-orchestration')
    )

    if mssg_sender_session_id and backend_url and internal_token:
        communicate_tools = []
        if "mssg" not in disabled_builtin_tools:
            communicate_tools.append(_build_mssg_tool(
                sender_session_id=str(mssg_sender_session_id),
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        if "ask" not in disabled_builtin_tools:
            communicate_tools.append(_build_ask_tool(
                sender_session_id=str(mssg_sender_session_id),
                app_session_id=app_session_id or "",
                model=model,
                cwd=cwd,
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        if "ensure_named_worker" not in disabled_builtin_tools:
            communicate_tools.append(_build_ensure_named_worker_tool(
                cwd=cwd,
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        if "list_available_provider_models" not in disabled_builtin_tools:
            communicate_tools.append(_build_list_available_provider_models_tool())
        if "chat" not in disabled_builtin_tools:
            communicate_tools.append(_build_chat_tool(
                sender_session_id=str(mssg_sender_session_id),
            ))
        if "read_chat_history" not in disabled_builtin_tools:
            communicate_tools.append(_build_read_chat_history_tool())
        if "create_chat" not in disabled_builtin_tools:
            communicate_tools.append(_build_create_chat_tool(
                sender_session_id=str(mssg_sender_session_id),
            ))
        if "set_chat_sender_policy" not in disabled_builtin_tools:
            communicate_tools.append(_build_set_chat_sender_policy_tool(
                sender_session_id=str(mssg_sender_session_id),
            ))
        if "delete_chat" not in disabled_builtin_tools:
            communicate_tools.append(_build_delete_chat_tool())
        if communicate_tools:
            communicate_server = create_sdk_mcp_server(
                name="communicate",
                version="1.0.0",
                tools=communicate_tools,
            )
            mcp_servers["communicate"] = communicate_server

    # Generic handoff tools — available to ALL sessions (team AND native), not
    # just team. delegate (detached off-topic handoff) + create_session (spin
    # up a fresh standalone session to hand work off to). Gated only on loopback
    # credentials; the sender is the calling session itself.
    if app_session_id and backend_url and internal_token:
        handoff_tools = []
        if "delegate_task" not in disabled_builtin_tools:
            handoff_tools.append(_build_delegate_task_tool(
                sender_session_id=str(app_session_id),
                cwd=cwd,
                model=model,
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        if "create_session" not in disabled_builtin_tools:
            handoff_tools.append(_build_create_session_tool(
                sender_session_id=str(app_session_id),
                cwd=cwd,
                model=model,
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        if "create_sub_session" not in disabled_builtin_tools:
            handoff_tools.append(_build_create_sub_session_tool(
                sender_session_id=str(app_session_id),
                cwd=cwd,
                model=model,
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        if handoff_tools:
            handoff_server = create_sdk_mcp_server(
                name="handoff",
                version="1.0.0",
                tools=handoff_tools,
            )
            mcp_servers["handoff"] = handoff_server

    # Capability management — let the model scope its own session (load/release/
    # list scoped capabilities). Internal, non-bare sessions only; bare sessions
    # are deliberately capability-stripped.
    if app_session_id and backend_url and internal_token and not _bare:
        mcp_servers["capabilities"] = create_sdk_mcp_server(
            name="capabilities",
            version="1.0.0",
            tools=_build_capability_tools(
                app_session_id=str(app_session_id),
                backend_url=backend_url,
                internal_token=internal_token,
            ),
        )

    if mode == "manager" and team_orchestration_enabled:
        if not app_session_id or not backend_url or not internal_token:
            _fail(
                run_dir,
                t("runner.manager_mode_missing_fields"),
            )
            return 1
        # create_worker is its own MCP server (team managers only). ask(
        # run_mode="fork") — in the `communicate` server above — is the
        # delegation surface; its fork engine lives behind /api/internal/ask-fork.
        create_worker_tool = _build_create_worker_tool(
            app_session_id=app_session_id,
            backend_url=backend_url,
            internal_token=internal_token,
            model=model,
            cwd=cwd,
        )
        sdk_server = create_sdk_mcp_server(
            name="create-worker",
            version="1.0.0",
            tools=[create_worker_tool],
        )
        mcp_servers["create-worker"] = sdk_server

    # Open-file-panel tool — enabled ONLY for genuine user-facing
    # top-level turns (manager OR native). Worker delegations and
    # supervisor/verdict turns never set this flag, so the agent can
    # only point the user at code from a turn the user actually
    # initiated. Mode-agnostic, like browser-harness.
    open_file_panel_enabled = inputs.get("open_file_panel_enabled", False)
    file_editing_mode = inputs.get("working_mode") == "file_editing"
    # Bare (TestApe-isolated) sessions are headless: they get NONE of the
    # user-facing extras (open-file-panel, cross-session bridge, durable
    # scheduler). They DO get the credential broker — a bare device worker
    # needs it to fetch login secrets — wired off the bare signal instead
    # of the user-facing one.
    _user_facing_extras = open_file_panel_enabled and not _bare
    _cred_enabled = open_file_panel_enabled or _bare

    if (_user_facing_extras or _cred_enabled) and not backend_url:
        backend_url = get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000")
    if _user_facing_extras:
        if not internal_token:
            _fail(run_dir, "open-file-panel requires internal_token but none provided")
            return 1
        ofp_tool = _build_open_file_panel_tool(
            app_session_id=app_session_id or "",
            backend_url=backend_url,
            internal_token=internal_token,
        )
        request_user_input_tool = _build_request_user_input_tool(
            app_session_id=app_session_id or "",
            backend_url=backend_url,
            internal_token=internal_token,
            run_id=run_dir.name,
        )
        tools = [ofp_tool, request_user_input_tool]
        if file_editing_mode:
            tools.append(_build_start_file_discussion_tool(
                app_session_id=app_session_id or "",
                backend_url=backend_url,
                internal_token=internal_token,
            ))
        ofp_server = create_sdk_mcp_server(
            name="ui",
            version="1.0.0",
            tools=tools,
        )
        mcp_servers["ui"] = ofp_server

    # NOTE: the Ask container (ASK_SINGLETON_ID) runs NO claude turns of its
    # own — its search turns are orchestrated server-side by
    # `session_search.search()` (which spawns an ephemeral search worker).
    # So it gets no ask MCP tools here.

    for _extension_mcp_name, _extension_mcp_config in extension_store.runtime_mcp_server_configs(
        inputs,
        user_facing=bool(_user_facing_extras and app_session_id),
        bare=_bare,
    ).items():
        mcp_servers.setdefault(_extension_mcp_name, _extension_mcp_config)
    if _bare:
        for _extension_mcp_name, _extension_mcp_config in (
            await _bridge_native_extension_mcp_servers(
                inputs,
                user_facing=bool(_user_facing_extras and app_session_id),
                bare=_bare,
            )
        ).items():
            mcp_servers.setdefault(_extension_mcp_name, _extension_mcp_config)
    if not _bare:
        for _extension_mcp_name, _extension_mcp_config in extension_store.native_mcp_server_configs(
            inputs,
            user_facing=bool(_user_facing_extras and app_session_id),
            bare=_bare,
        ).items():
            if extension_store.is_reserved_mcp_server_name(_extension_mcp_name):
                mcp_servers[_extension_mcp_name] = _extension_mcp_config
                continue
            mcp_servers.setdefault(_extension_mcp_name, _extension_mcp_config)

    fork = bool(inputs.get("fork", False))

    _runner_options: dict = {}

    def _append_system_prompt(text: str) -> None:
        existing = (_runner_options.get("system_prompt") or {}).get("append", "")
        merged = f"{existing}\n\n{text}" if existing else text
        _runner_options["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": merged,
        }

    def _capability_prompt() -> str:
        blocks = []
        for item in inputs.get("capability_contexts") or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            name = str(item.get("name") or "Capability")
            category = str(item.get("category") or "capability")
            blocks.append(
                f"## {name} ({category})\n\n{content.strip()}"
            )
        if not blocks:
            return ""
        return render_prompt(
            "runner/capability_context.md",
            {"blocks": "\n\n".join(blocks)},
        )

    capability_prompt = _capability_prompt()
    if capability_prompt:
        _append_system_prompt(capability_prompt)

    extra_args = {"exclude-dynamic-system-prompt-sections": None}
    if _bare:
        # Claude Code 2.1.x on Windows treats --bare as an unauthenticated
        # environment for subscription auth, yielding "Not logged in" even
        # when the regular CLI is logged in. Keep isolation via empty
        # setting_sources, no runtime skill plugin, and disabled slash commands.
        extra_args["disable-slash-commands"] = None

    raw_provider_run_config = inputs.get("provider_run_config")
    provider_run_config = raw_provider_run_config if isinstance(raw_provider_run_config, dict) else {}
    skill_plugin = _materialize_claude_skill_plugin(
        run_dir,
        cwd,
        provider_run_config,
        bare_config=_bare,
    )
    plugins = [skill_plugin] if skill_plugin else []

    # CAVEAT (unverified): can_use_tool is registered even on resumed turns
    # (resume=session_id). If the Claude Agent SDK rejects can_use_tool+resume
    # at query time, every non-first interactive turn breaks. Needs a live SDK
    # probe to confirm; if it fails, add probe-and-degrade here (force bypass
    # for the turn + surface a one-time notice) — never a silent fallback.
    # Interactive tool approvals: for permission modes that ask, register a
    # can_use_tool callback that round-trips to the backend → frontend. The
    # SDK only invokes it when the mode actually requires approval, so it's
    # safe to register for any non-bypass mode. bypassPermissions/dontAsk
    # never ask, so no callback (and no async-iterable prompt constraint).
    interactive_permissions = (
        permission_mode in ("default", "acceptEdits", "plan", "auto")
        and bool(backend_url)
        and bool(internal_token)
        and bool(app_session_id)
    )
    can_use_tool_cb = None
    if interactive_permissions:
        _approval_run_id = run_dir.name
        _approval_cancel_path = run_dir / "cancel"
        _approval_backend_url = backend_url
        _approval_token = internal_token
        _approval_session = app_session_id

        async def _wait_cancel() -> bool:
            while True:
                if _approval_cancel_path.exists():
                    return True
                await asyncio.sleep(0.5)

        async def _can_use_tool(tool_name, tool_input, context):
            summary = _describe_tool_call(tool_name, tool_input)
            # Race the backend decision against the turn cancel sentinel so
            # Stop aborts an in-flight approval instead of hanging up to the
            # backend timeout. The losing task is cancelled.
            approval_task = asyncio.ensure_future(asyncio.to_thread(
                request_tool_approval,
                backend_url=_approval_backend_url,
                internal_token=_approval_token,
                app_session_id=_approval_session,
                run_id=_approval_run_id,
                provider_kind="claude",
                tool_name=str(tool_name),
                summary=summary,
            ))
            cancel_task = asyncio.ensure_future(_wait_cancel())
            done, pending = await asyncio.wait(
                {approval_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if cancel_task in done:
                return PermissionResultDeny(
                    behavior="deny", message="Cancelled by user",
                )
            approved = approval_task.result() if approval_task in done else False
            if approved:
                return PermissionResultAllow(behavior="allow")
            return PermissionResultDeny(
                behavior="deny",
                message="Denied by user in Better Agent",
            )

        can_use_tool_cb = _can_use_tool

    options = ClaudeAgentOptions(
        mcp_servers=mcp_servers,
        permission_mode=permission_mode,
        can_use_tool=can_use_tool_cb,
        hooks=_background_policy_hooks(),
        cwd=cwd,
        model=model,
        effort=reasoning_effort,
        resume=session_id if session_id else None,
        fork_session=fork,
        setting_sources=setting_sources,
        disallowed_tools=disallowed_tools,
        enable_file_checkpointing=True,
        # SDK default is 1 MiB per stdout JSON line; a single image
        # tool_result (base64 embedded twice per line by the CLI) exceeds
        # that and kills the turn mid-run with SDKJSONDecodeError.
        max_buffer_size=SUBPROCESS_LINE_LIMIT_BYTES,
        cli_path=_resolve_claude_cli(),
        extra_args=extra_args,
        plugins=plugins,
        env=_claude_cache_env(),
        **_runner_options,
    )

    # Compute pre_query_byte_offset: for resumes, snapshot current jsonl size
    # BEFORE we send the query so the backend tailer can slice at the right
    # offset (otherwise it would re-emit turn N-1's events under turn N's
    # assistant message — verified duplication bug).
    #
    # Forks are treated as fresh sessions from the tailer's POV: claude
    # writes the forked conversation to a BRAND-NEW sid's jsonl, so there
    # are no pre-existing lines to skip and no early-resume state.json to
    # write. We fall through to the system.init code path below.
    pre_query_byte_offset = 0
    pre_query_jsonl_inode: Optional[int] = None
    resume_jsonl: Optional[Path] = None
    if session_id and not fork:
        resume_jsonl = (
            _claude_config_dir / "projects"
            / encode_cwd(cwd) / f"{session_id}.jsonl"
        )
        try:
            if resume_jsonl.exists():
                # Phase-1 stage-5 hardening: FD_CLOEXEC so bg shell
                # children spawned later do NOT inherit this read FD
                # to the user's jsonl (contains conversation history).
                _fd = os.open(
                    # O_CLOEXEC is POSIX-only and missing on Windows, where
                    # os.open already returns a non-inheritable FD by default
                    # (PEP 446); fall back to 0 so this doesn't AttributeError.
                    str(resume_jsonl), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
                with os.fdopen(_fd, "rb") as rf:
                    rf.seek(0, os.SEEK_END)
                    pre_query_byte_offset = rf.tell()
                    pre_query_jsonl_inode = os.fstat(rf.fileno()).st_ino
        except OSError:
            pre_query_byte_offset = 0

    state: dict = {
        "run_id": run_dir.name,
        "mode": mode,
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": None,
        "jsonl_path": None,
        "pre_query_byte_offset": pre_query_byte_offset,
        "pre_query_jsonl_inode": pre_query_jsonl_inode,
        "complete": False,
        "foreground_status": "running",
        "background_work_ids": [],
        "activity_revision": 1,
    }
    state_path = run_dir / "state.json"
    fork_parent_line_count = _fork_parent_line_count(provider_run_config)

    # On resume, write state.json EARLY (before connecting) so the backend
    # bootstrap can start tailing immediately without waiting for the SDK
    # to emit system.init.
    if session_id and resume_jsonl is not None:
        state["session_id"] = session_id
        state["jsonl_path"] = str(resume_jsonl)
        try:
            _atomic_write_json(state_path, state)
            log.info(
                "state.json written early for resume: session_id=%s pre_query_byte_offset=%d",
                session_id, pre_query_byte_offset,
            )
        except Exception:
            logger.exception("failed to write early state.json")

    client = ClaudeSDKClient(options=options)

    # Heartbeat: refreshes runner_alive every 5s so the backend's
    # _watch_complete can detect a stuck runner (SDK hang, zombie)
    # vs a healthy one (long tool call, streaming). The heartbeat
    # stops if the event loop is blocked (stuck SDK receive_response).
    _heartbeat_shutdown = asyncio.Event()
    _current_turn_holder = [None]
    _heartbeat_task = asyncio.create_task(
        _heartbeat_writer(run_dir, _current_turn_holder, _heartbeat_shutdown),
        name="runner-heartbeat",
    )

    # Network retry: infinite reconnect on transient failures.
    _retry_backoff = 2.0
    _cancel_path = run_dir / "cancel"

    async def _retry_sleep(seconds: float) -> None:
        """Sleep with cancel sentinel check every 0.5s."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if _cancel_path.exists():
                raise asyncio.CancelledError()
            await asyncio.sleep(min(0.5, deadline - time.monotonic()))

    while True:
        try:
            await client.connect()
        except Exception as e:
            if not _is_network_error(e):
                raise
            log.warning(
                "connect() network error, retry %.1fs: %s", _retry_backoff, e,
            )
            await _retry_sleep(_retry_backoff)
            _retry_backoff = min(_retry_backoff * 2, 60.0)
            client = ClaudeSDKClient(options=options)
            continue

        try:
            turn_result = await _run_one_turn(
                client=client,
                prompt=prompt,
                images=images,
                files=inputs.get("files", []) or [],
                run_dir=run_dir,
                turn_id=run_dir.name,
                pre_query_byte_offset=pre_query_byte_offset,
                fork_parent_line_count=fork_parent_line_count if fork else 0,
                state=state,
                state_path=state_path,
                cwd=cwd,
                claude_config_dir=_claude_config_dir,
                log=log,
                interactive_permissions=interactive_permissions,
                current_turn_holder=_current_turn_holder,
            )
        except Exception as e:
            if not _is_network_error(e):
                try:
                    await client.disconnect()
                except Exception:
                    logger.exception("client.disconnect() failed")
                raise
            try:
                await client.disconnect()
            except Exception:
                pass
            # Resume discovered session on retry
            if state.get("session_id"):
                options.resume = state["session_id"]
                _rj = (
                    _claude_config_dir / "projects"
                    / encode_cwd(cwd) / f"{state['session_id']}.jsonl"
                )
                try:
                    if _rj.exists():
                        _fd = os.open(str(_rj), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                        with os.fdopen(_fd, "rb") as _rf:
                            _rf.seek(0, os.SEEK_END)
                            pre_query_byte_offset = _rf.tell()
                            pre_query_jsonl_inode = os.fstat(_rf.fileno()).st_ino
                except OSError:
                    pass
            log.warning(
                "turn network error, retry %.1fs: %s", _retry_backoff, e,
            )
            await _retry_sleep(_retry_backoff)
            _retry_backoff = min(_retry_backoff * 2, 60.0)
            client = ClaudeSDKClient(options=options)
            continue

        # Success — reset backoff for future transient errors.
        # Disconnect happens right after the completion artifacts are
        # durable below — the runner is strictly per-turn (background
        # execution is disabled on every run, see runs_dir
        # BACKGROUND_TASKS_DISABLE_ENV), so nothing outlives the turn.
        _retry_backoff = 2.0
        break

    # `discovered_sid` falls back to `state.session_id` when the turn
    # didn't discover one (e.g. raised pre-discovery on a retry) — the
    # run-level complete.json must still record the sid state.json has.
    discovered_sid = turn_result["discovered_sid"] or state.get("session_id")
    total_usage = turn_result["total_usage"]
    error = turn_result["error"]
    cancelled = turn_result["cancelled"]
    sdk_output_parts = turn_result["sdk_output_parts"]
    final_success = turn_result["final_success"]
    final_assistant_text = turn_result.get("final_assistant_text")
    context_window = turn_result.get("context_window")

    # Write complete.json (run-level — the backend's _watch_complete
    # finalizes the turn off this file).
    complete = {
        "success": final_success,
        "session_id": discovered_sid,
        "error": error,
        "token_usage": total_usage or None,
        "context_window": context_window,
        "finished_at": datetime.now().isoformat(),
        "sdk_output": " ".join(sdk_output_parts).strip() or None,
        "final_assistant_text": final_assistant_text,
    }
    try:
        # Atomic: the backend's _watch_complete fires on this file's
        # APPEARANCE (possibly mid-write under plain write_text) — a torn
        # read would silently fall back to the per-turn payload.
        from runs_dir import atomic_write_json as _awj
        _awj(run_dir / "complete.json", complete)
    except Exception:
        logger.exception("failed to write complete.json")

    # Finalize state.json
    state["complete"] = True
    state["finished_at"] = complete["finished_at"]
    if discovered_sid and not state.get("session_id"):
        state["session_id"] = discovered_sid
    try:
        _atomic_write_json(state_path, state)
    except Exception:
        logger.exception("failed to finalize state.json")

    # Per-turn process: the turn is finalized (complete.json + state.json
    # durable) — close the CLI and exit. Background execution is disabled
    # on every run, so no work can outlive this process.
    try:
        # Bounded: a hung disconnect must not pin this process — the
        # backend's wind-down escalation reaps a runner that outlives
        # its grace window, but exiting promptly here is the normal path.
        await asyncio.wait_for(client.disconnect(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("client.disconnect() timed out — exiting anyway")
    except Exception:
        logger.exception("client.disconnect() failed")

    # CLI closed → stop the heartbeat, THEN remove the
    # `runner_alive` sentinel. Stopping BEFORE the unlink prevents a final
    # heartbeat tick from re-creating the file after we delete it. The
    # sentinel thus outlives the completion artifact the watchdog waits
    # for, closing the kill-race at the source.
    _heartbeat_shutdown.set()
    _heartbeat_task.cancel()
    try:
        await _heartbeat_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        from runs_dir import runner_alive_path as _rap
        _rap(run_dir).unlink(missing_ok=True)
    except OSError:
        pass

    log.info(
        "runner done success=%s session_id=%s error=%s",
        final_success, discovered_sid, error,
    )
    return 0 if final_success else 1


def _fail(run_dir: Path, error: str) -> None:
    """Write an error complete.json for fatal pre-run failures."""
    logger.error("runner fatal: %s", error)
    payload = {
        "success": False,
        "session_id": None,
        "error": error,
        "token_usage": None,
        "finished_at": datetime.now().isoformat(),
    }
    try:
        from runs_dir import atomic_write_json as _awj
        _awj(run_dir / "complete.json", payload)
    except Exception:
        logger.exception("failed to write error complete.json")


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("runner")
    log.info("runner starting for run_dir=%s", run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as e:
        _fail(run_dir, t("runner.failed_read_input", e=str(e)))
        return 1

    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as e:
        logger.exception("runner top-level failure")
        # Exception path: `_run` raised before reaching its success-path
        # sentinel cleanup, so `runner_alive` may linger. `_fail` makes
        # the error complete.json durable first; THEN remove the sentinel
        # (same ordering invariant as the success path — sentinel removed
        # only once a complete.json exists). asyncio.run has already
        # cancelled the heartbeat task at loop teardown, so no tick can
        # re-create the file here.
        _fail(run_dir, f"{type(e).__name__}: {e}")
        try:
            from runs_dir import runner_alive_path as _rap
            _rap(run_dir).unlink(missing_ok=True)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
