import type { Dispatch, SetStateAction } from "react";

import type {
  ChatMessage,
  OpenFilePanel,
  RunInfo,
  Session,
  WSEvent,
} from "../../types";
import type { InlineTag } from "../../types/inlineTag";

export interface AppWebSocketOptions {
  /** The app_session_id currently being viewed in the UI. When this
   * changes, the hook sends `unsubscribe` for the previous id and
   * `subscribe` for the new one so the backend's SessionWatcher knows
   * where to push live events for this WS. Pass null when no session
   * is open. */
  currentAppSessionId?: string | null;
  /** Before a reconnect restores subscriptions, reconcile the selected
   * tree and seed its cursors from authoritative REST state. */
  prepareSessionSubscriptions?: () => void | Promise<void>;
  /** Additional session ids to keep subscribed beyond the focused one.
   * Used by the split-pane fork view: every visible pane's session
   * stays subscribed so its messages_replay / messages_delta /
   * user_message_persisted / run_state / session_metadata_updated
   * frames flow in. Live `manager_event`/`worker_event` frames route
   * only when the backend provides their owning `app_session_id`. */
  additionalAppSessionIds?: string[];
  onRewindComplete?: (appSessionId: string, messages: ChatMessage[]) => void;
  /** Backend's response to a subscribe with `since_seq=N`. Carries
   * every persisted message with `seq >= N` plus the live in-flight
   * assistant message if mid-stream. The caller upserts by id. */
  onMessagesReplay?: (appSessionId: string, messages: ChatMessage[]) => void;
  /** Per-event message updates — currently fired by the backend when
   * the lazy assistant message is born. Caller upserts by id (same
   * reducer as messages_replay). */
  onMessagesDelta?: (appSessionId: string, messages: ChatMessage[]) => void;
  /** Backend ack: the user's prompt has been persisted. Carries the
   * canonical user_message (with `client_id` echo) so the caller can
   * drop the matching optimistic pending entry and append the
   * canonical message to its session. Dispatched imperatively in
   * `onmessage` rather than via the `events` buffer because that
   * buffer is wiped by `turn_start`'s `setEvents([])` and a tight
   * back-to-back burst can lose the ack before the React effect runs. */
  onUserMessagePersisted?: (
    appSessionId: string,
    userMessage: ChatMessage
  ) => void;
  onSteerPromptPersisted?: (
    appSessionId: string,
    clientId?: string | null
  ) => void;
  onPromptSendError?: (
    appSessionId: string,
    clientId: string,
    errorText: string
  ) => void;
  /** Backend-owned run_state snapshot for a session. Authoritative.
   * Empty array means "nothing running for this session". */
  onRunState?: (appSessionId: string, runs: RunInfo[]) => void;
  /** Live `manager_event` / `worker_event` / `worker_start` /
   * `worker_complete` / `turn_start` / `turn_complete` frames
   * for the currently-viewed session. The caller routes them onto
   * the canonical assistant message in `messages[]` so the rendered
   * bubble grows in real time without needing a synthetic
   * "streamingMessage" twin. */
  onLiveTurnEvent?: (appSessionId: string, event: WSEvent) => void;
  /** Turn ended (complete, stopped, or error). Gives the session layer
   * a chance to flip `isStreaming` on the in-flight assistant message
   * and stamp `stopped_at` so the "Running…" indicator vanishes
   * immediately without waiting for REST. When `errorText` is present
   * (the `error` frame), the backend's exception path already removed
   * the persisted assistant message, so the caller drops the live
   * streaming placeholder rather than leaving a phantom "No output". */
  onTurnTerminal?: (
    appSessionId: string,
    stoppedAt?: string,
    interruptedByMsgId?: string | null,
    errorText?: string,
  ) => void;
  /** Backend lost the turn (shutdown/restart) but the detached runner
   * keeps the CLI alive. Caller stamps `isDetached` on the in-flight
   * assistant message so the bubble renders "Reconnecting…" instead of
   * a stuck "Running…" spinner. Clears on reconnect via REST replay. */
  onTurnDetached?: (appSessionId: string) => void;
  /** User-message lifecycle state transitions emitted by the backend's
   * event bus. Five event types — `user_message_queued`,
   * `user_message_sent`, `user_message_received`, `user_message_done`,
   * `user_message_failed`. Caller projects the lifecycle state onto
   * the message identified by `lifecycle_msg_id` (queued events also
   * carry `kind` ∈ {send, queued_behind, interrupt} and optional
   * `interrupts_msg_id`; done events optionally carry
   * `interrupted_by_msg_id`). All five events are persisted to
   * events.jsonl so the projection survives reconnects via replay. */
  onUserMsgLifecycle?: (appSessionId: string, event: WSEvent) => void;
  /** Read the highest seq the caller has applied for a given session.
   * Sent as `since_seq` on every subscribe so the backend knows where
   * to start the replay. Returning 0 means "send everything". */
  getSinceSeq?: (appSessionId: string | null) => number;
  /** Read the highest events.jsonl seq the caller has already received
   * for a given session (typically seeded from the REST snapshot's
   * `max_seq_by_sid`). Sent as `events_from_seq` on every subscribe;
   * the backend's wire tailer drains the gap before live events flow,
   * eliminating the REST↔WS race without uuid-dedup reliance. */
  getEventsFromSeq?: (appSessionId: string | null) => number;
  getEventsCursorKnown?: (appSessionId: string | null) => boolean;
  /** Notify the caller that an event with the given seq was just
   * received for `appSessionId`, so its watermark cursor can advance.
   * Called for every WS frame that carries a top-level `seq`. */
  onEventSeqAdvance?: (appSessionId: string, seq: number) => void;
  /** Cross-tab metadata sync (inline_tags, draft_input, draft_images, fork_closed).
   * Backend echoes the patch on every REST mutation. The originating
   * tab skips its own broadcasts (compared via `originated_by`) so a
   * debounced PATCH echo never clobbers newer keystrokes. */
  onSessionMetadataUpdated?: (
    appSessionId: string,
    patch: {
      inline_tags?: InlineTag[];
      open_file_panels?: OpenFilePanel[];
      open_config_panels?: import("../../types").OpenConfigPanel[];
      draft_input?: string;
      draft_images?: import("../../types").PastedImage[];
      queued_prompts?: import("../../types").QueuedPrompt[];
      fork_closed?: boolean;
      model?: string;
      cwd?: string;
      supervisor_enabled?: boolean;
      message_count?: number;
      updated_at?: string;
      last_user_prompt_at?: string;
      last_opened_at?: string;
      right_panel_open?: boolean;
      right_panel_active_tab?:
        | "files"
        | "notes"
        | "canvas"
        | "comments"
        | "todos"
        | "screen"
        | "changes"
        | "communications"
        | "board"
        | null;
      right_panel_width?: number | null;
      right_panel_mobile_height?: number | null;
      right_panel_todos_dismissed?: boolean;
      right_panel_auto_opened_by?: import("../../types").Session["right_panel_auto_opened_by"];
      sidebar_minimized?: boolean;
    }
  ) => void;
  /** A new fork session was just born (server-emitted on every fork
   * creation). Caller appends to its split-pane state if it's viewing
   * the parent. */
  onSessionForked?: (
    childSession: Session,
    parentSessionId: string | null
  ) => void;
  /** A NEW (non-fork) session was just created in some tab — added
   * for INV-3 / DIV-4 multi-tab convergence. Frontend dedup-by-id is
   * required since the originating tab already inserted via the REST
   * POST response. */
  onSessionCreated?: (session: Session) => void;
  /** A session was deleted in some tab — multi-tab convergence so
   * tab B's sidebar drops it without a manual refresh. Frontend
   * dedups-by-id (the originating tab already filtered locally). */
  onSessionDeleted?: (sessionId: string) => void;
  /** A session was renamed (auto-title from first prompt, or manual
   * rename in another tab). Replaces a prior pattern that scanned the
   * shared `events` buffer on every render — the scan cost grew with
   * the buffer and re-ran on every WS frame from any session. */
  onSessionRenamed?: (sessionId: string, name: string) => void;
  /** Backend's project list changed (auto-add on session create or
   * REST POST/DELETE/touch from any tab). Caller refetches the list.
   * Replaces a buffer-tail scan that fired on every WS frame. */
  onProjectsChanged?: () => void;
  /** Project structure updates changed (new capture or marked seen).
   * Carries project_id and unseen_count. */
  onProjectUpdatesChanged?: (data: { project_id: string; unseen_count: number }) => void;
  /** Worker list for a session changed (created/destroyed/updated).
   * Caller refetches sessions to update worker_count in the sidebar. */
  onWorkersChanged?: () => void;
  /** Virtual session folders/tags changed. Caller refetches organization
   * snapshot and session summaries. */
  onSessionOrganizationChanged?: () => void;
  /** Project mapping groups changed (auto-match rebuild or user edit).
   * Caller refetches GET /api/project-mappings. */
  onProjectMappingsChanged?: () => void;
  /** Backend-emitted notification that supervisor verdict failed
   * (kind=verdict_failed), hit MAX_VERDICTS_PER_TURN
   * (kind=verdict_capped), or terminated because the worker is
   * legitimately blocked on user input (kind=await_user). Called
   * once per emit; caller renders a banner / toast. Without this,
   * supervision failures + await_user signals are invisible. */
  onSupervisorEvent?: (info: {
    sessionId?: string;
    kind: string;
    message?: string;
    error?: string;
    reason?: string;
  }) => void;
  /** A pull request was created by the agent (Claude CLI `pr-link`
   * agent_message). Fired only on the LIVE push, never on replay, so the
   * caller can show an ephemeral chat-panel toast. */
  onPrLink?: (info: {
    sessionId?: string;
    prNumber?: number;
    prUrl: string;
    prRepository?: string;
  }) => void;
  /** A fire-and-forget notice pushed by an extension via
   * Client.notify_toast — one generic event type every extension shares,
   * instead of each inventing its own. Fired only on the LIVE push. */
  onExtensionToast?: (info: {
    sessionId?: string;
    message: string;
    level?: string;
  }) => void;
  /** Backend ack that a prompt was queued (not sent immediately
   * because another turn was running). */
  onPromptQueued?: (data: {
    app_session_id: string;
    queued_id: string;
    prompt_preview: string;
    send_mode: string;
    queue_position: number;
    client_id?: string;
  }) => void;
  /** A queued/interrupted turn has started processing (queue drained). */
  onTurnStarted?: (appSessionId: string) => void;
  /** Backend consumed a queued prompt (either live or re-emitted on
   * subscribe to clear stale frontend state). */
  onQueueConsumed?: (data: {
    app_session_id: string;
    queued_id: string | null;
  }) => void;
  /** Catch-all hook called once per parsed WS frame, BEFORE typed
   * handlers run. Used by the progress bus to match `extendUntilWS`
   * predicates against backend lifecycle events (rewind_complete,
   * turn_complete, turn_start, etc.) and resolve in-flight ops
   * whose backend work continues past the originating REST call. */
  onAnyEvent?: (event: WSEvent) => void;
  /** Per-message transient pill fired by backend run_recovery while it
   * reconciles an in-flight run after a backend restart. Caller flips
   * the matching assistant message's `isRecovering` field so the
   * MessageBubble renders an "Updating state…" indicator until the
   * value flips back to false. */
  onMessageRecoveringChanged?: (
    appSessionId: string,
    msgId: string,
    value: boolean
  ) => void;
  /** Per-message pill fired by the orchestrator while it sleeps between
   * a rate-limited (429) attempt and the next retry. `retryAt` is the
   * absolute ISO timestamp of the next attempt; `null` clears the pill
   * (next attempt is firing now). The bubble renders "Retrying in Ns…"
   * with a locally-ticking countdown until `retryAt` passes or the
   * field clears. */
  onMessageRetryingChanged?: (
    appSessionId: string,
    msgId: string,
    retryAt: string | null,
    errorText: string | null
  ) => void;
  /** Terminal error stamped on an assistant message outside live turn
   * framing (run-recovery finalize) — renders the failed bubble + Retry
   * without a reload. */
  onMessageErrorChanged?: (
    appSessionId: string,
    msgId: string,
    errorText: string | null
  ) => void;
  /** A turn that succeeded only after >=1 automatic retry — durable
   * badge so the recovery is distinguishable from a clean first-try run. */
  onMessageAutoRetryChanged?: (
    appSessionId: string,
    msgId: string,
    autoRetry: { count: number; kind: string } | null
  ) => void;
  onMessageContentUpdated?: (
    appSessionId: string,
    msgId: string,
    content: string
  ) => void;
  onMessageContinuationChanged?: (
    appSessionId: string,
    msgId: string,
    chainDepth: number | null
  ) => void;
  /** Per-turn provider/model/effort actually used. Re-stamped on each retry
   *  iteration so a mid-message selector switch updates the badge live. */
  onMessageRunMetaChanged?: (
    appSessionId: string,
    msgId: string,
    runMeta: import("../../types").ChatMessage["run_meta"]
  ) => void;
  /** Per-turn picker payload (`ask_result`) stamped on an assistant
   * message — drives the inline session picker rendered below that turn. */
  onMessageAskResultChanged?: (
    appSessionId: string,
    msgId: string,
    askResult: import("../../types").AskResult | null
  ) => void;
  /** The session the user chose from a turn's picker (highlighted row). */
  onMessageAskChoiceChanged?: (
    appSessionId: string,
    msgId: string,
    chosenSessionId: string | null
  ) => void;
  /** Backend reconcile completed (fast or slow). The initial GET may
   * have returned stale cache; the frontend should silently refetch
   * if the user is viewing this root's session. */
  onSessionReconciled?: (rootId: string) => void | Promise<void>;
  /** Stable per-tab id sent in PATCH bodies; events whose
   * `originated_by` matches this id are ignored locally. */
  clientId?: string;
}

export interface WebSocketProjectionState {
  currentAppSessionId: string | null;
  clientId?: string;
  streamingLoadPhase: "starting" | "connected" | null;
  setEvents: Dispatch<SetStateAction<WSEvent[]>>;
  setIsStreaming: Dispatch<SetStateAction<boolean>>;
  setStreamingPhase: Dispatch<SetStateAction<"manager" | "worker" | null>>;
  setStreamingLoadPhase: Dispatch<SetStateAction<"starting" | "connected" | null>>;
  setLastResult: Dispatch<SetStateAction<Record<string, unknown> | null>>;
  setStreamingAppSessionId: Dispatch<SetStateAction<string | null>>;
  applyTerminalEvent: (result: Record<string, unknown> | null) => void;
}

export interface WebSocketProjectorContext {
  callbacks: AppWebSocketOptions;
  state: WebSocketProjectionState;
}

export interface ProjectionResult {
  stop?: boolean;
  completion?: void | Promise<void>;
}
