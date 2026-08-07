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
    INSTRUCTION_WIDGET = "instruction_widget"
    TURN = "turn"
    TYPED_PROMPT = "typed_prompt"
    EXPLANATION = "explanation"
    ASSISTANT_TEXT = "assistant_text"
    THINKING = "thinking"
    TOOL_INTERACTION = "tool_interaction"
    WORKER_INTERACTION = "worker_interaction"
    STEERING_MESSAGE = "steering_message"
    NATIVE_SUBAGENT_TURN = "native_subagent_turn"
    WORKER_TURN = "worker_turn"
    SUB_SESSION_TURN = "sub_session_turn"
    SESSION_TURN = "session_turn"
    MODEL_CHANGE = "model_change"
    HARNESS_CHANGE = "harness_change"
    RESULT = "result"
    COMPACTION = "compaction"
    CONTINUATION_SESSION = "continuation_session"
    FAILURE = "failure"
    DIAGNOSTIC = "diagnostic"
    USER_INTERACTION = "user_interaction"
    LIFECYCLE_NOTICE = "lifecycle_notice"
    FACT = "fact"
    UNKNOWN = "unknown"


STRUCTURAL_KINDS = frozenset(
    {
        NodeKind.TURN,
        NodeKind.EXPLANATION,
        NodeKind.NATIVE_SUBAGENT_TURN,
        NodeKind.WORKER_TURN,
        NodeKind.SUB_SESSION_TURN,
        NodeKind.SESSION_TURN,
        NodeKind.RESULT,
    }
)

# SubAgentTurn family: one render contract, four sourcing modes.
SUBAGENT_TURN_KINDS = frozenset(
    {
        NodeKind.NATIVE_SUBAGENT_TURN,
        NodeKind.WORKER_TURN,
        NodeKind.SUB_SESSION_TURN,
        NodeKind.SESSION_TURN,
    }
)

# RuntimeChanged family: boundary nodes rendered before their affected turn.
RUNTIME_CHANGED_KINDS = frozenset({NodeKind.MODEL_CHANGE, NodeKind.HARNESS_CHANGE})


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
    ASK = "ask"
    SUPERVISOR = "supervisor"


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
    # Byte size when cheaply derivable at normalize time (file_attachment
    # metadata rows carry it directly; decoded image-block length otherwise).
    # None when not derivable — never a guessed/zero placeholder.
    size: int | None = None


# Always the originator's verbatim text — never backend-synthesized fixed
# text (that is instruction_widget).
@dataclass(frozen=True, slots=True)
class TypedPromptPayload:
    text: str
    attachments: tuple[Attachment, ...] = ()
    send_mode: SendMode = SendMode.QUEUE
    origin: PromptOrigin = PromptOrigin.USER
    source_session_ref: str | None = None  # set when origin=ask
    # Actually-dispatched prompt when harness wrapping diverges from text;
    # rendered on demand only (full-prompt affordance), never inline.
    sent_text: str | None = None
    intent_id: str | None = None


@dataclass(frozen=True, slots=True)
class InstructionWidgetPayload:
    text: str
    action: dict[str, object] | None = None  # forward-compat {kind, label, payload}


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


# User-initiated only: harness_profile_id never changes mid-turn without
# user action.
@dataclass(frozen=True, slots=True)
class HarnessChangePayload:
    from_harness_profile_id: str | None
    to_harness_profile_id: str


@dataclass(frozen=True, slots=True)
class WorkerInteractionPayload:
    fact_kind: str  # worker_start | worker_event | worker_complete
    fact: dict[str, object]


@dataclass(frozen=True, slots=True)
class ResultPayload:
    result_kind: ResultKind
    text: str | None = None
    is_error: bool = False


class CompactionOrigin(StrEnum):
    NATIVE = "native"
    BETTER_AGENT = "better_agent"


@dataclass(frozen=True, slots=True)
class CompactionPayload:
    origin: CompactionOrigin
    summary: str
    replaced_node_ids: tuple[NodeId, ...] = ()


# Fresh provider execution continuing the same app session's turn.
@dataclass(frozen=True, slots=True)
class ContinuationSessionPayload:
    execution_ref: str
    chain_depth: int
    summary: str | None = None


class FailureSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FailureResolution(StrEnum):
    NONE = "none"
    RETRY = "retry"
    FIX_CREDENTIAL = "fix_credential"
    CHOOSE_FALLBACK = "choose_fallback"
    OPEN_SETTINGS = "open_settings"


@dataclass(frozen=True, slots=True)
class FailurePayload:
    code: str
    text: str
    data: dict[str, object] | None = None
    severity: FailureSeverity = FailureSeverity.ERROR
    retryable: bool = False
    resolution: FailureResolution = FailureResolution.NONE


@dataclass(frozen=True, slots=True)
class DiagnosticPayload:
    severity: str
    code: DiagnosticCode
    text: str
    data: dict[str, object] | None = None


class UserInteractionState(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


# UI that requests user input (tool/worker-creation approval, credential
# consent, memory proposal, request-user-input…). kind-extensible.
@dataclass(frozen=True, slots=True)
class UserInteractionPayload:
    kind: str
    request: dict[str, object]
    state: UserInteractionState = UserInteractionState.PENDING
    response: dict[str, object] | None = None


class LifecycleNoticeKind(StrEnum):
    RETRYING = "retrying"
    DETACHED = "detached"
    RECOVERING = "recovering"
    AUTO_RETRIED = "auto_retried"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class LifecycleNoticePayload:
    kind: LifecycleNoticeKind
    data: dict[str, object] | None = None


# Backend-synthesized structured fact rendered as a compact chip
# (e.g. kind="pr_link": PR number + repository).
@dataclass(frozen=True, slots=True)
class FactPayload:
    kind: str
    data: dict[str, object]


# Forward-compat sink for anything not recognized (yet).
@dataclass(frozen=True, slots=True)
class UnknownPayload:
    label: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class ChildManifest:
    renderable_child_count: int
    has_children: bool


NodePayload = (
    InstructionWidgetPayload
    | TypedPromptPayload
    | AssistantTextPayload
    | ThinkingPayload
    | ToolInteractionPayload
    | WorkerInteractionPayload
    | SteeringMessagePayload
    | ModelChangePayload
    | HarnessChangePayload
    | ResultPayload
    | CompactionPayload
    | ContinuationSessionPayload
    | FailurePayload
    | DiagnosticPayload
    | UserInteractionPayload
    | LifecycleNoticePayload
    | FactPayload
    | UnknownPayload
    | None
)


# Backend-issued address of an embedded turn in another session; never
# client-suppliable, authorization-checked on every access (ADR 0006 §0).
@dataclass(frozen=True, slots=True)
class TargetRef:
    session_id: str
    turn_id: TurnId


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
    target_ref: TargetRef | None = None
    child_manifest: ChildManifest | None = None


@dataclass(frozen=True, slots=True)
class Run:
    run_ref: RunRef
    provider_id: str
    account_name: str | None
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
