"""Project CRUD/touch and project-mapping group routes.

Depends on the coordinator only through the three capabilities it
actually needs, bound by the composition root (see `configure`). The
`projects_changed` fan-out itself (which also rebuilds project
mappings) is triggered from session-mutation paths outside this
module too, so it stays owned by main.py and is only invoked here
through the bound callback.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException, Query

import project_mapping_store
import project_store
from i18n import t
from session_manager import manager as session_manager

router = APIRouter()

_notify_projects_changed: Optional[Callable[[], Awaitable[None]]] = None
_broadcast_global: Optional[Callable[[str, dict], Any]] = None
_cached_state_snapshot: Optional[Callable[[], tuple]] = None


def configure(
    notify_projects_changed: Callable[[], Awaitable[None]],
    broadcast_global: Callable[[str, dict], Any],
    cached_state_snapshot: Callable[[], tuple],
) -> None:
    """Bind the coordinator capabilities this router needs."""
    global _notify_projects_changed, _broadcast_global, _cached_state_snapshot
    _notify_projects_changed = notify_projects_changed
    _broadcast_global = broadcast_global
    _cached_state_snapshot = cached_state_snapshot


def _require_configured() -> tuple[
    Callable[[], Awaitable[None]], Callable[[str, dict], Any], Callable[[], tuple],
]:
    if (
        _notify_projects_changed is None
        or _broadcast_global is None
        or _cached_state_snapshot is None
    ):
        raise HTTPException(status_code=503, detail="projects API is not configured")
    return _notify_projects_changed, _broadcast_global, _cached_state_snapshot


async def _broadcast_mappings_changed() -> None:
    _, broadcast_global, _ = _require_configured()
    await broadcast_global("project_mappings_changed", {})


_project_aggregates_cache: dict[tuple[str, str], dict[str, int]] = {}
_project_aggregates_gen = 0


def _project_aggregates() -> dict[tuple[str, str], dict[str, int]]:
    """Compute per-project (cwd, node_id) → counts for status badges.

    Cached: recompute only when the generation counter bumps (set by
    session mutation events via `invalidate_project_aggregates`).
    Reads from the background-tick running-state cache — no PID
    probing on the event loop."""
    global _project_aggregates_cache, _project_aggregates_gen
    if _project_aggregates_gen > 0 and _project_aggregates_cache:
        return _project_aggregates_cache
    import working_mode as _wm
    _, _, cached_state_snapshot = _require_configured()
    running_sids, _ = cached_state_snapshot()
    unread_by_sid = session_manager.unread_counts_snapshot()
    agg: dict[tuple[str, str], dict[str, int]] = {}
    for s in session_manager.list():
        if _wm.should_hide_from_sidebar(s):
            continue
        sid = s.get("id")
        cwd = s.get("cwd") or ""
        if not sid or not cwd:
            continue
        key = (cwd, s.get("node_id") or "primary")
        slot = agg.setdefault(
            key, {"running_count": 0, "unread_session_count": 0}
        )
        if sid in running_sids:
            slot["running_count"] += 1
        if unread_by_sid.get(sid, 0) > 0:
            slot["unread_session_count"] += 1
    _project_aggregates_cache = agg
    _project_aggregates_gen += 1
    return agg


def invalidate_project_aggregates() -> None:
    """Bump the generation counter so the next _project_aggregates call
    recomputes. Called from session mutation broadcast paths."""
    global _project_aggregates_gen
    _project_aggregates_gen = 0


@router.get("/api/projects")
async def get_projects():
    aggs = await asyncio.to_thread(_project_aggregates)
    out: list[dict] = []
    for p in await asyncio.to_thread(project_store.list_projects):
        key = (p.get("path") or "", p.get("node_id") or "primary")
        slot = aggs.get(key, {"running_count": 0, "unread_session_count": 0})
        out.append({
            **p,
            "running_count": slot["running_count"],
            "unread_session_count": slot["unread_session_count"],
        })
    return {"projects": out}


@router.post("/api/projects")
async def create_project(body: dict):
    notify_projects_changed, _, _ = _require_configured()
    record = await asyncio.to_thread(
        project_store.add_project,
        path=body.get("path", ""),
        name=body.get("name") or None,
        node_id=body.get("node_id") or "primary",
    )
    if not record:
        raise HTTPException(status_code=400, detail=t("error.invalid_path"))
    await notify_projects_changed()
    return record


@router.delete("/api/projects")
async def delete_project(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    notify_projects_changed, _, _ = _require_configured()
    deleted = await asyncio.to_thread(
        project_store.remove_project,
        path,
        node_id=node_id,
    )
    if deleted:
        await notify_projects_changed()
    return {"deleted": deleted}


@router.post("/api/projects/touch")
async def touch_project(body: dict):
    notify_projects_changed, _, _ = _require_configured()
    await asyncio.to_thread(
        project_store.touch_project,
        body.get("path", ""),
        node_id=body.get("node_id") or "primary",
    )
    await notify_projects_changed()
    return {"status": "ok"}


# ── Project mappings ───────────────────────────────────────────


@router.get("/api/project-mappings")
async def get_project_mappings():
    return {"groups": await asyncio.to_thread(project_mapping_store.list_mappings)}


@router.post("/api/project-mappings/rebuild")
async def rebuild_project_mappings():
    projects = await asyncio.to_thread(project_store.list_projects)
    groups = await asyncio.to_thread(project_mapping_store.rebuild_and_save, projects)
    await _broadcast_mappings_changed()
    return {"groups": groups}


@router.patch("/api/project-mappings/{group_id}")
async def update_project_mapping(group_id: str, body: dict):
    result = await asyncio.to_thread(
        project_mapping_store.update_group,
        group_id,
        label=body.get("label"),
        members=body.get("members"),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Mapping group not found")
    await _broadcast_mappings_changed()
    return result


@router.delete("/api/project-mappings/{group_id}")
async def delete_project_mapping(group_id: str):
    deleted = await asyncio.to_thread(project_mapping_store.remove_group, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Mapping group not found")
    await _broadcast_mappings_changed()
    return {"deleted": True}
