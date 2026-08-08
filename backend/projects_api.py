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

import project_aggregate_projection
import project_mapping_store
import project_store
from i18n import t

router = APIRouter()

_notify_projects_changed: Optional[Callable[[], Awaitable[None]]] = None
_broadcast_global: Optional[Callable[[str, dict], Any]] = None
_aggregate_snapshot: Optional[Callable[[], Awaitable[dict]]] = None


def configure(
    notify_projects_changed: Callable[[], Awaitable[None]],
    broadcast_global: Callable[[str, dict], Any],
    aggregate_snapshot: Callable[[], Awaitable[dict]],
) -> None:
    """Bind the coordinator capabilities this router needs."""
    global _notify_projects_changed, _broadcast_global
    global _aggregate_snapshot
    _notify_projects_changed = notify_projects_changed
    _broadcast_global = broadcast_global
    _aggregate_snapshot = aggregate_snapshot


def _require_configured() -> tuple[
    Callable[[], Awaitable[None]],
    Callable[[str, dict], Any],
    Callable[[], Awaitable[dict]],
]:
    if (
        _notify_projects_changed is None
        or _broadcast_global is None
        or _aggregate_snapshot is None
    ):
        raise HTTPException(status_code=503, detail="projects API is not configured")
    return (
        _notify_projects_changed,
        _broadcast_global,
        _aggregate_snapshot,
    )


async def _broadcast_mappings_changed() -> None:
    _, broadcast_global, _ = _require_configured()
    await broadcast_global("project_mappings_changed", {})


empty_aggregate = project_aggregate_projection.empty_aggregate


@router.get("/api/projects")
async def get_projects():
    _, _, aggregate_snapshot = _require_configured()
    projection = await aggregate_snapshot()
    aggs = projection["aggregates"]
    session_counts = await asyncio.to_thread(project_store.session_counts_by_cwd)
    out: list[dict] = []
    for p in await asyncio.to_thread(project_store.list_projects):
        key = (p.get("path") or "", p.get("node_id") or "primary")
        out.append({
            **p,
            **aggs.get(key, empty_aggregate()),
            "session_count": session_counts.get(p.get("path") or "", 0),
        })
    return {
        "projects": out,
        "epoch": projection["epoch"],
        "revision": projection["revision"],
    }


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
