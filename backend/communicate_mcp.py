from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

from env_compat import get_env, require_env
from mcp.server.fastmcp import FastMCP

from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC,
    DEFAULT_ASK_MODE,
    normalize_ask_mode,
)
from orchestration_tool_descriptions import (
    ASK_DESCRIPTION,
    CREATE_SESSION_DESCRIPTION,
    CREATE_SUB_SESSION_DESCRIPTION,
    CREATE_WORKER_DESCRIPTION,
    DELEGATE_TASK_DESCRIPTION,
    ENSURE_NAMED_WORKER_DESCRIPTION,
    MSSG_DESCRIPTION,
)


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
    "create_session",
    "create_sub_session",
    "delegate_task",
    "ensure_named_worker",
    "mssg",
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


def _safe_result(fn):
    """Wrap a tool body so HTTP/infra errors come back as {success: False}
    instead of crashing the stdio MCP server."""
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
    message: str,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    target_session_id = (target_session_id or "").strip()
    target_worker_id = (target_worker_id or "").strip()
    target_worker_pool = (target_worker_pool or "").strip()
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
        "message": message,
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
    }


def mssg_response(
    message: str,
    target_session_id: str = "",
    target_worker_id: str = "",
    target_worker_pool: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    payload = _communication_payload(
        target_session_id,
        target_worker_id,
        target_worker_pool,
        message,
        provider_id,
        model,
        reasoning_effort,
    )
    if payload.get("success") is False:
        return payload
    return _post_json("/api/internal/mssg", payload, timeout=30.0)


def delegate_task_response(
    task: str,
    target_session_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    sub_session: bool = True,
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
    return _post_json("/api/internal/delegate-task", {
        "sender_session_id": sender_session_id,
        "task": task,
        "target_session_id": target or None,
        "cwd": _env("BETTER_CLAUDE_CWD"),
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "sub_session": sub_session is not False,
    }, timeout=_LONG_TIMEOUT)


def ask_response(
    message: str,
    target_session_id: str = "",
    target_worker_id: str = "",
    target_worker_pool: str = "",
    run_mode: str = "direct",
    worker_description: str = "",
    worker_registry_cwd: str = "",
    ephemeral: bool = False,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    mode: str = DEFAULT_ASK_MODE,
) -> dict[str, Any]:
    target_session_id = (target_session_id or "").strip()
    target_worker_id = (target_worker_id or "").strip()
    target_worker_pool = (target_worker_pool or "").strip()
    message = (message or "").strip()
    if not any((target_session_id, target_worker_id, target_worker_pool)) or not message:
        return {"success": False, "error": "one target and message are required"}
    run_mode = (run_mode or "direct").strip() or "direct"
    if run_mode not in ("direct", "fork"):
        return {"success": False, "error": "run_mode must be 'direct' or 'fork'"}
    try:
        mode = normalize_ask_mode(mode)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if mode == ASK_MODE_CONTINUE_AND_EXPECT_MSSG_BACK_ASYNC and run_mode == "fork":
        return {"success": False, "error": "async ask mode requires run_mode='direct'"}
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
        return _post_json("/api/internal/ask-fork", {
            "app_session_id": sender_session_id,
            "instructions": message,
            "worker_session_id": target_session_id,
            "worker_description": worker_description,
            "model": selected_model,
            "provider_id": (provider_id or "").strip() or None,
            "reasoning_effort": (reasoning_effort or "").strip() or None,
            "cwd": cwd,
            "client_delegation_id": client_delegation_id,
            "run_mode": "fork",
            "worker_registry_cwd": registry_cwd or None,
            "ephemeral": bool(ephemeral),
        }, timeout=_LONG_TIMEOUT)

    ask_id = f"ask_{uuid.uuid4().hex[:10]}"
    return _post_json("/api/internal/ask", {
        "sender_session_id": sender_session_id,
        "target_session_id": target_session_id,
        "target_worker_id": target_worker_id,
        "target_worker_pool": target_worker_pool,
        "message": message,
        "ask_id": ask_id,
        "mode": mode,
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
    }, timeout=_LONG_TIMEOUT)


def create_worker_response(
    worker_description: str,
    justification: str,
    orchestration_mode: str,
    node_id: str = "",
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
        "cwd": _env("BETTER_CLAUDE_CWD"),
        "client_request_id": client_request_id,
        "node_id": node_id.strip() or None,
    }, timeout=_LONG_TIMEOUT)


def ensure_named_worker_response(
    name: str,
    cwd: str,
    orchestration_mode: str,
    provision_prompt: str = "",
    description: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    node_id: str = "",
) -> dict[str, Any]:
    name = (name or "").strip()
    cwd = (cwd or "").strip()
    mode = (orchestration_mode or "").strip()
    if not name or not cwd or not mode:
        return {"success": False, "error": "name, cwd and orchestration_mode are required"}
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
    }
    if (provision_prompt or "").strip():
        spec["provision_prompt"] = provision_prompt.strip()
    if (provider_id or "").strip():
        spec["provider_id"] = provider_id.strip()
    if (model or "").strip():
        spec["model"] = model.strip()
    if (reasoning_effort or "").strip():
        spec["reasoning_effort"] = reasoning_effort.strip()
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
        "cwd": _env("BETTER_CLAUDE_CWD"),
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "orchestration_mode": mode,
        "node_id": node_id.strip() or None,
    }, timeout=30.0)


def create_sub_session_response(
    description: str = "",
    node_id: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    return _post_json("/api/internal/create-sub-session", {
        "sender_session_id": _env_required("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"),
        "description": (description or "").strip(),
        "cwd": _env("BETTER_CLAUDE_CWD"),
        "provider_id": (provider_id or "").strip() or None,
        "model": (model or "").strip(),
        "reasoning_effort": (reasoning_effort or "").strip() or None,
        "node_id": node_id.strip() or None,
    }, timeout=30.0)


def build_server() -> FastMCP:
    disabled_tools = _disabled_builtin_tools()
    server = FastMCP(
        "communicate",
        instructions=(
            "Team tools for Better Agent sessions: mssg (queued message, "
            "fire-and-forget), ask (waits by default; mode="
            "'continue_and_expect_mssg_back_async' continues and expects a "
            "later mssg reply), delegate_task "
            "(detached handoff — offload "
            "heavy tangential/off-topic real work so you can remain focused; "
            "not for reviews; auto-routing may run session search and has a "
            "cost; set target_session_id only when you already know the target "
            "to bypass routing), run_mode='fork' runs an "
            "isolated branch from existing session context for reviews/checks; "
            "do not use fork for brand-new sessions), create_session (standalone "
            "session; orchestration_mode='team' is for complex tasks that need "
            "their own coordinator), create_sub_session (hidden native "
            "sub-session; send work to it later with mssg or ask), and "
            "create_worker (team worker, may require approval). Leave provider/"
            "model/reasoning selectors unprovided unless a specific different "
            "provider or model is truly required."
        ),
    )

    if "mssg" not in disabled_tools:
        @server.tool(description=MSSG_DESCRIPTION)
        def mssg(
            message: str,
            target_session_id: str = "",
            target_worker_id: str = "",
            target_worker_pool: str = "",
            provider_id: str = "",
            model: str = "",
            reasoning_effort: str = "",
        ) -> dict[str, Any]:
            return _safe_result(mssg_response)(
                message,
                target_session_id,
                target_worker_id,
                target_worker_pool,
                provider_id,
                model,
                reasoning_effort,
            )

    if "delegate_task" not in disabled_tools:
        @server.tool(description=DELEGATE_TASK_DESCRIPTION)
        def delegate_task(
            task: str,
            target_session_id: str = "",
            provider_id: str = "",
            model: str = "",
            reasoning_effort: str = "",
            sub_session: bool = True,
        ) -> dict[str, Any]:
            return _safe_result(delegate_task_response)(
                task,
                target_session_id,
                provider_id,
                model,
                reasoning_effort,
                sub_session,
            )

    if "create_session" not in disabled_tools:
        @server.tool(description=CREATE_SESSION_DESCRIPTION)
        def create_session(
            name: str,
            orchestration_mode: str = "native",
            node_id: str = "",
            provider_id: str = "",
            model: str = "",
            reasoning_effort: str = "",
        ) -> dict[str, Any]:
            return _safe_result(create_session_response)(
                name,
                orchestration_mode,
                node_id,
                provider_id,
                model,
                reasoning_effort,
            )

    if "create_sub_session" not in disabled_tools:
        @server.tool(description=CREATE_SUB_SESSION_DESCRIPTION)
        def create_sub_session(
            description: str = "",
            node_id: str = "",
            provider_id: str = "",
            model: str = "",
            reasoning_effort: str = "",
        ) -> dict[str, Any]:
            return _safe_result(create_sub_session_response)(
                description,
                node_id,
                provider_id,
                model,
                reasoning_effort,
            )

    if "ask" not in disabled_tools:
        @server.tool(description=ASK_DESCRIPTION)
        def ask(
            message: str,
            target_session_id: str = "",
            target_worker_id: str = "",
            target_worker_pool: str = "",
            run_mode: str = "direct",
            worker_description: str = "",
            worker_registry_cwd: str = "",
            ephemeral: bool = False,
            provider_id: str = "",
            model: str = "",
            reasoning_effort: str = "",
            mode: str = DEFAULT_ASK_MODE,
        ) -> dict[str, Any]:
            return _safe_result(ask_response)(
                message,
                target_session_id,
                target_worker_id,
                target_worker_pool,
                run_mode,
                worker_description,
                worker_registry_cwd,
                ephemeral,
                provider_id,
                model,
                reasoning_effort,
                mode,
            )

    @server.tool(description=CREATE_WORKER_DESCRIPTION)
    def create_worker(
        worker_description: str,
        justification: str,
        orchestration_mode: str,
        node_id: str = "",
    ) -> dict[str, Any]:
        return _safe_result(create_worker_response)(
            worker_description, justification, orchestration_mode, node_id,
        )

    if "ensure_named_worker" not in disabled_tools:
        @server.tool(description=ENSURE_NAMED_WORKER_DESCRIPTION)
        def ensure_named_worker(
            name: str,
            cwd: str,
            orchestration_mode: str,
            provision_prompt: str = "",
            description: str = "",
            provider_id: str = "",
            model: str = "",
            reasoning_effort: str = "",
            node_id: str = "",
        ) -> dict[str, Any]:
            return _safe_result(ensure_named_worker_response)(
                name, cwd, orchestration_mode, provision_prompt, description,
                provider_id, model, reasoning_effort, node_id,
            )

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
