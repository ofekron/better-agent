"""Launch a saved task as an autonomous Better Agent session.

A task (see `stores/task_store.py`) is a reusable definition. "Running" a
task means: create (or reuse, for singleton tasks) a real Better Agent
session pre-configured with the task's autonomy levers, then submit the
task's prompt as its first turn through the normal
`coordinator.submit_prompt` funnel — the SAME path user prompts and the
scheduler ticker use. The run therefore inherits the full
ingestion / convergence / crash-recovery machinery: a backend restart
mid-run resumes via `run_recovery` exactly like any other session.

Why a fresh session per run (by default):

  - Sessions are the unit of recovery, history, and isolation. Reusing
    that machinery is what makes a task "run on demand, survive restarts"
    for free — we add no parallel execution/recovery code.
  - Each run is fully isolated: a failed or messy run never corrupts the
    next one. (Opt into a singleton session per task when continuity
    across runs is wanted.)

Why this is the "broadest, least-dependent-on-the-user" tool:

  - The run is a normal session, so the agent has EVERY capability a
    session has — all tools, skills, MCPs, sub-agents, capabilities.
  - The task's `permission` override is applied so tool calls are
    auto-accepted (no human approval clicks mid-run).
  - `worker_creation_policy` defaults to "approve" so the agent may spawn
    helper sub-sessions without asking the user.
  - The turn is submitted with `user_initiated=False` / `source="task"`,
    matching the scheduler's "the human didn't type this turn" semantics.

This module owns ZERO durable state: `task_store` owns task definitions,
`session_manager`/`session_store` own sessions, and the coordinator owns
the run. We only orchestrate the launch.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TaskLaunchError(Exception):
    """Raised when a task launch cannot proceed. Carries a user-surfaceable
    message and an HTTP-ish status hint so the REST layer can map it."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


async def _noop_ws_callback(_event: dict) -> None:
    # submit_prompt requires a ws_callback in params; the per-session
    # processor swaps in the registry-based dispatcher before the turn
    # runs (identical to scheduler.py's _noop_ws_callback contract).
    return None


def _resolve_singleton_session(task: dict):
    """Return the reusable session dict for a singleton task, or None.

    Validates that the remembered session still exists; if it was deleted,
    the binding is cleared so the caller mints a fresh one."""
    from session_manager import manager as session_manager
    from stores import task_store

    sid = task.get("singleton_session_id")
    if not sid:
        return None
    existing = session_manager.get_lite(sid)
    if existing is None:
        task_store.clear_singleton_session(task["id"])
        return None
    return existing


async def launch_task(
    task_id: str,
    *,
    coordinator,
    prompt_override: Optional[str] = None,
    client_id: Optional[str] = None,
) -> dict[str, Any]:
    """Launch a saved task. Returns a small descriptor:

        {"task_id", "session_id", "queue_item_id", "reused": bool}

    Steps, all on top of existing primitives:
      1. Load + validate the task definition.
      2. Resolve provider/model (the task's pin, else the active default).
      3. Create a fresh session (or reuse the singleton) carrying the
         task's autonomy levers.
      4. Submit the task prompt as the first turn (user_initiated=False).
      5. Record the run breadcrumb on the task and broadcast the change.
    """
    import asyncio

    import config_store
    from session_manager import manager as session_manager
    from stores import task_store

    task = await asyncio.to_thread(task_store.get, task_id)
    if task is None:
        raise TaskLaunchError("unknown task", status=404)

    prompt = prompt_override if (prompt_override and prompt_override.strip()) else task.get("prompt")
    if not prompt or not str(prompt).strip():
        raise TaskLaunchError("task has no prompt", status=400)
    prompt = str(prompt)

    cwd = task.get("cwd") or ""
    if not cwd:
        raise TaskLaunchError("task is missing cwd", status=400)
    node_id = task.get("node_id") or "primary"
    orchestration_mode = task.get("orchestration_mode") or "native"
    if orchestration_mode == "manager":
        orchestration_mode = "team"
    worker_creation_policy = task.get("worker_creation_policy") or "approve"

    # Team mode requires the team-orchestration runtime; fail closed with a
    # clear message rather than silently downgrading the task's mode.
    if orchestration_mode == "team":
        import extension_store
        not_ready = extension_store.runtime_not_ready_message(
            extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
        )
        if not_ready is not None:
            raise TaskLaunchError(not_ready, status=409)

    # Resolve provider/model. A task may pin them; otherwise fall back to the
    # default_session internal-LLM assignment (active provider + its default
    # model), so an unconfigured task still runs.
    provider_id = task.get("provider_id")
    model = task.get("model")
    reasoning_effort = task.get("reasoning_effort")
    if not provider_id or not model:
        defaults = await asyncio.to_thread(config_store.resolve_internal_llm, "default_session")
        provider_id = provider_id or defaults.get("provider_id")
        model = model or defaults.get("model")
        if reasoning_effort is None:
            reasoning_effort = defaults.get("reasoning_effort") or None
    if not model:
        raise TaskLaunchError(
            "no model configured — pin a model on the task or configure a "
            "default provider", status=400,
        )

    permission = task.get("permission")  # opaque provider-validated dict or None
    capability_contexts = task.get("capability_contexts") or []

    # Apply provider env (ANTHROPIC_* / config dir) before any turn runs —
    # mirrors every other backend-driven submit path.
    await asyncio.to_thread(config_store.apply_env_vars)

    reused = False
    session = None
    if task.get("singleton"):
        session = await asyncio.to_thread(_resolve_singleton_session, task)
        reused = session is not None

    if session is None:
        try:
            session = await asyncio.to_thread(
                lambda: session_manager.create(
                    name=task.get("name") or "Task",
                    model=model,
                    cwd=cwd,
                    orchestration_mode=orchestration_mode,
                    source="web",
                    provider_id=provider_id,
                    reasoning_effort=reasoning_effort,
                    permission=permission,
                    node_id=node_id,
                    worker_creation_policy=worker_creation_policy,
                    # The user is AWARE this session exists (they clicked Run
                    # and we deep-link to it), so it is user-initiated even
                    # though the FIRST TURN is not (that flag is on the prompt).
                    user_initiated=True,
                    capability_contexts=capability_contexts,
                )
            )
        except ValueError as exc:
            raise TaskLaunchError(f"could not create task session: {exc}", status=400) from exc

    session_id = session["id"]

    # Submit the task prompt as the first turn through the normal funnel.
    # `user_initiated=False` + `source="task"`: the human didn't type this
    # turn (same semantic the scheduler uses for fired prompts).
    item_id = await coordinator.submit_prompt_async(session_id, {
        "prompt": prompt,
        "app_session_id": session_id,
        "model": session.get("model") or model,
        "cwd": session.get("cwd") or cwd,
        "ws_callback": _noop_ws_callback,
        "images": None,
        "files": None,
        "orchestration_mode": session.get("orchestration_mode") or orchestration_mode,
        "client_id": client_id,
        "source": "task",
        "user_initiated": False,
    })

    await asyncio.to_thread(task_store.record_run, task_id, session_id, now=datetime.now())
    await broadcast_tasks_changed(coordinator, cwd, node_id)

    return {
        "task_id": task_id,
        "session_id": session_id,
        "queue_item_id": item_id,
        "reused": reused,
    }


async def broadcast_tasks_changed(coordinator, cwd: str, node_id: str = "primary") -> None:
    """Cross-tab invalidation ping for a project's task list. Authoritative
    state lives in `task_store`; clients refetch on receipt (mirrors
    `broadcast_workers_changed`)."""
    try:
        await coordinator.broadcast_global("tasks_changed", {
            "cwd": cwd,
            "node_id": node_id or "primary",
        })
    except Exception:
        logger.debug("tasks_changed broadcast failed", exc_info=True)
