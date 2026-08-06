// TypeScript mirror of the backend Chat Surface Contract wire shapes.
//
// Source of truth (read these before changing anything here):
//   backend/surface_contract/identity.py   — SnapshotIdentity, cursors, ProjectionResult
//   backend/surface_contract/nodes.py      — NodeKind (24 kinds) + per-kind payloads
//   backend/surface_contract/frames.py     — live-plane ChatFrame / ControlFrame
//   backend/surface_contract/chat_surface.py — CompactSessionSnapshot / OlderPage / SearchMatch
//   backend/adapters/serialize.py          — to_wire (dataclass -> dict, enum -> value)
//   backend/adapter_api.py                 — REST envelope + WS frame `type` naming
//
// `to_wire` recurses a dataclass into `{field: to_wire(value)}` with no
// discriminant tag, so `Node.payload`'s concrete shape is only knowable via
// `Node.kind` (see mapToRenderModel.ts, which is the only place that casts
// `payload` to a concrete payload interface).
//
// This module is a pure type/decoder layer — no fetch, no WebSocket, no UI.

export const CONTRACT_VERSION = 1;

export type SessionId = string;
export type SurfaceId = string;
export type NodeId = string;
export type TurnId = string;
export type RunRef = string;
export type SidecarRef = string;
export type ApprovalRef = string;

// ---- identity.py ----------------------------------------------------------

export interface SnapshotIdentity {
  incarnation: string;
  render_rev: number;
  hist_rev: number;
}

export interface SurfaceCursor {
  surface_id: SurfaceId;
  incarnation: string;
  render_rev: number;
}

export type Focus = "opened" | "warm";

export interface SessionSelectorsWire {
  provider_id: string | null;
  runtime_profile_id: string | null;
  model: string | null;
  reasoning_effort: string | null;
  orchestration_mode: string | null;
  cwd: string | null;
}

// REST envelope discriminant. `_result_body` in adapter_api.py flattens a
// dataclass value's fields directly onto the envelope object; a list/tuple
// value is wrapped under `value` instead (see decode helpers in client.ts,
// which pick the right shape per endpoint).
export type ProjectionKind = "ok" | "rebuilding" | "stale_cursor";

export interface RebuildingEnvelope {
  kind: "rebuilding";
  retry_after_ms: number | null;
}

export interface StaleCursorEnvelope {
  kind: "stale_cursor";
}

// ---- nodes.py ---------------------------------------------------------

export type NodeKindWire =
  | "instruction_widget"
  | "turn"
  | "typed_prompt"
  | "explanation"
  | "assistant_text"
  | "thinking"
  | "tool_interaction"
  | "worker_interaction"
  | "steering_message"
  | "native_subagent_turn"
  | "worker_turn"
  | "sub_session_turn"
  | "session_turn"
  | "model_change"
  | "harness_change"
  | "result"
  | "compaction"
  | "continuation_session"
  | "failure"
  | "diagnostic"
  | "user_interaction"
  | "lifecycle_notice"
  | "fact"
  | "unknown";

export type ContentStatusWire =
  | "queued"
  | "streaming"
  | "partial"
  | "complete"
  | "failed"
  | "stopped";

export type SendModeWire = "queue" | "interrupt" | "steer";
export type PromptOriginWire = "user" | "queued" | "offline_sync" | "ask" | "supervisor";
export type ResultKindWire = "provider" | "derived";
export type DiagnosticCodeWire = "execution_continuation" | "other";
export type ModelChangeSourceWire = "user" | "provider";
export type CompactionOriginWire = "native" | "better_agent";
export type UserInteractionStateWire = "pending" | "resolved" | "cancelled";
export type LifecycleNoticeKindWire =
  | "retrying"
  | "detached"
  | "recovering"
  | "auto_retried"
  | "rate_limited";

export interface AttachmentWire {
  name: string;
  media_type: string;
  ref: string;
}

export interface TypedPromptPayloadWire {
  text: string;
  attachments: AttachmentWire[];
  send_mode: SendModeWire;
  origin: PromptOriginWire;
  source_session_ref: string | null;
  sent_text: string | null;
  intent_id: string | null;
}

export interface InstructionWidgetPayloadWire {
  text: string;
  action: Record<string, unknown> | null;
}

export interface AssistantTextPayloadWire {
  text: string;
}

export interface ThinkingPayloadWire {
  text: string;
  redacted: boolean;
}

export interface ToolInteractionPayloadWire {
  tool_name: string;
  args: Record<string, unknown>;
  result: { output?: string } & Record<string, unknown> | null;
  approval_ref: ApprovalRef | null;
  ui_kind: string | null;
  derived_view: string | null;
}

export interface SteeringMessagePayloadWire {
  text: string;
  target: string;
}

export interface ModelChangePayloadWire {
  // Populated from the raw provider `fallback` block's `from.model`/
  // `to.model` (backend/adapters/normalize.py::_assistant_block_node) —
  // display model name strings, not an opaque RunRef requiring a
  // separate `runs[]` lookup, despite the `RunRef` field typing.
  from_run_ref: RunRef | null;
  to_run_ref: RunRef;
  source: ModelChangeSourceWire;
}

export interface HarnessChangePayloadWire {
  from_harness_profile_id: string | null;
  to_harness_profile_id: string;
}

export interface WorkerInteractionPayloadWire {
  fact_kind: "worker_start" | "worker_event" | "worker_complete" | string;
  fact: Record<string, unknown>;
}

export interface ResultPayloadWire {
  result_kind: ResultKindWire;
  text: string | null;
  is_error: boolean;
}

export interface CompactionPayloadWire {
  origin: CompactionOriginWire;
  summary: string;
  replaced_node_ids: NodeId[];
}

export interface ContinuationSessionPayloadWire {
  execution_ref: string;
  chain_depth: number;
  summary: string | null;
}

export interface FailurePayloadWire {
  code: string;
  text: string;
  data: Record<string, unknown> | null;
}

export interface DiagnosticPayloadWire {
  severity: string;
  code: DiagnosticCodeWire;
  text: string;
  data: Record<string, unknown> | null;
}

export interface UserInteractionPayloadWire {
  kind: string;
  request: Record<string, unknown>;
  state: UserInteractionStateWire;
  response: Record<string, unknown> | null;
}

export interface LifecycleNoticePayloadWire {
  kind: LifecycleNoticeKindWire;
  data: Record<string, unknown> | null;
}

export interface FactPayloadWire {
  kind: string;
  data: Record<string, unknown>;
}

export interface UnknownPayloadWire {
  label: string;
  payload: Record<string, unknown>;
}

/** Union of every possible `Node.payload` wire shape. Narrow by
 * `Node.kind` (see mapToRenderModel.ts) — there is no discriminant tag. */
export type NodePayloadWire =
  | InstructionWidgetPayloadWire
  | TypedPromptPayloadWire
  | AssistantTextPayloadWire
  | ThinkingPayloadWire
  | ToolInteractionPayloadWire
  | WorkerInteractionPayloadWire
  | SteeringMessagePayloadWire
  | ModelChangePayloadWire
  | HarnessChangePayloadWire
  | ResultPayloadWire
  | CompactionPayloadWire
  | ContinuationSessionPayloadWire
  | FailurePayloadWire
  | DiagnosticPayloadWire
  | UserInteractionPayloadWire
  | LifecycleNoticePayloadWire
  | FactPayloadWire
  | UnknownPayloadWire
  | null;

export interface ChildManifestWire {
  renderable_child_count: number;
  has_children: boolean;
}

export interface TargetRefWire {
  session_id: string;
  turn_id: TurnId;
}

export interface NodeWire {
  cv: number;
  node_id: NodeId;
  parent_id: NodeId | null;
  turn_id: TurnId;
  surface_id: SurfaceId;
  kind: NodeKindWire;
  ts: number;
  seq: number;
  status: ContentStatusWire | null;
  payload: NodePayloadWire;
  run_ref: RunRef | null;
  sidecar_ref: SidecarRef | null;
  target_ref: TargetRefWire | null;
  child_manifest: ChildManifestWire | null;
}

export interface RunWire {
  run_ref: RunRef;
  provider_id: string;
  account_name: string | null;
  model: string;
  reasoning_effort: string | null;
  runner: string;
}

export type ApprovalStateWire = "pending" | "approved" | "denied";

export interface ApprovalWire {
  approval_ref: ApprovalRef;
  subject: string;
  summary: string;
  risk_scope: string;
  state: ApprovalStateWire;
}

export interface SidecarWire {
  sidecar_ref: SidecarRef;
  panel_kind: string;
  status: string;
  payload: Record<string, unknown>;
}

// ---- chat_surface.py ----------------------------------------------------

export interface CompactTurnWire {
  turn: NodeWire;
  prompt: NodeWire | null;
  results: NodeWire[];
  manifest: ChildManifestWire;
  runtime_change: NodeWire | null;
}

export interface CompactSessionSnapshotWire {
  session_id: SessionId;
  surface_id: SurfaceId;
  instruction_widget: NodeWire | null;
  turns: CompactTurnWire[];
  live_turn_nodes: NodeWire[];
  runs: RunWire[];
  // Opaque `?cursor=` token (adapter_api._encode_cursor), or null when
  // there is no older page.
  older_cursor: string | null;
}

export interface OlderPageWire {
  turns: CompactTurnWire[];
  runs: RunWire[];
  older_cursor: string | null;
}

export interface SearchMatchWire {
  turn_id: string;
  node_id: NodeId;
  path: NodeId[];
}

/** `kind: "ok"` envelope for a dataclass-valued endpoint (snapshot, older):
 * the value's fields are flattened directly onto the envelope. */
export type OkDataclassEnvelope<T> = T & {
  kind: "ok";
  snapshot_identity: SnapshotIdentity;
};

/** `kind: "ok"` envelope for a list-valued endpoint (children, search):
 * the list is wrapped under `value`. */
export interface OkListEnvelope<T> {
  kind: "ok";
  value: T[];
  snapshot_identity: SnapshotIdentity;
}

export type SnapshotEnvelope =
  | OkDataclassEnvelope<CompactSessionSnapshotWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type ChildrenEnvelope =
  | OkListEnvelope<NodeWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type OlderEnvelope =
  | OkDataclassEnvelope<OlderPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type SearchEnvelope =
  | OkListEnvelope<SearchMatchWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

// ---- frames.py (live plane) ------------------------------------------

export type TurnPhaseWire =
  | "queued"
  | "starting"
  | "running"
  | "awaiting_approval"
  | "reconnecting"
  | "stopping"
  | "completed"
  | "stopped"
  | "failed";

export type TerminalReasonWire =
  | "ok"
  | "user_stopped"
  | "provider_error"
  | "unknown_after_recovery";

export interface UsageWire {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

interface FrameBaseWire {
  cv: number;
  surface_id: SurfaceId;
  snapshot: SnapshotIdentity;
}

export interface NodeUpsertFrame extends FrameBaseWire {
  type: "node_upsert";
  node: NodeWire;
}

export interface TextDeltaFrame extends FrameBaseWire {
  type: "text_delta";
  node_id: NodeId;
  appended_text: string;
}

export interface NodeStatusFrame extends FrameBaseWire {
  type: "node_status";
  node_id: NodeId;
  status: ContentStatusWire;
}

export interface TurnLifecycleFrame extends FrameBaseWire {
  type: "turn_lifecycle";
  turn_id: TurnId;
  phase: TurnPhaseWire;
  reason: TerminalReasonWire | null;
  usage: UsageWire | null;
}

export interface RunUpsertFrame extends FrameBaseWire {
  type: "run_upsert";
  run: RunWire;
}

export interface ApprovalUpsertFrame extends FrameBaseWire {
  type: "approval_upsert";
  approval: ApprovalWire;
}

export interface SidecarUpsertFrame extends FrameBaseWire {
  type: "sidecar_upsert";
  sidecar: SidecarWire;
}

export interface SessionStateFrame extends FrameBaseWire {
  type: "session_state";
  intent_id: string | null;
  title: string | null;
  markers: string[] | null;
  selectors: SessionSelectorsWire | null;
}

export interface NoticeFrame extends FrameBaseWire {
  type: "notice";
  scope: string;
  payload: Record<string, unknown>;
}

// Not FrameBase-shaped (no `snapshot`) — a resubscribe with a stale
// `incarnation` gets this instead of NodeUpsert replay (chat_adapter.py
// `subscribe`). The client's response is always "refetch the snapshot".
export interface ResyncRequiredFrame {
  type: "resync_required";
  cv: number;
  surface_id: SurfaceId;
}

export type ChatFrame =
  | NodeUpsertFrame
  | TextDeltaFrame
  | NodeStatusFrame
  | TurnLifecycleFrame
  | RunUpsertFrame
  | ApprovalUpsertFrame
  | SidecarUpsertFrame
  | SessionStateFrame
  | NoticeFrame
  | ResyncRequiredFrame;

/** WS subscribe message shape the client sends (adapter_api.py
 * `_parse_surface_cursors`). */
export interface SurfaceSubscribeMessage {
  surfaces: SurfaceCursor[];
  focus?: Focus;
}
