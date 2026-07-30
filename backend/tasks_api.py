"""Task definitions, their on-demand launch, and the published outputs
they produce — including the tokenised preview URL the frontend embeds.

The coordinator is injected by the composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import extension_store
import internal_guards
import task_output_preview_urls
from i18n import t
from session_detail_api import _resolve_session_node_id

router = APIRouter()

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator task launches and broadcasts run through."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="tasks API is not configured")
    return _coordinator_ref


_OUTPUT_CONTENT_HEADERS = {
    "Content-Security-Policy": (
        "sandbox allow-popups allow-popups-to-escape-sandbox; "
        "default-src 'none'; img-src data: blob: https:; "
        "style-src 'unsafe-inline'; font-src data:; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}


def _require_tasks_internal(x_internal_token: str) -> None:
    """Gate for the tasks substrate. Tasks are surfaced by the (private)
    routines extension; in a pure-public checkout the extension is absent and
    this fails closed."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('routines')
    )


@router.post("/api/internal/tasks")
async def internal_tasks(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Backend-owned task definitions + on-demand launch.

    Tasks are reusable, run-when-clicked definitions that spin up an
    autonomous session (via `task_runner.launch_task`). Core owns the
    durable store + launch; the routines extension's routes/MCP only forward
    here. All validation is server-side (`task_store` raises ValueError
    with a surfaceable message). Actions: list | get | create | update |
    delete | run | stop.
    """
    _require_tasks_internal(x_internal_token)
    from stores import task_store
    from stores import task_trigger_store
    import task_runner

    action = (body or {}).get("action")

    if action == "list":
        cwd = str((body or {}).get("cwd") or "").strip()
        node_id = str((body or {}).get("node_id") or "primary").strip() or "primary"
        if not cwd:
            return {"success": False, "error": "cwd is required"}
        return {
            "success": True,
            "tasks": await asyncio.to_thread(task_store.list_for_project, cwd, node_id),
        }

    if action == "get":
        task_id = str((body or {}).get("task_id") or "").strip()
        rec = await asyncio.to_thread(task_store.get, task_id)
        if rec is None:
            return {"success": False, "error": "unknown task_id"}
        return {"success": True, "task": rec}

    if action == "create":
        node_id = _resolve_session_node_id(body or {})
        try:
            rec = await asyncio.to_thread(
                task_store.create,
                cwd=str((body or {}).get("cwd") or "").strip(),
                name=(body or {}).get("name"),
                prompt=(body or {}).get("prompt"),
                node_id=node_id,
                description=(body or {}).get("description") or "",
                orchestration_mode=(body or {}).get("orchestration_mode") or "native",
                worker_creation_policy=(body or {}).get("worker_creation_policy") or "approve",
                model=(body or {}).get("model"),
                provider_id=(body or {}).get("provider_id"),
                reasoning_effort=(body or {}).get("reasoning_effort"),
                runner=(body or {}).get("runner"),
                permission=(body or {}).get("permission"),
                capability_contexts=(body or {}).get("capability_contexts"),
                singleton=bool((body or {}).get("singleton", False)),
                goal=(body or {}).get("goal") or "",
                trigger=(body or {}).get("trigger"),
                scripts=(body or {}).get("scripts"),
                assessment=(body or {}).get("assessment"),
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await asyncio.to_thread(task_trigger_store.register_for_task, rec)
        await task_runner.broadcast_tasks_changed(_coordinator(), rec["cwd"], rec["node_id"])
        return {"success": True, "task": rec}

    if action == "update":
        task_id = str((body or {}).get("task_id") or "").strip()
        patch = (body or {}).get("patch")
        if not isinstance(patch, dict):
            return {"success": False, "error": "patch must be an object"}
        try:
            rec = await asyncio.to_thread(task_store.update, task_id, patch)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        if rec is None:
            return {"success": False, "error": "unknown task_id"}
        await asyncio.to_thread(task_trigger_store.register_for_task, rec)
        await task_runner.broadcast_tasks_changed(_coordinator(), rec["cwd"], rec["node_id"])
        return {"success": True, "task": rec}

    if action == "delete":
        task_id = str((body or {}).get("task_id") or "").strip()
        import routine_memory

        try:
            removed = await routine_memory.delete_task(task_id)
        except (routine_memory.RoutineMemoryAccessError, routine_memory.RoutineMemoryBusyError) as exc:
            return {"success": False, "error": str(exc)}
        if removed is None:
            return {"success": False, "error": "unknown task_id"}
        from stores import task_output_store
        await asyncio.to_thread(task_output_store.delete_for_task, task_id)
        await asyncio.to_thread(task_trigger_store.unregister_task, task_id)
        await task_runner.broadcast_tasks_changed(
            _coordinator(), removed.get("cwd") or "", removed.get("node_id") or "primary",
        )
        return {"success": True}

    if action == "run":
        task_id = str((body or {}).get("task_id") or "").strip()
        try:
            result = await task_runner.launch_task(
                task_id,
                coordinator=_coordinator(),
                prompt_override=(body or {}).get("prompt"),
                client_id=(body or {}).get("client_id"),
            )
        except task_runner.TaskLaunchError as e:
            return {"success": False, "error": str(e)}
        return {"success": True, **result}

    if action == "stop":
        task_id = str((body or {}).get("task_id") or "").strip()
        try:
            result = await task_runner.stop_task(task_id, coordinator=_coordinator())
        except task_runner.TaskLaunchError as e:
            return {"success": False, "error": str(e)}
        return {"success": True, **result}

    return {"success": False, "error": "action must be list|get|create|update|delete|run|stop"}


@router.post("/api/internal/task-outputs")
async def internal_task_outputs(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_tasks_internal(x_internal_token)
    from stores import task_output_store, task_store
    import task_runner

    action = str((body or {}).get("action") or "").strip()
    task_id = str((body or {}).get("task_id") or "").strip()
    if not task_id:
        return {"success": False, "error": "task_id is required"}
    task = await asyncio.to_thread(task_store.get, task_id)
    if task is None:
        return {"success": False, "error": "unknown task_id"}

    if action == "list":
        outputs = await asyncio.to_thread(
            task_output_store.list_for_task,
            task_id,
            limit=int((body or {}).get("limit") or 50),
        )
        return {"success": True, "outputs": outputs, "latest": outputs[0] if outputs else None}

    if action == "publish":
        try:
            output = await asyncio.to_thread(
                task_output_store.publish,
                task_id=task_id,
                task_cwd=str(task.get("cwd") or ""),
                title=(body or {}).get("title"),
                kind=(body or {}).get("kind") or "artifact",
                content_type=(body or {}).get("content_type") or "text/html",
                content=(body or {}).get("content") or "",
                file_path=(body or {}).get("file_path") or "",
                session_id=(body or {}).get("session_id") or (body or {}).get("app_session_id") or "",
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await task_runner.broadcast_tasks_changed(
            _coordinator(),
            task.get("cwd") or "",
            task.get("node_id") or "primary",
        )
        return {"success": True, "output": output}

    return {"success": False, "error": "action must be list|publish"}


@router.get("/api/internal/task-outputs/{task_id}/{output_id}/content")
async def internal_task_output_content(
    task_id: str,
    output_id: str,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_tasks_internal(x_internal_token)
    from fastapi.responses import FileResponse
    from stores import task_output_store

    try:
        path, content_type = await asyncio.to_thread(
            task_output_store.content_path,
            task_id,
            output_id,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="unknown output")
    return FileResponse(
        path,
        media_type=content_type,
        headers=dict(_OUTPUT_CONTENT_HEADERS),
    )


@router.get("/api/task-outputs/{task_id}/{output_id}/preview-url")
async def task_output_preview_url(task_id: str, output_id: str):
    from stores import task_output_store

    try:
        await asyncio.to_thread(task_output_store.content_path, task_id, output_id)
        token = task_output_preview_urls.mint(task_id, output_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="unknown output")
    return {"url": f"/api/task-output/preview/{token}/{task_id}/{output_id}"}


@router.get("/api/task-output/preview/{token}/{task_id}/{output_id}")
async def task_output_preview(token: str, task_id: str, output_id: str):
    from fastapi.responses import FileResponse
    from stores import task_output_store

    try:
        task_output_preview_urls.verify(token, task_id, output_id)
    except ValueError:
        raise HTTPException(status_code=403, detail="invalid preview token")
    try:
        path, content_type = await asyncio.to_thread(
            task_output_store.content_path,
            task_id,
            output_id,
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="unknown output")
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            **_OUTPUT_CONTENT_HEADERS,
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, no-store",
        },
    )
