"""Capability & display plane (ADR 0006 §6) and provider-config read models
(ADR 0007). The UI never branches on provider identity — everything renders
from these descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import ProviderId


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


@dataclass(frozen=True, slots=True)
class LoginFlowState:
    provider_id: ProviderId
    intent_id: str
    phase: LoginPhase
    data: dict[str, object] | None = None


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


ProviderFrame = (
    ProviderUpsert
    | InstallableCatalogChanged
    | CredentialState
    | LoginFlowFrame
    | ModelCatalogChanged
)
