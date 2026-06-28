from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TaskLaunchError(Exception):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


async def _noop_ws_callback(_event: dict) -> None:
    return None


def _resolve_singleton_session(task: dict):
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

    if orchestration_mode == "team":
        import extension_store
        not_ready = extension_store.runtime_not_ready_message(
            extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
        )
        if not_ready is not None:
            raise TaskLaunchError(not_ready, status=409)

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
            "no model configured - pin a model on the task or configure a "
            "default provider", status=400,
        )

    permission = task.get("permission")
    capability_contexts = task.get("capability_contexts") or []

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
                    user_initiated=True,
                    capability_contexts=capability_contexts,
                )
            )
        except ValueError as exc:
            raise TaskLaunchError(f"could not create task session: {exc}", status=400) from exc

    session_id = session["id"]

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
    try:
        await coordinator.broadcast_global("tasks_changed", {
            "cwd": cwd,
            "node_id": node_id or "primary",
        })
    except Exception:
        logger.debug("tasks_changed broadcast failed", exc_info=True)
