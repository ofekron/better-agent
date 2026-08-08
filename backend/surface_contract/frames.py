"""Live delta plane frames (ADR 0006 §4) plus companion-surface push frames
(ADRs 0007-0009). Every frame is stamped with the surface snapshot identity;
one mutation batch = one render_rev, delivered atomically."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    IntentId,
    NodeId,
    SessionId,
    SessionSelectors,
    SnapshotIdentity,
    SurfaceId,
    TurnId,
)
from backend.surface_contract.nodes import (
    ContentStatus,
    Node,
    Run,
    Sidecar,
    Usage,
    UserInteraction,
)


class TurnPhase(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    # A turn paused on ANY UserInteraction kind (approval, choice, or
    # input) — one phase, not one per kind (ADR 0006 §4).
    AWAITING_INTERACTION = "awaiting_interaction"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class TerminalReason(StrEnum):
    OK = "ok"
    USER_STOPPED = "user_stopped"
    PROVIDER_ERROR = "provider_error"
    UNKNOWN_AFTER_RECOVERY = "unknown_after_recovery"


@dataclass(frozen=True, slots=True)
class FrameBase:
    cv: int
    surface_id: SurfaceId
    snapshot: SnapshotIdentity


@dataclass(frozen=True, slots=True)
class NodeUpsert(FrameBase):
    node: Node


@dataclass(frozen=True, slots=True)
class TextDelta(FrameBase):
    node_id: NodeId
    appended_text: str


@dataclass(frozen=True, slots=True)
class NodeStatus(FrameBase):
    node_id: NodeId
    status: ContentStatus


@dataclass(frozen=True, slots=True)
class TurnLifecycle(FrameBase):
    turn_id: TurnId
    phase: TurnPhase
    reason: TerminalReason | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class RunUpsert(FrameBase):
    run: Run


@dataclass(frozen=True, slots=True)
class UserInteractionUpsert(FrameBase):
    user_interaction: UserInteraction


@dataclass(frozen=True, slots=True)
class SidecarUpsert(FrameBase):
    sidecar: Sidecar


@dataclass(frozen=True, slots=True)
class SessionState(FrameBase):
    intent_id: IntentId | None = None
    title: str | None = None
    markers: tuple[str, ...] | None = None
    selectors: SessionSelectors | None = None


@dataclass(frozen=True, slots=True)
class Notice(FrameBase):
    scope: str
    payload: dict[str, object]


# Control-scope frames are not bound to a surface subscription (ADR 0006 §4/§5).
@dataclass(frozen=True, slots=True)
class SessionCreated:
    cv: int
    intent_id: IntentId
    session_id: SessionId
    surface_id: SurfaceId


@dataclass(frozen=True, slots=True)
class ResyncRequired:
    cv: int
    surface_id: SurfaceId


ChatFrame = (
    NodeUpsert
    | TextDelta
    | NodeStatus
    | TurnLifecycle
    | RunUpsert
    | UserInteractionUpsert
    | SidecarUpsert
    | SessionState
    | Notice
    | ResyncRequired
)

ControlFrame = SessionCreated
