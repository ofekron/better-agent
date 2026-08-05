"""Content-plane node vocabulary (ADR 0006 §1) — the closed, versioned
serialization of the chat-panel.md grammar. Structural kinds carry no
status; lifecycle frames are their single state authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from backend.surface_contract.identity import (
    ApprovalRef,
    NodeId,
    RunRef,
    SidecarRef,
    SurfaceId,
    TurnId,
)


class NodeKind(StrEnum):
    TURN = "turn"
    TYPED_PROMPT = "typed_prompt"
    EXPLANATION = "explanation"
    ASSISTANT_TEXT = "assistant_text"
    THINKING = "thinking"
    TOOL_INTERACTION = "tool_interaction"
    STEERING_MESSAGE = "steering_message"
    NATIVE_SUBAGENT_TURN = "native_subagent_turn"
    WORKER_TURN = "worker_turn"
    MODEL_CHANGE = "model_change"
    RESULT = "result"
    COMPACTION = "compaction"
    DIAGNOSTIC = "diagnostic"
    OTHER_TYPED_WORK = "other_typed_work"


STRUCTURAL_KINDS = frozenset(
    {
        NodeKind.TURN,
        NodeKind.EXPLANATION,
        NodeKind.NATIVE_SUBAGENT_TURN,
        NodeKind.WORKER_TURN,
        NodeKind.RESULT,
    }
)


class ContentStatus(StrEnum):
    QUEUED = "queued"
    STREAMING = "streaming"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


class SendMode(StrEnum):
    QUEUE = "queue"
    INTERRUPT = "interrupt"
    STEER = "steer"


class PromptOrigin(StrEnum):
    USER = "user"
    QUEUED = "queued"
    OFFLINE_SYNC = "offline_sync"


class ResultKind(StrEnum):
    PROVIDER = "provider"
    DERIVED = "derived"


class DiagnosticCode(StrEnum):
    EXECUTION_CONTINUATION = "execution_continuation"
    OTHER = "other"


class ModelChangeSource(StrEnum):
    USER = "user"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    media_type: str
    ref: str


@dataclass(frozen=True, slots=True)
class TypedPromptPayload:
    text: str
    attachments: tuple[Attachment, ...] = ()
    send_mode: SendMode = SendMode.QUEUE
    origin: PromptOrigin = PromptOrigin.USER
    intent_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssistantTextPayload:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingPayload:
    text: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class ToolInteractionPayload:
    tool_name: str
    args: dict[str, object]
    result: dict[str, object] | None = None
    approval_ref: ApprovalRef | None = None
    ui_kind: str | None = None
    derived_view: str | None = None


@dataclass(frozen=True, slots=True)
class SteeringMessagePayload:
    text: str
    target: str


@dataclass(frozen=True, slots=True)
class ModelChangePayload:
    from_run_ref: RunRef | None
    to_run_ref: RunRef
    source: ModelChangeSource


@dataclass(frozen=True, slots=True)
class ResultPayload:
    result_kind: ResultKind


@dataclass(frozen=True, slots=True)
class CompactionPayload:
    summary: str
    replaced_node_ids: tuple[NodeId, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticPayload:
    severity: str
    code: DiagnosticCode
    text: str
    data: dict[str, object] | None = None


# Also carries WorkerInteraction facts (worker_start/worker_event/
# worker_complete) verbatim — the grammar gives them no dedicated shape.
@dataclass(frozen=True, slots=True)
class OtherTypedWorkPayload:
    label: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class ChildManifest:
    renderable_child_count: int
    has_children: bool


NodePayload = (
    TypedPromptPayload
    | AssistantTextPayload
    | ThinkingPayload
    | ToolInteractionPayload
    | SteeringMessagePayload
    | ModelChangePayload
    | ResultPayload
    | CompactionPayload
    | DiagnosticPayload
    | OtherTypedWorkPayload
    | None
)


@dataclass(frozen=True, slots=True)
class Node:
    cv: int
    node_id: NodeId
    parent_id: NodeId | None
    turn_id: TurnId
    surface_id: SurfaceId
    kind: NodeKind
    ts: float
    seq: int
    status: ContentStatus | None = None
    payload: NodePayload = None
    run_ref: RunRef | None = None
    sidecar_ref: SidecarRef | None = None
    child_manifest: ChildManifest | None = None


@dataclass(frozen=True, slots=True)
class Run:
    run_ref: RunRef
    provider_id: str
    model: str
    reasoning_effort: str | None
    runner: str


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class Approval:
    approval_ref: ApprovalRef
    subject: str
    summary: str
    risk_scope: str
    state: ApprovalState


@dataclass(frozen=True, slots=True)
class Sidecar:
    sidecar_ref: SidecarRef
    panel_kind: str
    status: str
    payload: dict[str, object] = field(default_factory=dict)
