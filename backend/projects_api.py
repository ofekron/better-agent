"""Project CRUD/touch and project-mapping group routes.

Depends on the coordinator only through the four capabilities it
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
import session_status
import user_input_store
from i18n import t
from session_manager import manager as session_manager

router = APIRouter()

_notify_projects_changed: Optional[Callable[[], Awaitable[None]]] = None
_broadcast_global: Optional[Callable[[str, dict], Any]] = None
_projected_state_snapshot: Optional[Callable[[], tuple]] = None
_projected_state_version: Optional[Callable[[], int]] = None


def configure(
    notify_projects_changed: Callable[[], Awaitable[None]],
    broadcast_global: Callable[[str, dict], Any],
    projected_state_snapshot: Callable[[], tuple],
    projected_state_version: Callable[[], int],
) -> None:
    """Bind the coordinator capabilities this router needs."""
    global _notify_projects_changed, _broadcast_global
    global _projected_state_snapshot, _projected_state_version
    _notify_projects_changed = notify_projects_changed
    _broadcast_global = broadcast_global
    _projected_state_snapshot = projected_state_snapshot
    _projected_state_version = projected_state_version


def _require_configured() -> tuple[
    Callable[[], Awaitable[None]],
    Callable[[str, dict], Any],
    Callable[[], tuple],
    Callable[[], int],
]:
    if (
        _notify_projects_changed is None
        or _broadcast_global is None
        or _projected_state_snapshot is None
        or _projected_state_version is None
    ):
        raise HTTPException(status_code=503, detail="projects API is not configured")
    return (
        _notify_projects_changed,
        _broadcast_global,
        _projected_state_snapshot,
        _projected_state_version,
    )


async def _broadcast_mappings_changed() -> None:
    _, broadcast_global, _, _ = _require_configured()
    await broadcast_global("project_mappings_changed", {})


_project_aggregates_cache: dict[tuple[str, str], dict[str, int]] = {}
_project_aggregates_gen = 0
_project_aggregates_state_version = -1


def empty_aggregate() -> dict[str, int]:
    """The zero value for every project counter. One definition, shared
    by the aggregation itself and by projects that have no sessions."""
    return {
        "running_count": 0,
        "unread_session_count": 0,
        "waiting_for_user_count": 0,
        "errored_count": 0,
    }


def _project_aggregates() -> dict[tuple[str, str], dict[str, int]]:
    """Compute per-project (cwd, node_id) → counts for status badges.

    Counts each status dimension independently via
    `session_status.compute`, the same derivation the sidebar rows use,
    so a project counter can never disagree with the rows it summarizes.
    Dimensions do not mask each other: one errored, unread session
    increments both counters.

    Cached: recompute when a session-dimension invalidation fires or the
    authoritative monitoring projection version advances.
    Reads from the same monitoring projection published to WS consumers;
    no PID probing occurs on the event loop."""
    global _project_aggregates_cache, _project_aggregates_gen
    global _project_aggregates_state_version
    _, _, projected_state_snapshot, projected_state_version = _require_configured()
    state_version = projected_state_version()
    if (
        _project_aggregates_gen > 0
        and _project_aggregates_cache
        and _project_aggregates_state_version == state_version
    ):
        return _project_aggregates_cache
    import working_mode as _wm
    _, monitoring_by_sid = projected_state_snapshot()
    unread_by_sid = session_manager.unread_counts_snapshot()
    pending_input_by_sid = user_input_store.pending_counts_by_session()
    agg: dict[tuple[str, str], dict[str, int]] = {}
    for s in session_manager.list():
        if _wm.should_hide_from_sidebar(s):
            continue
        sid = s.get("id")
        cwd = s.get("cwd") or ""
        if not sid or not cwd:
            continue
        status = session_status.compute(
            s, monitoring_by_sid, unread_by_sid, pending_input_by_sid
        )
        slot = agg.setdefault((cwd, s.get("node_id") or "primary"), empty_aggregate())
        if status.running == session_status.RUNNING:
            slot["running_count"] += 1
        if status.unread:
            slot["unread_session_count"] += 1
        if status.waiting_for_user:
            slot["waiting_for_user_count"] += 1
        if status.errored:
            slot["errored_count"] += 1
    _project_aggregates_cache = agg
    _project_aggregates_gen += 1
    _project_aggregates_state_version = state_version
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
        out.append({**p, **aggs.get(key, empty_aggregate())})
    return {"projects": out}


@router.post("/api/projects")
async def create_project(body: dict):
    notify_projects_changed, _, _, _ = _require_configured()
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
    notify_projects_changed, _, _, _ = _require_configured()
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
    notify_projects_changed, _, _, _ = _require_configured()
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
