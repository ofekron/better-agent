"""Concrete ProviderConfigSurface implementation (ADR 0007).

Reads exclusively through `backend.adapters.store_access.store_access`.
Every method on this surface returns a plain value (never a
`ProjectionResult`) per the ABC — ADR 0007's "settings render entirely
from descriptors" model has no rebuilding/stale-cursor state to project.

Every field this adapter cannot honestly derive from
`StoreAccess`'s current surface is called out inline as a `# gap:`
comment rather than guessed at (CLAUDE.md: "never invent"); the caller's
report enumerates them.
"""

from __future__ import annotations

from backend.adapters.store_access import ProviderRecord, store_access
from backend.surface_contract.descriptors import (
    AuthFlow,
    Capabilities,
    CatalogModel,
    ConfigState,
    Display,
    InstallableDescriptor,
    ModelCatalog,
    ProviderDescriptor,
    RuntimeProfile,
)
from backend.surface_contract.identity import Emit, ProviderId, Subscription
from backend.surface_contract.intents import IntentRejected, ProviderIntent, TransportAck
from backend.surface_contract.provider_config_surface import ProviderConfigSurface


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _capabilities(record: ProviderRecord) -> Capabilities:
    caps = record.capabilities
    return Capabilities(
        fork=caps.get("supports_fork", False),
        manager_mode=caps.get("supports_manager_mode", False),
        rewind=caps.get("supports_rewind", False),
        steering=caps.get("supports_steering", False),
        native_subagents=caps.get("supports_native_subagents", False),
        reasoning_effort=caps.get("supports_reasoning_effort", False),
        # config_store's capability matrix (`_CAPABILITY_KEYS`) never
        # carries these two keys — default False when absent, per spec.
        usage_reporting=caps.get("supports_usage_reporting", False),
        startup_monitoring=caps.get("supports_startup_monitoring", False),
    )


# config_store.provider_credential_status's exhaustive return set
# (backend/credential_session_client.py's `CredentialStatus` Literal) is
# {"available", "missing", "blocked", "unknown"} — only the two with an
# unambiguous ConfigState analog are mapped; "available" needs no
# refinement (ACTIVE already), and "unknown" (status not yet probed) isn't
# confidently either credential_required or credential_failed, so it's
# left at ACTIVE too rather than guessed. RETRYING has no source anywhere
# in this read layer: `retry_provider_credential` is an intent-triggered
# action, not a persisted status this facade can observe — stays
# unreachable.
_CREDENTIAL_STATUS_TO_CONFIG_STATE = {
    "missing": ConfigState.CREDENTIAL_REQUIRED,
    "blocked": ConfigState.CREDENTIAL_FAILED,
}


def _config_state(record: ProviderRecord) -> ConfigState:
    if record.suspended:
        return ConfigState.SUSPENDED
    # provider_credential_status only means anything for api_key providers
    # (config_store itself only probes it in that case — see
    # config_store._provider_ui_state); subscription providers have no
    # credential-broker status to read.
    if record.mode == "api_key":
        status = store_access.get_provider_credential_status(record.id)
        refined = _CREDENTIAL_STATUS_TO_CONFIG_STATE.get(status)
        if refined is not None:
            return refined
    return ConfigState.ACTIVE


# config_store's provider `mode` field ("subscription" | "api_key" — see
# frontend/src/types.ts ProviderMode) is the only auth-mode signal
# store_access can reach; NONE has no store source distinguishing it from
# "not yet configured", so it's never emitted here.
_MODE_TO_AUTH_FLOW = {
    "subscription": AuthFlow.OAUTH_SUBSCRIPTION,
    "api_key": AuthFlow.API_KEY,
}


def _auth_flows(record: ProviderRecord) -> tuple[AuthFlow, ...]:
    flow = _MODE_TO_AUTH_FLOW.get(record.mode)
    return (flow,) if flow is not None else ()


def _map_descriptor(record: ProviderRecord) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=record.id,
        display=Display(
            label=record.name or record.kind,
            icon_id=record.kind,
            config_copy_key=f"provider.config_copy.{record.kind}",
        ),
        auth_flows=_auth_flows(record),
        capabilities=_capabilities(record),
        # gap: no orchestration-mode / send-mode source in store_access.
        orchestration_modes=(),
        send_modes=(),
        model_catalog_ref=record.id,
        config_state=_config_state(record),
    )


class ProviderConfigSurfaceAdapter(ProviderConfigSurface):
    def bind(self) -> None:
        # gap: no bus source for provider-config facts is reachable
        # within the adapter import boundary yet — no-op.
        return None

    # ---- read plane ------------------------------------------------

    def list_providers(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(_map_descriptor(r) for r in store_access.list_provider_records())

    def installable_catalog(self) -> tuple[InstallableDescriptor, ...]:
        # gap: no store source for installable-kind templates — empty
        # rather than hardcoding the frontend's static kind list.
        return ()

    def model_catalog(self, provider_id: ProviderId) -> ModelCatalog:
        record = next(
            (r for r in store_access.list_provider_records() if r.id == provider_id), None,
        )
        if record is None:
            return ModelCatalog(provider_id=provider_id, models=())
        # gap: models aren't individually tagged with a runner — best
        # available signal is the provider's own runtime profile(s); if
        # more than one runner is configured for this provider, every
        # model is stamped with the first one found (list order).
        runner = next(
            (p.runner for p in store_access.list_runtime_profiles() if p.provider_id == provider_id),
            "",
        )
        models = tuple(
            # gap: no per-model reasoning-effort or retirement signal.
            CatalogModel(model=m, runner=runner, reasoning_efforts=(), retired=False)
            for m in record.models
        )
        return ModelCatalog(provider_id=provider_id, models=models)

    def runtime_profiles(self) -> tuple[RuntimeProfile, ...]:
        return tuple(
            RuntimeProfile(
                runtime_profile_id=p.id,
                provider_id=p.provider_id,
                runner=p.runner,
                default_model=p.default_model,
                default_reasoning_effort=p.default_reasoning_effort or None,
            )
            for p in store_access.list_runtime_profiles()
        )

    # ---- live plane --------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        # No live source is registered (see bind()) — a well-formed,
        # inert subscription so callers don't need to special-case this
        # surface.
        return _SubscriptionImpl(lambda: None)

    # ---- command plane -------------------------------------------------

    def submit(self, intent: ProviderIntent) -> TransportAck:
        return IntentRejected(
            intent_id=intent.intent_id,
            code="unsupported_contract_phase",
            message="command routing deferred; the legacy REST/WS path remains authoritative",
        )
