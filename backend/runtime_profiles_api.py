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
import user_prefs

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
    old-session display), the default profile id, the provider graveyard
    tombstoned profiles resolve their display names through, and the
    per-profile last-used prefill maps."""
    profiles = config_store.list_runtime_profiles(include_deleted=True)
    known = {p["id"] for p in profiles}
    last_models = {
        k: v for k, v in user_prefs.get_last_models().items() if k in known
    }
    last_efforts = {
        k: v
        for k, v in user_prefs.get_last_reasoning_efforts().items()
        if k in known
    }
    return {
        "runtime_profiles": profiles,
        "default_runtime_profile_id": config_store.get_default_runtime_profile_id(),
        "deleted_providers": config_store.list_deleted_providers(),
        "last_models": last_models,
        "last_reasoning_efforts": last_efforts,
    }


async def _broadcast_changed() -> None:
    broadcast_global = _require_configured()
    payload = await asyncio.to_thread(_snapshot)
    await broadcast_global("runtime_profiles_changed", payload)


def runtime_profile_id_for_session(session: dict) -> Optional[str]:
    """Resolve the live profile behind a session's stamped fields.

    Pre-B1 sessions carry no runtime_profile_id; the (provider, runner)
    identity pins the profile when one exists."""
    provider_id = str(session.get("provider_id") or "")
    if not provider_id:
        return None
    return config_store.provider_execution_defaults(
        provider_id, session.get("runner")
    )["runtime_profile_id"]


async def record_last_model(
    runtime_profile_id: str | None, model: str | None
) -> None:
    """Remember the model last used with a runtime profile so pickers can
    pre-choose it. Broadcasts only on an actual change so the prefs write
    doesn't spam refetches."""
    if not runtime_profile_id or not model:
        return
    changed = await asyncio.to_thread(
        user_prefs.set_last_model, runtime_profile_id, model
    )
    if changed:
        await _broadcast_changed()


async def record_last_reasoning_effort(
    runtime_profile_id: str | None, reasoning_effort: str | None
) -> None:
    if not runtime_profile_id or not reasoning_effort:
        return
    changed = await asyncio.to_thread(
        user_prefs.set_last_reasoning_effort,
        runtime_profile_id,
        reasoning_effort,
    )
    if changed:
        await _broadcast_changed()


async def model_for_profile_switch(
    provider_id: str, provider_record: dict, runner: object = None
) -> str:
    """Prefill chain when switching without an explicit model: the profile's
    last-used model, then its default model, then the first cached active
    model that validates. Never leaves the old selection attached."""
    from fastapi import HTTPException as _HTTPException

    import models as models_mod
    from provider_validation import validate_provider_model

    defaults = await asyncio.to_thread(
        config_store.provider_execution_defaults, provider_id, runner
    )
    last_models = await asyncio.to_thread(user_prefs.get_last_models)
    candidates: list[str] = []
    profile_id = defaults["runtime_profile_id"]
    for value in (
        last_models.get(profile_id) if profile_id else None,
        defaults["default_model"],
    ):
        model = str(value or "").strip()
        if model and model not in candidates:
            candidates.append(model)
    try:
        available = await asyncio.to_thread(models_mod.available_models, provider_id)
    except Exception:
        available = []
    for value in available:
        model = str(value or "").strip()
        if model and model not in candidates:
            candidates.append(model)

    for model in candidates:
        try:
            await asyncio.to_thread(validate_provider_model, provider_id, model, True)
            return model
        except _HTTPException:
            continue

    name = provider_record.get("name") or provider_id
    raise HTTPException(
        status_code=400,
        detail=f"{name} has no known models; cannot switch without a model",
    )


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
