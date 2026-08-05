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


def _config_state(record: ProviderRecord) -> ConfigState:
    # gap: only `suspended` reaches store_access — no credential-status
    # signal, so CREDENTIAL_REQUIRED / CREDENTIAL_FAILED / RETRYING are
    # unreachable states here.
    return ConfigState.SUSPENDED if record.suspended else ConfigState.ACTIVE


def _map_descriptor(record: ProviderRecord) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=record.id,
        display=Display(
            label=record.name or record.kind,
            icon_id=record.kind,
            config_copy_key=f"provider.config_copy.{record.kind}",
        ),
        # gap: ProviderRecord carries no auth-mode/credential-kind field
        # to derive flows from — emit none rather than guess by `kind`.
        auth_flows=(),
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
