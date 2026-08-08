"""Command plane (ADR 0006 §5; ADR 0007-0008 intents). Acks are projection
facts echoing intent_id — the transport result here is accept/reject only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    ApprovalRef as InteractionRef,
    FolderRef,
    IntentId,
    NodeId,
    ProjectRef,
    ProviderId,
    SessionId,
    TagRef,
    TurnId,
)
from backend.surface_contract.nodes import ApprovalDecision, Attachment, SendMode


class SendTargetKind(StrEnum):
    CURRENT = "current"
    FORK = "fork"
    NEW_SESSION = "new_session"


@dataclass(frozen=True, slots=True)
class SendTarget:
    kind: SendTargetKind
    fork_node_id: NodeId | None = None


@dataclass(frozen=True, slots=True)
class IntentBase:
    cv: int
    intent_id: IntentId
    # None only for send_prompt with target new_session (ADR 0006 §5).
    session_id: SessionId | None


@dataclass(frozen=True, slots=True)
class SendPrompt(IntentBase):
    text: str
    attachments: tuple[Attachment, ...]
    send_mode: SendMode
    target: SendTarget


@dataclass(frozen=True, slots=True)
class Stop(IntentBase):
    turn_id: TurnId


# ---- resolve_interaction's typed response union (ADR 0006 §5) -------------
# One dataclass per UserInteractionKind, keyed by the resource's own `kind`
# — never a single untyped dict, so a malformed/kind-mismatched response is
# a parse-time rejection, not a silent no-op deep inside the resolving
# store.


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    decision: ApprovalDecision


@dataclass(frozen=True, slots=True)
class ChoiceResponse:
    picked_ref: str | None  # None = decline/cancel


@dataclass(frozen=True, slots=True)
class InputResponse:
    response: dict[str, object]


InteractionResponse = ApprovalResponse | ChoiceResponse | InputResponse


@dataclass(frozen=True, slots=True)
class ResolveInteraction(IntentBase):
    interaction_ref: InteractionRef
    response: InteractionResponse


@dataclass(frozen=True, slots=True)
class EditQueued(IntentBase):
    node_id: NodeId
    text: str


@dataclass(frozen=True, slots=True)
class DeleteQueued(IntentBase):
    node_id: NodeId


@dataclass(frozen=True, slots=True)
class SetSelectors(IntentBase):
    runtime_profile_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    permission: str | None = None
    harness_profile_id: str | None = None
    orchestration_mode: str | None = None


@dataclass(frozen=True, slots=True)
class Rewind(IntentBase):
    node_id: NodeId


ChatIntent = (
    SendPrompt | Stop | ResolveInteraction | EditQueued | DeleteQueued | SetSelectors | Rewind
)


@dataclass(frozen=True, slots=True)
class CreateProvider(IntentBase):
    kind: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class UpdateProvider(IntentBase):
    provider_id: ProviderId
    config_patch: dict[str, object]


@dataclass(frozen=True, slots=True)
class DeleteProvider(IntentBase):
    provider_id: ProviderId


@dataclass(frozen=True, slots=True)
class SuspendProvider(IntentBase):
    provider_id: ProviderId
    suspended: bool


@dataclass(frozen=True, slots=True)
class RetryCredential(IntentBase):
    provider_id: ProviderId


@dataclass(frozen=True, slots=True)
class BeginLogin(IntentBase):
    provider_id: ProviderId
    flow: str


@dataclass(frozen=True, slots=True)
class CancelLogin(IntentBase):
    provider_id: ProviderId


@dataclass(frozen=True, slots=True)
class RefreshModels(IntentBase):
    provider_id: ProviderId


@dataclass(frozen=True, slots=True)
class SaveRuntimeProfile(IntentBase):
    profile: dict[str, object]


@dataclass(frozen=True, slots=True)
class DeleteRuntimeProfile(IntentBase):
    runtime_profile_id: str


ProviderIntent = (
    CreateProvider
    | UpdateProvider
    | DeleteProvider
    | SuspendProvider
    | RetryCredential
    | BeginLogin
    | CancelLogin
    | RefreshModels
    | SaveRuntimeProfile
    | DeleteRuntimeProfile
)


@dataclass(frozen=True, slots=True)
class ArchiveSession(IntentBase):
    archived: bool


@dataclass(frozen=True, slots=True)
class RenameSession(IntentBase):
    title: str


@dataclass(frozen=True, slots=True)
class AssignProject(IntentBase):
    project_ref: ProjectRef | None


@dataclass(frozen=True, slots=True)
class CreateProject(IntentBase):
    name: str
    # A project's identity IS its filesystem path (see `Project.project_ref`,
    # `backend/surface_contract/session_surface.py`) — there is no legacy
    # mechanism that creates a project from a name alone, so this intent
    # cannot omit it.
    path: str


@dataclass(frozen=True, slots=True)
class RenameProject(IntentBase):
    project_ref: ProjectRef
    name: str


@dataclass(frozen=True, slots=True)
class DeleteProject(IntentBase):
    project_ref: ProjectRef


@dataclass(frozen=True, slots=True)
class MarkOpened(IntentBase):
    pass


# ---- ADR 0008 folders/tags/session-organization intents -------------------
# None of these carry a real `session_id` except AssignFolder/AssignTags
# (folder/tag catalog mutations are project- or catalog-scoped, not
# session-scoped) — same "IntentBase.session_id is None" idiom CreateProject/
# RenameProject/DeleteProject above already use.


@dataclass(frozen=True, slots=True)
class CreateFolder(IntentBase):
    project_ref: ProjectRef
    name: str
    parent_folder_ref: FolderRef | None = None


@dataclass(frozen=True, slots=True)
class RenameFolder(IntentBase):
    folder_ref: FolderRef
    name: str


@dataclass(frozen=True, slots=True)
class MoveFolder(IntentBase):
    folder_ref: FolderRef
    parent_folder_ref: FolderRef | None


class FolderDeleteMode(StrEnum):
    UNASSIGN = "unassign"
    DELETE_SESSIONS = "delete_sessions"


@dataclass(frozen=True, slots=True)
class DeleteFolder(IntentBase):
    folder_ref: FolderRef
    mode: FolderDeleteMode


@dataclass(frozen=True, slots=True)
class CreateTag(IntentBase):
    name: str
    project_ref: ProjectRef | None = None
    color: str | None = None


@dataclass(frozen=True, slots=True)
class RenameTag(IntentBase):
    tag_ref: TagRef
    name: str


@dataclass(frozen=True, slots=True)
class RecolorTag(IntentBase):
    tag_ref: TagRef
    color: str


@dataclass(frozen=True, slots=True)
class DeleteTag(IntentBase):
    tag_ref: TagRef


@dataclass(frozen=True, slots=True)
class AssignFolder(IntentBase):
    folder_ref: FolderRef | None


@dataclass(frozen=True, slots=True)
class AssignTags(IntentBase):
    """Source-scoped delta (`add_tag_refs`/`remove_tag_refs`) and sync
    (`sync_tag_refs`) modes are mutually exclusive per call — both touch
    only `tag_refs` entries whose `source` matches this call's `source`
    (source-isolation invariant, ADR 0008)."""

    source: str = "manual"
    add_tag_refs: tuple[TagRef, ...] | None = None
    remove_tag_refs: tuple[TagRef, ...] | None = None
    sync_tag_refs: tuple[TagRef, ...] | None = None


SessionIntent = (
    ArchiveSession
    | RenameSession
    | AssignProject
    | CreateProject
    | RenameProject
    | DeleteProject
    | MarkOpened
    | CreateFolder
    | RenameFolder
    | MoveFolder
    | DeleteFolder
    | CreateTag
    | RenameTag
    | RecolorTag
    | DeleteTag
    | AssignFolder
    | AssignTags
)


# ---- ADR 0011 System & Host Surface intents --------------------------------


@dataclass(frozen=True, slots=True)
class UpdateExtensionConfig(IntentBase):
    extension_id: str
    section: str
    patch: dict[str, object]


@dataclass(frozen=True, slots=True)
class SaveHarnessProfile(IntentBase):
    """`harness_profile_id=None` creates a new profile from `config`
    (matches legacy `POST /api/harness-profiles`'s body). A non-`None` id
    writes `writes` (legacy `HarnessFieldWrite[]`, the shape
    `HarnessSettingsEditor.tsx`'s `writeFields` actually sends) against
    `revision` for the optimistic-concurrency check (legacy `PATCH
    .../fields`'s `{revision, writes}` body) — `config` is unused on that
    path."""

    harness_profile_id: str | None
    config: dict[str, object]
    revision: str | None = None
    writes: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class DeleteHarnessProfile(IntentBase):
    harness_profile_id: str
    # Matches legacy `DELETE .../{id}?revision=...`'s optimistic-concurrency
    # check — `None` skips the check, same as an omitted query param.
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class SetDefaultHarnessProfile(IntentBase):
    harness_profile_id: str


@dataclass(frozen=True, slots=True)
class InstallExtension(IntentBase):
    extension_id: str
    source: dict[str, object]


@dataclass(frozen=True, slots=True)
class UpdateExtension(IntentBase):
    extension_id: str


@dataclass(frozen=True, slots=True)
class UninstallExtension(IntentBase):
    extension_id: str


@dataclass(frozen=True, slots=True)
class EnableExtension(IntentBase):
    extension_id: str


@dataclass(frozen=True, slots=True)
class DisableExtension(IntentBase):
    extension_id: str


class MarketplaceDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class DecideMarketplaceIntent(IntentBase):
    marketplace_intent_id: str
    decision: MarketplaceDecision


@dataclass(frozen=True, slots=True)
class RevokeMarketplaceDevice(IntentBase):
    device_ref: str


@dataclass(frozen=True, slots=True)
class CreateSchedule(IntentBase):
    target_session_id: str
    prompt: str
    cadence: dict[str, object]


@dataclass(frozen=True, slots=True)
class DeleteSchedule(IntentBase):
    schedule_id: str


@dataclass(frozen=True, slots=True)
class SetInstallationCapability(IntentBase):
    capability_id: str
    enabled: bool
    # Mirrors legacy `PATCH /api/installation-profile/capabilities/{id}`'s
    # `confirm_cancels_extension_work` body field exactly: required `True`
    # to disable `integrations`, since that cancels extension-owned work at
    # the next start.
    confirm_cancels_extension_work: bool = False


@dataclass(frozen=True, slots=True)
class RemoveNode(IntentBase):
    node_id: str


@dataclass(frozen=True, slots=True)
class SyncNodeProviders(IntentBase):
    """Covers only the credential-sync path (legacy `POST .../sync-
    providers` with `include_secrets: true` + a non-empty `provider_ids`)
    — the same route's `include_secrets: false` push has no durable state
    to represent (a one-shot RPC ack, nothing `store_access` can re-derive
    from), so `system_commands.py` rejects that shape as `unsupported`
    rather than silently no-op-ing it."""

    node_id: str
    include_secrets: bool = False
    provider_ids: tuple[str, ...] = ()


class NodeRegistrationDecisionValue(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ResolveNodeRegistration(IntentBase):
    node_id: str
    decision: NodeRegistrationDecisionValue


SystemIntent = (
    UpdateExtensionConfig
    | SaveHarnessProfile
    | DeleteHarnessProfile
    | SetDefaultHarnessProfile
    | InstallExtension
    | UpdateExtension
    | UninstallExtension
    | EnableExtension
    | DisableExtension
    | DecideMarketplaceIntent
    | RevokeMarketplaceDevice
    | CreateSchedule
    | DeleteSchedule
    | SetInstallationCapability
    | RemoveNode
    | SyncNodeProviders
    | ResolveNodeRegistration
)


@dataclass(frozen=True, slots=True)
class IntentAccepted:
    intent_id: IntentId


@dataclass(frozen=True, slots=True)
class IntentRejected:
    intent_id: IntentId
    code: str
    message: str


TransportAck = IntentAccepted | IntentRejected
