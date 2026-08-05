"""Command plane (ADR 0006 §5; ADR 0007-0008 intents). Acks are projection
facts echoing intent_id — the transport result here is accept/reject only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    ApprovalRef,
    IntentId,
    NodeId,
    ProjectRef,
    ProviderId,
    SessionId,
    TurnId,
)
from backend.surface_contract.nodes import Attachment, SendMode


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


@dataclass(frozen=True, slots=True)
class Approve(IntentBase):
    approval_ref: ApprovalRef
    decision: str
    scope: str


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


ChatIntent = SendPrompt | Stop | Approve | EditQueued | DeleteQueued | SetSelectors | Rewind


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


@dataclass(frozen=True, slots=True)
class RenameProject(IntentBase):
    project_ref: ProjectRef
    name: str


@dataclass(frozen=True, slots=True)
class DeleteProject(IntentBase):
    project_ref: ProjectRef


@dataclass(frozen=True, slots=True)
class Rearrange(IntentBase):
    ordering_patch: dict[str, object]


@dataclass(frozen=True, slots=True)
class MarkOpened(IntentBase):
    pass


SessionIntent = (
    ArchiveSession
    | RenameSession
    | AssignProject
    | CreateProject
    | RenameProject
    | DeleteProject
    | Rearrange
    | MarkOpened
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
