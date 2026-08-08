"""System & Host Surface ABC (ADR 0011, Part 1): extensions, harness
profiles, extension UI, extension catalog, marketplace bridge, schedules,
host startup tasks, installation capabilities, machine/node topology +
provider-credential sync + node-registration approval.

Every read model below is a root/installation-scoped resource (never
session-scoped, except `ScheduleSummary.session_id` which is a foreign
reference, not a scope) — mirrors ADR 0006 §0's `cv`/typed-states/
change-only-push conventions verbatim, same posture ADRs 0007-0009
already take. `NodeRegistrationRequest` mirrors `UserInteraction`'s
kind/request/state/response idiom (ADR 0006 §1), root-scoped instead of
session-scoped, per ADR 0011 §9."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    Emit,
    NodeId,
    PageCursor,
    ProjectionResult,
    ProviderId,
    SessionId,
    Subscription,
)
from backend.surface_contract.intents import SystemIntent, TransportAck

# ---------------------------------------------------------------------------
# §1 Extension notices — toast + generic signal (producer-driven, no read
# model, no intents — same posture as ADR 0006's `notice`).
# ---------------------------------------------------------------------------


class ExtensionToastLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ExtensionToast:
    extension_id: str
    level: ExtensionToastLevel
    message: str
    session_id: SessionId | None = None


@dataclass(frozen=True, slots=True)
class ExtensionSignal:
    extension_id: str
    event_name: str
    data: dict[str, object]


# ---------------------------------------------------------------------------
# §2 Extension config + harness profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtensionConfigSections:
    """One optional sub-object per legacy config sub-topic — `None` means
    this extension honestly has nothing declared for that section (never
    an empty dict standing in for absence, per ADR 0011's descriptor-
    honesty conformance gate)."""

    settings: dict[str, object] | None = None
    instructions: dict[str, object] | None = None
    ui_settings: dict[str, object] | None = None
    internal_llm: dict[str, object] | None = None
    permissions: dict[str, object] | None = None
    mcp: dict[str, object] | None = None
    skills: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ExtensionConfigDescriptor:
    extension_id: str
    cv: int
    sections: ExtensionConfigSections


@dataclass(frozen=True, slots=True)
class ExtensionConfigPage:
    descriptors: tuple[ExtensionConfigDescriptor, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class ExtensionConfigUpsert:
    cv: int
    extension_id: str
    section: str


@dataclass(frozen=True, slots=True)
class HarnessProfileDescriptor:
    harness_profile_id: str
    cv: int
    display: str
    is_default: bool
    disabled_builtin_extensions: tuple[str, ...]
    disabled_builtin_tools: tuple[str, ...]
    config_schema: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class HarnessProfilePage:
    profiles: tuple[HarnessProfileDescriptor, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class HarnessProfileUpsert:
    cv: int
    profile: HarnessProfileDescriptor
    # Stamped only on the upsert `_run_command` force-emits right after a
    # `SaveHarnessProfile` intent that CREATED a new profile settles (same
    # "create-shaped intent, no synchronous ref, learn it from the live
    # frame" idiom `session_adapter.py`'s `FolderUpsert`/`TagUpsert` already
    # use) — `None` on every organically fact-triggered upsert.
    intent_id: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessDefaultChanged:
    cv: int
    harness_profile_id: str


# ---------------------------------------------------------------------------
# §3 Extension UI modules
# ---------------------------------------------------------------------------


class ExtensionUiModuleKind(StrEnum):
    QUICK_BUTTON = "quick_button"
    PAGE = "page"
    FRONTEND_MODULE = "frontend_module"


class HookActionKind(StrEnum):
    NAVIGATE = "navigate"
    ENSURE = "ensure"
    MODULE = "module"


@dataclass(frozen=True, slots=True)
class HookAction:
    """Closed, kind-scoped schema (ADR 0011 §3) — fields outside `kind`'s
    own variant stay unset. Unknown `kind` renders generically per ADR
    0006 §0, so this stays open to forward-compat `kind` values on the
    wire even though only the three named ones are typed here."""

    kind: str
    path: str | None = None
    endpoint: str | None = None
    path_template: str | None = None
    id_field: str | None = None
    include_cwd: bool | None = None
    module_url: str | None = None


@dataclass(frozen=True, slots=True)
class ExtensionUiDisplay:
    label: str
    icon: str | None = None


@dataclass(frozen=True, slots=True)
class ExtensionUiModule:
    extension_id: str
    cv: int
    kind: ExtensionUiModuleKind
    display: ExtensionUiDisplay
    action: HookAction | None = None
    placements: tuple[str, ...] = ()
    badge_count: int | None = None
    slot: str | None = None
    module_url: str | None = None
    payments: bool | None = None
    marketplace_auth: bool | None = None


@dataclass(frozen=True, slots=True)
class ExtensionUiPage:
    modules: tuple[ExtensionUiModule, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class ExtensionUiChanged:
    cv: int


# ---------------------------------------------------------------------------
# §4 Extension catalog / updates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtensionCatalogEntry:
    extension_id: str
    cv: int
    display: str
    installed_version: str
    available_version: str | None
    update_available: bool
    enabled: bool
    source: str


@dataclass(frozen=True, slots=True)
class ExtensionCatalogPage:
    entries: tuple[ExtensionCatalogEntry, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class ExtensionCatalogUpsert:
    cv: int
    entry: ExtensionCatalogEntry


# ---------------------------------------------------------------------------
# §5 Marketplace bridge
# ---------------------------------------------------------------------------


class MarketplaceConnectionState(StrEnum):
    UNPAIRED = "unpaired"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class PairedDevice:
    device_ref: str
    label: str


@dataclass(frozen=True, slots=True)
class MarketplaceBridgeState:
    cv: int
    connection_state: MarketplaceConnectionState
    revocation_pending: bool
    paired_devices: tuple[PairedDevice, ...]


@dataclass(frozen=True, slots=True)
class MarketplaceBridgeStateChanged:
    cv: int
    state: MarketplaceBridgeState


@dataclass(frozen=True, slots=True)
class MarketplaceExtensionRef:
    id: str
    name: str | None = None
    version: str | None = None
    publisher: str | None = None
    permission_delta: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MarketplaceIntent:
    intent_id: str
    cv: int
    action: str  # pair | install | enable | disable | update | uninstall
    status: str  # pending|leased|awaiting_confirmation|committing|redeemed|
    # succeeded|rejected|failed|failed_unknown|expired|cancelled
    extension: MarketplaceExtensionRef | None = None
    account_label: str | None = None
    site_label: str | None = None
    device_label: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MarketplaceIntentPage:
    intents: tuple[MarketplaceIntent, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class MarketplaceIntentUpsert:
    cv: int
    intent: MarketplaceIntent


# ---------------------------------------------------------------------------
# §6 Schedules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleSummary:
    schedule_id: str
    cv: int
    session_id: SessionId
    prompt_preview: str
    cadence: str
    next_run_at: str | None
    last_run_at: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class SchedulePage:
    schedules: tuple[ScheduleSummary, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class ScheduleUpsert:
    cv: int
    schedule: ScheduleSummary


@dataclass(frozen=True, slots=True)
class ScheduleRemoved:
    cv: int
    schedule_id: str


# ---------------------------------------------------------------------------
# §7 Host startup tasks
# ---------------------------------------------------------------------------


class HostStartupTaskState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HostStartupTask:
    id: str
    cv: int
    label: str
    state: HostStartupTaskState
    started_at: str
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class HostStartupTaskSnapshot:
    tasks: tuple[HostStartupTask, ...]
    epoch: int


@dataclass(frozen=True, slots=True)
class HostStartupTaskUpsert:
    cv: int
    task: HostStartupTask


@dataclass(frozen=True, slots=True)
class HostStartupTaskCleared:
    cv: int
    epoch: int


# ---------------------------------------------------------------------------
# §8 Installation capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallationCapability:
    capability_id: str
    cv: int
    enabled: bool
    display: str
    # `installation_capabilities._capability_state`'s remaining fields — the
    # legacy consumer (`InstallationCapabilities.tsx`) renders all six; the
    # read model previously carried only `enabled`.
    provisioned: bool = False
    active: bool = False
    restart_required: bool = False
    self_provisionable: bool = False
    in_app_restart_supported: bool = False


@dataclass(frozen=True, slots=True)
class InstallationCapabilityChanged:
    cv: int
    capability: InstallationCapability


# ---------------------------------------------------------------------------
# §9 Machine/node topology + provider-credential sync + node registration
# ---------------------------------------------------------------------------


class NodeRole(StrEnum):
    PRIMARY = "primary"
    WORKER_NODE = "worker_node"


class NodeConnState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class NodeVersionStatus(StrEnum):
    OK = "ok"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NodeDescriptor:
    node_id: NodeId
    cv: int
    role: NodeRole
    address: str
    cwd_roots: tuple[str, ...]
    state: NodeConnState
    last_seen: float | None
    connected_at: float | None
    version_status: NodeVersionStatus
    app_commit_sha: str | None = None
    app_dirty: bool | None = None
    primary_commit_sha: str | None = None
    primary_dirty: bool | None = None


@dataclass(frozen=True, slots=True)
class MachinePage:
    nodes: tuple[NodeDescriptor, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class MachineNodeUpsert:
    """Named distinctly from `frames.NodeUpsert` (the chat content-node
    frame) so the two never collide on the wire — `adapter_api.
    _frame_type_name` derives the wire `type` string purely from the
    Python class name."""

    cv: int
    node: NodeDescriptor


@dataclass(frozen=True, slots=True)
class NodeRemoved:
    cv: int
    node_id: NodeId


class NodeProviderCredentialState(StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NodeProviderCredentialStatus:
    node_id: NodeId
    provider_id: ProviderId
    cv: int
    status: NodeProviderCredentialState
    authorized_at: str | None
    updated_at: str | None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class NodeProviderCredentialUpsert:
    cv: int
    status: NodeProviderCredentialStatus


class NodeRegistrationState(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class NodeRegistrationDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class NodeRegistrationRequestPayload:
    address: str
    cwd_roots: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class NodeRegistrationResponse:
    decision: NodeRegistrationDecision


@dataclass(frozen=True, slots=True)
class NodeRegistrationRequest:
    node_id: NodeId
    cv: int
    request: NodeRegistrationRequestPayload
    state: NodeRegistrationState
    response: NodeRegistrationResponse | None = None


@dataclass(frozen=True, slots=True)
class NodeRegistrationPage:
    requests: tuple[NodeRegistrationRequest, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class NodeRegistrationUpsert:
    cv: int
    node_id: NodeId
    request: NodeRegistrationRequest


SystemFrame = (
    ExtensionToast
    | ExtensionSignal
    | ExtensionConfigUpsert
    | HarnessProfileUpsert
    | HarnessDefaultChanged
    | ExtensionUiChanged
    | ExtensionCatalogUpsert
    | MarketplaceBridgeStateChanged
    | MarketplaceIntentUpsert
    | ScheduleUpsert
    | ScheduleRemoved
    | HostStartupTaskUpsert
    | HostStartupTaskCleared
    | InstallationCapabilityChanged
    | MachineNodeUpsert
    | NodeRemoved
    | NodeProviderCredentialUpsert
    | NodeRegistrationUpsert
)


class SystemSurface(ABC):
    # ---- §2 extension config + harness profiles ----------------------

    @abstractmethod
    def extension_config(self, extension_id: str) -> ProjectionResult[ExtensionConfigDescriptor]: ...

    @abstractmethod
    def list_extension_configs(
        self, cursor: PageCursor | None
    ) -> ProjectionResult[ExtensionConfigPage]: ...

    @abstractmethod
    def list_harness_profiles(
        self, cursor: PageCursor | None
    ) -> ProjectionResult[HarnessProfilePage]: ...

    # ---- §3 extension UI modules ---------------------------------------

    @abstractmethod
    def list_extension_ui(self, cursor: PageCursor | None) -> ProjectionResult[ExtensionUiPage]: ...

    # ---- §4 extension catalog -------------------------------------------

    @abstractmethod
    def list_extension_catalog(
        self, cursor: PageCursor | None
    ) -> ProjectionResult[ExtensionCatalogPage]: ...

    # ---- §5 marketplace bridge -------------------------------------------

    @abstractmethod
    def marketplace_bridge_state(self) -> ProjectionResult[MarketplaceBridgeState]: ...

    @abstractmethod
    def list_marketplace_intents(
        self, cursor: PageCursor | None
    ) -> ProjectionResult[MarketplaceIntentPage]: ...

    # ---- §6 schedules ------------------------------------------------------

    @abstractmethod
    def list_schedules(
        self, session_id: SessionId | None, cursor: PageCursor | None
    ) -> ProjectionResult[SchedulePage]: ...

    # ---- §7 host startup tasks -------------------------------------------

    @abstractmethod
    def host_startup_tasks(self) -> ProjectionResult[HostStartupTaskSnapshot]: ...

    # ---- §8 installation capabilities -------------------------------------

    @abstractmethod
    def list_installation_capabilities(
        self,
    ) -> ProjectionResult[tuple[InstallationCapability, ...]]: ...

    # ---- §9 machine/node topology ------------------------------------------

    @abstractmethod
    def list_machines(self, cursor: PageCursor | None) -> ProjectionResult[MachinePage]: ...

    @abstractmethod
    def node_provider_credentials(
        self, node_id: NodeId
    ) -> ProjectionResult[tuple[NodeProviderCredentialStatus, ...]]: ...

    @abstractmethod
    def list_node_registrations(
        self, cursor: PageCursor | None
    ) -> ProjectionResult[NodeRegistrationPage]: ...

    # ---- live + command planes ---------------------------------------------

    @abstractmethod
    def subscribe(self, emit: Emit) -> Subscription:
        """emit receives SystemFrame values, change-only."""

    @abstractmethod
    def submit(self, intent: SystemIntent) -> TransportAck: ...
