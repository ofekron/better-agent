"""HTTP surface for the native-transcript domain.

Routes validate their input and delegate to `native_index_manager`.
They never import `native_import` or `native_transcript_index`, and they
never choose where the work runs — the manager owns both.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException

from internal_guards import require_internal
from native_index_manager import manager

router = APIRouter(tags=["native-import"])


def _parse_provider_ids(body: dict) -> Optional[list[str]]:
    provider_ids = body.get("provider_ids") if isinstance(body, dict) else None
    if provider_ids is not None and not isinstance(provider_ids, list):
        raise HTTPException(
            status_code=400,
            detail="provider_ids must be a list of ids or omitted",
        )
    return provider_ids


def _parse_limit(body: dict) -> Optional[int]:
    raw = body.get("limit") if isinstance(body, dict) else None
    if raw is None:
        return None
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer")
    if limit < 0:
        raise HTTPException(status_code=400, detail="limit must be >= 0")
    return limit or None


async def _parse_project_paths(body: dict) -> Optional[list[str]]:
    if isinstance(body, dict) and body.get("all_projects") is True:
        return None
    raw = body.get("project_paths") if isinstance(body, dict) else None
    if raw is None:
        return await manager.loaded_project_paths()
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail="project_paths must be a list of paths or omitted",
        )
    return [str(p) for p in raw if isinstance(p, str) and p]


async def _start(body: dict) -> dict:
    return await manager.start_import(
        _parse_provider_ids(body),
        _parse_limit(body),
        await _parse_project_paths(body),
    )


@router.post("/api/native-import")
async def start_native_import(body: dict = Body(...)):
    """Start a background job that ingests every native CLI session of the
    given providers (all configured providers when `provider_ids` is
    omitted) into Better Agent sessions. Single-flight. Returns current job
    status. `limit` caps the number of NEW sessions imported."""
    return await _start(body)


@router.get("/api/native-import/status")
async def native_import_status():
    return await manager.import_status()


@router.get("/api/native-import/summary")
async def native_import_summary(
    provider_ids: Optional[str] = None,
    all_projects: bool = False,
):
    ids = provider_ids.split(",") if provider_ids else None
    return await manager.import_summary(ids, all_projects)


@router.post("/api/internal/native-import")
async def internal_start_native_import(
    body: dict = Body(...),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Internal-token-gated trigger for the native import. Runs the import
    INSIDE the backend process, which is the only safe way when the backend
    is live — a separate process writing session.json races the backend's
    in-memory cache (it re-persists and clobbers the render tree). Used by
    the CLI/import scripts."""
    require_internal()
    return await _start(body)


@router.get("/api/internal/native-import/status")
async def internal_native_import_status(
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_internal()
    return await manager.import_status()
