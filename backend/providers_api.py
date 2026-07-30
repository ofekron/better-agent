"""Provider CRUD, model catalogs, and provider-setup install routes.

Depends on the coordinator only through the broadcast capability it
actually needs, bound by the composition root (see `configure`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import config_store
import provider_auth
import provider_setup
import runtime_profile
from i18n import t
from provider_validation import (
    is_loopback_request,
    provider_auth_result_response,
    provider_not_suspended,
    validate_provider_default_reasoning_effort,
    validate_provider_model,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_broadcast_global: Optional[Callable[[str, dict], Any]] = None


def configure(broadcast_global: Callable[[str, dict], Any]) -> None:
    """Bind the coordinator capability this router needs."""
    global _broadcast_global
    _broadcast_global = broadcast_global
    provider_auth.configure_config_change_broadcast(_broadcast_provider_changed)


def _require_configured() -> Callable[[str, dict], Any]:
    if _broadcast_global is None:
        raise HTTPException(status_code=503, detail="providers API is not configured")
    return _broadcast_global


class ProviderPayload(BaseModel):
    name: str = ""
    nickname: str = ""
    kind: str = "claude"  # provider kind — selects the Provider impl
    mode: str = "subscription"  # "subscription" | "api_key"
    api_key: str = ""
    base_url: str = ""
    config_dir: str = ""
    custom_models: list[str] = []
    default_model: str = ""
    runner: str = ""
    default_reasoning_effort: str = ""
    default_permission: dict = {}
    capabilities: dict[str, bool] | None = None
    suspended: bool = False


class ProviderPatch(BaseModel):
    """All fields optional — only the supplied ones are written."""
    name: str | None = None
    nickname: str | None = None
    kind: str | None = None
    mode: str | None = None
    api_key: str | None = None  # "__keep__" preserves the existing key
    base_url: str | None = None
    config_dir: str | None = None
    custom_models: list[str] | None = None
    default_model: str | None = None
    runner: str | None = None
    default_reasoning_effort: str | None = None
    default_permission: dict | None = None
    capabilities: dict[str, bool] | None = None
    suspended: bool | None = None
    expected_generation: str
    expected_revision: int


class ProviderAuthorityPayload(BaseModel):
    expected_generation: str
    expected_revision: int


class ProviderSuspensionPayload(ProviderAuthorityPayload):
    suspended: bool = True


class ProviderSetupInstallPayload(BaseModel):
    kind: str


async def _broadcast_provider_changed():
    broadcast_global = _require_configured()
    state = await asyncio.to_thread(config_store.list_provider_ui_state)
    state = await asyncio.to_thread(_with_login_states, state)
    await broadcast_global(
        "provider_changed",
        state,
    )


def _with_login_states(state: dict) -> dict:
    """Attach per-record OAuth login/logout flow state so the settings UI
    can reflect login progress without a second WS channel."""
    providers = state.get("providers")
    if not isinstance(providers, list):
        return state
    state["providers"] = [provider_auth.attach_login_state(p) for p in providers]
    return state


async def _broadcast_install(event_type: str, data: dict) -> None:
    """Fan-out for streaming provider-CLI installs. provider_setup calls
    this per stdout/stderr line and on completion."""
    broadcast_global = _require_configured()
    await broadcast_global(event_type, data)


async def broadcast_model_catalog_fact(fact) -> None:
    broadcast_global = _require_configured()
    await broadcast_global(
        "models_catalog_changed",
        {
            "provider_id": fact.provider_id,
            "kind": fact.kind,
            "idempotency_key": fact.idempotency_key,
            "projection": (
                fact.projection.to_dict()
                if fact.projection is not None
                else None
            ),
        },
    )


async def _broadcast_models_catalog_changed(provider_id: str, diff: dict) -> None:
    """Per-provider catalog delta. Four disjoint transition sets:
    newly_added / became_active / went_retired / truly_removed.
    Frontend `useModelsCatalogChanged` refetches `/api/models` on receipt."""
    broadcast_global = _require_configured()
    await broadcast_global(
        "models_catalog_changed",
        {
            "provider_id": provider_id,
            "newly_added": diff.get("newly_added", []),
            "became_active": diff.get("became_active", []),
            "went_retired": diff.get("went_retired", []),
            "truly_removed": diff.get("truly_removed", []),
        },
    )


def provider_models_catalog(provider_id: str) -> dict:
    import models as models_mod
    catalog = models_mod.models_catalog(provider_id)
    record = config_store.get_provider(provider_id) or {}
    catalog["runtime_profiles"] = [
        {
            "runner": runner,
            "model": model,
            "reasoning_efforts": list(runtime_profile.reasoning_efforts(
                record, runner, model=model,
            )),
        }
        for runner in runtime_profile.supported_runners(record)
        for model in catalog.get("models", [])
    ]
    return catalog


async def _refresh_provider_models(record: dict) -> dict | None:
    if record.get("kind") == "codex":
        import model_catalog_refresh

        model_catalog_refresh.request_refresh_background(str(record["id"]))
        return None
    if record.get("kind") == "fugu":
        # Curated catalog (FUGU_MODELS) — nothing external to refresh.
        return None
    import models as models_mod

    return await models_mod.refresh_one(str(record["id"]))


@router.get("/api/startup_tasks")
async def get_startup_tasks():
    """Snapshot of in-flight + recent-history backend startup tasks.
    Frontend banner reads this on mount for first paint, then
    subscribes to `startup_task_changed` WS events for live deltas.
    Authoritative state lives in `startup_task_registry` (in-memory)."""
    from startup_tasks import startup_task_registry
    return startup_task_registry.list()


@router.get("/api/providers")
async def get_providers():
    state = await asyncio.to_thread(config_store.list_provider_ui_state)
    state = _with_login_states(state)
    for record in state.get("providers", []):
        # Lazily resolve the durable auth-status for subscription providers
        # whose cached value is missing (e.g. right after a restart) so the
        # UI can show Log out for an account signed in via the CLI.
        provider_auth.maybe_schedule_refresh(record.get("id") or "", _broadcast_provider_changed)
    return state


@router.post("/api/providers")
async def create_provider(payload: ProviderPayload):
    body = payload.model_dump()
    body["default_reasoning_effort"] = validate_provider_default_reasoning_effort(
        body, body.get("default_reasoning_effort"),
    )
    try:
        record = await asyncio.to_thread(config_store.add_provider, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _broadcast_provider_changed()
    return record


@router.patch("/api/providers/{provider_id}")
async def patch_provider(provider_id: str, payload: ProviderPatch):
    body = {k: v for k, v in payload.model_dump().items() if v is not None}
    expected_generation = body.pop("expected_generation")
    expected_revision = body.pop("expected_revision")
    if "default_reasoning_effort" in body:
        current = await asyncio.to_thread(config_store.get_provider, provider_id)
        if current is None:
            raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
        candidate = dict(current)
        candidate.update(body)
        body["default_reasoning_effort"] = validate_provider_default_reasoning_effort(
            candidate, body.get("default_reasoning_effort"),
        )
    try:
        record = await asyncio.to_thread(
            config_store.update_provider,
            provider_id,
            body,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
        )
    except config_store.ProviderConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if body.get("suspended") is True:
        try:
            from provider import cancel_provider_runs
            cancelled = await asyncio.to_thread(cancel_provider_runs, provider_id)
            if cancelled:
                logger.info("provider %s suspended; cancelled %d run(s)", provider_id, cancelled)
        except Exception:
            logger.exception("failed to cancel runs for suspended provider %s", provider_id)
    await _broadcast_provider_changed()
    return record


@router.post("/api/providers/{provider_id}/suspended")
async def set_provider_suspended(
    provider_id: str,
    body: ProviderSuspensionPayload,
):
    suspended = body.suspended
    try:
        state = await asyncio.to_thread(
            config_store.set_provider_suspended,
            provider_id,
            suspended,
            expected_generation=body.expected_generation,
            expected_revision=body.expected_revision,
        )
    except config_store.ProviderConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if state is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    cancelled = 0
    if suspended:
        try:
            from provider import cancel_provider_runs
            cancelled = await asyncio.to_thread(cancel_provider_runs, provider_id)
        except Exception:
            logger.exception("failed to cancel runs for suspended provider %s", provider_id)
    await _broadcast_provider_changed()
    return {"suspended": suspended, "cancelled_runs": cancelled, **state}


@router.post("/api/providers/{provider_id}/credential/retry")
async def retry_provider_credential(provider_id: str):
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if provider.get("mode") != "api_key":
        raise HTTPException(status_code=409, detail="Provider does not use an API key.")
    status = await asyncio.to_thread(config_store.retry_provider_credential, provider_id)
    await _broadcast_provider_changed()
    return {"credential_status": status, "has_api_key": status == "available"}


@router.post("/api/providers/{provider_id}/login")
async def login_provider(request: Request, provider_id: str):
    """Spawn the provider's own OAuth login against this record's isolated
    credential dir (desktop/loopback only — the CLI opens the OS browser).
    Returns immediately; state transitions arrive via `provider_changed`."""
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="OAuth login is available only from a loopback session.")
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if not provider_auth.supports_auth(provider):
        raise HTTPException(status_code=409, detail="Provider does not support OAuth login.")
    result = await provider_auth.start_login(provider_id, _broadcast_provider_changed)
    return provider_auth_result_response(result)


@router.post("/api/providers/{provider_id}/login/cancel")
async def cancel_provider_login(request: Request, provider_id: str):
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="OAuth login is available only from a loopback session.")
    killed = await provider_auth.cancel(provider_id)
    return {"cancelled": killed}


@router.post("/api/providers/{provider_id}/logout")
async def logout_provider(request: Request, provider_id: str):
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="OAuth logout is available only from a loopback session.")
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if not provider_auth.supports_auth(provider):
        raise HTTPException(status_code=409, detail="Provider does not support OAuth login.")
    result = await provider_auth.start_logout(provider_id, _broadcast_provider_changed)
    return provider_auth_result_response(result)


@router.delete("/api/providers/{provider_id}")
async def remove_provider(
    provider_id: str,
    authority: ProviderAuthorityPayload,
):
    try:
        deleted, reason = await asyncio.to_thread(
            config_store.delete_provider,
            provider_id,
            expected_generation=authority.expected_generation,
            expected_revision=authority.expected_revision,
        )
    except config_store.ProviderConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if deleted:
        # Drop transient auth state so a deleted record leaves no
        # in-flight/registry entry (and none survives id reuse).
        provider_auth.clear_state(provider_id)
    if not deleted:
        if reason == "missing":
            raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
        if reason == "default":
            raise HTTPException(
                status_code=409,
                detail=t("error.cannot_delete_default_provider"),
            )
        raise HTTPException(status_code=400, detail=reason)
    await _broadcast_provider_changed()
    return {"deleted": True}



@router.post("/api/providers/default/custom_models")
async def add_custom_model(body: dict):
    """Append a custom model to the currently-active provider. Used by the
    ModelSelector's '+ custom' button so the frontend doesn't need to know
    which provider is active locally."""
    name = (body or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail=t("error.name_required"))
    record = await asyncio.to_thread(config_store.add_custom_model_to_default, name)
    if record is None:
        raise HTTPException(status_code=400, detail=t("error.no_default_provider"))
    await _broadcast_provider_changed()
    return record


@router.get("/api/models")
async def get_models():
    """Projection/cache-only read. NEVER makes a provider I/O call."""
    import models as models_mod
    return models_mod.models_catalog()


@router.get("/api/model-catalogs")
async def get_provider_model_catalogs():
    state = await asyncio.to_thread(config_store.list_providers)
    records = [
        provider
        for provider in state.get("providers", [])
        if provider.get("kind") in {"codex", "fugu"}
        and provider.get("suspended") is not True
    ]
    return {
        "catalogs": await asyncio.gather(
            *(
                asyncio.to_thread(provider_models_catalog, provider["id"])
                for provider in records
            ),
        ),
    }


@router.post("/api/models/refresh", status_code=202)
async def refresh_active_models_endpoint():
    """Refresh the active provider through its authoritative catalog owner."""
    active = await asyncio.to_thread(config_store.get_default_provider)
    if active is None:
        raise HTTPException(status_code=400, detail=t("error.no_default_provider"))
    diff = await _refresh_provider_models(active)
    if diff:
        await _broadcast_models_catalog_changed(active["id"], diff)
    catalog = provider_models_catalog(active["id"])
    return {
        "accepted": True,
        "provider_id": active["id"],
        "provider_generation": str(active.get("generation") or ""),
        "catalog": catalog,
    }


@router.post("/api/providers/{provider_id}/models/refresh", status_code=202)
async def refresh_provider_models_endpoint(provider_id: str):
    """Refresh one provider through its authoritative catalog owner."""
    record = await asyncio.to_thread(config_store.get_provider, provider_id)
    if record is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if record.get("suspended") is True:
        raise HTTPException(status_code=409, detail=t("error.provider_suspended"))
    diff = await _refresh_provider_models(record)
    if diff:
        await _broadcast_models_catalog_changed(provider_id, diff)
    return {
        "accepted": True,
        "provider_id": provider_id,
        "provider_generation": str(record.get("generation") or ""),
        "catalog": provider_models_catalog(provider_id),
    }


@router.get("/api/providers/{provider_id}/models")
async def get_provider_models(provider_id: str):
    """Models for a specific provider — used by the ProviderForm dropdown
    so the user can pick a default_model without activating the provider
    first. Projection/cache-only read; it performs no provider I/O."""
    record = await asyncio.to_thread(config_store.get_provider, provider_id)
    if record is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    return provider_models_catalog(provider_id)


@router.get("/api/provider-setup/status")
async def get_provider_setup_status():
    results = await asyncio.gather(
        *[
            provider_setup.provider_setup_status(kind)
            for kind in provider_setup.supported_provider_kinds()
        ]
    )
    return {"providers": results}


@router.get("/api/provider-setup/installs")
async def get_provider_setup_installs():
    """Snapshot of in-memory install runs for first paint; live deltas
    arrive via `provider_install_progress` / `provider_install_finished`
    WS events."""
    return {"runs": provider_setup.get_install_runs()}


@router.post("/api/provider-setup/install")
async def install_provider_setup(payload: ProviderSetupInstallPayload):
    try:
        return await provider_setup.start_install(payload.kind, _broadcast_install)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
