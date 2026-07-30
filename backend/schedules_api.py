"""Scheduled prompts: the agent-facing create/list/delete gateway over the
durable schedule store, plus the user-facing cross-session snapshot.

The coordinator is injected by the composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException

import internal_guards
from i18n import t
from scheduler import broadcast_schedules
from stores import schedule_store
from session_helpers import session_exists as _session_exists
from session_manager import manager as session_manager

router = APIRouter()

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator schedule changes broadcast through."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="schedules API is not configured")
    return _coordinator_ref


@router.post("/api/internal/schedules")
async def internal_schedules(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Invoked by the runner's `scheduler` SDK MCP tools. Backend owns
    the durable schedule store; the runner only publishes the request.
    All validation is server-side (schedule_store.create raises
    ValueError with a tool-surfaceable message)."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))

    app_session_id = body.get("app_session_id") or ""
    if not await _session_exists(app_session_id):
        return {"success": False, "error": t("error.session_not_found_retry")}

    action = body.get("action")
    if action == "create":
        delay = body.get("delay_seconds")
        fire_at = body.get("fire_at")
        if fire_at is None and delay is not None:
            import math as _math
            if (not isinstance(delay, (int, float)) or isinstance(delay, bool)
                    or not _math.isfinite(delay) or delay < 0
                    or delay > 366 * 24 * 3600):
                return {"success": False,
                        "error": "delay_seconds must be a finite number of "
                                 "seconds between 0 and one year"}
            fire_at = (datetime.now() + timedelta(seconds=float(delay))).isoformat()
        from stores import task_store
        source_task_id = None
        owner = await asyncio.to_thread(task_store.find_pending_run_for_session, app_session_id)
        if owner is not None:
            source_task_id = owner[0]
        try:
            rec = await asyncio.to_thread(
                schedule_store.create,
                app_session_id=app_session_id,
                prompt=body.get("prompt"),
                kind=body.get("kind"),
                fire_at=fire_at,
                interval_seconds=body.get("interval_seconds"),
                source_task_id=source_task_id,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        await broadcast_schedules(_coordinator(), app_session_id)
        return {"success": True, "schedule": rec}

    if action == "list":
        return {
            "success": True,
            "schedules": await asyncio.to_thread(
                schedule_store.list_for_session,
                app_session_id,
            ),
        }

    if action == "delete":
        schedule_id = str(body.get("schedule_id") or "")
        existing = await asyncio.to_thread(schedule_store.get, schedule_id)
        if existing is None or existing.get("app_session_id") != app_session_id:
            return {"success": False, "error": "unknown schedule_id"}
        await asyncio.to_thread(schedule_store.delete, schedule_id)
        await broadcast_schedules(_coordinator(), app_session_id)
        return {"success": True}

    return {"success": False, "error": "action must be create|list|delete"}


@router.get("/api/schedules")
async def get_all_schedules():
    """User-facing snapshot of every schedule across all sessions,
    enriched with the owning session's name and existence. Deliberately
    NOT exposed on the model-facing internal endpoint — a session's
    model must not read other sessions' schedule prompts. Push side:
    the global `schedules_changed` WS ping (clients refetch here)."""
    schedules = await asyncio.to_thread(schedule_store.list_all)
    summaries = await asyncio.to_thread(session_manager.list)
    names = {s.get("id"): s.get("name") for s in summaries}
    out = []
    for rec in schedules:
        sid = rec.get("app_session_id")
        entry = dict(rec)
        if sid in names:
            entry["session_name"] = names[sid]
            entry["session_exists"] = True
        else:
            # Non-root sid (fork) or deleted session — resolve the rare
            # case individually instead of loading every root.
            sess = await asyncio.to_thread(session_manager.get, sid)
            entry["session_name"] = (sess or {}).get("name")
            entry["session_exists"] = sess is not None
        out.append(entry)
    return {"schedules": out}


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule_by_id(schedule_id: str):
    """User-facing cancel by id. No session-exists gate: schedules
    whose session was deleted (orphans) must stay cancelable."""
    removed = await asyncio.to_thread(schedule_store.delete, schedule_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="unknown schedule_id")
    await broadcast_schedules(_coordinator(), removed.get("app_session_id") or "")
    return {"success": True}
