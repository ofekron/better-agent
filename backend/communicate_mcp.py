from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from functools import wraps
from typing import Any

from better_agent_sdk.surfaces import OperationSpec, build_mcp_server, run_mcp_or_cli
from env_compat import get_env, require_env

from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
    DEFAULT_ASK_MODE,
    normalize_ask_execution,
)
from orchestration_tool_descriptions import (
    ASK_DESCRIPTION,
    CHAT_DESCRIPTION,
    CREATE_CHAT_DESCRIPTION,
    CREATE_SESSION_DESCRIPTION,
    CREATE_SUB_SESSION_DESCRIPTION,
    CREATE_WORKER_DESCRIPTION,
    DELETE_CHAT_DESCRIPTION,
    DELEGATE_TASK_DESCRIPTION,
    ENSURE_NAMED_WORKER_DESCRIPTION,
    INBOX_DESCRIPTION,
    LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION,
    MSSG_DESCRIPTION,
    READ_INBOX_HISTORY_DESCRIPTION,
    SET_CHAT_SENDER_POLICY_DESCRIPTION,
    STOP_TURN_DESCRIPTION,
)


import chat_store
import inbox_store
from provider_catalog_mcp import available_provider_models_response


_LONG_TIMEOUT = 24 * 60 * 60  # fork runs / ask waits / approval can be long


def _env(name: str, default: str = "") -> str:
    if name.startswith("BETTER_CLAUDE_"):
        return get_env(name, default).strip()
    return (os.environ.get(name, "") or default).strip()


def _env_required(name: str) -> str:
    if name.startswith("BETTER_CLAUDE_"):
        return require_env(name)
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


_DISABLEABLE_BUILTIN_TOOLS = frozenset({
    "ask",
    "chat",
    "create_chat",
    "create_session",
    "create_sub_session",
    "delegate_task",
    "delete_chat",
    "ensure_named_worker",
    "inbox",
    "list_available_provider_models",
    "mssg",
    "read_chat_history",
    "read_inbox_history",
    "set_chat_sender_policy",
    "stop_turn",
})


def _disabled_builtin_tools() -> set[str]:
    raw = _env("BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS")
    return {
        item.strip()
        for item in raw.split(",")
        if item.strip() in _DISABLEABLE_BUILTIN_TOOLS
    }


def _post_json(endpoint: str, payload: dict, timeout: float) -> dict[str, Any]:
    backend_url = _env_required("BETTER_CLAUDE_BACKEND_URL").rstrip("/")
    internal_token = _env_required("BETTER_CLAUDE_INTERNAL_TOKEN")
    req = urllib.request.Request(
        backend_url + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _post_mcp_job(endpoint: str, operation: str, payload: dict, timeout: float) -> dict[str, Any]:
    job_id = f"mcp_{uuid.uuid4().hex}"
    fire_payload = {**payload, "_mcp_job_id": job_id, "_mcp_job_wait": 0}
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        response = _post_json(endpoint, fire_payload, timeout=min(30.0, max(1.0, timeout)))
    except Exception:
        response = _post_json(
            "/api/internal/mcp-jobs/results",
            {"operation": operation, "id": job_id, "_mcp_job_wait": 0},
            timeout=30.0,
        )
    while isinstance(response, dict) and response.get("ready") is False:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return response
        time.sleep(min(1.0, max(0.05, remaining)))
        response = _post_json(
            "/api/internal/mcp-jobs/results",
            {
                "operation": operation,
                "id": job_id,
                "_mcp_job_wait": min(5.0, max(0.0, remaining)),
            },
            timeout=min(30.0, max(1.0, remaining)),
        )
    if isinstance(response, dict) and response.get("ready") is True and "result" in response:
        result = response.get("result")
        return result if isinstance(result, dict) else {"success": False, "error": "MCP job returned invalid result"}
    return response


def _safe_result(fn):
    """Wrap a tool body so HTTP/infra errors come back as {success: False}
    instead of crashing the stdio MCP server."""
    @wraps(fn)
    def wrapper(*a, **kw) -> dict[str, Any]:
        try:
            return fn(*a, **kw)
        except urllib.error.HTTPError as exc:
            return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:  # noqa: BLE001 — surface to the model
            return {"success": False, "error": str(exc)}
    return wrapper


def _communication_payload(
    target_session_id: str,
    target_worker_id: str,
    target_worker_pool: str,
    pool_affinity_key: str,
    message: str,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    collapse_key: str = "",
    collapse_policy: str = "",
) -> dict[str, Any]:
    target_session_id = (target_session_id or "").strip()
    target_worker_id = (target_worker_id or "").strip()
    target_worker_pool = (target_worker_pool or "").strip()
    pool_affinity_key = (pool_affinity_key or "").strip()
    message = (message or "").strip()
    targets = [target for target in (target_session_id, target_worker_id, target_worker_pool) if target]
    if len(targets) != 1 or not message:
        return {"success": False, "error": "exactly one target and message are required"}
    sender_session_id = _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID")
    return {
        "sender_session_id": sender_session_id,
        "target_session_id": target_session_id,
        "target_worker_id": target_worker_id,
        "target_worker_pool": target_worker_pool,
        "pool_affinity_key": pool_affinity_key,
        "message": message,
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "runner": (runner or "").strip() or None,
        "collapse_key": (collapse_key or "").strip(),
        "collapse_policy": (collapse_policy or "").strip(),
    }


def mssg_response(
    message: str,
    target_session_id: str = "",
    target_worker_id: str = "",
    target_worker_pool: str = "",
    pool_affinity_key: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    collapse_key: str = "",
    collapse_policy: str = "",
) -> dict[str, Any]:
    payload = _communication_payload(
        target_session_id,
        target_worker_id,
        target_worker_pool,
        pool_affinity_key,
        message,
        provider_id,
        model,
        reasoning_effort,
        runner,
        collapse_key,
        collapse_policy,
    )
    if payload.get("success") is False:
        return payload
    return _post_mcp_job("/api/internal/mssg", "mssg", payload, timeout=30.0)


def stop_turn_response(target_session_id: str) -> dict[str, Any]:
    target_session_id = (target_session_id or "").strip()
    if not target_session_id:
        return {"success": False, "error": "target_session_id is required"}
    return _post_json(
        "/api/internal/stop-turn",
        {
            "caller_session_id": _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
            "target_session_id": target_session_id,
        },
        timeout=30.0,
    )


def _resolve_cwd(cwd: str) -> str:
    """cwd override-or-inherit: use the caller-supplied cwd if provided,
    otherwise inherit the calling session's cwd."""
    return (cwd or "").strip() or _env("BETTER_CLAUDE_CWD")


def delegate_task_response(
    task: str,
    target_session_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    sub_session: bool = True,
    cwd: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Smart detached handoff. POSTs /api/internal/delegate-task which routes
    per the global delegate_task_policy (search first suggestion / create new /
    approval) then dispatches detached — the target's completion does NOT join
    the sender's turn. Use for heavy tangential / off-topic real work, not
    reviews. Auto-routing has a cost because it may run session search; pass
    target_session_id ONLY to bypass routing."""
    task = (task or "").strip()
    if not task:
        return {"success": False, "error": "task is required"}
    target = (target_session_id or "").strip()
    sender_session_id = _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID")
    return _post_mcp_job("/api/internal/delegate-task", "delegate-task", {
        "sender_session_id": sender_session_id,
        "task": task,
        "target_session_id": target or None,
        "cwd": _resolve_cwd(cwd),
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "runner": (runner or "").strip() or None,
        "sub_session": sub_session is not False,
        "folder_id": (folder_id or "").strip() or None,
        "tag_ids": tag_ids or [],
    }, timeout=_LONG_TIMEOUT)


def ask_response(
    message: str,
    target_session_id: str = "",
    target_worker_id: str = "",
    target_worker_pool: str = "",
    pool_affinity_key: str = "",
    run_mode: str = "direct",
    worker_description: str = "",
    worker_registry_cwd: str = "",
    ephemeral: bool = False,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    mode: str = DEFAULT_ASK_MODE,
) -> dict[str, Any]:
    target_session_id = (target_session_id or "").strip()
    target_worker_id = (target_worker_id or "").strip()
    target_worker_pool = (target_worker_pool or "").strip()
    pool_affinity_key = (pool_affinity_key or "").strip()
    message = (message or "").strip()
    if not any((target_session_id, target_worker_id, target_worker_pool)) or not message:
        return {"success": False, "error": "one target and message are required"}
    try:
        mode, run_mode = normalize_ask_execution(mode, run_mode)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if run_mode == "fork" and not target_session_id:
        return {"success": False, "error": "run_mode='fork' requires target_session_id"}
    if ephemeral and run_mode != "fork":
        return {"success": False, "error": "ephemeral is only valid for run_mode='fork'"}

    # The manager session is both the team-message sender and the fork caller.
    sender_session_id = _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID")
    selected_model = (model or "").strip() or _env("BETTER_CLAUDE_MODEL")
    cwd = _env("BETTER_CLAUDE_CWD")

    if run_mode == "fork":
        worker_description = (worker_description or "").strip()
        registry_cwd = worker_registry_cwd.strip()
        client_delegation_id = f"del_{uuid.uuid4().hex[:10]}"
        return _post_mcp_job("/api/internal/ask-fork", "ask-fork", {
            "app_session_id": sender_session_id,
            "instructions": message,
            "worker_session_id": target_session_id,
            "worker_description": worker_description,
            "model": selected_model,
            "provider_id": (provider_id or "").strip() or None,
            "reasoning_effort": (reasoning_effort or "").strip() or None,
            "runner": (runner or "").strip() or None,
            "cwd": cwd,
            "client_delegation_id": client_delegation_id,
            "run_mode": "fork",
            "ask_mode": mode,
            "worker_registry_cwd": registry_cwd or None,
            "ephemeral": bool(ephemeral),
        }, timeout=_LONG_TIMEOUT)

    ask_id = f"ask_{uuid.uuid4().hex[:10]}"
    return _post_mcp_job("/api/internal/ask", "ask", {
        "sender_session_id": sender_session_id,
        "target_session_id": target_session_id,
        "target_worker_id": target_worker_id,
        "target_worker_pool": target_worker_pool,
        "pool_affinity_key": pool_affinity_key,
        "message": message,
        "ask_id": ask_id,
        "mode": mode,
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "runner": (runner or "").strip() or None,
    }, timeout=_LONG_TIMEOUT)


def create_worker_response(
    worker_description: str,
    justification: str,
    orchestration_mode: str,
    node_id: str = "",
    cwd: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    worker_description = (worker_description or "").strip()
    justification = (justification or "").strip()
    orchestration_mode = (orchestration_mode or "").strip()
    if not worker_description or not justification or not orchestration_mode:
        return {
            "success": False,
            "error": "worker_description, justification and orchestration_mode are required",
        }
    if orchestration_mode == "manager":
        orchestration_mode = "team"
    if orchestration_mode not in ("team", "native"):
        return {"success": False, "error": "orchestration_mode must be 'team' or 'native'"}
    sender_session_id = _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID")
    client_request_id = f"cw_{uuid.uuid4().hex[:10]}"
    return _post_json("/api/internal/create-worker", {
        "app_session_id": sender_session_id,
        "worker_description": worker_description,
        "justification": justification,
        "orchestration_mode": orchestration_mode,
        "cwd": _resolve_cwd(cwd),
        "client_request_id": client_request_id,
        "node_id": node_id.strip() or None,
        "folder_id": (folder_id or "").strip() or None,
        "tag_ids": tag_ids or [],
    }, timeout=_LONG_TIMEOUT)


def ensure_named_worker_response(
    name: str,
    orchestration_mode: str,
    cwd: str = "",
    provision_prompt: str = "",
    description: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    node_id: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    cwd = _resolve_cwd(cwd)
    mode = (orchestration_mode or "").strip()
    if not name or not mode:
        return {"success": False, "error": "name and orchestration_mode are required"}
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        return {"success": False, "error": "orchestration_mode must be 'team' or 'native'"}
    spec: dict[str, Any] = {
        "role_key": name,
        "description": (description or "").strip() or f"worker:{name}",
        "orchestration_mode": mode,
        "node_id": node_id.strip() or None,
        "tags": [name],
        "folder_id": (folder_id or "").strip() or None,
        "tag_ids": tag_ids or [],
    }
    if (provision_prompt or "").strip():
        spec["provision_prompt"] = provision_prompt.strip()
    if (provider_id or "").strip():
        spec["provider_id"] = provider_id.strip()
    if (model or "").strip():
        spec["model"] = model.strip()
    if (reasoning_effort or "").strip():
        spec["reasoning_effort"] = reasoning_effort.strip()
    if (runner or "").strip():
        spec["runner"] = runner.strip()
    result = _post_json("/api/internal/workers/provision", {
        "cwd": cwd,
        "workers": [spec],
    }, timeout=_LONG_TIMEOUT)
    workers = (result or {}).get("workers") or []
    if not workers:
        return {"success": False, "error": "provision returned no worker"}
    worker = workers[0]
    return {
        "success": True,
        "agent_session_id": worker.get("agent_session_id"),
        "name": worker.get("name"),
        "created": bool(worker.get("created")),
        "orchestration_mode": worker.get("orchestration_mode"),
        "registry_cwd": worker.get("registry_cwd") or worker.get("cwd"),
    }


def create_session_response(
    name: str,
    orchestration_mode: str = "native",
    node_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    cwd: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "name is required"}
    mode = (orchestration_mode or "native").strip() or "native"
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        return {"success": False, "error": "orchestration_mode must be 'team' or 'native'"}
    return _post_json("/api/internal/create-session", {
        "sender_session_id": _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        "name": name,
        "cwd": _resolve_cwd(cwd),
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "runner": (runner or "").strip() or None,
        "orchestration_mode": mode,
        "node_id": node_id.strip() or None,
        "folder_id": (folder_id or "").strip() or None,
        "tag_ids": tag_ids or [],
        "mcp_servers": mcp_servers or [],
        "preset": (preset or "").strip(),
    }, timeout=30.0)


def create_sub_session_response(
    description: str = "",
    node_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    cwd: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    preset: str = "",
) -> dict[str, Any]:
    return _post_json("/api/internal/create-sub-session", {
        "sender_session_id": _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        "description": (description or "").strip(),
        "cwd": _resolve_cwd(cwd),
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "runner": (runner or "").strip() or None,
        "node_id": node_id.strip() or None,
        "folder_id": (folder_id or "").strip() or None,
        "tag_ids": tag_ids or [],
        "mcp_servers": mcp_servers or [],
    }, timeout=30.0)


def chat_response(
    chat_id: str,
    message: str = "",
    history_mode: str = "",
) -> dict[str, Any]:
    return chat_store.post_and_read(
        chat_id=chat_id,
        reader_id=_env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        message=message,
        history_mode=history_mode,
    )


def inbox_response(
    recipient_session_id: str = "",
    message: str = "",
) -> dict[str, Any]:
    return inbox_store.post_or_read(
        caller_session_id=_env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        recipient_session_id=recipient_session_id,
        message=message,
    )


def read_inbox_history_response(
    limit: int = 50,
    before_seq: int | None = None,
) -> dict[str, Any]:
    return inbox_store.read_history(
        recipient_session_id=_env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        limit=limit,
        before_seq=before_seq,
    )


def read_chat_history_response(
    chat_id: str,
    limit: int = 50,
    before_seq: int | None = None,
) -> dict[str, Any]:
    return chat_store.read_history(chat_id=chat_id, limit=limit, before_seq=before_seq)


def create_chat_response(
    chat_id: str,
    name: str = "",
    new_readers_see_history: bool = True,
    sender_policy: str = "",
    sender_ids: list[str] | None = None,
) -> dict[str, Any]:
    return chat_store.create_chat(
        chat_id=chat_id,
        created_by=_env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        name=name,
        new_readers_see_history=new_readers_see_history,
        sender_policy=sender_policy,
        sender_ids=sender_ids,
    )


def set_chat_sender_policy_response(
    chat_id: str,
    sender_policy: str,
    sender_ids: list[str] | None = None,
) -> dict[str, Any]:
    return chat_store.set_sender_policy(
        chat_id=chat_id,
        owner_id=_env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        sender_policy=sender_policy,
        sender_ids=sender_ids,
    )


def delete_chat_response(chat_id: str) -> dict[str, Any]:
    return chat_store.delete_chat(chat_id)


def create_sub_session_surface_response(
    description: str = "",
    node_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    cwd: str = "",
    folder_id: str = "",
    tag_ids: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    return create_sub_session_response(
        description,
        node_id,
        provider_id,
        model,
        reasoning_effort,
        runner,
        cwd,
        folder_id,
        tag_ids,
        mcp_servers,
    )


_INSTRUCTIONS = (
    "Team tools for Better Agent sessions. mssg is one-way; ask waits inline by default. "
    "Async ask and delegate_task return through Inbox. Use fork mode for isolated reviews, "
    "create_session for standalone sessions, and create_sub_session for hidden helpers."
)


def _specs() -> tuple[OperationSpec, ...]:
    disabled = _disabled_builtin_tools()
    candidates = (
        ("mssg", mssg_response, MSSG_DESCRIPTION),
        ("stop_turn", stop_turn_response, STOP_TURN_DESCRIPTION),
        (
            "list_available_provider_models",
            available_provider_models_response,
            LIST_AVAILABLE_PROVIDER_MODELS_DESCRIPTION,
        ),
        ("chat", chat_response, CHAT_DESCRIPTION),
        ("inbox", inbox_response, INBOX_DESCRIPTION),
        ("read_inbox_history", read_inbox_history_response, READ_INBOX_HISTORY_DESCRIPTION),
        ("read_chat_history", read_chat_history_response, "Read shared chat history."),
        ("create_chat", create_chat_response, CREATE_CHAT_DESCRIPTION),
        (
            "set_chat_sender_policy",
            set_chat_sender_policy_response,
            SET_CHAT_SENDER_POLICY_DESCRIPTION,
        ),
        ("delete_chat", delete_chat_response, DELETE_CHAT_DESCRIPTION),
        ("delegate_task", delegate_task_response, DELEGATE_TASK_DESCRIPTION),
        ("create_session", create_session_response, CREATE_SESSION_DESCRIPTION),
        (
            "create_sub_session",
            create_sub_session_surface_response,
            CREATE_SUB_SESSION_DESCRIPTION,
        ),
        ("ask", ask_response, ASK_DESCRIPTION),
        ("ensure_named_worker", ensure_named_worker_response, ENSURE_NAMED_WORKER_DESCRIPTION),
    )
    specs = [
        OperationSpec(
            name,
            _safe_result(handler),
            description,
            operation=f"runtime_communication_{name}",
        )
        for name, handler, description in candidates
        if name not in disabled
    ]
    specs.append(
        OperationSpec(
            "create_worker",
            _safe_result(create_worker_response),
            CREATE_WORKER_DESCRIPTION,
            operation="runtime_communication_create_worker",
        )
    )
    return tuple(specs)


def build_server():
    return build_mcp_server("communicate", _specs(), instructions=_INSTRUCTIONS)

def main() -> int:
    return run_mcp_or_cli("communicate", _specs(), instructions=_INSTRUCTIONS)


if __name__ == "__main__":
    sys.exit(main())
