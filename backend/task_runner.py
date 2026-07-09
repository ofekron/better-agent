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


def _routine_prompt(task: dict, prompt: str) -> str:
    name = str(task.get("name") or "Routine").strip() or "Routine"
    summary = str(task.get("description") or "").strip()
    criteria = str(task.get("goal") or "").strip()
    parts = [
        f"You are running the saved routine: {name}.",
        (
            "Treat the routine description as the source spec. Infer the "
            "concrete steps, create any needed todo plan, run the required "
            "checks or follow-up work, and report what happened."
        ),
    ]
    if summary:
        parts.append(f"Summary: {summary}")
    if criteria:
        parts.append(f"Success criteria: {criteria}")
    parts.append(f"Routine description:\n{prompt.strip()}")
    return "\n\n".join(parts)


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
    source: str = "manual",
) -> dict[str, Any]:
    import asyncio

    import config_store
    import task_script
    from session_manager import manager as session_manager
    from stores import task_store

    task = await asyncio.to_thread(task_store.get, task_id)
    if task is None:
        raise TaskLaunchError("unknown task", status=404)
    if task.get("stopped"):
        raise TaskLaunchError("routine is stopped - resume it before running", status=409)

    prompt = prompt_override if (prompt_override and prompt_override.strip()) else task.get("prompt")
    if not prompt or not str(prompt).strip():
        raise TaskLaunchError("task has no prompt", status=400)
    prompt = _routine_prompt(task, str(prompt))

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

    # Pre-scripts gate the run: run before the agent, stop at first failure.
    # Their combined stdout is appended to the prompt so the agent sees the
    # setup output (test inventory, env snapshot, etc.) as context.
    scripts = task.get("scripts") or {}
    pre_scripts = scripts.get("pre") or []
    if pre_scripts:
        ok, stdout = await asyncio.to_thread(
            lambda: task_script.run_scripts(pre_scripts, fallback_cwd=cwd, timeout=120),
        )
        if not ok:
            raise TaskLaunchError(
                f"pre-script failed:\n{stdout[:2000]}", status=400,
            )
        if stdout.strip():
            prompt = f"{prompt}\n\n<pre-script-output>\n{stdout.strip()}\n</pre-script-output>"

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
                    name=task.get("name") or "Routine",
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

    prompt_params = {
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
    }

    item_id = None
    if task.get("singleton"):
        # A singleton task reuses one session across fires. If a scheduled
        # trigger fires again while the previous fire is still queued behind
        # an in-flight run, collapse into that queued item instead of piling
        # up an unbounded backlog of stale "check now" prompts.
        import team_messaging

        collapse_key = f"routine:{task_id}"
        prompt_params["collapse_key"] = collapse_key
        prompt_params["collapse_policy"] = team_messaging.COLLAPSE_POLICY_TAKE_LATEST
        item_id = await coordinator.collapse_queued_prompt_take_latest(
            session_id, collapse_key, None, prompt_params,
        )
    if item_id is None:
        item_id = await coordinator.submit_prompt_async(session_id, prompt_params)

    await asyncio.to_thread(
        task_store.record_run, task_id, session_id,
        queue_item_id=str(item_id) if item_id is not None else None,
        now=datetime.now(),
    )

    # Close the stop/launch race: stop_task snapshots the ledger when it
    # flips `stopped`, so a launch in flight at that moment is invisible to
    # the cascade. Both record_run and set_stopped serialize on the store
    # lock — re-reading AFTER our ledger write guarantees one side sees the
    # other: either stop saw our session, or we see `stopped` and unwind.
    latest = await asyncio.to_thread(task_store.get, task_id)
    if latest is not None and latest.get("stopped"):
        # Best-effort unwind: the session stays on the ledger either way, so
        # a failed step here is retryable via stop and must not mask the 409.
        try:
            coordinator.cancel_queued(session_id)
            await asyncio.to_thread(session_manager.remove_queued_prompt, session_id, None)
            await coordinator.cancel_session(session_id)
        except Exception:
            logger.exception(
                "launch_task: unwind after stop race failed for session %s",
                session_id,
            )
        raise TaskLaunchError("routine was stopped during launch", status=409)

    await broadcast_tasks_changed(coordinator, cwd, node_id)

    return {
        "task_id": task_id,
        "session_id": session_id,
        "queue_item_id": item_id,
        "reused": reused,
        "source": source,
    }


async def stop_task(task_id: str, *, coordinator) -> dict[str, Any]:
    """Stop a routine and tear down everything it spawned: mark it stopped
    (blocks new launches, fail-closed first), drop its armed triggers, and
    for every session the routine ever launched — cancel queued prompts,
    cancel all in-flight runs, and delete schedules that session created.
    Sessions themselves are kept as run history. Resume via update with
    stopped=false, which re-arms the trigger config."""
    import asyncio

    from session_manager import manager as session_manager
    from stores import schedule_store, task_store, task_trigger_store

    task = await asyncio.to_thread(task_store.set_stopped, task_id, True)
    if task is None:
        raise TaskLaunchError("unknown task", status=404)
    await asyncio.to_thread(task_trigger_store.unregister_task, task_id)

    session_ids = [s for s in (task.get("spawned_session_ids") or []) if s]
    if task.get("singleton_session_id") and task["singleton_session_id"] not in session_ids:
        session_ids.append(task["singleton_session_id"])

    # One store read for schedule attribution instead of a read per sid.
    ledger = set(session_ids)
    schedules_by_sid: dict[str, list[dict]] = {}
    for sched in await asyncio.to_thread(schedule_store.list_all):
        sid = sched.get("app_session_id") or ""
        if sid in ledger and sched.get("source_task_id") == task_id:
            schedules_by_sid.setdefault(sid, []).append(sched)

    # Bounds the fan-out: the ledger is uncapped, and each queued-prompt
    # removal that actually finds a queue hydrates a full session root.
    teardown_slots = asyncio.Semaphore(8)

    async def _teardown(sid: str) -> dict[str, int]:
        counts = {"runs": 0, "queued": 0, "schedules": 0}
        async with teardown_slots:
            had_queue = coordinator.cancel_queued(sid)
            counts["queued"] = 1 if had_queue else 0
            # Skip the session-root hydration + rewrite when both the live
            # queue and the queue projection agree there is nothing queued.
            if had_queue or session_manager.queued_prompt_count(sid) > 0:
                await asyncio.to_thread(session_manager.remove_queued_prompt, sid, None)
            counts["runs"] = await coordinator.cancel_session(sid)
            deleted = 0
            for sched in schedules_by_sid.get(sid, []):
                if await asyncio.to_thread(schedule_store.delete, sched["id"]) is not None:
                    deleted += 1
            counts["schedules"] = deleted
            if deleted:
                from scheduler import broadcast_schedules
                await broadcast_schedules(coordinator, sid)
        return counts

    # Per-sid isolation: one failing session must not shield the rest from
    # teardown. Errors are aggregated, never swallowed.
    results = await asyncio.gather(
        *(_teardown(sid) for sid in session_ids), return_exceptions=True,
    )
    cancelled_runs = 0
    cancelled_queued = 0
    deleted_schedules = 0
    errors: list[str] = []
    for sid, res in zip(session_ids, results):
        if isinstance(res, BaseException):
            logger.exception("stop_task: teardown failed for session %s", sid, exc_info=res)
            errors.append(f"{sid}: {res}")
            continue
        cancelled_runs += res["runs"]
        cancelled_queued += res["queued"]
        deleted_schedules += res["schedules"]

    await broadcast_tasks_changed(
        coordinator, task.get("cwd") or "", task.get("node_id") or "primary",
    )
    return {
        "task_id": task_id,
        "stopped_sessions": session_ids,
        "cancelled_runs": cancelled_runs,
        "cancelled_queued_sessions": cancelled_queued,
        "deleted_schedules": deleted_schedules,
        "ledger_partial": bool(task.get("spawned_ledger_partial")),
        "errors": errors,
    }


async def broadcast_tasks_changed(coordinator, cwd: str, node_id: str = "primary") -> None:
    try:
        await coordinator.broadcast_global("tasks_changed", {
            "cwd": cwd,
            "node_id": node_id or "primary",
        })
    except Exception:
        logger.debug("tasks_changed broadcast failed", exc_info=True)
