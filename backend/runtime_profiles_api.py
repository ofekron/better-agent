"""Runtime-profile CRUD and activation routes.

Profile activation is the single default-selection surface (the provider-level
set-default endpoint is gone). Depends on the coordinator only through the
broadcast capability bound by the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config_store

router = APIRouter()
logger = logging.getLogger(__name__)

_broadcast_global: Optional[Callable[[str, dict], Any]] = None


def configure(broadcast_global: Callable[[str, dict], Any]) -> None:
    """Bind the coordinator capability this router needs."""
    global _broadcast_global
    _broadcast_global = broadcast_global


def _require_configured() -> Callable[[str, dict], Any]:
    if _broadcast_global is None:
        raise HTTPException(
            status_code=503, detail="runtime profiles API is not configured"
        )
    return _broadcast_global


def _snapshot() -> dict:
    """Non-secret frontend projection: every profile (tombstones included, for
    old-session display), the default profile id, and the provider graveyard
    tombstoned profiles resolve their display names through."""
    return {
        "runtime_profiles": config_store.list_runtime_profiles(include_deleted=True),
        "default_runtime_profile_id": config_store.get_default_runtime_profile_id(),
        "deleted_providers": config_store.list_deleted_providers(),
    }


async def _broadcast_changed() -> None:
    broadcast_global = _require_configured()
    payload = await asyncio.to_thread(_snapshot)
    await broadcast_global("runtime_profiles_changed", payload)


class RuntimeProfileCreate(BaseModel):
    provider_id: str
    runner: str
    name: str = ""
    default_model: str = ""
    default_reasoning_effort: str = ""


class RuntimeProfilePatch(BaseModel):
    name: Optional[str] = None
    default_model: Optional[str] = None
    default_reasoning_effort: Optional[str] = None


@router.get("/api/runtime-profiles")
async def list_runtime_profiles():
    _require_configured()
    return await asyncio.to_thread(_snapshot)


@router.post("/api/runtime-profiles")
async def create_runtime_profile(body: RuntimeProfileCreate):
    _require_configured()
    try:
        profile = await asyncio.to_thread(
            config_store.add_runtime_profile, body.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _broadcast_changed()
    return profile


@router.patch("/api/runtime-profiles/{profile_id}")
async def patch_runtime_profile(profile_id: str, body: RuntimeProfilePatch):
    _require_configured()
    try:
        profile = await asyncio.to_thread(
            config_store.update_runtime_profile,
            profile_id,
            body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if profile is None:
        raise HTTPException(status_code=404, detail="runtime profile not found")
    await _broadcast_changed()
    return profile


@router.delete("/api/runtime-profiles/{profile_id}")
async def delete_runtime_profile(profile_id: str):
    _require_configured()
    deleted, reason = await asyncio.to_thread(
        config_store.delete_runtime_profile, profile_id
    )
    if not deleted:
        if reason == "missing":
            raise HTTPException(status_code=404, detail="runtime profile not found")
        raise HTTPException(status_code=409, detail=reason)
    await _broadcast_changed()
    return {"deleted": True}


@router.post("/api/runtime-profiles/{profile_id}/activate")
async def activate_runtime_profile(profile_id: str):
    _require_configured()
    try:
        profile = await asyncio.to_thread(
            config_store.activate_runtime_profile, profile_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if profile is None:
        raise HTTPException(status_code=404, detail="runtime profile not found")
    await _broadcast_changed()
    return profile
