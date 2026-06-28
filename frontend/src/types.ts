import type { InlineTag } from "./types/inlineTag";

/** An image attachment (pasted or file-selected) stored as base64. */
export interface PastedImage {
  dataUrl: string;
  base64: string;
  mediaType: string;
}

/** A non-image file attachment stored as base64. */
export interface FileAttachment {
  name: string;
  base64: string;
  mediaType: string;
  size: number;
}

export type WSEventType =
  // Primary inner event: a pass-through of one claude CLI session jsonl
  // line. The backend's FileTailer no longer translates these — they
  // arrive with their native shape intact (`type`, `message`, `uuid`,
  // `parentUuid`, `sessionId`, `isSidechain`, `toolUseResult`, per-message
  // `usage`, etc.). See MessageBubble.renderSingleEvent for the switch
  // on the inner `data.type` (assistant / user / system / ...).
  | "agent_message"
  // Legacy inner event types. The translator that produced these was
  // deleted in the "jsonl as source of truth" refactor, but persisted
  // sessions on disk still carry them — keep for backward-compat render.
  | "thinking"
  | "tool_call"
  | "output"
  | "steer_prompt"
  // Envelope events synthesized by the backend tailer (not from jsonl).
  | "session_discovered"
  | "complete"
  | "error"
  // Turn lifecycle + worker events
  | "turn_start"
  | "manager_event" // legacy backward compat
  | "turn_complete"
  // Server-side ack that the user's prompt has been persisted to disk
  // (and an empty streaming-assistant placeholder created beside it).
  // Frontend uses this to drop optimistic pendingMessages immediately.
  | "user_message_persisted"
  | "steer_prompt_persisted"
  // Sequence-cursor replay: backend's response to a subscribe with
  // `since_seq=N`. Carries every persisted message with `seq >= N` so
  // the frontend can converge on the canonical state on (re)connect.
  | "messages_replay"
  // Backend reconcile (post-restart) appended late events to a
  // COLLAPSED historical turn — its stale stub must be replaced so an
  // expanded turn re-fetches fresh full events.
  | "stub_invalidated"
  // Per-event message updates (currently only fired when the lazy
  // assistant message is born — the rest of the streaming state still
  // flows via manager_event/worker_event frames that the frontend
  // routes onto the canonical message in memory).
  | "messages_delta"
  // Backend-owned per-session multi-run state. Fired on every run
  // start/end and on subscribe. Drives the labeled "running" badges
  // on the bubble / worker panels.
  | "run_state"
  | "worker_start"
  | "worker_event"
  | "worker_complete"
  // Worker-redesign: fresh worker creation requires user approval.
  | "worker_creation_requested"
  | "worker_creation_approved"
  | "worker_creation_failed"
  | "user_input_requested"
  | "user_input_resolved"
  // Worker prep turn (the one-time context-loading run on a freshly
  // approved worker Better Agent session — happens before its first delegation).
  | "worker_prep_start"
  | "worker_prep_event"
  | "worker_prep_complete"
  | "worker_prep_cancelled"
  // State-ownership notifications: backend mutated something the
  // frontend has a snapshot of; refetch your slice.
  | "workers_changed"
  | "session_organization_changed"
  | "user_prefs_changed"
  // Per-machine UI selection (selected project + remembered sessions)
  // changed; payload is the full ui_selection snapshot.
  | "ui_selection_changed"
  // Credential-broker consent list changed; refetch GET /api/credentials/pending.
  | "credential_consent_changed"
  | "projects_changed"
  | "project_updates_changed"
  | "project_mappings_changed"
  | "turn_started"
  | "turn_stopped"
  | "turn_detached"
  | "trace_step"
  | "session_renamed"
  | "rewind_complete"
  // Experimental rearranger feature (see backend/rearranger.py)
  | "rearranger_state"
  | "rearranger_updated"
  // Interactive tool/command approval (Claude can_use_tool / Codex app-server).
  | "tool_approval_requested"
  | "tool_approval_resolved"
  // Per-session metadata patch (inline_tags, draft_input, fork_closed) —
  // broadcast on every REST mutation so other tabs converge.
  | "session_metadata_updated"
  // Fork creation broadcast — fired when a new fork session is born so
  // any tab viewing the parent can append it to its split-view.
  | "session_forked"
  // Brand-new (non-fork) session born in some tab — DIV-4 multi-tab
  // convergence so other tabs add the row to their sidebar without
  // polling. Frontend MUST dedup by id (originating tab already
  // inserted via REST POST response).
  | "session_created"
  // Provider list / active-provider changed — frontend refetches its
  // ModelSelector + open ProvidersModal so all tabs converge.
  | "provider_changed"
  // Streaming provider-CLI install (Settings → Provider CLI tools).
  // provider_setup streams installer stdout/stderr line-by-line
  // (progress) and a terminal state (finished); useProviderInstalls
  // owns the registry projection.
  | "provider_install_progress"
  | "provider_install_finished"
  | "provider_config_sync_changed"
  | "extensions_changed"
  // Per-provider model catalog delta — fired by the daily refresher
  // (and manual refresh endpoints) when the cached model list changes.
  // Frontend refetches `/api/models`. Payload carries four disjoint
  // transition sets so toasts/badges can render per-transition.
  | "models_catalog_changed"
  // Backend ack that a prompt was queued (not sent immediately because
  // another turn was running).
  | "prompt_queued"
  // Supervisor verdict event (verdict_failed, verdict_capped, etc.)
  | "supervisor_event"
  // Per-message lifecycle (user_msg) — surfaced for the frontend's
  // optimistic-ack bridge in pendingMessages.
  | "user_message_queued"
  | "user_message_sent"
  | "user_message_received"
  | "user_message_done"
  | "user_message_failed"
  // Backend signals it is rebuilding state for a message (long replay
  // or recovery) so the bubble can render a recovering badge.
  | "message_recovering_changed"
  // Backend is sleeping between a rate-limited (429) attempt and the
  // next retry; carries the absolute ISO timestamp of the next attempt
  // so the bubble can render a 'Retrying in Ns…' pill that ticks down
  // locally.
  | "message_retrying_changed"
  // A turn succeeded after backend automatic retry attempts.
  | "message_auto_retry_changed"
  // Per-turn session-picker payload (`propose_sessions` result) stamped on
  // the producing assistant message; drives the inline picker per turn.
  | "message_ask_result_changed"
  // Which session the user chose from a turn's picker (highlighted row).
  | "message_ask_choice_changed"
  // Async session-level processing progress (reconcile, large-replay).
  | "session_processing_started"
  | "session_processing_finished"
  // Backend reconcile completed (fast or slow). Initial GET may have
  // returned stale cache; frontend silently refetches the session tree.
  | "session_reconciled"
  // Per-session running-flag transition. Authoritative state is the
  // backend's `session_manager._running_sids` (transient — rebuilt
  // by run_recovery via the same hook). The frontend's
  // `sessionRegistry` mirrors and powers `<SessionStatusBadge>` /
  // `<ProjectStatusBadge>` consumers via the typed eventBus.
  | "session_running_changed"
  // Per-session unread-cursor transition. Fires on every event-append
  // in `apply_event` AND on ack via POST /api/sessions/{id}/seen.
  // Authoritative state is `session_manager._unread_counts`
  // (lazy-hydrated from the persisted `last_seen_event_uid`).
  | "session_unread_changed"
  // Per-session unseen-error transition. Set when a turn ends in an
  // unrecoverable error, cleared on view-ack or next successful turn.
  | "session_error_changed"
  // Per-session pending request_user_input count changed.
  | "session_user_input_changed"
  // Extension attention marker on a session changed (set/cleared).
  | "session_marker_changed"
  // Multi-machine: live up/down transitions of worker-nodes.
  | "node_state_changed"
  // Multi-machine: a brand-new worker-node is awaiting operator
  // approval (requested) or its request was resolved (approve/deny).
  // `useWebSocket` re-dispatches both as window CustomEvents that
  // `usePendingNodeRegistrations` converges; authoritative snapshot
  // comes from the machine-nodes extension's pending-node snapshot.
  | "node_registration_requested"
  | "node_registration_resolved"
  // Sidebar convergence: a session was deleted in another tab.
  | "session_deleted"
  // Frontend-synthesized surfacing of an inner event the bubble didn't
  // recognize (unknown agent_message inner-type or unknown content block).
  // Never produced by the backend — `MessageBubble.flattenClaudeMessages`
  // emits these so a future provider/CLI shape isn't silently swallowed,
  // and `renderSingleEvent` renders them as a `DiagnosticEvent` card.
  | "diagnostic"
  // Known provider lifecycle notices that should render in the timeline
  // without becoming assistant output text.
  | "lifecycle_notice"
  // Live PR-creation notice. Normally diverted in `useWebSocket` to the
  // ephemeral chat-panel toast (commit 9653bf49) because pr-link has no
  // uuid and never lands on the render tree; kept in the union so any
  // frame that does reach `MessageBubble.renderGroupedEvents` (e.g. a
  // future replay path) still type-checks and renders via PrLinkEvent.
  | "pr_link"
  // Backend startup task lifecycle delta (register/done/failed/reset).
  // `useWebSocket` re-dispatches as a window CustomEvent that
  // `StartupTasksBanner` listens to; the banner's authoritative
  // snapshot comes from `GET /api/startup_tasks` on mount.
  | "startup_task_changed"
  // Stamped into events.jsonl by the REST middleware and the CLI
  // bridge BEFORE the handler runs, so every mutation has a
  // traceable origin. Not rendered — the bubble filters it.
  | "command_received"
  // Backend consumed a queued prompt — clear stale frontend state.
  | "queue_consumed"
  // Manager's todo list snapshot, pushed after every delegation cycle.
  | "todos_snapshot"
  // A runner started/stopped babysitter-lingering (turn over, background
  // shells/monitors still running). Payload: {app_session_id, run_id,
  // lingering}. Snapshot: GET /api/sessions/{id}/background.
  | "run_lingering"
  // The session's schedule list changed (created/fired/cancelled).
  // Payload carries the full authoritative list.
  | "schedules_updated";

export interface WSEvent {
  type: WSEventType;
  data: Record<string, unknown>;
  /**
   * Render-time enrichment: timestamp of the source event. Set by the
   * timeline renderer before flattening so each rendered row can show
   * when its source event arrived. Not part of the persisted shape.
   */
  _ts?: string;
}

/** A model-created scheduled prompt for a session. Authoritative state
 * lives on the backend; the frontend pulls via
 * the scheduler extension backend and converges on `schedules_updated`
 * WS frames. */
export interface Schedule {
  id: string;
  app_session_id: string;
  prompt: string;
  kind: "once" | "recurring";
  fire_at: string;
  interval_seconds: number | null;
  created_at: string;
  last_fired_at: string | null;
}

/** Snapshot of one machine (node) in the multi-machine topology.
 * Mirrors `backend/node_store.snapshot()` element shape. Authoritative
 * state lives on the backend; the frontend pulls a snapshot via
 * the machine-nodes extension and patches per-machine `state`/`last_seen` from
 * `node_state_changed` WS frames. */
export interface NodeSnapshot {
  id: string;
  role: "primary" | "worker_node";
  address: string;
  cwd_roots: string[];
  state: "connected" | "disconnected" | "unknown";
  connected_at: number | null;
  last_seen: number | null;
}

/** A single member in a project mapping group — one project on one node. */
export interface ProjectMappingMember {
  node_id: string;
  path: string;
  name: string;
  git_remote?: string;
}

/** A group of projects across machines that are the same logical project. */
export interface ProjectMappingGroup {
  group_id: string;
  label: string;
  confidence: "git_remote" | "path" | "name" | "manual";
  members: ProjectMappingMember[];
}

/** Payload of the `node_state_changed` WS frame. `last_seen` is the
 * backend-owned epoch timestamp (seconds) at the moment of the
 * transition — included so the frontend never has to fabricate a
 * timestamp from its own clock (CLAUDE.md state-ownership rule). */
export interface NodeStateChangedData {
  node_id: string;
  state: "connected" | "disconnected" | "unknown";
  last_seen: number | null;
}

/** A worker-node awaiting operator approval before it can join the
 * topology. Mirrors `node_link._public_rec`. The node's secret never
 * crosses the wire — only `fingerprint` (a short sha256 prefix) is
 * shown so the operator can sanity-check which node is connecting.
 * Authoritative snapshot lives behind the machine-nodes extension; the
 * approval popup converges via `node_registration_requested` /
 * `node_registration_resolved` WS frames. */
export interface PendingNodeRegistration {
  node_id: string;
  address: string;
  cwd_roots: string[];
  fingerprint: string;
  status: "pending" | "approved" | "denied";
  created_at: string;
  expires_at: string;
}

/** Payload of the `node_registration_resolved` WS frame. */
export interface NodeRegistrationResolvedData {
  node_id: string;
  status: "approved" | "denied";
}

/** A line range to focus/highlight when opening a file in the viewer. */
export interface FileFocus {
  startLine: number;
  endLine: number;
  startColumn?: number;
  endColumn?: number;
}

/** A backend-owned file panel open in the session's tabbed/split
 * right-panel viewer. The list + the agent-/user-requested
 * focus/selection is persisted; the user's live scroll/selection
 * within a panel stays frontend-transient (snapshotted at send time).
 * Panels are de-duplicated by `path` server-side. */
export interface OpenFilePanel {
  id: string;
  path: string;
  focus?: FileFocus | null;
  selection?: FileFocus | null;
}

export interface OpenConfigPanel {
  id: string;
  capability_id: string;
  scope: "global" | "project";
  cwd: string;
}

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_5m_tokens?: number;
  cache_creation_1h_tokens?: number;
}

export interface WorkerInfo {
  agent_session_id: string;
  name: string;
  cwd?: string;
  registry_cwd?: string;
  orchestration_mode: OrchestrationMode;
  agent_sid?: string | null;
  live_parent_agent_sid?: string | null;
  initialized?: boolean;
  /** True when the worker Better Agent session's underlying agent_sid has rotated
   * since registration (rewind) — manager forks are stale. UI shows a
   * badge + "Reset fork" affordance. */
  diverged?: boolean;
  created_at?: string;
  last_active?: string;
  delegation_count?: number;
  token_usage?: TokenUsage;
}

export interface RequirementTag {
  id: string;
  kind: "product" | "feature";
  label: string;
  count: number;
}

export interface SessionFolder {
  id: string;
  project_id: string;
  parent_folder_id?: string | null;
  name: string;
  order: number;
  created_at: string;
  updated_at: string;
}

export interface SessionTag {
  id: string;
  project_id?: string | null;
  name: string;
  color?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionOrganizationSnapshot {
  schema_version: number;
  folders: SessionFolder[];
  tags: SessionTag[];
  /** Distinct models across ALL of the project's sessions, regardless of
   * the active filter — the stable universe for the model filter. */
  models?: string[];
  assignments: Record<string, {
    folder_id?: string | null;
    tag_ids?: string[];
  }>;
}

/** A pending fresh-worker-creation request. Backend emits
 * worker_creation_requested on the WS and persists the same payload at
 * ~/.better-claude/pending_approvals/<delegation_id>.json so the
 * frontend can rehydrate through the Team Orchestration extension on reconnect. */
export interface PendingApproval {
  delegation_id: string;
  app_session_id: string;
  cwd: string;
  justification: string;
  proposed_description: string;
  proposed_orchestration_mode: OrchestrationMode;
  instructions_preview: string;
  model: string;
  status: "pending" | "approved" | "denied";
  created_at: string;
  expires_at: string;
  resolved_at?: string | null;
  approved_description?: string | null;
  approved_orchestration_mode?: OrchestrationMode | null;
}

/** Public view of a pending credential-broker consent — the display-safe
 * projection from consent_store.public_view. NEVER contains the secret
 * value (consents never hold it; the user binds it at approval). */
export interface CredentialConsentSink {
  sink_kind: string;
  computed_host: string;
  computed_target: string;
  egress: boolean;
  risk: "low" | "high";
  risk_reasons: string[];
  label_mismatch: boolean;
}

export interface CredentialConsent {
  consent_id: string;
  app_session_id: string;
  provider_id: string;
  label: string;
  sink: CredentialConsentSink;
  secret_names: string[];
  secret_sources?: Record<string, {
    kind: "password_manager";
    service: string;
    account: string;
  }>;
  status: "pending" | "approved" | "denied" | "revoked";
  created_at: string;
  expires_at: string;
  use_count: number;
}

export interface UserInputOption {
  label: string;
  description?: string;
}

export interface UserInputQuestion {
  id: string;
  header: string;
  question: string;
  options: UserInputOption[];
}

export interface UserInputRequest {
  request_id: string;
  app_session_id: string;
  questions: UserInputQuestion[];
  status: "pending" | "resolved" | "cancelled" | "expired";
  created_at: number;
  expires_at?: number | null;
  resolved_at?: number | null;
}

export interface WorkerPanel {
  delegation_id: string;
  /** Better Agent session id of the worker (NOT the claude jsonl sid). Under the
   * worker-redesign, the manager refers to workers by their persistent
   * Better Agent identity; the per-(caller, worker) fork sid is internal. */
  worker_session_id: string;
  worker_description: string;
  panel_kind?:
    | "worker"
    | "sub_session"
    | "session"
    | "sub_session_created"
    | "session_created";
  started_at?: string;
  /** Inline position in the manager message's event stream where this
   * delegation occurred — the count of manager events already on the
   * message when the worker was delegated. Stamped once by the backend
   * (single source of truth) so the panel renders at the same spot in
   * live, reload, and restore. `tagEvents` interleaves panels by this
   * index instead of by wall-clock timestamp (which is unreliable:
   * `started_at` is absent on ~half of panels and manager-event
   * timestamps use inconsistent formats). Undefined on legacy panels,
   * which fall back to appending after the manager stream. */
  insert_at?: number;
  /** Orchestration mode the worker Better Agent session runs in. May be undefined
   * on legacy panels persisted before the redesign. */
  orchestration_mode?: OrchestrationMode;
  is_new: boolean;
  instructions_preview: string;
  events: WSEvent[];
  jsonl_path?: string | null;
  new_byte_offset?: number;
  /** Fork agent_sid this delegation ran against. Internal; used by the
   * UI for the "view fork" affordance. */
  fork_agent_sid?: string | null;
  token_usage?: TokenUsage;
  run_mode?: string;
  success?: boolean;
  error?: string | null;
}

export type EntityType = "manager" | "worker" | "agent";

/** A single event tagged with its source entity for timeline rendering. */
export interface TaggedEvent {
  event: WSEvent;
  entityType: EntityType;
  entityId: string;
  entityLabel: string;
  panelKind?: WorkerPanel["panel_kind"];
  startedAt?: string;
  seq: number;
}

/** Consecutive events from the same entity, grouped for visual rendering. */
export interface EntityBlock {
  entityType: EntityType;
  entityId: string;
  entityLabel: string;
  panelKind?: WorkerPanel["panel_kind"];
  startedAt?: string;
  events: WSEvent[];
  /** Timestamps matching each event (parallel array). */
  timestamps: (string | undefined)[];
}

export interface MessageImage {
  filename?: string;
  media_type: string;
  dataUrl?: string;
}

export interface MessageFile {
  name: string;
  media_type: string;
  size: number;
}

export interface ChatMessage {
  id: string;
  /** Monotonic per-session sequence number assigned at persist time.
   * Drives the WS replay protocol — frontend sends its highest seen
   * `seq` as `since_seq` on every subscribe so the backend can
   * replay only what's new (or re-send the in-flight assistant msg). */
  seq?: number;
  /** Echo of the frontend's optimistic in-flight id. Set on the user
   * message by the backend; the frontend uses it to retire the
   * matching `pendingMessages` entry once the canonical message
   * arrives. Server-only on assistant messages (always undefined
   * there). */
  client_id?: string | null;
  /** Backend correlation id for the 5-state user-message lifecycle
   * (queued→sent→received→done/failed). Present on user messages;
   * used by the frontend to map lifecycle WS events back to this
   * message for status display (MessageStatus). */
  lifecycle_msg_id?: string | null;
  file_discussion_id?: string | null;
  role: "user" | "assistant";
  content: string;
  events: WSEvent[];
  tokenUsage?: TokenUsage;
  timestamp: string;
  isStreaming: boolean;
  /** Successful assistant message finalization time. Cancel/interruption
   * uses stopped_at; failed turns use error/errorText. */
  completed_at?: string;
  stopped_at?: string;
  /** When this turn was interrupted by a new prompt, holds the
   * lifecycle_msg_id of the displacing prompt. Set alongside
   * stopped_at; distinguishes "Interrupted" from "Stopped". */
  interrupted_by_msg_id?: string;
  /** Transient marker set by the backend while it reconciles this
   * message after a backend restart (run_recovery is replaying jsonl,
   * pinning streaming/stopped_at, etc). Pushed via
   * `message_recovering_changed` WS frames and stamped onto REST
   * snapshots while reconciliation is in flight. Cleared the moment
   * recovery finishes — never persists across restarts. */
  isRecovering?: boolean;
  /** Backend lost the turn (shutdown/restart) but the detached runner
   * keeps the CLI process alive. Stamped by `turn_detached` WS event;
   * cleared on reconnect when REST replay overwrites the message. */
  isDetached?: boolean;
  /** No events arrived while streaming for STALE_TIMEOUT_MS. The
   * orchestrator task likely died silently. Cleared on the next event
   * or terminal transition (REST replay overwrites the message). */
  isStale?: boolean;
  /** ISO timestamp of the next retry attempt. Set by the backend on
   * upstream 429 / rate-limit while it sleeps between attempts and
   * cleared the moment the retry fires. Pushed via
   * `message_retrying_changed` WS frames; the bubble renders a
   * 'Retrying in Ns…' pill that ticks down to this timestamp. */
  retrying_until?: string | null;
  continuation_active?: number | null;
  /** Set when this turn succeeded only after >=1 automatic retry
   * (rate-limit / transient). Durable so the recovery stays visible
   * across reloads; pushed via `message_auto_retry_changed`. The bubble
   * badges the turn so an auto-retried-then-succeeded run is
   * distinguishable from a clean first-try run. */
  auto_retry?: { count: number; kind: string } | null;
  /** Per-turn session-picker payload — the `propose_sessions` MCP result
   * stamped on the assistant message that produced it. Drives the inline
   * picker rendered below this turn. Pushed via `message_ask_result_changed`
   * WS frames; null/absent when this turn proposed nothing. */
  ask_result?: AskResult | null;
  /** Id of the session the user CHOSE from this turn's picker (the
   * highlighted row). Pushed via `message_ask_choice_changed`; null/absent
   * until the user clicks Choose. Persists across reloads / tabs / previous
   * turns. */
  chosen_session_id?: string | null;
  status?: "sending" | "received" | "running" | "error" | "offline";
  errorText?: string;
  error?: boolean;
  /** Per-turn primary CLI session id for the agent that produced this
   * message (manager session in manager mode, native session otherwise).
   * Set via `turn_start` / `turn_complete` WS frames. */
  agent_session_id?: string | null;
  workers?: WorkerPanel[];
  images?: MessageImage[];
  files?: MessageFile[];
  trace_id?: string;
  agent_message_uuid?: string | null;
  event_payload_omitted?: boolean;
  /** Origin of this message. Set on user messages created by the supervisor
   * verdict loop so the frontend can nest them under the original user msg. */
  source?: string;
  /** Id of the parent user message for sub-turn prompts (supervisor verdict,
   * worker delegation, etc.). Used to render jump-to-parent navigation. */
  parent_id?: string;
  /** Present when the backend stubbed this message's events for transfer
   * (Tier-1 lazy fetch). When set, `events` /
   * `workers[*].events` are EMPTY; `event_count` is the true total and
   * `last_events` holds a small tail for the collapsed preview. Fetch the
   * full message via
   * `GET /api/sessions/{id}/messages/{messageId}/events`. */
  stub?: { event_count: number; last_events: WSEvent[] };
  /** Monotonic counter bumped whenever a `stub_invalidated` WS frame
   * replaces this message's stub. The collapse/expand component keys its
   * fetch cache on `id:stubVersion` so a re-stubbed (already-expanded)
   * turn busts its cached full events and re-fetches. */
  stubVersion?: number;
}

export interface FileDiscussion {
  id: string;
  file_path: string;
  line: number;
  title?: string;
  collapsed?: boolean;
  opened_by?: "user" | "agent" | string;
  created_at?: string;
  updated_at?: string;
}

/** One in-flight CLI run for a session, as reported by the backend's
 * authoritative `run_state` event. Multiple of these can be active at
 * once (manager turn + N worker delegations). The frontend renders a
 * labeled "running" badge per entry on the message / panel it targets. */
export interface RunInfo {
  run_id: string;
  kind: "manager" | "native" | "worker";
  /** Assistant message id this run mutates, or null if the lazy
   * assistant message hasn't been born yet (the badge then sits on
   * the user message bubble). */
  target_message_id: string | null;
  /** Worker panel id (workers only). */
  delegation_id?: string | null;
  /** OS PID of the runner subprocess. Null until the provider
   * spawns the runner and stamps the PID. Consumers can check
   * liveness via this PID. */
  pid: number | null;
  started_at: string;
  /** ISO timestamp of the most recent event the backend mirrored for
   * this run. Used internally for staleness tracking; the frontend
   * badge displays elapsed time from `started_at` instead. */
  last_event_at: string;
}

export type OrchestrationMode = "team" | "native" | "virtual";
export type SendMode = "queue" | "interrupt" | "steer" | "alter";

export interface CapabilityContextOutput {
  provider_kind: string;
  provider_name: string;
  content_kind: string;
  content: string;
}

export interface CapabilityContext {
  source_id: string;
  capability_id: string;
  name: string;
  category: string;
  outputs: CapabilityContextOutput[];
}

export interface QueuedPrompt {
  id: string;
  lifecycle_msg_id?: string;
  content: string;
  kind?: "send" | "queued_behind" | "interrupt";
  queue_position?: number;
  images_count?: number;
  files_count?: number;
  images?: import("./hooks/useWebSocket").ImagePayload[];
  files?: import("./hooks/useWebSocket").FilePayload[];
  orchestration_mode?: OrchestrationMode;
  send_target?: "worker" | "supervisor" | null;
  cli_prompt?: string | null;
  client_id?: string | null;
  capability_contexts?: CapabilityContext[];
  created_at?: string;
}

/** Reference from a rearranged tree node back to a concrete original
 * trace step — the rearranger re-parents flat trace steps under a goal
 * hierarchy and each node's `trace_refs` points at the step(s) it
 * represents. `step_index` is a 0-based index into the linked trace's
 * `steps` array. */
export interface TraceRef {
  trace_id: string;
  step_index: number;
}

/** Experimental rearranger: a hierarchical intent tree emitted by a
 * side Claude CLI session. See backend/rearranger.py + backend/rearranger_prompt.py.
 * `level` is 0..3; root is always level 0. */
export interface RearrangerNode {
  title: string;
  summary: string;
  level: number;
  /** Optional: the concrete trace step(s) this node re-parents. Leaf
   * nodes typically have exactly one ref; inner nodes may have zero
   * (pure grouping) or multiple (span summary). */
  trace_refs?: TraceRef[];
  children: RearrangerNode[];
}

export interface RearrangerTree {
  root: RearrangerNode;
}

/** Accumulated spend for the rearranger side-session. Tracked per
 * better-agent session so the UI can show "chat vs. rearranger" as a
 * group-by breakdown alongside the grand total. */
export interface RearrangerStats {
  call_count: number;
  total_cost_usd: number;
  token_usage: TokenUsage;
}

/** Lightweight worker entry returned in the session list payload — just
 * the agent_session_id + orchestration_mode. The full WorkerInfo (with
 * name, status, token usage) is fetched through the Team Orchestration extension. */
/** Adversarial-sync overlay — a per-message text substitution proposed
 * by the orchs.adv_sync ping-pong loop. Anchored to a `message_id` on
 * the parent session; carries the two driving forks' ids so the
 * frontend can navigate into the side-by-side ForkSplitView on click. */
export interface AdvSyncOverlay {
  id: string;
  message_id: string;
  original_text: string;
  agreed_text: string | null;
  status: "running" | "converged" | "failed" | "stopped" | "interrupted";
  supportive_fork_id: string;
  adversarial_fork_id: string;
  rounds_completed: number;
  max_rounds: number;
  created_at: string;
  updated_at: string;
  error?: string | null;
}

export interface SessionWorkerRef {
  agent_session_id: string;
  orchestration_mode: OrchestrationMode;
}

export type WorkerCreationPolicy = "ask" | "approve" | "deny";

/** Result of a turn's `propose_sessions` MCP tool, stamped per-turn on the
 * producing assistant message (`ChatMessage.ask_result`), pushed via
 * `message_ask_result_changed`, and rendered as the inline session picker
 * below that turn. Reusable in any session that proposes, not just the
 * Ask singleton. */
export interface AskResult {
  session_ids: string[];
  reasoning: string;
  /** Model's suggestion for the project the user should create the new
   * session in (pre-fills `NewSessionModal`). Empty string when the model
   * didn't propose one OR the proposed path didn't match a known project
   * (backend `_resolve_proposed_project` validates). The frontend treats
   * this as a shortcut, not a constraint — user can change the project
   * in the modal. */
  proposed_project_path?: string;
  /** `node_id` of the project at `proposed_project_path`, resolved
   * server-side from `project_store`. Required so multi-machine
   * deploys with two projects sharing the same `path` on different
   * nodes pre-fill the correct machine — the frontend would otherwise
   * pick the first `path` match arbitrarily. Empty when path is empty. */
  proposed_project_node_id?: string;
  /** Discriminates the picker's purpose. Absent/"ask" = the Ask flow.
   * "delegate_approval" = a session-bridge `delegate_to_session` call is
   * blocked waiting for the user to confirm the target session. */
  purpose?: "ask" | "delegate_approval";
  /** delegate_approval only: id of the pending delegation to resolve via
   * the Session Bridge extension backend. */
  delegation_id?: string;
  /** delegate_approval only: "fork" | "continue". */
  run_mode?: string;
  /** delegate_approval only: a preview of the prompt to be delegated. */
  prompt_preview?: string;
  /** delegate_approval only: true when the delegation will create a brand-
   * new session rather than target an existing one. The picker shows
   * "Create new session" as the approve action. */
  create_new?: boolean;
  /** delegate_approval only: set once the delegation has been resolved
   * (chosen/cancelled/expired). The footer picker stops rendering so
   * every open tab clears. */
  resolved?: boolean;
  /** Ask flow only: user-facing notice shown inside the picker when the
   * search worker couldn't return a usable answer (e.g. the reply had no
   * parseable result). Empty/absent on success. The picker still offers
   * Create-new / Never-mind — the error is NOT a red "Failed" bubble. */
  error?: string;
}

export interface Session {
  id: string;
  /** True only for a frontend-created session waiting in the durable
   * offline-action backlog. Cleared when POST /api/sessions succeeds. */
  offline_pending?: boolean;
  file_path?: string;
  manager_agent_session_id?: string | null;
  native_agent_session_id?: string | null;
  parent_session_id?: string | null;
  forked_from_agent_sid?: string | null;
  pagination?: {
    total_messages: number;
    oldest_loaded_seq: number | null;
    has_older: boolean;
  };
  /** Parent's last persisted message seq at fork time. Frontend uses
   * this to slice the rendered messages: seq <= fork_point_seq render
   * once above the split, seq > fork_point_seq render per-pane. Null
   * on root sessions. */
  fork_point_seq?: number | null;
  /** True once this fork has been "closed" — pane stays rendered but
   * cannot be focused and cannot receive new prompts. Persistent. */
  fork_closed?: boolean;
  /** Embedded child forks of this session. Only populated when the
   * session was loaded via /api/sessions/{id} (which returns the full
   * root tree). The sidebar list response always omits this field; use
   * `fork_count` there instead. Each entry is itself a full Session
   * with its own messages, claude sids, draft, etc. */
  forks?: Session[];
  /** Count of embedded forks (sidebar summary). Detail responses use
   * `forks` directly; the count is just for the sidebar badge. */
  fork_count?: number;
  orchestration_mode?: OrchestrationMode;
  /** Where the session was created. Defaults to "web" for legacy records
   * (backend migrates on read). CLI-created sessions are tagged "cli" so
   * the sidebar can render a badge. "import" = ingested from a native
   * provider CLI session. */
  source?: "web" | "cli" | "extension" | "import" | "internal";
  /** Whether the user is AWARE of having created this session (UI/CLI
   * create, import, file-edit, a fork, or a worker the user approved via
   * the popup) versus a session the system or an agent spun up on its own
   * (provisioning, agent create_session, auto-approved workers, internal
   * forks). Orthogonal to `source`. Backend migrates legacy records on
   * read; defaults to false (fail-closed) for non-user-aware sessions. */
  user_initiated?: boolean;
  virtual?: boolean;
  extension_id?: string;
  backing_session_ids?: string[];
  metadata?: Record<string, unknown>;
  name: string;
  model: string;
  reasoning_effort?: ReasoningEffort | "";
  permission?: Permission;
  provider_id?: string;
  cwd: string;
  /** Multi-machine: which node the session's filesystem ops route to.
   * `"primary"` for single-machine deploys (the sentinel for the local
   * backend) and for sessions created before the multi-machine cutover. */
  node_id?: string;
  created_at: string;
  updated_at: string;
  /** Timestamp of the most recent user-role message (sidebar/tabs sort). */
  last_user_prompt_at?: string;
  /** Timestamp this session was last opened on a client (tabs sort). */
  last_opened_at?: string;
  messages: ChatMessage[];
  /** Render-tree events that never got an owning assistant message
   * (events.jsonl rows with msg_id=None, e.g. a line the provider
   * flushed after the turn finalized). Rendered as detached "root
   * children" rather than dropped. Backend dedupes against stamped
   * uuids and filters to render types. */
  root_events?: WSEvent[];
  max_seq_by_sid?: Record<string, number>;
  message_count?: number;
  token_usage_total?: TokenUsage;
  /** Last turn's token usage (not cumulative). Used for context fill bar. */
  token_usage_last?: TokenUsage | null;
  /** Model's max context window capacity (tokens). Set by the backend
   * from the SDK's model_usage response. Null when unavailable (Gemini). */
  context_window?: number | null;
  /** Compact entries from the global worker registry. For full worker
   * details, hit the Team Orchestration extension directly. */
  workers?: SessionWorkerRef[];
  requirement_tags?: RequirementTag[];
  folder_id?: string | null;
  session_tags?: SessionTag[];
  search_score?: number;
  worker_count?: number;
  worker_creation_policy?: WorkerCreationPolicy;
  /** Supervisor toggle — when true, every primary turn is followed by
   * a supervisor verdict loop. Lazy-spawns `supervisor_agent_session_id`
   * on first enable; preserves it across off→on cycles for context
   * continuity. Owned by the backend; flipped via the supervisor extension. */
  supervisor_enabled?: boolean;
  /** Custom per-turn prompt for the supervisor. Empty string → use the
   * default adversarial verdict prompt. Persisted on the backend session. */
  supervisor_custom_prompt?: string;
  // Experimental rearranger feature — per-session opt-in.
  rearranger_enabled?: boolean;
  rearranger_tree?: RearrangerTree | null;
  rearranger_session_id?: string | null;
  rearranger_last_message_count?: number;
  rearranger_stats?: RearrangerStats | null;
  inline_tags?: InlineTag[];
  /** Per-message text substitutions produced by the adversarial-sync
   * ping-pong loop (orchs.adv_sync). Same push channel as inline_tags
   * — full post-mutation list arrives via `session_metadata_updated`
   * on every transition (round_completed, converged, failed, stopped,
   * interrupted). Anchored to a parent-session message_id; the two
   * forks live as siblings under this session's `forks` array. */
  adv_sync_overlays?: AdvSyncOverlay[];
  /** Backend-owned set of file panels open in the tabbed/split
   * right-panel viewer. Pulled via the session REST snapshot, pushed
   * via `session_metadata_updated` (same channel as inline_tags). */
  open_file_panels?: OpenFilePanel[];
  /** Provider-config-sync capability panels popped into the right side
   *  panel from an inline `open_config_panel` tool widget. Backend-owned,
   *  broadcast via `session_metadata_updated` (kind open_config_panels_set). */
  open_config_panels?: OpenConfigPanel[];
  /** In-progress chat input for this session. Persisted on the backend
   * via debounced PATCH /api/sessions/{id}/draft, broadcast via
   * `session_metadata_updated` so every tab converges. */
  draft_input?: string;
  /** Monotonic seq for stale-write guard on PATCH /draft. */
  draft_input_seq?: number;
  /** In-progress image attachments for this session. Persisted alongside
   * draft_input so they survive navigation and page reloads. */
  draft_images?: PastedImage[];
  /** Prompts accepted while another turn is running. Persisted so reloads
   * show them as queued banners instead of losing them or rendering them as
   * normal chat messages before the backend actually sends them. */
  queued_prompts?: QueuedPrompt[];
  capability_contexts?: CapabilityContext[];
  /** True if this session is the ephemeral inner side of a prompt-
   * engineering session. Filtered out of the sidebar list — the
   * resume affordance is the parent's `pending_eng_session_id`.
   * @deprecated — use working_mode instead. */
  is_prompt_engineering?: boolean;
  /** Working mode discriminator — set for ephemeral sessions
   * (prompt_engineering, file_editing, etc). Null/undefined for
   * normal sessions. Sidebar filters these out unless the session is
   * a persistent file-mode session (`working_mode_meta.persistent`). */
  working_mode?: string | null;
  /** Per-mode metadata shipped from the backend (see
   * working_mode.mark_working_mode). The fields populated depend on
   * `working_mode`:
   *   - "file_editing": project_cwd, file_paths, original_contents,
   *     persistent.
   *   - "prompt_engineering": parent_session_id, temp_file_path,
   *     original_content, mode ("fork" | "new"). */
  working_mode_meta?: {
    project_cwd?: string;
    file_paths?: string[];
    original_contents?: Record<string, string>;
    file_discussions?: FileDiscussion[];
    temp_file_path?: string;
    original_content?: string;
    parent_session_id?: string;
    mode?: "fork" | "new";
    persistent?: boolean;
  } | null;
  /** Sidebar summary only — present when this (parent) session has a
   * live ephemeral engineering session pointing at it. The sidebar
   * renders a ⚙ "resume" badge when set; clicking the badge re-enters
   * the engineering overlay against the referenced eng session. */
  pending_eng_session_id?: string | null;
  /** Discriminator for internal-only embedded Better Agent sessions. "user" =
   * normal session (root or user-facing fork). Other values are
   * filtered out of the sidebar fork list and the ForkSplitView so
   * users never see them; backend session_watcher still tails their
   * jsonls.
   *   - "delegate_fork": per-(caller, worker) thread used by
   *     manager-mode delegations. */
  kind?: "user" | "delegate_fork" | "supervisor_worker" | "adv_sync_fork";
  caller_agent_session_id?: string | null;
  /** Whether the browser-harness MCP tool is available for this session.
   * Checked by default on new sessions. The tool spawns a dedicated
   * testing agent that autonomously executes high-level test descriptions. */
  browser_harness_enabled?: boolean;
  browser_harness_headless?: boolean;
  pinned?: boolean;
  archived?: boolean;
  /** User opted this session in as eligible for the Team worker picker.
   * Only sessions with this flag appear in the "mark existing" picker. */
  worker_eligible?: boolean;
  /** Per-session scratchpad notes. Persisted on the backend, pushed
   * via `session_metadata_updated` for cross-tab convergence. */
  notes?: Note[];
  /** Cross-provider TODO list reconstructed from TodoWrite (Claude) /
   * update_topic (Gemini) tool_use events. Backend is the single
   * source of truth: the `apply_event` hook + `todos_extractor`
   * derive this in real time and broadcast via `session_metadata_updated`. */
  current_todos?: TodoItem[];
  /** Task list reconstructed from TaskCreate / TaskUpdate tool_use
   * events. Stored separately from current_todos for clear UI
   * separation. */
  current_tasks?: TaskItem[];
  /** Right-panel UI state — persisted per-session. `right_panel_open`
   * defaults to true (default-on-read at the backend load boundary);
   * `right_panel_active_tab` defaults to null (render-time fallback
   * picks the first tab with content, or 'files' if all empty). */
  right_panel_open?: boolean;
  right_panel_active_tab?:
    | "files"
    | "notes"
    | "canvas"
    | "comments"
    | "todos"
    | null;
  /** Sidebar-list decorate fields — added by the backend session-list
   * decorate step, NOT on the persisted tree. Live badges read status from
   * `sessionRegistry`; these power the status-sort row-rank fallback for
   * deeper-page rows the registry has not seeded yet. */
  is_running?: boolean;
  monitoring_state?: string;
  unread_count?: number;
  pending_user_input_count?: number;
  markers?: Record<string, { color: string; tooltip: string; sound?: boolean; tag?: string }>;
}

export interface Note {
  id: string;
  text: string;
  created_at: string;
}

export interface TodoItem {
  /** Display text. Claude: `todos[i].content`. Gemini: `update_topic.title`. */
  content: string;
  status: "pending" | "in_progress" | "completed";
  /** Present-tense description shown while in_progress. Claude:
   * `todos[i].activeForm`. Gemini: `update_topic.summary`. */
  activeForm?: string;
  /** Dedup id used by the Gemini delta branch (Claude REPLACE never
   * sets it). Stable per tool_use across replay. */
  source_id?: string;
}

/** Task item from TaskCreate / TaskUpdate tool calls. Same shape as
 * TodoItem — the backend uses the identical field set. */
export type TaskItem = TodoItem;

export interface TraceStep {
  trace_id: string;
  step_index: number;
  step_type: string;
  thread_id?: string;
  thread_name?: string;
  ephemeral: boolean;
  input_prompt: string;
  raw_output: string;
  parsed_output?: unknown;
  parse_error?: string;
  token_usage?: Record<string, number>;
  duration_ms?: number;
  error?: string;
  subagent_types?: string[];
}

export interface Trace {
  trace_id: string;
  session_id: string;
  user_prompt: string;
  timestamp: string;
  duration_ms?: number;
  total_token_usage: Record<string, number>;
  step_count: number;
  steps: TraceStep[];
}

export interface TraceIndexEntry {
  trace_id: string;
  session_id: string;
  timestamp: string;
  user_prompt_preview: string;
  duration_ms?: number;
  step_count: number;
  total_token_usage: Record<string, number>;
}

export interface FileNode {
  name: string;
  path: string;
  type: "file" | "directory";
  children?: FileNode[];
}

export type SearchMethod = "path" | "name" | "symbols";

export interface FileSearchResult {
  root: FileNode | null;
  truncated: boolean;
  count: number;
  symbols_unavailable?: boolean;
}

export interface Project {
  path: string;
  /** Multi-machine: which node this project's path lives on. Default
   * `"primary"` for sessions/projects created before the cutover. */
  node_id?: string;
  name: string;
  created_at: string;
  last_used: string;
  running_count?: number;
  unread_session_count?: number;
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: { name: string; path: string }[];
  /** False when the typed path doesn't resolve to an existing directory.
   * The picker offers to create it on select. */
  exists: boolean;
}

export type ProviderMode = "subscription" | "api_key";
export type ReasoningEffort = "none" | "minimal" | "low" | "medium" | "high" | "xhigh";

/** Per-provider-native permission. Kind-shaped: {"mode"} for claude/gemini/openai,
 * {"approval","sandbox"} for codex. {} = inherit the provider default. */
export type Permission = Record<string, string>;
/** Axis → allowed-values map for the provider's permission selector(s). */
export type PermissionOptions = Record<string, string[]>;

/** A pending interactive tool/command approval from a runner mid-turn. */
export interface ToolApproval {
  approval_id: string;
  app_session_id: string;
  run_id: string;
  provider_kind: string;
  tool_name: string;
  summary: Record<string, unknown>;
}

export interface Provider {
  id: string;
  name: string;
  kind: string;
  mode: ProviderMode;
  base_url: string;
  config_dir: string;
  custom_models: string[];
  default_model: string;
  reasoning_effort_options: ReasoningEffort[];
  default_reasoning_effort: ReasoningEffort | "";
  permission_options: PermissionOptions;
  default_permission: Permission;
  /** Last model the user chose for this provider (backend-remembered).
   * Pickers pre-choose it over `default_model` when switching provider. */
  last_model?: string;
  last_reasoning_effort?: ReasoningEffort;
  has_api_key: boolean;
  /** Whether this provider can branch a session via the CLI's
   * fork-session primitive. Drives UI gating for Fork, Fork-and-send,
   * Adversarial Sync, Prompt-Engineer refine, Rearranger toggle.
   * Backend-resolved from Provider.supports_fork. */
  supports_fork: boolean;
  /** Whether this provider can drive the persistent "manager" session
   * in manager orchestration mode. Backend-resolved from
   * Provider.supports_manager_mode. Drives `NewSessionModal` gating of
   * the "manager" mode button and filtering of the manager-role
   * provider picker. */
  supports_manager_mode: boolean;
  /** Whether this provider can rewind / truncate a session at a given
   * message UUID. Backend-resolved from Provider.supports_rewind.
   * Drives UI gating for the Rewind / rewind-and-retry buttons. */
  supports_rewind: boolean;
  supports_steering: boolean;
  supports_native_subagents: boolean;
  supports_reasoning_effort: boolean;
  /** Raw per-provider capability overrides (only explicitly-set keys).
   * The resolved `supports_*` fields above already bake these in; this
   * map lets the provider editor show tri-state inherit/force-on/force-off
   * without confusing an override with a kind default. */
  capability_overrides: Partial<Record<string, boolean>>;
}

export interface ProvidersState {
  default_provider_id: string | null;
  providers: Provider[];
}

export interface ProjectConfigFile {
  name: string;
  path: string;
  category: "instructions" | "settings" | "skill" | "hook";
  description: string;
  exists: boolean;
  size: number;
  modified: string | null;
}

/** Backend startup task — long-running work (migrations, recovery)
 * moved off the FastAPI startup critical path. Authoritative state
 * lives in the backend's in-memory `startup_task_registry`; the
 * frontend reflects it via `GET /api/startup_tasks` on mount and live
 * `startup_task_changed` WS pings. Non-blocking banner; sessions whose
 * messages are still being recovered show a per-message `isRecovering`
 * pill independently. */
export interface StartupTask {
  id: string;
  label: string;
  state: "running" | "done" | "failed";
  started_at: string;
  finished_at: string | null;
  error: string | null;
}
