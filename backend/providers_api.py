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

import background_work
import config_store
import provider_auth
import provider_setup
import runtime_profile
from background_work import background_work_registry
from event_bus import BusEvent, bus, register_event_schema
from i18n import t
from provider_validation import (
    is_loopback_request,
    provider_auth_result_response,
    validate_provider_default_reasoning_effort,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_broadcast_global: Optional[Callable[[str, dict], Any]] = None

# ---- D1: per-concern bus facts, decomposed from the flat provider_changed
# broadcast below. backend/adapters/provider_adapter.py's bind() subscribes
# to these to push typed ADR 0007 frames without polling. Each fact carries
# only a provider_id — the adapter re-derives its own read model from
# store_access (durable state) on receipt — EXCEPT FACT_LOGIN_FLOW_CHANGED,
# whose source (provider_auth's transient in-flight flow state) has no
# store_access equivalent, so it carries the full projected payload
# directly (the same shape backend/adapters/session_adapter.py's bus-only
# signals — e.g. `session.fire.*`'s "deleted"/"running_changed" — already
# use for facts with no durable-store counterpart).
FACT_PROVIDER_MUTATED = "provider.mutated"
FACT_CREDENTIAL_STATE_CHANGED = "provider.credential_state_changed"
FACT_MODEL_CATALOG_CHANGED = "provider.model_catalog_changed"
FACT_LOGIN_FLOW_CHANGED = "provider.login_flow_changed"
# Install-progress v2 (closure 2): mirrors FACT_LOGIN_FLOW_CHANGED's own
# rationale — `provider_setup._INSTALL_RUNS` is transient, in-memory,
# store_access-unreachable state (same shape of gap E2/E3 document for
# turn_manager's `_run_state`), so this fact also carries its full
# projected payload directly rather than a provider_id the adapter would
# re-derive. See `_broadcast_install`.
FACT_INSTALL_PROGRESS_CHANGED = "provider.install_progress_changed"
# Closure 3 (RuntimeProfile v2 parity): fired unconditionally by
# runtime_profiles_api._broadcast_changed, carrying the SAME full
# `_snapshot()` payload its legacy `runtime_profiles_changed` WS broadcast
# already sends — see that module for the fact's producer.
FACT_RUNTIME_PROFILES_CHANGED = "provider.runtime_profiles_changed"

for _fact_type in (
    FACT_PROVIDER_MUTATED,
    FACT_CREDENTIAL_STATE_CHANGED,
    FACT_MODEL_CATALOG_CHANGED,
    FACT_LOGIN_FLOW_CHANGED,
    FACT_INSTALL_PROGRESS_CHANGED,
    FACT_RUNTIME_PROFILES_CHANGED,
):
    register_event_schema(_fact_type, 1)
del _fact_type


async def _publish_fact(fact_type: str, payload: dict) -> None:
    await bus.publish(
        BusEvent(type=fact_type, root_id="", sid="", payload=payload, persist=False)
    )


# Legacy provider_auth login/logout states collapse onto ADR 0007's
# LoginPhase vocabulary. provider_auth exposes no browser/code-prompt
# sub-phase (the CLI subprocess is opaque about its own OAuth exchange), so
# both *_running states map to "starting" — awaiting_browser/awaiting_code/
# polling stay unreachable from this source (documented gap; see the
# caller's report).
_LOGIN_STATUS_TO_PHASE = {
    provider_auth.STATE_LOGIN_RUNNING: "starting",
    provider_auth.STATE_LOGOUT_RUNNING: "starting",
    provider_auth.STATE_LOGIN_SUCCESS: "completed",
    provider_auth.STATE_LOGGED_OUT: "completed",
    provider_auth.STATE_LOGIN_FAILED: "failed",
    provider_auth.STATE_LOGOUT_FAILED: "failed",
}

# provider_id -> intent_id of the BeginLogin/CancelLogin intent that most
# recently started a flow for it. "" for flows started off the legacy REST
# route (no intent framework involved) — LoginFlowState.intent_id is a
# required str field, so "" is the honest "no intent" value, not a
# fabricated one.
_login_intent_by_provider: dict[str, str] = {}


def _login_flow_fact_payload(provider_id: str) -> Optional[dict]:
    state = provider_auth.login_state(provider_id)
    phase = _LOGIN_STATUS_TO_PHASE.get(state.get("status"))
    if phase is None:
        return None
    return {
        "provider_id": provider_id,
        "intent_id": _login_intent_by_provider.get(provider_id, ""),
        "phase": phase,
        # gap: provider_auth's CLI-subprocess login has no verification
        # URL/user-code signal to surface here — always None.
        "data": None,
    }


# D1 decomposition state: last-observed per-provider projection slice, used
# to turn the flat, always-fires `_broadcast_provider_changed` call (every
# mutation route below, plus periodic auth-status refreshes) into
# change-only facts. Session-scoped (module-level, not persisted) — mirrors
# the "diff against last known" idiom `backend/adapters/session_adapter.py`'s
# `_last_summary` cache already uses for the same purpose.
_last_provider_projection: dict[str, dict] = {}


def _provider_fact_slice(record: dict) -> dict:
    login = record.get("login_state") or {}
    return {
        "generation": record.get("generation"),
        "revision": record.get("revision"),
        "suspended": record.get("suspended"),
        "credential_status": record.get("credential_status"),
        "login_status": login.get("status"),
        "login_authenticated": login.get("authenticated"),
    }


async def _publish_provider_facts(state: dict) -> None:
    """D1: decompose the flat provider_changed broadcast into per-concern
    facts. Called from every existing `_broadcast_provider_changed` call
    site with the SAME state snapshot that call already computed (no extra
    I/O). Diffs each provider's slice against the last-observed one and
    fires only the facts whose underlying concern actually changed."""
    seen: set[str] = set()
    for record in state.get("providers", []):
        provider_id = record.get("id")
        if not provider_id:
            continue
        seen.add(provider_id)
        current = _provider_fact_slice(record)
        previous = _last_provider_projection.get(provider_id)
        if previous == current:
            continue
        _last_provider_projection[provider_id] = current
        structural = previous is None or (
            previous["generation"] != current["generation"]
            or previous["revision"] != current["revision"]
        )
        credential = previous is None or (
            previous["suspended"] != current["suspended"]
            or previous["credential_status"] != current["credential_status"]
        )
        login = previous is None or (
            previous["login_status"] != current["login_status"]
            or previous["login_authenticated"] != current["login_authenticated"]
        )
        if structural:
            await _publish_fact(FACT_PROVIDER_MUTATED, {"provider_id": provider_id})
        if credential:
            await _publish_fact(FACT_CREDENTIAL_STATE_CHANGED, {"provider_id": provider_id})
        if login:
            payload = _login_flow_fact_payload(provider_id)
            if payload is not None:
                await _publish_fact(FACT_LOGIN_FLOW_CHANGED, payload)
    removed = set(_last_provider_projection) - seen
    for provider_id in removed:
        _last_provider_projection.pop(provider_id, None)
        # gap: ADR 0007's ProviderFrame union has no removal/tombstone
        # variant (mirrors SessionRemoved's absent ProviderRemoved sibling
        # in ADR 0008) — still fire provider.mutated so a subscriber
        # tracking per-provider state at least learns something changed;
        # provider_adapter.py's re-derivation finds no record and stays
        # silent (documented in that module).
        await _publish_fact(FACT_PROVIDER_MUTATED, {"provider_id": provider_id})


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
    await _publish_provider_facts(state)


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
    this per stdout/stderr line and on completion.

    D1-style decomposition (closure 2): alongside the legacy
    `provider_install_progress`/`provider_install_finished` WS broadcast,
    also publish `FACT_INSTALL_PROGRESS_CHANGED` carrying the CURRENT full
    run snapshot (`provider_setup.get_install_runs()` — the same shape
    `GET /api/provider-setup/installs` already returns for cold-start),
    not just this event's own partial delta (a bare `{"kind","phase"}`
    ping or a single `{"kind","stream","text"}` line) — so
    `provider_adapter.py`'s subscriber never needs to accumulate lines
    itself; each frame is a complete, self-consistent read of the run.
    `data["kind"]` is present on every one of provider_setup.py's three
    call sites (started/line/finished).

    The per-line events also stay as they are — the Settings page renders
    the full installer log from them. Alongside both of those, the run is
    projected into the background work registry so an install started from
    Settings stays visible after the user navigates away (`_project_install_
    work` below) — this is a third, independent consumer of the same
    started/line/finished sequence, not a replacement for the other two."""
    broadcast_global = _require_configured()
    _project_install_work(event_type, data)
    await broadcast_global(event_type, data)
    kind = data.get("kind")
    if isinstance(kind, str) and kind:
        run = provider_setup.get_install_runs().get(kind)
        if run is not None:
            await _publish_fact(FACT_INSTALL_PROGRESS_CHANGED, run)


def _project_install_work(event_type: str, data: dict) -> None:
    kind = str(data.get("kind") or "")
    if not kind:
        return
    local_id = f"install:{kind}"
    if event_type == "provider_install_progress":
        phase = data.get("phase")
        text = data.get("text")
        if phase == "started":
            background_work_registry.report(
                owner_kind=background_work.OWNER_CORE,
                owner_id="provider_setup",
                local_id=local_id,
                label=kind,
                title_key="backgroundWork.providerInstall",
                title_params={"provider": kind},
            )
            return
        # Installer output has no total to count against, so the row stays
        # indeterminate and shows the latest line as its phase.
        if text:
            background_work_registry.update(
                f"{background_work.OWNER_CORE}:provider_setup:{local_id}",
                phase=str(text).strip(),
            )
        return
    if event_type == "provider_install_finished":
        failed = data.get("state") == "failed" or data.get("returncode") not in (0, None)
        background_work_registry.finish(
            f"{background_work.OWNER_CORE}:provider_setup:{local_id}",
            status=(
                background_work.STATUS_FAILED if failed
                else background_work.STATUS_SUCCEEDED
            ),
            error=str(data.get("message") or "") or None if failed else None,
        )


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
    await _publish_fact(FACT_MODEL_CATALOG_CHANGED, {"provider_id": fact.provider_id})


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
    await _publish_fact(FACT_MODEL_CATALOG_CHANGED, {"provider_id": provider_id})


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


async def _create_provider(body: dict) -> dict:
    """Core create logic, reused by the `POST /api/providers` route and
    ADR 0007's `CreateProvider` intent (see `backend/adapter_api.py`'s
    `ProviderCommandPort`) — single source of truth for provider creation,
    per CLAUDE.md's DRY rule."""
    body = dict(body)
    body["default_reasoning_effort"] = validate_provider_default_reasoning_effort(
        body, body.get("default_reasoning_effort"),
    )
    record = await asyncio.to_thread(config_store.add_provider, body)
    await _broadcast_provider_changed()
    return record


@router.post("/api/providers")
async def create_provider(payload: ProviderPayload):
    try:
        return await _create_provider(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _patch_provider(
    provider_id: str,
    body: dict,
    *,
    expected_generation: str | None,
    expected_revision: int | None,
) -> dict:
    """Core patch logic, reused by `PATCH /api/providers/{id}` and ADR
    0007's `UpdateProvider` intent. `expected_generation`/`expected_revision`
    both `None` skips the optimistic-concurrency check entirely (a mode
    `config_store.update_provider` already supports natively) — the intent
    path uses this since `UpdateProvider`'s `config_patch` dict is the only
    place a caller could supply them, and the ADR doesn't mandate it."""
    body = dict(body)
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


@router.patch("/api/providers/{provider_id}")
async def patch_provider(provider_id: str, payload: ProviderPatch):
    body = {k: v for k, v in payload.model_dump().items() if v is not None}
    expected_generation = body.pop("expected_generation")
    expected_revision = body.pop("expected_revision")
    return await _patch_provider(
        provider_id, body,
        expected_generation=expected_generation,
        expected_revision=expected_revision,
    )


async def _set_provider_suspended(
    provider_id: str,
    suspended: bool,
    *,
    expected_generation: str | None,
    expected_revision: int | None,
) -> dict:
    """Core suspend/resume logic, reused by `POST .../suspended` and ADR
    0007's `SuspendProvider` intent."""
    try:
        state = await asyncio.to_thread(
            config_store.set_provider_suspended,
            provider_id,
            suspended,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
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


@router.post("/api/providers/{provider_id}/suspended")
async def set_provider_suspended(
    provider_id: str,
    body: ProviderSuspensionPayload,
):
    return await _set_provider_suspended(
        provider_id, body.suspended,
        expected_generation=body.expected_generation,
        expected_revision=body.expected_revision,
    )


async def _retry_provider_credential(provider_id: str) -> dict:
    """Core credential-retry logic, reused by `POST .../credential/retry`
    and ADR 0007's `RetryCredential` intent."""
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if provider.get("mode") != "api_key":
        raise HTTPException(status_code=409, detail="Provider does not use an API key.")
    status = await asyncio.to_thread(config_store.retry_provider_credential, provider_id)
    await _broadcast_provider_changed()
    return {"credential_status": status, "has_api_key": status == "available"}


@router.post("/api/providers/{provider_id}/credential/retry")
async def retry_provider_credential(provider_id: str):
    return await _retry_provider_credential(provider_id)


async def _login_provider_unchecked(provider_id: str, *, intent_id: str = "") -> dict:
    """Login-start logic minus the loopback gate — the ONE thing every
    caller must check before reaching this, but each does so against its
    own transport-native request object (`fastapi.Request` for REST,
    `starlette.WebSocket` for the v2 intent path — see
    `backend/adapter_api.py`'s `_handle_intent`, which gates `begin_login`/
    `cancel_login` before ever calling `ProviderConfigSurfaceAdapter.
    submit()`, since by the time a command-port coroutine like
    `ProviderCommandPort.begin_login` runs there is no request/websocket
    object left in scope to check). Not exported as a route itself —
    `_login_provider` below is the checked, REST-facing entry point."""
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if not provider_auth.supports_auth(provider):
        raise HTTPException(status_code=409, detail="Provider does not support OAuth login.")
    _login_intent_by_provider[provider_id] = intent_id
    result = await provider_auth.start_login(provider_id, _broadcast_provider_changed)
    return provider_auth_result_response(result)


async def _login_provider(client: object, provider_id: str, *, intent_id: str = "") -> dict:
    """Core login-start logic, reused by `POST .../login` and ADR 0007's
    `BeginLogin` intent. `client` only needs a `.client.host` attribute
    (`is_loopback_request` reads nothing else) — both `fastapi.Request` and
    `starlette.WebSocket` satisfy that, so the v2 WS transport passes its
    `websocket` straight through, preserving the same desktop/loopback-only
    gate the legacy REST route enforces."""
    if not is_loopback_request(client):
        raise HTTPException(status_code=403, detail="OAuth login is available only from a loopback session.")
    return await _login_provider_unchecked(provider_id, intent_id=intent_id)


@router.post("/api/providers/{provider_id}/login")
async def login_provider(request: Request, provider_id: str):
    """Spawn the provider's own OAuth login against this record's isolated
    credential dir (desktop/loopback only — the CLI opens the OS browser).
    Returns immediately; state transitions arrive via `provider_changed`."""
    return await _login_provider(request, provider_id)


async def _cancel_provider_login_unchecked(provider_id: str, *, intent_id: str = "") -> dict:
    """Login-cancel logic minus the loopback gate — see
    `_login_provider_unchecked`'s docstring for why this split exists."""
    if intent_id:
        _login_intent_by_provider[provider_id] = intent_id
    killed = await provider_auth.cancel(provider_id)
    if killed:
        # Explicit-cancel is the one moment this port KNOWS the user
        # cancelled — provider_auth itself has no "cancelled" status (the
        # killed subprocess otherwise resolves through `_monitor`'s own
        # completion path, usually login_failed), so fire the honest
        # cancelled phase directly here rather than waiting for (and
        # possibly racing) that generic failure.
        await _publish_fact(FACT_LOGIN_FLOW_CHANGED, {
            "provider_id": provider_id,
            "intent_id": _login_intent_by_provider.get(provider_id, ""),
            "phase": "cancelled",
            "data": None,
        })
    return {"cancelled": killed}


async def _cancel_provider_login(client: object, provider_id: str, *, intent_id: str = "") -> dict:
    """Core login-cancel logic, reused by `POST .../login/cancel` and ADR
    0007's `CancelLogin` intent."""
    if not is_loopback_request(client):
        raise HTTPException(status_code=403, detail="OAuth login is available only from a loopback session.")
    return await _cancel_provider_login_unchecked(provider_id, intent_id=intent_id)


@router.post("/api/providers/{provider_id}/login/cancel")
async def cancel_provider_login(request: Request, provider_id: str):
    return await _cancel_provider_login(request, provider_id)


@router.post("/api/providers/{provider_id}/logout")
async def logout_provider(request: Request, provider_id: str):
    # No ADR 0007 intent covers logout (its `ProviderIntent` union has no
    # `Logout*` variant — see the caller's report) — legacy-route-only.
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="OAuth logout is available only from a loopback session.")
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=t("error.provider_not_found"))
    if not provider_auth.supports_auth(provider):
        raise HTTPException(status_code=409, detail="Provider does not support OAuth login.")
    result = await provider_auth.start_logout(provider_id, _broadcast_provider_changed)
    return provider_auth_result_response(result)


async def _delete_provider(
    provider_id: str,
    *,
    expected_generation: str | None,
    expected_revision: int | None,
) -> None:
    """Core delete logic, reused by `DELETE /api/providers/{id}` and ADR
    0007's `DeleteProvider` intent. `DeleteProvider` carries no concurrency
    token at all (no `config` dict to smuggle one in, unlike
    `UpdateProvider`) — `None`/`None` (skip-check) is the only option the
    intent shape allows, same rationale as `_patch_provider`'s fallback."""
    try:
        deleted, reason = await asyncio.to_thread(
            config_store.delete_provider,
            provider_id,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
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


@router.delete("/api/providers/{provider_id}")
async def remove_provider(
    provider_id: str,
    authority: ProviderAuthorityPayload,
):
    await _delete_provider(
        provider_id,
        expected_generation=authority.expected_generation,
        expected_revision=authority.expected_revision,
    )
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


async def _refresh_provider_models_by_id(provider_id: str) -> dict:
    """Core one-provider refresh logic, reused by `POST .../models/refresh`
    and ADR 0007's `RefreshModels` intent."""
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


@router.post("/api/providers/{provider_id}/models/refresh", status_code=202)
async def refresh_provider_models_endpoint(provider_id: str):
    """Refresh one provider through its authoritative catalog owner."""
    return await _refresh_provider_models_by_id(provider_id)


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
