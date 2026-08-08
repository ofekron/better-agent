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
// Renamed from the backend's now-deleted `ApprovalRef` (ADR 0006 §5's
// unified UserInteraction resource, Package A backend pass) — namespace-
// prefixed per mechanism (`tool_approval:`/`worker_approval:`/
// `delegate_choice:`/`user_input:`, see backend/surface_contract/nodes.py's
// `*_REF_PREFIX` constants), never a raw bearer token.
export type InteractionRef = string;

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

// "alter" mirrors backend `surface_contract/nodes.py`'s `SendMode.ALTER` —
// atomic rewind-latest-user-and-resend (or an in-place queued update when
// the target prompt is still queued), same string on both transports
// (legacy `/ws/chat`'s own "alter" send_mode and this v2 intent converge
// on the same `SurfaceCommandsImpl.send_prompt()` implementation
// server-side — backend/surface_commands.py).
export type SendModeWire = "queue" | "interrupt" | "steer" | "alter";
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
export type FailureSeverityWire = "info" | "warning" | "error";
export type FailureResolutionWire =
  | "none"
  | "retry"
  | "fix_credential"
  | "choose_fallback"
  | "open_settings";

export interface AttachmentWire {
  name: string;
  media_type: string;
  ref: string;
  size: number | null;
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
  // Renamed from `approval_ref` (backend `ToolInteractionPayload.
  // interaction_ref`, Package A backend pass) — same field, ADR 0006 §5
  // unified naming.
  interaction_ref: InteractionRef | null;
  ui_kind: string | null;
  derived_view: string | null;
}

export interface SteeringMessagePayloadWire {
  text: string;
  target: string;
  attachments: AttachmentWire[];
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
  severity: FailureSeverityWire;
  retryable: boolean;
  resolution: FailureResolutionWire;
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

/** `worker_turn` | `sub_session_turn` | `session_turn` payload — mirrors
 * backend's `SubAgentTurnPayload` (surface_contract/nodes.py). Real
 * delegated-task description (legacy's `worker_description`), sourced
 * from every fact of the delegation group, not just `worker_start` — a
 * group missing its own `worker_start` (live/mid-stream) still resolves
 * it. `null` for a container the backend can't yet label (e.g. no
 * dedicated `_created`-variant signal exists on the wire — see
 * Container.tsx's kind-label fallback). `native_subagent_turn` never
 * carries this payload (backend never constructs one for it). */
export interface SubAgentTurnPayloadWire {
  label: string | null;
  // True iff this container was born from a `*_created` panel_kind fact
  // (backend's `SubAgentTurnPayload.created` — the target session already
  // existed, nothing ran, no `worker_complete` will ever follow). Legacy
  // rendered this as a distinct "created" panel variant
  // (`CollapsibleTimelineBlock`'s `created` prop / `.timeline-block-created`
  // CSS modifier, `isCreationPanelKind` in utils/mergeEvents.ts) — mirrored
  // here so Container.tsx's `SubAgentTurnView` can apply the same visual
  // treatment. `false` (default) for every other producer.
  created: boolean;
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
  | SubAgentTurnPayloadWire
  | null;

export interface ChildManifestWire {
  renderable_child_count: number;
  has_children: boolean;
}

/** Backend-issued, authorization-checked address of an embedded turn in
 * another session. `turn_id` is genuinely nullable on the wire today: no
 * producer can yet correlate a cross-session `TurnId` for the
 * SubAgentTurn family (backend traced and documented the gap rather than
 * fabricating a value — phase2-inventory.md's "target_ref.turn_id: honest
 * gap" section) — every current producer sends `turn_id: null`.
 * `session_id` alone is sufficient for the only thing the frontend does
 * with this today (`Container.tsx`'s `SubAgentTargetLink`, a
 * session-open link — never reads `.turn_id`). */
export interface TargetRefWire {
  session_id: string;
  turn_id: TurnId | null;
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
  /** Populated only for `worker_turn`/`sub_session_turn`/`session_turn`
   * nodes born from the ask/delegate_task flow, once the delegation
   * reaches `worker_complete` — `native_subagent_turn` and Task-tool-flow
   * `worker_turn` nodes stay `null` (no producer yet, backend-documented
   * gap). Optional (not `field: T | null` like this interface's other
   * fields) DELIBERATELY: a required field broke `src/surface/state.ts`'s
   * existing `NodeWire` object literal (excluded from this round's scope,
   * owned by send-path) — `?:` keeps every pre-existing literal
   * source-compatible (an omitted optional property reads as
   * `undefined`, never mistaken for a real `UsageWire`) while the wire
   * itself always sends the key (backend's `to_wire` serializes every
   * dataclass field regardless of Python-side default). */
  usage?: UsageWire | null;
}

export interface RunWire {
  run_ref: RunRef;
  provider_id: string;
  account_name: string | null;
  model: string;
  reasoning_effort: string | null;
  runner: string;
}

// ADR 0006 §5's unified UserInteraction resource — replaces the backend's
// now-deleted `Approval`/`ApprovalState` (Package A backend pass). One
// resource family for all 3 legacy approval/choice mechanisms plus the
// user-input/memory-proposal mechanism, keyed by `kind`; `request`/
// `response` are kind-shaped dicts (see backend/surface_contract/nodes.py's
// `UserInteraction` docstring for the exact per-kind shapes):
//   approval -> request {subject, summary, risk_scope}, response {decision, scope?}
//   choice   -> request {candidates, prompt?}, response {selected_ref}
//   input    -> request {schema, prompt?}, response {value} (free-form)
export type UserInteractionKindWire = "approval" | "choice" | "input";

export interface UserInteractionWire {
  interaction_ref: InteractionRef;
  kind: UserInteractionKindWire;
  request: Record<string, unknown>;
  state: UserInteractionStateWire;
  response: Record<string, unknown> | null;
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
  // Cold-hydration source for every currently-pending UserInteraction on
  // this session (ADR 0006 §5) — the RESOURCE plane, not a node in the
  // turn tree. Live updates arrive as `user_interaction_upsert` frames.
  interactions: UserInteractionWire[];
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
  // Covers a turn paused on any UserInteraction kind: approval, choice, or
  // input (ADR 0006 §5's TurnPhase rename, Package A backend pass).
  | "awaiting_interaction"
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
  cache_creation_input_tokens: number | null;
  cache_read_input_tokens: number | null;
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

// Renamed from the backend's now-deleted `ApprovalUpsertFrame`/
// `approval_upsert` (ADR 0006 §5, Package A backend pass) — one frame for
// every UserInteraction kind (approval/choice/input), not one per legacy
// mechanism.
export interface UserInteractionUpsertFrame extends FrameBaseWire {
  type: "user_interaction_upsert";
  user_interaction: UserInteractionWire;
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
  | UserInteractionUpsertFrame
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

// ---- intents.py (command plane) — client -> backend over the same
// `/ws/v2/surface` connection, wrapped as `{"intent": {...}}`
// (adapter_api.py `_handle_intent`/`_parse_send_prompt`). Only the intent
// kinds with a real client-side trigger point today are mirrored here —
// `Stop`/`Approve`/`SetSelectors`/`Rewind` exist backend-side
// (surface_contract/intents.py) but have no caller yet; add their wire
// shapes alongside the caller that first needs them. ------------------

export type SendTargetKindWire = "current" | "fork" | "new_session";

export interface SendTargetWire {
  kind: SendTargetKindWire;
  fork_node_id: NodeId | null;
}

/** Outgoing attachment reference — `Attachment` (surface_contract/nodes.py)
 * has no `size` field, unlike the wire-received `AttachmentWire` above. */
export interface AttachmentInputWire {
  name: string;
  media_type: string;
  ref: string;
}

export interface SendPromptIntentWire {
  kind: "send_prompt";
  cv: number;
  intent_id: string;
  session_id: SessionId | null;
  text: string;
  attachments: AttachmentInputWire[];
  send_mode: SendModeWire;
  target: SendTargetWire;
}

/** `EditQueued`/`DeleteQueued` (surface_contract/intents.py) — `node_id` is
 * NOT a Contract graph `NodeId` despite the field name: it's `session_manager`'s
 * queued-prompt id (same string legacy's `queuedPrompt.id` /
 * `onCancelQueued(queuedId)` already use — `backend/surface_commands.py`'s
 * `edit_queued`/`delete_queued` thread it straight into
 * `session_manager.update_queued_prompt`/`remove_queued_prompt`). The native
 * surface has no live projection of a queued prompt in the turn tree today
 * (queued rows are dropped in `adapters/normalize.py`) — these intents only
 * replace the WIRE TRANSPORT the legacy queued-banner's edit/delete actions
 * go over when eligible; the banner's own data model stays legacy-owned
 * (see components/Chat.tsx's queued-transport wiring). */
export interface EditQueuedIntentWire {
  kind: "edit_queued";
  cv: number;
  intent_id: string;
  session_id: SessionId | null;
  node_id: string;
  text: string;
}

export interface DeleteQueuedIntentWire {
  kind: "delete_queued";
  cv: number;
  intent_id: string;
  session_id: SessionId | null;
  node_id: string;
}

// `ResolveInteraction` (ADR 0006 §5) — one intent for all 4 legacy
// mechanisms (tool/worker approval, delegation choice, user-input/memory
// proposal), keyed by `interaction_kind`. `interaction_kind`, NOT `kind`:
// this intent's own dispatch selector ("resolve") already owns the
// top-level `kind` key (backend/adapter_api.py's `_parse_resolve_interaction`
// — same rename rationale as ProviderIntent's `provider_kind`). `response`
// is a nested object whose shape depends on `interaction_kind`:
//   approval -> { decision: "approve" | "deny" }
//   choice   -> { picked_ref: string | null }
//   input    -> { response: Record<string, unknown> } (free-form, double-
//                nested — matches backend's `InputResponse.response` field
//                literally, verified against `_parse_resolve_interaction`)
export interface ApprovalResolveResponseWire {
  decision: "approve" | "deny";
}

export interface ChoiceResolveResponseWire {
  picked_ref: string | null;
}

export interface InputResolveResponseWire {
  response: Record<string, unknown>;
}

export type ResolveResponseWire =
  | ApprovalResolveResponseWire
  | ChoiceResolveResponseWire
  | InputResolveResponseWire;

export interface ResolveInteractionIntentWire {
  kind: "resolve";
  cv: number;
  intent_id: string;
  session_id: SessionId;
  interaction_ref: InteractionRef;
  interaction_kind: UserInteractionKindWire;
  response: ResolveResponseWire;
}

export type ChatIntentWire =
  | SendPromptIntentWire
  | EditQueuedIntentWire
  | DeleteQueuedIntentWire
  | ResolveInteractionIntentWire;

// ---- intents.py TransportAck — backend -> client, over the same
// connection, in direct response to a submitted intent (adapter_api.py
// `_handle_intent`). NOT part of `ChatFrame`/`FrameBaseWire`: an ack has
// no `snapshot`/`cv` — it is a synchronous accept/reject of the intent
// itself, not a projection fact (surface_contract/intents.py's own
// module docstring). Matched to the request by `intent_id`.

export interface IntentAcceptedFrame {
  type: "intent_accepted";
  intent_id: string;
}

export interface IntentRejectedFrame {
  type: "intent_rejected";
  intent_id: string;
  code: string;
  message: string;
}

export type TransportAckFrame = IntentAcceptedFrame | IntentRejectedFrame;

export function isTransportAckFrame(value: { type: string }): value is TransportAckFrame {
  return value.type === "intent_accepted" || value.type === "intent_rejected";
}

// ---- provider_config_surface.py / descriptors.py (ADR 0007, Package D) --
//
// Non-cursor live plane: pushed over the SAME `/ws/v2/surface` connection
// as ChatFrame, gated by `{"feeds": ["providers"]}` instead of a per-
// session `{"surfaces": [...]}` cursor (adapter_api.py's `_FEED_SURFACES`)
// — the same `FeedName`/`setFeeds` mechanism the `"runs"` feed below
// already uses. These frames carry no `snapshot`/`surface_id` (unlike
// FrameBaseWire) — ADR 0007 has no per-surface cursor concept, only a
// monotonic `cv` — so, like `RunsFrame`, they are dispatched via their own
// `SurfaceSocketHandlers.onProviderFrame` rather than folded into
// `ChatFrame`'s union (see client.ts).
//
// Wire `type` values are the backend's own `_frame_type_name` (CamelCase
// dataclass name -> snake_case, adapter_api.py:635-636) — note
// `LoginFlowFrame` -> "login_flow_frame", NOT "login_flow_state" (that
// name belongs to the nested `LoginFlowState` dataclass carried in the
// frame's `.state` field, verified against
// backend/scripts/test_adapter_provider_integration.py's own frame-type
// string assertions).

export type ConfigStateWire =
  | "active"
  | "suspended"
  | "credential_required"
  | "credential_failed"
  | "retrying";

export type AuthFlowWire = "oauth_subscription" | "api_key" | "none";

export type LoginPhaseWire =
  | "starting"
  | "awaiting_browser"
  | "awaiting_code"
  | "polling"
  | "completed"
  | "failed"
  | "cancelled";

export type FormFieldKindWire = "text" | "path" | "secret" | "enum" | "bool";

export interface FormFieldWire {
  name: string;
  kind: FormFieldKindWire;
  label_key: string;
  required: boolean;
  choices: string[];
  default: string | boolean | null;
  pattern: string | null;
  max_length: number | null;
  // Closure 4 (form-copy regression) — per-kind placeholder/hint i18n
  // keys restoring copy the deleted frontend `apiEnvCopyForKind`/
  // `configDirCopyForKind` used to vary per provider kind. `null` when a
  // field has no placeholder/hint copy.
  placeholder_key: string | null;
  hint_key: string | null;
}

export interface DisplayWire {
  label: string;
  icon_id: string;
  config_copy_key: string;
}

export interface ProviderCapabilitiesWire {
  fork: boolean;
  manager_mode: boolean;
  rewind: boolean;
  steering: boolean;
  native_subagents: boolean;
  reasoning_effort: boolean;
  usage_reporting: boolean;
  startup_monitoring: boolean;
}

export interface ProviderDescriptorWire {
  provider_id: string;
  display: DisplayWire;
  auth_flows: AuthFlowWire[];
  capabilities: ProviderCapabilitiesWire;
  orchestration_modes: string[];
  send_modes: string[];
  model_catalog_ref: string;
  config_state: ConfigStateWire;
}

export interface InstallableDescriptorWire {
  kind: string;
  display: DisplayWire;
  form_schema: FormFieldWire[];
  defaults: Record<string, unknown>;
  auth_flows: AuthFlowWire[];
}

export interface CatalogModelWire {
  model: string;
  runner: string;
  reasoning_efforts: string[];
  retired: boolean;
}

export interface ModelCatalogWire {
  provider_id: string;
  models: CatalogModelWire[];
}

export interface RuntimeProfileDescriptorWire {
  runtime_profile_id: string;
  provider_id: string;
  runner: string;
  default_model: string;
  default_reasoning_effort: string | null;
  // Closure 3 (RuntimeProfile v2 parity) additions.
  name: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

// Closure 3: display-only provider graveyard entry (mirrors
// descriptors.py's `DeletedProviderRef`/store_access's
// `DeletedProviderRecord`) — tombstoned profiles resolve their provider's
// display name through this, never through the live provider list.
export interface DeletedProviderRefWire {
  provider_id: string;
  name: string;
  nickname: string;
  kind: string;
  deleted_at: string | null;
}

/** Closure 3: the FULL runtime-profile plane snapshot
 * (`descriptors.py`'s `RuntimeProfilesSnapshot`) — `runtime_profiles()`'s
 * real return shape now (was a bare `RuntimeProfileDescriptorWire[]`).
 * `profiles` includes tombstones (`.deleted`), matching the legacy
 * `runtime_profiles_api._snapshot()`'s own `include_deleted=True` read. */
export interface RuntimeProfilesSnapshotWire {
  profiles: RuntimeProfileDescriptorWire[];
  default_runtime_profile_id: string | null;
  last_models: Record<string, string>;
  last_reasoning_efforts: Record<string, string>;
  deleted_providers: DeletedProviderRefWire[];
}

export interface LoginFlowStateWire {
  provider_id: string;
  intent_id: string;
  phase: LoginPhaseWire;
  data: Record<string, unknown> | null;
}

// REST envelopes (adapter_api.py's `_envelope` — ADR 0007 has no
// rebuilding/stale-cursor state, always `kind: "ok"`; a tuple-valued
// method wraps under `value`, a single-dataclass-valued method (only
// `model_catalog`) flattens its fields directly onto the envelope).
export interface ProviderListEnvelope {
  kind: "ok";
  value: ProviderDescriptorWire[];
}

export interface InstallableListEnvelope {
  kind: "ok";
  value: InstallableDescriptorWire[];
}

export type ModelCatalogEnvelope = ModelCatalogWire & { kind: "ok" };

/** Closure 3: `runtime_profiles()`'s REST envelope now flattens
 * `RuntimeProfilesSnapshotWire`'s fields directly onto the body (a
 * single-dataclass-valued method, adapter_api.py's `_envelope` — same
 * flattening convention `ModelCatalogEnvelope` already uses), NOT a
 * `{value: [...]}`-wrapped list anymore. */
export type RuntimeProfilesSnapshotEnvelope = RuntimeProfilesSnapshotWire & { kind: "ok" };

export interface ProviderUpsertFrame {
  type: "provider_upsert";
  cv: number;
  descriptor: ProviderDescriptorWire;
  intent_id: string | null;
}

export interface InstallableCatalogChangedFrame {
  type: "installable_catalog_changed";
  cv: number;
}

export interface CredentialStateFrame {
  type: "credential_state";
  cv: number;
  provider_id: string;
  config_state: ConfigStateWire;
}

export interface LoginFlowFrameWire {
  type: "login_flow_frame";
  cv: number;
  state: LoginFlowStateWire;
}

export interface ModelCatalogChangedFrame {
  type: "model_catalog_changed";
  cv: number;
  provider_id: string;
}

// Closure 2 (install progress v2) — mirrors `provider_setup._INSTALL_RUNS`'s
// own state vocabulary; no fourth value exists.
export type InstallStateWire = "running" | "succeeded" | "failed";

export interface InstallLineWire {
  stream: string;
  text: string;
}

/** Same shape `GET /api/provider-setup/installs` already returns per kind
 * for cold-start (the legacy REST route this frame's live push
 * complements, not replaces — see `useProviderInstalls.ts`). */
export interface InstallRunWire {
  kind: string;
  label: string;
  command: string;
  state: InstallStateWire;
  lines: InstallLineWire[];
  started_at: string | null;
  finished_at: string | null;
  returncode: number | null;
  installed: boolean | null;
  message: string | null;
}

export interface InstallProgressChangedFrame {
  type: "install_progress_changed";
  cv: number;
  run: InstallRunWire;
}

/** Closure 3: the dedicated runtime-profile-changed frame — replaces the
 * previous reuse of `model_catalog_changed` as the "only honest signal"
 * (see `runtime_profiles_api.py`'s `_broadcast_changed`), fired
 * unconditionally on every runtime-profile mutation path. */
export interface RuntimeProfilesChangedFrame {
  type: "runtime_profiles_changed";
  cv: number;
  snapshot: RuntimeProfilesSnapshotWire;
}

/** Union of every possible frame pushed under the `"providers"` feed.
 * `installable_catalog_changed` is part of the backend contract
 * (`descriptors.py`'s `ProviderFrame` union) but is never actually
 * broadcast anywhere in `provider_adapter.py` today (the installable
 * catalog is a static, code-defined table, not store-backed) — mirrored
 * here for completeness/forward-compat, not currently reachable.
 *
 * `IntentRejectedFrame` is ALSO a member (descriptors.py's `ProviderFrame`
 * union, since D4's intent-error-propagation fix): a mutation admitted
 * synchronously (`intent_accepted`) can still fail once its fire-and-
 * forget coroutine runs, and that failure is pushed as a SECOND, async
 * `intent_rejected` frame over this same feed — `client.ts`'s `onmessage`
 * forwards it to `onProviderFrame` specifically when no `submit()` call is
 * still waiting on that `intent_id` (see its own comment); it is
 * deliberately NOT added to `_PROVIDER_FRAME_TYPES`/`isProviderFrame`
 * below, since a `submit()`-pending `intent_rejected` must still resolve
 * through `SurfaceSocket.submit()`'s normal ack path first. */
export type ProviderFrame =
  | ProviderUpsertFrame
  | InstallableCatalogChangedFrame
  | CredentialStateFrame
  | LoginFlowFrameWire
  | ModelCatalogChangedFrame
  | InstallProgressChangedFrame
  | RuntimeProfilesChangedFrame
  | IntentRejectedFrame;

const _PROVIDER_FRAME_TYPES: ReadonlySet<string> = new Set([
  "provider_upsert",
  "installable_catalog_changed",
  "credential_state",
  "login_flow_frame",
  "model_catalog_changed",
  "install_progress_changed",
  "runtime_profiles_changed",
]);

export function isProviderFrame(value: { type: string }): value is ProviderFrame {
  return _PROVIDER_FRAME_TYPES.has(value.type);
}

// ---- intents.py ProviderIntent (command plane, over the same `{"intent":
// {...}}` envelope ChatIntentWire uses) — `session_id` is always `null`
// (IntentBase.session_id, ADR 0007 intents are session-less); `cv` mirrors
// SendPromptIntentWire's own CONTRACT_VERSION convention. Wire key
// "provider_kind" (NOT "kind") on `create_provider` avoids colliding with
// the transport's own dispatch-selector key, `data["kind"]`
// (adapter_api.py's `_PROVIDER_INTENT_PARSERS` comment). -----------------

export interface CreateProviderIntentWire {
  kind: "create_provider";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_kind: string;
  config: Record<string, unknown>;
}

export interface UpdateProviderIntentWire {
  kind: "update_provider";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
  config_patch: Record<string, unknown>;
}

export interface DeleteProviderIntentWire {
  kind: "delete_provider";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
}

export interface SuspendProviderIntentWire {
  kind: "suspend_provider";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
}

export interface ResumeProviderIntentWire {
  kind: "resume_provider";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
}

export interface RetryCredentialIntentWire {
  kind: "retry_credential";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
}

export interface BeginLoginIntentWire {
  kind: "begin_login";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
  flow: string;
}

export interface CancelLoginIntentWire {
  kind: "cancel_login";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
}

export interface RefreshModelsIntentWire {
  kind: "refresh_models";
  cv: number;
  intent_id: string;
  session_id: null;
  provider_id: string;
}

/** `profile` carries `runtime_profile_id` to patch an existing profile,
 * or omits it to create one (`runtime_profiles_api.py`'s
 * `save_runtime_profile` port method branches on the same key). */
export interface SaveRuntimeProfileIntentWire {
  kind: "save_runtime_profile";
  cv: number;
  intent_id: string;
  session_id: null;
  profile: Record<string, unknown>;
}

export interface DeleteRuntimeProfileIntentWire {
  kind: "delete_runtime_profile";
  cv: number;
  intent_id: string;
  session_id: null;
  runtime_profile_id: string;
}

export type ProviderIntentWire =
  | CreateProviderIntentWire
  | UpdateProviderIntentWire
  | DeleteProviderIntentWire
  | SuspendProviderIntentWire
  | ResumeProviderIntentWire
  | RetryCredentialIntentWire
  | BeginLoginIntentWire
  | CancelLoginIntentWire
  | RefreshModelsIntentWire
  | SaveRuntimeProfileIntentWire
  | DeleteRuntimeProfileIntentWire;

/** `Omit<Union, K>` does NOT distribute over a union in plain TypeScript —
 * `keyof (A | B)` is the INTERSECTION of each member's keys, so a plain
 * `Omit<ProviderIntentWire, "cv" | "intent_id" | "session_id">` collapses
 * to only the fields every one of the 11 variants shares (`kind`),
 * silently erasing `provider_id`/`config_patch`/`flow`/etc. everywhere
 * they're actually needed. This conditional form forces per-member
 * distribution (`T extends unknown ? ... : never` — a standard TS idiom),
 * so each variant keeps its own extra fields after the omit. Used by
 * `useProviderSurfaceSocket.ts`'s `submitProviderIntent` call sites. */
export type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/** Widened intent union `SurfaceSocket.submit()` accepts — a pure type
 * widening (additive, backward compatible: every existing `ChatIntentWire`
 * call site still type-checks unchanged) so the provider AND session
 * planes can submit over the identical `{"intent": {...}}` / ack-by-
 * `intent_id` transport path chat intents already use, per Package D's
 * "one socket, one intent path" requirement — no second submit
 * implementation. `SessionIntentWire`/`SystemIntentWire` are declared
 * further down (session/system surface sections) but included here so
 * this stays the single union. */
export type SurfaceIntentWire =
  | ChatIntentWire
  | ProviderIntentWire
  | SessionIntentWire
  | SystemIntentWire;

// ---- runs_surface.py (ADR 0009, runs & diagnostics) --------------------
// Mirrors `backend/surface_contract/runs_surface.py` + the `GET
// /api/v2/surface/runs[/{run_id}]` REST routes and the `runs` live feed
// (`{"feeds": ["runs"]}` over `/ws/v2/surface` — adapter_api.py's
// `_FEED_SURFACES`/`_feed_surface`, distinct from the chat surface's
// cursor-scoped `{"surfaces": [...]}` subscribe). `RunSummaryUpsertFrame`
// is NOT `FrameBaseWire`-shaped (no `surface_id`/`snapshot`) — a feed
// push, not a cursor-tracked projection frame — so it is kept out of
// `ChatFrame`'s union; see `SurfaceSocketHandlers.onRunsFrame` in
// client.ts for the dispatch split.

export type RunPhaseWire =
  | "starting"
  | "running"
  | "reconnecting"
  | "stalled"
  | "completing"
  | "completed"
  | "failed"
  | "unknown_after_recovery";

export interface StartupProgressWire {
  phase: string;
  progress: number | null;
}

// Mirrors backend `turn_manager.py`'s run-state entry `kind` (`surface_
// contract/runs_surface.py`'s `RunKind` StrEnum) — backend-internal
// categorization, not a provider display string. Legacy's `RunBadge.tsx`
// showed this as a literal, un-i18n'd label ("manager"/"worker", raw
// English words — legacy never translated them; "native" gets no label
// at all, an empty string) alongside its `runBadge.running` suffix.
export type RunKindWire = "manager" | "native" | "worker";

export interface RunSummaryWire {
  run_id: string;
  session_id: SessionId;
  turn_id: TurnId | null;
  provider_id: string;
  runner: string;
  phase: RunPhaseWire;
  started_at: number;
  last_heartbeat_at: number | null;
  startup: StartupProgressWire | null;
  // Sourced from turn_manager.py's `run.state.*` facts (event-driven) —
  // live-only, in-memory: present while this backend incarnation has live
  // knowledge of the run, honestly `null` otherwise, INCLUDING after a
  // cold restart (never persisted, never stale) — same honesty rule as
  // every other run-state-fact field on this dataclass.
  kind: RunKindWire | null;
}

export type DetailValueKindWire = "text" | "duration" | "count" | "ref";

export interface RunDetailEntryWire {
  key: string;
  value_kind: DetailValueKindWire;
  value: unknown;
}

export interface RunDetailWire {
  summary: RunSummaryWire;
  entries: RunDetailEntryWire[];
}

export interface RunPageWire {
  runs: RunSummaryWire[];
  next_cursor: string | null;
}

export type RunsEnvelope =
  | OkDataclassEnvelope<RunPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type RunDetailEnvelope =
  | OkDataclassEnvelope<RunDetailWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

/** `runs` feed push (`RunsSurfaceAdapter.subscribe`/`_on_lifecycle`) —
 * change-only, including honest STARTING/RUNNING/STALLED transitions
 * (`runs_adapter.py`'s heartbeat-derived `_phase`). Not part of
 * `ChatFrame` — see module note above. */
export interface RunSummaryUpsertFrame {
  type: "run_summary_upsert";
  cv: number;
  summary: RunSummaryWire;
}

export type RunsFrame = RunSummaryUpsertFrame;

// ---- session_surface.py (ADR 0008, session/project/folder/tag surface,
// Package B) -----------------------------------------------------------
// Mirrors `backend/surface_contract/session_surface.py` + `GET
// /api/v2/surface/{sessions,projects,folders,tags}` + the `"sessions"`
// live feed (`{"feeds": ["sessions"]}` over `/ws/v2/surface` — one feed
// covers ALL 9 `SessionFrame` union members, unlike the system surface's
// per-frame-type feed split; adapter_api.py's `_feed_surface` maps
// `"sessions"` straight onto `SessionSurfaceAdapter.subscribe`, no
// per-frame filtering). See `lib/sessionSurfaceRegistry.ts` for the
// consumer — additive to the legacy `/ws/chat`-sourced
// `lib/sessionRegistry.ts` (that module's own per-subscription comments
// enumerate exactly which fields SessionSummary below still lacks:
// unread_count, pending_user_input_count, marker color/tooltip,
// current_todos/current_tasks, cwd/node_id routing, project aggregate
// counts — none of those exist on this contract).

export type ProjectRef = string;
export type FolderRef = string;
export type TagRef = string;

export type SessionRollupStateWire =
  | "idle"
  | "queued"
  | "running"
  | "awaiting_approval"
  | "reconnecting"
  | "failed"
  | "detached";

export interface TagAssignmentWire {
  tag_ref: TagRef;
  source: string;
}

/** Full per-extension attention marker shape — companion to the tag-only
 * `SessionSummaryWire.attention_markers` (same "richer field added
 * alongside, not instead of" idiom `TagAssignmentWire`/`tag_refs`
 * establishes). `color`/`tooltip` are `null` when an extension omitted
 * them, never fabricated. */
export interface AttentionMarkerDetailWire {
  extension_id: string;
  tag: string;
  color: string | null;
  tooltip: string | null;
  sound: boolean;
  sound_setting: string | null;
}

export interface SessionSummaryWire {
  session_id: SessionId;
  title: string;
  project_ref: ProjectRef | null;
  selectors: SessionSelectorsWire;
  state: SessionRollupStateWire;
  attention_markers: string[];
  opened_at: number | null;
  last_activity_at: number;
  archived: boolean;
  folder_ref: FolderRef | null;
  tag_refs: TagAssignmentWire[];
  // Live signals with no rollup-`state` equivalent (backend/adapters/
  // session_adapter.py's `_RollupFlags` docstring) — honestly default
  // (0/false/"stopped") until this connection has observed the
  // relevant fact for a given session, same "unknown -> default"
  // contract `state` itself already has.
  unread_count: number;
  has_error: boolean;
  monitoring_state: string;
  attention_marker_details: AttentionMarkerDetailWire[];
  pending_interactions: number;
}

export interface SessionPageWire {
  sessions: SessionSummaryWire[];
  next_cursor: string | null;
}

export interface ProjectWire {
  project_ref: ProjectRef;
  name: string;
  order: number;
  session_count: number;
}

export interface SessionFolderWire {
  folder_ref: FolderRef;
  project_ref: ProjectRef;
  name: string;
  parent_folder_ref: FolderRef | null;
  order: number;
}

export interface SessionTagWire {
  tag_ref: TagRef;
  project_ref: ProjectRef | null;
  name: string;
  color: string;
}

// REST envelopes (adapter_api.py's `_result_body` — same ProjectionResult
// states the chat surface uses, since `SessionSurface` methods return
// `ProjectionResult` too, unlike ADR 0007's always-`ok` `_envelope`).
export type SessionsEnvelope =
  | OkDataclassEnvelope<SessionPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type ProjectsEnvelope =
  | OkListEnvelope<ProjectWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type FoldersEnvelope =
  | OkListEnvelope<SessionFolderWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type TagsEnvelope =
  | OkListEnvelope<SessionTagWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export interface SessionSummaryUpsertFrame {
  type: "session_summary_upsert";
  cv: number;
  summary: SessionSummaryWire;
  intent_id: string | null;
}

/** Invalidation signal only (no node list) — a live subscriber re-fetches
 * the tree on receipt. NOTE: there is currently no `GET .../tree` REST
 * route in `adapter_api.py` at all (verified: zero matches for
 * "session_tree" there), so this frame has no way to be actioned from the
 * frontend today — mirrored here for wire-shape completeness /
 * forward-compat only; `lib/sessionSurfaceRegistry.ts` logs and drops it.
 * Fork-tree UI stays 100% legacy (`session_forked` on `/ws/chat`) until a
 * REST route exists. */
export interface SessionTreeChangedFrame {
  type: "session_tree_changed";
  cv: number;
  session_id: SessionId;
}

export interface ProjectUpsertFrame {
  type: "project_upsert";
  cv: number;
  project: ProjectWire;
  intent_id: string | null;
}

export interface SessionRemovedFrame {
  type: "session_removed";
  cv: number;
  session_id: SessionId;
}

export interface ProjectRemovedFrame {
  type: "project_removed";
  cv: number;
  project_ref: ProjectRef;
}

export interface FolderUpsertFrame {
  type: "folder_upsert";
  cv: number;
  folder: SessionFolderWire;
  intent_id: string | null;
}

export interface FolderRemovedFrame {
  type: "folder_removed";
  cv: number;
  folder_ref: FolderRef;
}

export interface TagUpsertFrame {
  type: "tag_upsert";
  cv: number;
  tag: SessionTagWire;
  intent_id: string | null;
}

export interface TagRemovedFrame {
  type: "tag_removed";
  cv: number;
  tag_ref: TagRef;
}

/** Union of every frame pushed under the `"sessions"` feed. */
export type SessionsFrame =
  | SessionSummaryUpsertFrame
  | SessionTreeChangedFrame
  | ProjectUpsertFrame
  | SessionRemovedFrame
  | ProjectRemovedFrame
  | FolderUpsertFrame
  | FolderRemovedFrame
  | TagUpsertFrame
  | TagRemovedFrame;

const _SESSIONS_FRAME_TYPES: ReadonlySet<string> = new Set([
  "session_summary_upsert",
  "session_tree_changed",
  "project_upsert",
  "session_removed",
  "project_removed",
  "folder_upsert",
  "folder_removed",
  "tag_upsert",
  "tag_removed",
]);

export function isSessionsFrame(value: { type: string }): value is SessionsFrame {
  return _SESSIONS_FRAME_TYPES.has(value.type);
}

// ---- intents.py SessionIntent (command plane, ADR 0008, Package B) —
// same `{"intent": {...}}` envelope every other intent surface uses.
// `session_id` is populated (non-null) only for the genuinely
// session-scoped members (archive/unarchive/rename/assign_project/
// mark_opened/assign_folder/assign_tags); the project/folder/tag catalog
// mutations carry `session_id: null`, same "IntentBase.session_id is
// optional" idiom `ProviderIntentWire` already uses. ------------------

export interface ArchiveSessionIntentWire {
  kind: "archive_session";
  cv: number;
  intent_id: string;
  session_id: SessionId;
}

export interface UnarchiveSessionIntentWire {
  kind: "unarchive_session";
  cv: number;
  intent_id: string;
  session_id: SessionId;
}

export interface RenameSessionIntentWire {
  kind: "rename_session";
  cv: number;
  intent_id: string;
  session_id: SessionId;
  title: string;
}

/** No "unassign" — `project_ref: null` is a hard rejection server-side
 * (no legacy session can exist without a cwd to route it to); this
 * intent always carries a real target. */
export interface AssignProjectIntentWire {
  kind: "assign_project";
  cv: number;
  intent_id: string;
  session_id: SessionId;
  project_ref: ProjectRef;
}

export interface CreateProjectIntentWire {
  kind: "create_project";
  cv: number;
  intent_id: string;
  session_id: null;
  name: string;
  path: string;
}

export interface RenameProjectIntentWire {
  kind: "rename_project";
  cv: number;
  intent_id: string;
  session_id: null;
  project_ref: ProjectRef;
  name: string;
}

export interface DeleteProjectIntentWire {
  kind: "delete_project";
  cv: number;
  intent_id: string;
  session_id: null;
  project_ref: ProjectRef;
}

export interface MarkOpenedIntentWire {
  kind: "mark_opened";
  cv: number;
  intent_id: string;
  session_id: SessionId;
}

export interface CreateFolderIntentWire {
  kind: "create_folder";
  cv: number;
  intent_id: string;
  session_id: null;
  project_ref: ProjectRef;
  name: string;
  parent_folder_ref: FolderRef | null;
}

export interface RenameFolderIntentWire {
  kind: "rename_folder";
  cv: number;
  intent_id: string;
  session_id: null;
  folder_ref: FolderRef;
  name: string;
}

export interface MoveFolderIntentWire {
  kind: "move_folder";
  cv: number;
  intent_id: string;
  session_id: null;
  folder_ref: FolderRef;
  parent_folder_ref: FolderRef | null;
}

export type FolderDeleteModeWire = "unassign" | "delete_sessions";

export interface DeleteFolderIntentWire {
  kind: "delete_folder";
  cv: number;
  intent_id: string;
  session_id: null;
  folder_ref: FolderRef;
  mode: FolderDeleteModeWire;
}

export interface CreateTagIntentWire {
  kind: "create_tag";
  cv: number;
  intent_id: string;
  session_id: null;
  name: string;
  project_ref: ProjectRef | null;
  color: string | null;
}

export interface RenameTagIntentWire {
  kind: "rename_tag";
  cv: number;
  intent_id: string;
  session_id: null;
  tag_ref: TagRef;
  name: string;
}

export interface RecolorTagIntentWire {
  kind: "recolor_tag";
  cv: number;
  intent_id: string;
  session_id: null;
  tag_ref: TagRef;
  color: string;
}

export interface DeleteTagIntentWire {
  kind: "delete_tag";
  cv: number;
  intent_id: string;
  session_id: null;
  tag_ref: TagRef;
}

export interface AssignFolderIntentWire {
  kind: "assign_folder";
  cv: number;
  intent_id: string;
  session_id: SessionId;
  folder_ref: FolderRef | null;
}

/** Sync mode (`sync_tag_refs`) and delta mode (`add_tag_refs`/
 * `remove_tag_refs`) are mutually exclusive per call — server rejects
 * both-or-neither. Both touch only entries whose `source` matches this
 * call's `source`. */
export interface AssignTagsIntentWire {
  kind: "assign_tags";
  cv: number;
  intent_id: string;
  session_id: SessionId;
  source: string;
  add_tag_refs: TagRef[] | null;
  remove_tag_refs: TagRef[] | null;
  sync_tag_refs: TagRef[] | null;
}

export type SessionIntentWire =
  | ArchiveSessionIntentWire
  | UnarchiveSessionIntentWire
  | RenameSessionIntentWire
  | AssignProjectIntentWire
  | CreateProjectIntentWire
  | RenameProjectIntentWire
  | DeleteProjectIntentWire
  | MarkOpenedIntentWire
  | CreateFolderIntentWire
  | RenameFolderIntentWire
  | MoveFolderIntentWire
  | DeleteFolderIntentWire
  | CreateTagIntentWire
  | RenameTagIntentWire
  | RecolorTagIntentWire
  | DeleteTagIntentWire
  | AssignFolderIntentWire
  | AssignTagsIntentWire;

// ---- system_surface.py (ADR 0011, System & Host Surface, Package C) ----
// Mirrors `backend/surface_contract/system_surface.py` + 13 `GET
// /api/v2/surface/{extension-config,harness-profiles,extension-ui,
// extension-catalog,marketplace-bridge,marketplace-intents,schedules,
// host-startup-tasks,installation-capabilities,machines,node-registrations}`
// routes + 12 `/ws/v2/surface` feeds (`extension_notices`,
// `extension_config`, `harness_profiles`, `extension_ui`,
// `extension_catalog`, `marketplace_bridge`, `marketplace_intents`,
// `schedules`, `host_startup_tasks`, `installation_capabilities`,
// `machines`, `node_registrations` — `adapter_api.py`'s
// `_SYSTEM_FEED_FRAME_TYPES`). All 12 feeds share ONE backend
// `SystemSurfaceAdapter` instance server-side, but each feed name only
// delivers its own frame types (`_SystemFeedView`) — client-side this is
// transparent, every system frame arrives via `onSystemFrame` regardless
// of which feed name(s) a connection subscribed to.
//
// `system_surface.MachineNodeUpsert` (machine/node topology) is named
// distinctly from `frames.NodeUpsert` (chat content node) precisely so
// the two never share a wire `type` string — `adapter_api._frame_type_name`
// derives the wire type purely from the Python class name, and two
// classes named identically would otherwise collide. `MachineNodeUpsert`
// serializes to `"machine_node_upsert"`; `isSystemFrame` below tells every
// system frame apart from every `ChatFrame` by plain type-string
// membership, same as every other system frame.

export type ExtensionToastLevelWire = "info" | "warning" | "error";

export interface ExtensionToastFrame {
  type: "extension_toast";
  extension_id: string;
  level: ExtensionToastLevelWire;
  message: string;
  session_id: SessionId | null;
}

export interface ExtensionSignalFrame {
  type: "extension_signal";
  extension_id: string;
  event_name: string;
  data: Record<string, unknown>;
}

export interface ExtensionConfigSectionsWire {
  settings: Record<string, unknown> | null;
  instructions: Record<string, unknown> | null;
  ui_settings: Record<string, unknown> | null;
  internal_llm: Record<string, unknown> | null;
  permissions: Record<string, unknown> | null;
  mcp: Record<string, unknown> | null;
  skills: Record<string, unknown> | null;
}

export interface ExtensionConfigDescriptorWire {
  extension_id: string;
  cv: number;
  sections: ExtensionConfigSectionsWire;
}

export interface ExtensionConfigPageWire {
  descriptors: ExtensionConfigDescriptorWire[];
  next_cursor: string | null;
}

export interface ExtensionConfigUpsertFrame {
  type: "extension_config_upsert";
  cv: number;
  extension_id: string;
  section: string;
}

export interface HarnessProfileDescriptorWire {
  harness_profile_id: string;
  cv: number;
  display: string;
  is_default: boolean;
  disabled_builtin_extensions: string[];
  disabled_builtin_tools: string[];
  config_schema: Record<string, unknown> | null;
}

export interface HarnessProfilePageWire {
  profiles: HarnessProfileDescriptorWire[];
  next_cursor: string | null;
}

export interface HarnessProfileUpsertFrame {
  type: "harness_profile_upsert";
  cv: number;
  profile: HarnessProfileDescriptorWire;
  // Stamped only on the forced re-emit right after a `save_harness_profile`
  // CREATE settles (`backend/adapters/system_adapter.py`'s `_run_command`)
  // — lets a caller correlate the new profile's server-generated id via
  // `lib/systemFeedRegistry.ts`'s `submitSystemIntentAwaitingUpsert`, same
  // idiom as `FolderUpsertFrame`/`TagUpsertFrame`'s own `intent_id`. `null`
  // on every organically fact-triggered upsert.
  intent_id: string | null;
}

export interface HarnessDefaultChangedFrame {
  type: "harness_default_changed";
  cv: number;
  harness_profile_id: string;
}

/** Closed, kind-scoped `Action` schema (ADR 0011 §3): `navigate` uses
 * `path`; `ensure` uses `endpoint`/`path_template`/`id_field`/
 * `include_cwd`; `module` uses `module_url`. An unknown `kind` still
 * decodes (fields simply stay `null`) — renders generically per ADR 0006
 * §0's forward-compat rule. */
export type HookActionKindWire = "navigate" | "ensure" | "module" | string;

export interface HookActionWire {
  kind: HookActionKindWire;
  path: string | null;
  endpoint: string | null;
  path_template: string | null;
  id_field: string | null;
  include_cwd: boolean | null;
  module_url: string | null;
}

export type ExtensionUiModuleKindWire = "quick_button" | "page" | "frontend_module";

export interface ExtensionUiDisplayWire {
  label: string;
  icon: string | null;
}

export interface ExtensionUiModuleWire {
  extension_id: string;
  cv: number;
  kind: ExtensionUiModuleKindWire;
  display: ExtensionUiDisplayWire;
  action: HookActionWire | null;
  placements: string[];
  badge_count: number | null;
  slot: string | null;
  module_url: string | null;
  payments: boolean | null;
  marketplace_auth: boolean | null;
}

export interface ExtensionUiPageWire {
  modules: ExtensionUiModuleWire[];
  next_cursor: string | null;
}

export interface ExtensionUiChangedFrame {
  type: "extension_ui_changed";
  cv: number;
}

export interface ExtensionCatalogEntryWire {
  extension_id: string;
  cv: number;
  display: string;
  installed_version: string;
  available_version: string | null;
  update_available: boolean;
  enabled: boolean;
  source: string;
}

export interface ExtensionCatalogPageWire {
  entries: ExtensionCatalogEntryWire[];
  next_cursor: string | null;
}

export interface ExtensionCatalogUpsertFrame {
  type: "extension_catalog_upsert";
  cv: number;
  entry: ExtensionCatalogEntryWire;
}

export type MarketplaceConnectionStateWire = "unpaired" | "connecting" | "connected" | "offline";

export interface PairedDeviceWire {
  device_ref: string;
  label: string;
}

export interface MarketplaceBridgeStateWire {
  cv: number;
  connection_state: MarketplaceConnectionStateWire;
  revocation_pending: boolean;
  paired_devices: PairedDeviceWire[];
}

export interface MarketplaceBridgeStateChangedFrame {
  type: "marketplace_bridge_state_changed";
  cv: number;
  state: MarketplaceBridgeStateWire;
}

export interface MarketplaceExtensionRefWire {
  id: string;
  name: string | null;
  version: string | null;
  publisher: string | null;
  permission_delta: Record<string, unknown> | null;
}

export type MarketplaceIntentActionWire =
  | "pair"
  | "install"
  | "enable"
  | "disable"
  | "update"
  | "uninstall";

export type MarketplaceIntentStatusWire =
  | "pending"
  | "leased"
  | "awaiting_confirmation"
  | "committing"
  | "redeemed"
  | "succeeded"
  | "rejected"
  | "failed"
  | "failed_unknown"
  | "expired"
  | "cancelled";

export interface MarketplaceIntentWire {
  intent_id: string;
  cv: number;
  action: MarketplaceIntentActionWire;
  status: MarketplaceIntentStatusWire;
  extension: MarketplaceExtensionRefWire | null;
  account_label: string | null;
  site_label: string | null;
  device_label: string | null;
  error: string | null;
}

export interface MarketplaceIntentPageWire {
  intents: MarketplaceIntentWire[];
  next_cursor: string | null;
}

export interface MarketplaceIntentUpsertFrame {
  type: "marketplace_intent_upsert";
  cv: number;
  intent: MarketplaceIntentWire;
}

export interface ScheduleSummaryWire {
  schedule_id: string;
  cv: number;
  session_id: SessionId;
  prompt_preview: string;
  cadence: string;
  next_run_at: string | null;
  last_run_at: string | null;
  enabled: boolean;
}

export interface SchedulePageWire {
  schedules: ScheduleSummaryWire[];
  next_cursor: string | null;
}

export interface ScheduleUpsertFrame {
  type: "schedule_upsert";
  cv: number;
  schedule: ScheduleSummaryWire;
}

export interface ScheduleRemovedFrame {
  type: "schedule_removed";
  cv: number;
  schedule_id: string;
}

export type HostStartupTaskStateWire = "running" | "done" | "failed";

export interface HostStartupTaskWire {
  id: string;
  cv: number;
  label: string;
  state: HostStartupTaskStateWire;
  started_at: string;
  finished_at: string | null;
}

export interface HostStartupTaskSnapshotWire {
  tasks: HostStartupTaskWire[];
  epoch: number;
}

export interface HostStartupTaskUpsertFrame {
  type: "host_startup_task_upsert";
  cv: number;
  task: HostStartupTaskWire;
}

export interface HostStartupTaskClearedFrame {
  type: "host_startup_task_cleared";
  cv: number;
  epoch: number;
}

export interface InstallationCapabilityWire {
  capability_id: string;
  cv: number;
  enabled: boolean;
  display: string;
  provisioned: boolean;
  active: boolean;
  restart_required: boolean;
  self_provisionable: boolean;
  in_app_restart_supported: boolean;
}

export interface InstallationCapabilityChangedFrame {
  type: "installation_capability_changed";
  cv: number;
  capability: InstallationCapabilityWire;
}

export type NodeRoleWire = "primary" | "worker_node";
export type NodeConnStateWire = "connected" | "disconnected" | "unknown";
export type NodeVersionStatusWire = "ok" | "mismatch" | "unknown";

export interface NodeDescriptorWire {
  node_id: string;
  cv: number;
  role: NodeRoleWire;
  address: string;
  cwd_roots: string[];
  state: NodeConnStateWire;
  last_seen: number | null;
  connected_at: number | null;
  version_status: NodeVersionStatusWire;
  app_commit_sha: string | null;
  app_dirty: boolean | null;
  primary_commit_sha: string | null;
  primary_dirty: boolean | null;
}

export interface MachinePageWire {
  nodes: NodeDescriptorWire[];
  next_cursor: string | null;
}

/** Machine/node-topology upsert (system_surface.py's `MachineNodeUpsert`)
 * — NOT the chat content-node `NodeUpsertFrame` above; distinct wire
 * `type` string by design (see the note at the top of this section). */
export interface SystemNodeUpsertFrame {
  type: "machine_node_upsert";
  cv: number;
  node: NodeDescriptorWire;
}

export interface SystemNodeRemovedFrame {
  type: "node_removed";
  cv: number;
  node_id: string;
}

export type NodeProviderCredentialStateWire = "pending" | "synced" | "failed";

export interface NodeProviderCredentialStatusWire {
  node_id: string;
  provider_id: string;
  cv: number;
  status: NodeProviderCredentialStateWire;
  authorized_at: string | null;
  updated_at: string | null;
  failure_code: string | null;
}

export interface NodeProviderCredentialUpsertFrame {
  type: "node_provider_credential_upsert";
  cv: number;
  status: NodeProviderCredentialStatusWire;
}

export type NodeRegistrationStateWire = "pending" | "resolved" | "cancelled";
export type NodeRegistrationDecisionWire = "approved" | "denied";

export interface NodeRegistrationRequestPayloadWire {
  address: string;
  cwd_roots: string[];
  fingerprint: string;
}

export interface NodeRegistrationResponseWire {
  decision: NodeRegistrationDecisionWire;
}

export interface NodeRegistrationRequestWire {
  node_id: string;
  cv: number;
  request: NodeRegistrationRequestPayloadWire;
  state: NodeRegistrationStateWire;
  response: NodeRegistrationResponseWire | null;
}

export interface NodeRegistrationPageWire {
  requests: NodeRegistrationRequestWire[];
  next_cursor: string | null;
}

export interface NodeRegistrationUpsertFrame {
  type: "node_registration_upsert";
  cv: number;
  node_id: string;
  request: NodeRegistrationRequestWire;
}

/** Union of every frame the 12 system feeds can push. Dispatched via its
 * own `SurfaceSocketHandlers.onSystemFrame` (same split rationale as
 * `onRunsFrame`/`onProviderFrame`/`onSessionsFrame` — no `surface_id`/
 * `snapshot`, so it cannot be a `ChatFrame` member). */
export type SystemFrame =
  | ExtensionToastFrame
  | ExtensionSignalFrame
  | ExtensionConfigUpsertFrame
  | HarnessProfileUpsertFrame
  | HarnessDefaultChangedFrame
  | ExtensionUiChangedFrame
  | ExtensionCatalogUpsertFrame
  | MarketplaceBridgeStateChangedFrame
  | MarketplaceIntentUpsertFrame
  | ScheduleUpsertFrame
  | ScheduleRemovedFrame
  | HostStartupTaskUpsertFrame
  | HostStartupTaskClearedFrame
  | InstallationCapabilityChangedFrame
  | SystemNodeUpsertFrame
  | SystemNodeRemovedFrame
  | NodeProviderCredentialUpsertFrame
  | NodeRegistrationUpsertFrame;

const _SYSTEM_FRAME_TYPES: ReadonlySet<string> = new Set([
  "extension_toast",
  "extension_signal",
  "extension_config_upsert",
  "harness_profile_upsert",
  "harness_default_changed",
  "extension_ui_changed",
  "extension_catalog_upsert",
  "marketplace_bridge_state_changed",
  "marketplace_intent_upsert",
  "schedule_upsert",
  "schedule_removed",
  "host_startup_task_upsert",
  "host_startup_task_cleared",
  "installation_capability_changed",
  "machine_node_upsert",
  "node_removed",
  "node_provider_credential_upsert",
  "node_registration_upsert",
]);

export function isSystemFrame(value: { type: string }): value is SystemFrame {
  return _SYSTEM_FRAME_TYPES.has(value.type);
}

// REST envelopes (adapter_api.py's `_result_body` — same ProjectionResult
// states the chat/session surfaces use, since `SystemSurface` methods
// return `ProjectionResult` too).
export type ExtensionConfigEnvelope =
  | OkDataclassEnvelope<ExtensionConfigDescriptorWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type ExtensionConfigPageEnvelope =
  | OkDataclassEnvelope<ExtensionConfigPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type HarnessProfilesEnvelope =
  | OkDataclassEnvelope<HarnessProfilePageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type ExtensionUiEnvelope =
  | OkDataclassEnvelope<ExtensionUiPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type ExtensionCatalogEnvelope =
  | OkDataclassEnvelope<ExtensionCatalogPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type MarketplaceBridgeEnvelope =
  | OkDataclassEnvelope<MarketplaceBridgeStateWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type MarketplaceIntentsEnvelope =
  | OkDataclassEnvelope<MarketplaceIntentPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type SchedulesEnvelope =
  | OkDataclassEnvelope<SchedulePageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type HostStartupTasksEnvelope =
  | OkDataclassEnvelope<HostStartupTaskSnapshotWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type InstallationCapabilitiesEnvelope =
  | OkListEnvelope<InstallationCapabilityWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type MachinesEnvelope =
  | OkDataclassEnvelope<MachinePageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type NodeProviderCredentialsEnvelope =
  | OkListEnvelope<NodeProviderCredentialStatusWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

export type NodeRegistrationsEnvelope =
  | OkDataclassEnvelope<NodeRegistrationPageWire>
  | RebuildingEnvelope
  | StaleCursorEnvelope;

// ---- intents.py SystemIntent (command plane, ADR 0011, Package C) — same
// `{"intent": {...}}` envelope every other intent surface uses.
// `session_id` is always `null` (`IntentBase.session_id` — every ADR 0011
// concern is root/installation-scoped; `ScheduleSummary.session_id`/
// `CreateSchedule.target_session_id` are foreign references, not the
// transport's own scoping field, same distinction `ProviderIntentWire`
// already draws). ------------------------------------------------------

export interface UpdateExtensionConfigIntentWire {
  kind: "update_extension_config";
  cv: number;
  intent_id: string;
  session_id: null;
  extension_id: string;
  section: string;
  patch: Record<string, unknown>;
}

/** `harness_profile_id: null` creates a new profile from `config` (legacy
 * `POST /api/harness-profiles`'s `{name}` body). A non-`null` id writes
 * `writes` (legacy `HarnessFieldWrite[]`, `HarnessSettingsEditor.tsx`'s
 * actual `writeFields` shape) against `revision`'s optimistic-concurrency
 * check (legacy `PATCH .../fields`'s `{revision, writes}` body) —
 * `config` is unused on that path. */
export interface SaveHarnessProfileIntentWire {
  kind: "save_harness_profile";
  cv: number;
  intent_id: string;
  session_id: null;
  harness_profile_id: string | null;
  config: Record<string, unknown>;
  revision: string | null;
  writes: Record<string, unknown>[];
}

export interface DeleteHarnessProfileIntentWire {
  kind: "delete_harness_profile";
  cv: number;
  intent_id: string;
  session_id: null;
  harness_profile_id: string;
  revision: string | null;
}

export interface SetDefaultHarnessProfileIntentWire {
  kind: "set_default_harness_profile";
  cv: number;
  intent_id: string;
  session_id: null;
  harness_profile_id: string;
}

export interface InstallExtensionIntentWire {
  kind: "install_extension";
  cv: number;
  intent_id: string;
  session_id: null;
  extension_id: string;
  source: Record<string, unknown>;
}

export interface UpdateExtensionIntentWire {
  kind: "update_extension";
  cv: number;
  intent_id: string;
  session_id: null;
  extension_id: string;
}

export interface UninstallExtensionIntentWire {
  kind: "uninstall_extension";
  cv: number;
  intent_id: string;
  session_id: null;
  extension_id: string;
}

export interface EnableExtensionIntentWire {
  kind: "enable_extension";
  cv: number;
  intent_id: string;
  session_id: null;
  extension_id: string;
}

export interface DisableExtensionIntentWire {
  kind: "disable_extension";
  cv: number;
  intent_id: string;
  session_id: null;
  extension_id: string;
}

/** Wire field is `intent_id_ref` (NOT `marketplace_intent_id`, the Python
 * attribute name) — `adapter_api.py`'s `_SYSTEM_INTENT_PARSERS` reads this
 * exact key so it doesn't collide with the envelope's own `intent_id`. */
export interface DecideMarketplaceIntentIntentWire {
  kind: "decide_marketplace_intent";
  cv: number;
  intent_id: string;
  session_id: null;
  intent_id_ref: string;
  decision: "approve" | "reject";
}

export interface RevokeMarketplaceDeviceIntentWire {
  kind: "revoke_marketplace_device";
  cv: number;
  intent_id: string;
  session_id: null;
  device_ref: string;
}

export interface CreateScheduleIntentWire {
  kind: "create_schedule";
  cv: number;
  intent_id: string;
  session_id: null;
  target_session_id: string;
  prompt: string;
  cadence: Record<string, unknown>;
}

export interface DeleteScheduleIntentWire {
  kind: "delete_schedule";
  cv: number;
  intent_id: string;
  session_id: null;
  schedule_id: string;
}

export interface SetInstallationCapabilityIntentWire {
  kind: "set_installation_capability";
  cv: number;
  intent_id: string;
  session_id: null;
  capability_id: string;
  enabled: boolean;
  // Mirrors legacy `PATCH /api/installation-profile/capabilities/{id}`'s
  // `confirm_cancels_extension_work` body field — required `true` to
  // disable `integrations`.
  confirm_cancels_extension_work: boolean;
}

export interface RemoveNodeIntentWire {
  kind: "remove_node";
  cv: number;
  intent_id: string;
  session_id: null;
  node_id: string;
}

/** Covers only the credential-sync path (legacy `POST .../sync-providers`
 * with `include_secrets: true` + a non-empty `provider_ids`) — see
 * `backend/surface_contract/intents.py`'s `SyncNodeProviders` docstring
 * for why the route's `include_secrets: false` shape has no v2
 * representation. */
export interface SyncNodeProvidersIntentWire {
  kind: "sync_node_providers";
  cv: number;
  intent_id: string;
  session_id: null;
  node_id: string;
  include_secrets: boolean;
  provider_ids: string[];
}

export interface ResolveNodeRegistrationIntentWire {
  kind: "resolve_node_registration";
  cv: number;
  intent_id: string;
  session_id: null;
  node_id: string;
  decision: NodeRegistrationDecisionWire;
}

export type SystemIntentWire =
  | UpdateExtensionConfigIntentWire
  | SaveHarnessProfileIntentWire
  | DeleteHarnessProfileIntentWire
  | SetDefaultHarnessProfileIntentWire
  | InstallExtensionIntentWire
  | UpdateExtensionIntentWire
  | UninstallExtensionIntentWire
  | EnableExtensionIntentWire
  | DisableExtensionIntentWire
  | DecideMarketplaceIntentIntentWire
  | RevokeMarketplaceDeviceIntentWire
  | CreateScheduleIntentWire
  | DeleteScheduleIntentWire
  | SetInstallationCapabilityIntentWire
  | RemoveNodeIntentWire
  | SyncNodeProvidersIntentWire
  | ResolveNodeRegistrationIntentWire;

/** Live-feed names the `/ws/v2/surface` `{"feeds": [...]}` message
 * accepts (adapter_api.py `_FEED_SURFACES`). `"runs"` (`lib/
 * runSummaryRegistry.ts`), `"providers"` (`hooks/useProviderSurfaceSocket.ts`)
 * and `"sessions"` (`lib/sessionSurfaceRegistry.ts`) all have client
 * consumers today; the 12 `"extension_notices"`..`"node_registrations"`
 * names are ADR 0011's system surface (`lib/systemFeedRegistry.ts`). */
export type FeedName =
  | "sessions"
  | "providers"
  | "runs"
  | "extension_notices"
  | "extension_config"
  | "harness_profiles"
  | "extension_ui"
  | "extension_catalog"
  | "marketplace_bridge"
  | "marketplace_intents"
  | "schedules"
  | "host_startup_tasks"
  | "installation_capabilities"
  | "machines"
  | "node_registrations";
