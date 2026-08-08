"""Content-plane node vocabulary (ADR 0006 §1) — the closed, versioned
serialization of the chat-panel.md grammar. Structural kinds carry no
status; lifecycle frames are their single state authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from backend.surface_contract.identity import (
    ApprovalRef as InteractionRef,
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
    # Atomic rewind-latest-user-and-resend (or, if the target prompt is
    # still queued, an in-place queued update — same underlying mutation
    # `edit_queued` uses, see `backend/orchestrator.py`'s
    # `update_queued`/`update_latest_queued`). Distinct from `edit_queued`
    # on the wire; the two converge on one implementation server-side.
    ALTER = "alter"


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
    # -> UserInteraction resource (interaction.kind in {approval, input,
    # choice}) that pauses/paused this tool call, when one exists. None
    # is the honest default — no producer currently stamps this on a raw
    # journal row (see chat_index._reconstruct_payload); never guessed.
    interaction_ref: InteractionRef | None = None
    ui_kind: str | None = None
    derived_view: str | None = None


@dataclass(frozen=True, slots=True)
class SteeringMessagePayload:
    text: str
    target: str
    # Same `Attachment` model as `TypedPromptPayload.attachments` — a
    # steer prompt's inline images (`orchestrator.steer_active_turn`
    # already saves them via `_save_message_images` the same way a normal
    # send does; see `normalize._handle_steer_prompt`). Files are not
    # modeled here, matching `TypedPromptPayload`'s own current scope
    # (image-only attachments) — no asymmetry introduced.
    attachments: tuple[Attachment, ...] = ()


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


# WORKER_TURN/SUB_SESSION_TURN/SESSION_TURN's entity label — the delegated
# task's free-text description (legacy `worker_description`, threaded from
# every `worker_start` fact's `data.worker_description`). NATIVE_SUBAGENT_
# TURN never carries this (its payload stays None — Claude's own transcript
# format has no separate delegation-description concept to source it from);
# this payload is used by the other 3 SUBAGENT_TURN_KINDS members only.
@dataclass(frozen=True, slots=True)
class SubAgentTurnPayload:
    label: str | None
    # True iff this container was born from a `*_created` panel_kind fact
    # (`orchestrator.emit_session_created_panel`'s `worker_start.data.
    # is_new: True` — the target session already existed; nothing ran, no
    # `worker_complete` will ever follow). Legacy rendered this as a
    # distinct "created" panel; structural kinds carry no `status` (this
    # module's own rule), so the render-time signal lives here instead —
    # a payload field, not a new NodeKind (no behavioral distinction beyond
    # display exists between e.g. a `sub_session`/`sub_session_created`
    # panel). False (default) for every other producer — never guessed,
    # sourced verbatim from the fact's own `is_new` when present.
    created: bool = False


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


class UserInteractionKind(StrEnum):
    APPROVAL = "approval"  # approve/deny (tool-use, worker-creation)
    CHOICE = "choice"  # pick-among-candidates (delegation picker)
    INPUT = "input"  # free-form (request-user-input, memory proposals)


class UserInteractionState(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


# Interaction-ref namespace prefixes — the single source both the producer
# side (chat_adapter.py/store_access.py) and the resolve-routing side
# (surface_commands.py) key off of to route a UserInteraction back to its
# one owning legacy store; the 3 legacy mechanisms share no id space of
# their own (tool_approval.registry's approval_id, pending_approvals'
# delegation_id, and session_bridge's own separately-namespaced
# delegation_id can collide as raw strings).
TOOL_APPROVAL_REF_PREFIX = "tool_approval:"
WORKER_APPROVAL_REF_PREFIX = "worker_approval:"
DELEGATE_CHOICE_REF_PREFIX = "delegate_choice:"
USER_INPUT_REF_PREFIX = "user_input:"


def interaction_fact_payload(
    interaction_ref: InteractionRef,
    kind: UserInteractionKind,
    request: dict[str, object],
    *,
    state: UserInteractionState = UserInteractionState.PENDING,
    response: dict[str, object] | None = None,
) -> dict[str, object]:
    """The one payload shape every `interaction.fire.requested`/
    `interaction.fire.resolved` bus-fact producer builds (tool_approvals_api.py,
    pending_approvals_api.py, surface_commands.py's `resolve_interaction`) —
    `chat_adapter._on_interaction_fact` maps it straight onto a
    `UserInteraction`, field-for-field. Kept here (not duplicated per
    producer) since it's pure dict construction over already-defined
    contract types, not a bus/transport concern."""
    payload: dict[str, object] = {
        "interaction_ref": interaction_ref, "kind": kind.value,
        "request": request, "state": state.value,
    }
    if response is not None:
        payload["response"] = response
    return payload


# UI that requests user input (tool/worker-creation approval, delegation
# choice, request-user-input…) — content-node render anchor ONLY; carries
# no payload of its own beyond the ref, resolves via the UserInteraction
# resource below (single source: kind/request/state/response live on the
# resource, never duplicated onto the node).
@dataclass(frozen=True, slots=True)
class UserInteractionPayload:
    interaction_ref: InteractionRef


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


# Single source of truth for a turn/panel's token-usage overlay — used by
# both TurnLifecycle (frames.py, turn-level) and Node.usage below
# (SubAgentTurn-family, panel-level). `frames.py` re-exports this name
# rather than defining its own copy (it already depends on this module).
@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    # Cache-token counts (Anthropic-style `usage.cache_creation_input_tokens`
    # / `usage.cache_read_input_tokens`, normalized turn-wide by
    # `trace_collector._normalize_token_usage`) — legacy's "Cache: Z"
    # summary segment (`MessageBubble.tsx`'s `CompleteEvent`) reads
    # `cache_read_input_tokens`. None when the provider result carried no
    # usage dict at all (never a guessed/zero placeholder).
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


def usage_from_raw(raw: object) -> Usage | None:
    """Parses `trace_collector._normalize_token_usage`'s dict shape
    (`input_tokens`/`output_tokens`/`cache_creation_input_tokens`/
    `cache_read_input_tokens`, always ints once present) into a typed
    `Usage` — the ONE parsing rule shared by every raw-dict usage source
    in the codebase: a live turn-lifecycle event's `payload["usage"]`
    (`chat_adapter._on_lifecycle`) and a journaled `worker_complete`
    fact's `data["token_usage"]` (`derive`'s SubAgentTurn-family
    construction) are the SAME dict shape, so they share this ONE
    conversion rather than each re-implementing it. `total_tokens` isn't a
    provider-reported field; derived here as input+output (cache tokens
    are informational context-window occupancy, not newly-generated
    tokens). None when `raw` isn't a dict, or carries no usage at all —
    never a guessed/zero Usage."""
    if not isinstance(raw, dict):
        return None
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    input_tokens = input_tokens if isinstance(input_tokens, int) else None
    output_tokens = output_tokens if isinstance(output_tokens, int) else None
    cache_creation = raw.get("cache_creation_input_tokens")
    cache_read = raw.get("cache_read_input_tokens")
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens,
        cache_creation_input_tokens=cache_creation if isinstance(cache_creation, int) else None,
        cache_read_input_tokens=cache_read if isinstance(cache_read, int) else None,
    )


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
    | SubAgentTurnPayload
    | None
)


# Backend-issued address of an embedded turn in another session; never
# client-suppliable, authorization-checked on every access (ADR 0006 §0).
# `turn_id` is None when only the target SESSION is known and no specific
# turn within it is resolvable yet — for the SubAgentTurn family
# (derive.build_subagent_panel_container), populated from the delegation's
# own `worker_complete` fact's `target_turn_id` once the ask/delegate_task
# team-message flow resolves it (orchestrator.py's `_team_message_target_
# turn_id`, via the target session's own `user_msg["id"]` — the SAME id
# space as this session's own TYPED_PROMPT node_id); still None for a
# still-running delegation, a `*_created` panel (nothing dispatched), or
# the Task-tool worker flow (no correlator threaded there yet) — a caller
# with only `session_id` can still render a session-open link; never
# fabricate a turn_id to fill it.
@dataclass(frozen=True, slots=True)
class TargetRef:
    session_id: str
    turn_id: TurnId | None = None


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
    # Panel-level token-usage overlay (chat-panel.md: "usage ... overlay
    # over any SubAgentTurn") — populated only for SUBAGENT_TURN_KINDS
    # members once their owning delegation reaches a terminal fact; None
    # otherwise (never a guessed/zero Usage). See `usage_from_raw`.
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class Run:
    run_ref: RunRef
    provider_id: str
    account_name: str | None
    model: str
    reasoning_effort: str | None
    runner: str


# Session-scoped cross-cutting resource (ADR 0006 §5) — the parent of every
# UserInteraction kind. `request`/`response` are kind-shaped dicts (subject/
# summary/risk_scope for approval; candidates/picked_ref for choice; schema/
# response for input) rather than a per-kind dataclass union: one resource
# shape, one push frame (`UserInteractionUpsert`), one resolve intent,
# matching the "one plane, one mechanism" ruling this type family exists to
# implement.
@dataclass(frozen=True, slots=True)
class UserInteraction:
    interaction_ref: InteractionRef
    kind: UserInteractionKind
    request: dict[str, object]
    state: UserInteractionState
    response: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Sidecar:
    sidecar_ref: SidecarRef
    panel_kind: str
    status: str
    payload: dict[str, object] = field(default_factory=dict)
