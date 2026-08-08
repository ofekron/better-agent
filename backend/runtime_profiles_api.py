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
from providers_api import (
    FACT_MODEL_CATALOG_CHANGED,
    FACT_RUNTIME_PROFILES_CHANGED,
    _publish_fact,
)

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


async def _broadcast_changed(*, provider_id: Optional[str] = None) -> None:
    """`provider_id`, when supplied, is the runtime profile's owning
    provider — a runtime-profile mutation is the only thing besides an
    actual model-catalog refresh that changes what `ProviderConfigSurface.
    model_catalog()` returns (it's the sole source `_map_descriptor`'s
    per-model `runner` tag reads — see `backend/adapters/provider_adapter.
    py`'s `model_catalog()`), so D1 decomposes this legacy broadcast onto
    the SAME `model_catalog_changed` fact a real catalog refresh uses —
    still fired ONLY when `provider_id` is given, unchanged.

    Closure 3: additionally fires `FACT_RUNTIME_PROFILES_CHANGED`
    UNCONDITIONALLY (matching this function's own unconditional legacy WS
    broadcast below exactly — including the two call sites,
    `record_last_model`/`record_last_reasoning_effort`, that carry no
    `provider_id` and so never reach the `model_catalog_changed` fact
    above), carrying the SAME `payload` this legacy broadcast already
    computed — no second read."""
    broadcast_global = _require_configured()
    payload = await asyncio.to_thread(_snapshot)
    await broadcast_global("runtime_profiles_changed", payload)
    if provider_id:
        await _publish_fact(FACT_MODEL_CATALOG_CHANGED, {"provider_id": provider_id})
    await _publish_fact(FACT_RUNTIME_PROFILES_CHANGED, payload)


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


async def _create_runtime_profile(payload: dict) -> dict:
    """Core create logic, reused by `POST /api/runtime-profiles` and ADR
    0007's `SaveRuntimeProfile` intent (see `backend/adapter_api.py`'s
    `ProviderCommandPort`) when the profile dict carries no
    `runtime_profile_id` — single source of truth for creation, per
    CLAUDE.md's DRY rule."""
    _require_configured()
    try:
        profile = await asyncio.to_thread(config_store.add_runtime_profile, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _broadcast_changed(provider_id=profile.get("provider_id"))
    return profile


@router.post("/api/runtime-profiles")
async def create_runtime_profile(body: RuntimeProfileCreate):
    return await _create_runtime_profile(body.model_dump(exclude_unset=True))


async def _patch_runtime_profile(profile_id: str, payload: dict) -> dict:
    """Core patch logic, reused by `PATCH /api/runtime-profiles/{id}` and
    ADR 0007's `SaveRuntimeProfile` intent when the profile dict carries a
    `runtime_profile_id`."""
    _require_configured()
    try:
        profile = await asyncio.to_thread(
            config_store.update_runtime_profile, profile_id, payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if profile is None:
        raise HTTPException(status_code=404, detail="runtime profile not found")
    await _broadcast_changed(provider_id=profile.get("provider_id"))
    return profile


@router.patch("/api/runtime-profiles/{profile_id}")
async def patch_runtime_profile(profile_id: str, body: RuntimeProfilePatch):
    return await _patch_runtime_profile(profile_id, body.model_dump(exclude_unset=True))


async def _delete_runtime_profile(profile_id: str) -> None:
    """Core delete logic, reused by `DELETE /api/runtime-profiles/{id}` and
    ADR 0007's `DeleteRuntimeProfile` intent."""
    _require_configured()
    existing = await asyncio.to_thread(config_store.get_runtime_profile, profile_id)
    deleted, reason = await asyncio.to_thread(
        config_store.delete_runtime_profile, profile_id
    )
    if not deleted:
        if reason == "missing":
            raise HTTPException(status_code=404, detail="runtime profile not found")
        raise HTTPException(status_code=409, detail=reason)
    await _broadcast_changed(provider_id=(existing or {}).get("provider_id"))


@router.delete("/api/runtime-profiles/{profile_id}")
async def delete_runtime_profile(profile_id: str):
    await _delete_runtime_profile(profile_id)
    return {"deleted": True}


@router.post("/api/runtime-profiles/{profile_id}/activate")
async def activate_runtime_profile(profile_id: str):
    # gap: ADR 0007's ProviderIntent union has no activate-runtime-profile
    # variant (SaveRuntimeProfile/DeleteRuntimeProfile only) — this route
    # stays legacy-only; see the caller's report.
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
