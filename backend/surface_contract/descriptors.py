"""Capability & display plane (ADR 0006 §6) and provider-config read models
(ADR 0007). The UI never branches on provider identity — everything renders
from these descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import ProviderId
from backend.surface_contract.intents import IntentRejected


class AuthFlow(StrEnum):
    OAUTH_SUBSCRIPTION = "oauth_subscription"
    API_KEY = "api_key"
    NONE = "none"


class ConfigState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CREDENTIAL_REQUIRED = "credential_required"
    CREDENTIAL_FAILED = "credential_failed"
    RETRYING = "retrying"


class LoginPhase(StrEnum):
    STARTING = "starting"
    AWAITING_BROWSER = "awaiting_browser"
    AWAITING_CODE = "awaiting_code"
    POLLING = "polling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FormFieldKind(StrEnum):
    TEXT = "text"
    PATH = "path"
    SECRET = "secret"  # write-only: never echoed in any read model or frame
    ENUM = "enum"
    BOOL = "bool"


@dataclass(frozen=True, slots=True)
class FormField:
    name: str
    kind: FormFieldKind
    label_key: str
    required: bool = False
    choices: tuple[str, ...] = ()
    default: str | bool | None = None
    pattern: str | None = None
    max_length: int | None = None
    # Closure 4 (form-copy regression): per-kind placeholder/hint i18n
    # keys — restores the copy the deleted frontend `apiEnvCopyForKind`/
    # `configDirCopyForKind` used to vary per underlying provider kind,
    # which `label_key` alone (one key per field, every kind) had
    # collapsed to a single generic string. `None` when a field has no
    # placeholder/hint copy at all (never a fabricated empty string).
    placeholder_key: str | None = None
    hint_key: str | None = None


@dataclass(frozen=True, slots=True)
class Display:
    label: str
    icon_id: str
    config_copy_key: str


@dataclass(frozen=True, slots=True)
class Capabilities:
    fork: bool
    manager_mode: bool
    rewind: bool
    steering: bool
    native_subagents: bool
    reasoning_effort: bool
    usage_reporting: bool
    startup_monitoring: bool


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: ProviderId
    display: Display
    auth_flows: tuple[AuthFlow, ...]
    capabilities: Capabilities
    orchestration_modes: tuple[str, ...]
    send_modes: tuple[str, ...]
    model_catalog_ref: str
    config_state: ConfigState


@dataclass(frozen=True, slots=True)
class InstallableDescriptor:
    kind: str
    display: Display
    form_schema: tuple[FormField, ...]
    defaults: dict[str, object]
    auth_flows: tuple[AuthFlow, ...]


@dataclass(frozen=True, slots=True)
class CatalogModel:
    model: str
    runner: str
    reasoning_efforts: tuple[str, ...]
    retired: bool = False


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    provider_id: ProviderId
    models: tuple[CatalogModel, ...]


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    runtime_profile_id: str
    provider_id: ProviderId
    runner: str
    default_model: str
    default_reasoning_effort: str | None
    # Closure 3 (RuntimeProfile v2 parity) additions — defaulted so every
    # pre-existing positional/keyword construction (tests, D4's
    # `save_runtime_profile` echo) stays valid unchanged. Every field here
    # is real, store_access.RuntimeProfileRecord-sourced data — never
    # fabricated to satisfy the frontend's `types.ts` RuntimeProfile shape.
    # `deleted_at` (not a derived boolean) mirrors the legacy field
    # exactly — every current frontend consumer only truthy-checks it
    # anyway, but the real timestamp costs nothing extra to carry and
    # loses nothing a boolean would have discarded.
    name: str = ""
    deleted_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class DeletedProviderRef:
    """Display-only graveyard entry (config_store.py's `deleted_providers`
    — "tombstoned runtime profiles resolve their provider display through
    it"). Mirrors `backend.adapters.store_access.DeletedProviderRecord`
    field-for-field."""

    provider_id: ProviderId
    name: str
    nickname: str
    kind: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class RuntimeProfilesSnapshot:
    """Closure 3: the FULL runtime-profile plane snapshot — extends the
    bare `tuple[RuntimeProfile, ...]` `runtime_profiles()` used to return
    with every other field `runtime_profiles_api._snapshot()`'s legacy
    broadcast carries (`default_runtime_profile_id`/`last_models`/
    `last_reasoning_efforts`/`deleted_providers`), enumerated from that
    function directly — nothing invented. `profiles` includes tombstones
    (`RuntimeProfile.deleted`), matching the legacy snapshot's own
    `include_deleted=True` read, so old-session display still resolves a
    deleted profile's name/defaults."""

    profiles: tuple[RuntimeProfile, ...]
    default_runtime_profile_id: str | None
    last_models: dict[str, str]
    last_reasoning_efforts: dict[str, str]
    deleted_providers: tuple[DeletedProviderRef, ...]


@dataclass(frozen=True, slots=True)
class LoginFlowState:
    provider_id: ProviderId
    intent_id: str
    phase: LoginPhase
    data: dict[str, object] | None = None


class InstallState(StrEnum):
    """`provider_setup._INSTALL_RUNS`'s own state vocabulary
    (backend/provider_setup.py) — no fourth value exists anywhere in that
    module's assignments; `install_if_missing`'s "already_installed" is a
    same-call return value, never persisted to `_INSTALL_RUNS`, so it never
    reaches this frame."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InstallLine:
    stream: str  # "stdout" | "stderr" — provider_setup.py's own literal
    text: str


@dataclass(frozen=True, slots=True)
class InstallRun:
    """Closure 2 (install progress v2): a full projection of one
    provider-CLI install run — the SAME shape `provider_setup._snapshot`
    already returns for `GET /api/provider-setup/installs`'s cold-start
    read, reused for the live frame too (`InstallProgressChanged.run`) so
    cold and live share one shape rather than a cold snapshot plus a
    second, partial live-delta shape."""

    kind: str
    label: str
    command: str
    state: InstallState
    lines: tuple[InstallLine, ...]
    started_at: str | None
    finished_at: str | None
    returncode: int | None
    installed: bool | None
    message: str | None


@dataclass(frozen=True, slots=True)
class ProviderUpsert:
    cv: int
    descriptor: ProviderDescriptor
    intent_id: str | None = None


@dataclass(frozen=True, slots=True)
class InstallableCatalogChanged:
    cv: int


@dataclass(frozen=True, slots=True)
class CredentialState:
    cv: int
    provider_id: ProviderId
    config_state: ConfigState


@dataclass(frozen=True, slots=True)
class LoginFlowFrame:
    cv: int
    state: LoginFlowState


@dataclass(frozen=True, slots=True)
class ModelCatalogChanged:
    cv: int
    provider_id: ProviderId


@dataclass(frozen=True, slots=True)
class InstallProgressChanged:
    cv: int
    run: InstallRun


@dataclass(frozen=True, slots=True)
class RuntimeProfilesChanged:
    """Closure 3: the dedicated runtime-profile-changed frame ADR 0007's
    `ProviderFrame` union previously had NO member for at all
    (`runtime_profiles_api._broadcast_changed`'s own comment: "no dedicated
    runtime-profile-changed frame... reuses model_catalog_changed, the
    only honest signal available" — now replaced by a real one). Carries
    the full snapshot directly (same rationale as `LoginFlowFrame`/
    `InstallProgressChanged`: `default_runtime_profile_id`/`last_models`/
    etc. have no single-entity `provider_id` to key a re-derive off, and
    the legacy broadcast this mirrors already sends the full snapshot
    unconditionally on every mutation path, including the two
    (`record_last_model`/`record_last_reasoning_effort`) that carry no
    `provider_id` at all)."""

    cv: int
    snapshot: RuntimeProfilesSnapshot


ProviderFrame = (
    ProviderUpsert
    | InstallableCatalogChanged
    | CredentialState
    | LoginFlowFrame
    | ModelCatalogChanged
    | InstallProgressChanged
    | RuntimeProfilesChanged
    # Async mutation-failure notification (ADR 0006 §5 "acks are projection
    # facts"): `submit()` still admits a well-formed intent synchronously
    # (IntentAccepted), but a business-logic failure discovered while the
    # port coroutine actually runs the mutation has no other compensating
    # signal — `ProviderConfigSurfaceAdapter.reject_intent` broadcasts the
    # SAME `IntentRejected` type the synchronous ack path already uses
    # (one ack vocabulary, two delivery times), matched by the client via
    # `intent_id`.
    | IntentRejected
)
