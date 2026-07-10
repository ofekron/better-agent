import { useState, useRef, useCallback, useEffect } from "react";
import type {
  ChatMessage,
  CapabilityContext,
  OpenFilePanel,
  OrchestrationMode,
  RunInfo,
  SendMode,
  Session,
  WSEvent,
} from "../types";
import type { InlineTag } from "../types/inlineTag";
import { eventBus } from "../lib/eventBus";
import { getWsUrl } from "../api";
import { logPromptSend } from "../lib/promptSendLog";
import { SnapshotTransport } from "../lib/snapshotTransport";

export interface ImagePayload {
  data: string;
  media_type: string;
}

export interface FilePayload {
  name: string;
  data: string;
  media_type: string;
  size: number;
}

type StubInvalidation = {
  app_session_id: string;
  msg_id: string;
  stub: { event_count: number; last_events: WSEvent[] };
};

export type StreamingPhase = "manager" | "worker" | null;

/** Fine-grained loading phase while the CLI subprocess is starting up.
 * Null once actual content starts flowing. */
export type StreamingLoadPhase = "starting" | "connected" | null;

function parseStubInvalidations(data: unknown): StubInvalidation[] {
  const payload = data as { changes?: unknown };
  const items = Array.isArray(payload?.changes) ? payload.changes : [data];
  return items.filter((item): item is StubInvalidation => {
    const row = item as StubInvalidation | null;
    return Boolean(
      row
        && typeof row.app_session_id === "string"
        && row.app_session_id.length > 0
        && typeof row.msg_id === "string"
        && row.msg_id.length > 0
        && row.stub
        && typeof row.stub.event_count === "number"
        && Array.isArray(row.stub.last_events),
    );
  });
}

export function resolveLiveFrameSessionId(
  event: WSEvent,
  _focusedSessionId: string | null | undefined,
): string | null {
  const data = event.data as {
    app_session_id?: unknown;
    session_id?: unknown;
  } | undefined;
  if (typeof data?.app_session_id === "string" && data.app_session_id) {
    return data.app_session_id;
  }
  if (event.type === "todos_snapshot" && typeof data?.session_id === "string" && data.session_id) {
    return data.session_id;
  }
  return null;
}

interface UseWebSocketOptions {
  /** The app_session_id currently being viewed in the UI. When this
   * changes, the hook sends `unsubscribe` for the previous id and
   * `subscribe` for the new one so the backend's SessionWatcher knows
   * where to push live events for this WS. Pass null when no session
   * is open. */
  currentAppSessionId?: string | null;
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
  /** A backend reconcile appended late events to a collapsed historical
   * turn — replace its stale stub so an expanded turn re-fetches fresh. */
  onStubInvalidated?: (
    appSessionId: string,
    msgId: string,
    stub: { event_count: number; last_events: WSEvent[] }
  ) => void;
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
   * immediately without waiting for REST. */
  onTurnTerminal?: (
    appSessionId: string,
    stoppedAt?: string,
    interruptedByMsgId?: string | null
  ) => void;
  /** Backend lost the turn (shutdown/restart) but the detached runner
   * keeps the CLI alive. Caller stamps `isDetached` on the in-flight
   * assistant message so the bubble renders "Reconnecting…" instead of
   * a stuck "Running…" spinner. Clears on reconnect via REST replay. */
  onTurnDetached?: (appSessionId: string) => void;
  /** No WS events arrived while streaming for STALE_TIMEOUT_MS.
   * Covers the case where the orchestrator task dies silently without
   * emitting turn_stopped/turn_complete/error. Caller stamps the
   * in-flight message as stale so the UI shows a warning instead of a
   * stuck spinner. Clears on the next event or terminal transition. */
  onTurnStale?: (appSessionId: string) => void;
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
      open_config_panels?: import("../types").OpenConfigPanel[];
      draft_input?: string;
      draft_images?: import("../types").PastedImage[];
      queued_prompts?: import("../types").QueuedPrompt[];
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
      right_panel_auto_opened_by?: import("../types").Session["right_panel_auto_opened_by"];
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
    runMeta: import("../types").ChatMessage["run_meta"]
  ) => void;
  /** Per-turn picker payload (`ask_result`) stamped on an assistant
   * message — drives the inline session picker rendered below that turn. */
  onMessageAskResultChanged?: (
    appSessionId: string,
    msgId: string,
    askResult: import("../types").AskResult | null
  ) => void;
  /** The session the user chose from a turn's picker (highlighted row). */
  onMessageAskChoiceChanged?: (
    appSessionId: string,
    msgId: string,
    chosenSessionId: string | null
  ) => void;
  /** Backend's async-reconcile progress notifier. Fires only when a
   * reconcile crosses the 0.3s threshold: `started` lands when the
   * timer fires, `finished` when the reconcile completes (or fails).
   * `root_id` keys the per-root tree. Caller renders a tiny
   * "reconciling…" badge while the flag is `started`. */
  onSessionProcessing?: (
    rootId: string,
    kind: "started" | "finished"
  ) => void;
  /** Backend reconcile completed (fast or slow). The initial GET may
   * have returned stale cache; the frontend should silently refetch
   * if the user is viewing this root's session. */
  onSessionReconciled?: (rootId: string, authoritative?: boolean) => void | Promise<void>;
  /** Stable per-tab id sent in PATCH bodies; events whose
   * `originated_by` matches this id are ignored locally. */
  clientId?: string;
}

interface UseWebSocketReturn {
  connected: boolean;
  sendMessage: (
    prompt: string,
    model: string,
    cwd: string,
    claudeSessionId?: string | null,
    appSessionId?: string | null,
    images?: ImagePayload[],
    orchestrationMode?: OrchestrationMode,
    clientId?: string | null,
    sendMode?: SendMode | null,
    sendTarget?: "worker" | "supervisor" | null,
    files?: FilePayload[],
    capabilityContexts?: CapabilityContext[],
  ) => boolean;
  stopStreaming: (appSessionId: string) => boolean;
  sendPromoteQueued: (
    appSessionId: string,
    action?: "interrupt" | "steer",
    queuedId?: string,
    queuedIds?: string[],
  ) => boolean;
  sendCancelQueued: (appSessionId: string, queuedId?: string) => boolean;
  sendUpdateQueued: (
    appSessionId: string,
    queuedId: string,
    content: string
  ) => boolean;
  sendBeginQueuedEdit: (appSessionId: string, queuedId: string) => boolean;
  sendFinishQueuedEdit: (appSessionId: string, queuedId: string) => boolean;
  events: WSEvent[];
  isStreaming: boolean;
  isStopping: boolean;
  streamingPhase: StreamingPhase;
  streamingLoadPhase: StreamingLoadPhase;
  lastResult: Record<string, unknown> | null;
  streamingAppSessionId: string | null;
}

export function useWebSocket(
  url: string,
  options: UseWebSocketOptions = {}
): UseWebSocketReturn {
  // Latest-callback refs so onmessage sees fresh handlers without
  // triggering a WebSocket reconnect every time App re-renders.
  const onRewindCompleteRef = useRef(options.onRewindComplete);
  const onMessagesReplayRef = useRef(options.onMessagesReplay);
  const onStubInvalidatedRef = useRef(options.onStubInvalidated);
  const onMessagesDeltaRef = useRef(options.onMessagesDelta);
  const onUserMessagePersistedRef = useRef(options.onUserMessagePersisted);
  const onSteerPromptPersistedRef = useRef(options.onSteerPromptPersisted);
  const onPromptSendErrorRef = useRef(options.onPromptSendError);
  const onRunStateRef = useRef(options.onRunState);
  const onLiveTurnEventRef = useRef(options.onLiveTurnEvent);
  const onTurnTerminalRef = useRef(options.onTurnTerminal);
  const onTurnDetachedRef = useRef(options.onTurnDetached);
  const onTurnStaleRef = useRef(options.onTurnStale);
  const onUserMsgLifecycleRef = useRef(options.onUserMsgLifecycle);
  const getSinceSeqRef = useRef(options.getSinceSeq);
  const getEventsFromSeqRef = useRef(options.getEventsFromSeq);
  const getEventsCursorKnownRef = useRef(options.getEventsCursorKnown);
  const onEventSeqAdvanceRef = useRef(options.onEventSeqAdvance);
  const onSessionMetadataUpdatedRef = useRef(options.onSessionMetadataUpdated);
  const onSessionForkedRef = useRef(options.onSessionForked);
  const onSessionCreatedRef = useRef(options.onSessionCreated);
  const onSessionDeletedRef = useRef(options.onSessionDeleted);
  const onSessionRenamedRef = useRef(options.onSessionRenamed);
  const onProjectsChangedRef = useRef(options.onProjectsChanged);
  const onProjectUpdatesChangedRef = useRef(options.onProjectUpdatesChanged);
  const onWorkersChangedRef = useRef(options.onWorkersChanged);
  const onSessionOrganizationChangedRef = useRef(options.onSessionOrganizationChanged);
  const onProjectMappingsChangedRef = useRef(options.onProjectMappingsChanged);
  const onSupervisorEventRef = useRef(options.onSupervisorEvent);
  const onPrLinkRef = useRef(options.onPrLink);
  const onPromptQueuedRef = useRef(options.onPromptQueued);
  const onTurnStartedRef = useRef(options.onTurnStarted);
  const onQueueConsumedRef = useRef(options.onQueueConsumed);
  const onAnyEventRef = useRef(options.onAnyEvent);
  const onMessageRecoveringChangedRef = useRef(
    options.onMessageRecoveringChanged
  );
  const onMessageRetryingChangedRef = useRef(
    options.onMessageRetryingChanged
  );
  const onMessageAutoRetryChangedRef = useRef(
    options.onMessageAutoRetryChanged
  );
  const onMessageContentUpdatedRef = useRef(
    options.onMessageContentUpdated
  );
  const onMessageContinuationChangedRef = useRef(
    options.onMessageContinuationChanged
  );
  const onMessageRunMetaChangedRef = useRef(options.onMessageRunMetaChanged);
  const onMessageAskResultChangedRef = useRef(
    options.onMessageAskResultChanged
  );
  const onMessageAskChoiceChangedRef = useRef(
    options.onMessageAskChoiceChanged
  );
  const onSessionProcessingRef = useRef(options.onSessionProcessing);
  const onSessionReconciledRef = useRef(options.onSessionReconciled);
  const clientIdRef = useRef(options.clientId);
  useEffect(() => {
    onRewindCompleteRef.current = options.onRewindComplete;
    onMessagesReplayRef.current = options.onMessagesReplay;
    onStubInvalidatedRef.current = options.onStubInvalidated;
    onMessagesDeltaRef.current = options.onMessagesDelta;
    onUserMessagePersistedRef.current = options.onUserMessagePersisted;
    onSteerPromptPersistedRef.current = options.onSteerPromptPersisted;
    onPromptSendErrorRef.current = options.onPromptSendError;
    onRunStateRef.current = options.onRunState;
    onLiveTurnEventRef.current = options.onLiveTurnEvent;
    onTurnTerminalRef.current = options.onTurnTerminal;
    onTurnDetachedRef.current = options.onTurnDetached;
    onTurnStaleRef.current = options.onTurnStale;
    onUserMsgLifecycleRef.current = options.onUserMsgLifecycle;
    getSinceSeqRef.current = options.getSinceSeq;
    getEventsFromSeqRef.current = options.getEventsFromSeq;
    getEventsCursorKnownRef.current = options.getEventsCursorKnown;
    onEventSeqAdvanceRef.current = options.onEventSeqAdvance;
    onSessionMetadataUpdatedRef.current = options.onSessionMetadataUpdated;
    onSessionForkedRef.current = options.onSessionForked;
    onSessionCreatedRef.current = options.onSessionCreated;
    onSessionDeletedRef.current = options.onSessionDeleted;
    onSessionRenamedRef.current = options.onSessionRenamed;
    onProjectsChangedRef.current = options.onProjectsChanged;
    onWorkersChangedRef.current = options.onWorkersChanged;
    onSessionOrganizationChangedRef.current = options.onSessionOrganizationChanged;
    onProjectMappingsChangedRef.current = options.onProjectMappingsChanged;
    onSupervisorEventRef.current = options.onSupervisorEvent;
    onPrLinkRef.current = options.onPrLink;
    onPromptQueuedRef.current = options.onPromptQueued;
    onTurnStartedRef.current = options.onTurnStarted;
    onQueueConsumedRef.current = options.onQueueConsumed;
    onAnyEventRef.current = options.onAnyEvent;
    onMessageRecoveringChangedRef.current = options.onMessageRecoveringChanged;
    onMessageRetryingChangedRef.current = options.onMessageRetryingChanged;
    onMessageAutoRetryChangedRef.current = options.onMessageAutoRetryChanged;
    onMessageContentUpdatedRef.current = options.onMessageContentUpdated;
    onMessageContinuationChangedRef.current = options.onMessageContinuationChanged;
    onMessageRunMetaChangedRef.current = options.onMessageRunMetaChanged;
    onMessageAskResultChangedRef.current = options.onMessageAskResultChanged;
    onMessageAskChoiceChangedRef.current = options.onMessageAskChoiceChanged;
    onSessionProcessingRef.current = options.onSessionProcessing;
    onSessionReconciledRef.current = options.onSessionReconciled;
    clientIdRef.current = options.clientId;
  }, [
    options.onRewindComplete,
    options.onMessagesReplay,
    options.onStubInvalidated,
    options.onMessagesDelta,
    options.onUserMessagePersisted,
    options.onSteerPromptPersisted,
    options.onPromptSendError,
    options.onRunState,
    options.onLiveTurnEvent,
    options.onTurnTerminal,
    options.onTurnDetached,
    options.onTurnStale,
    options.onUserMsgLifecycle,
    options.getSinceSeq,
    options.getEventsFromSeq,
    options.getEventsCursorKnown,
    options.onEventSeqAdvance,
    options.onSessionMetadataUpdated,
    options.onSessionForked,
    options.onSessionCreated,
    options.onSessionDeleted,
    options.onSessionRenamed,
    options.onProjectsChanged,
    options.onProjectUpdatesChanged,
    options.onWorkersChanged,
    options.onSessionOrganizationChanged,
    options.onProjectMappingsChanged,
    options.onPromptQueued,
    options.onTurnStarted,
    options.onQueueConsumed,
    options.onAnyEvent,
    options.onMessageRecoveringChanged,
    options.onMessageRetryingChanged,
    options.onMessageAutoRetryChanged,
    options.onMessageContentUpdated,
    options.onMessageContinuationChanged,
    options.onMessageAskResultChanged,
    options.onMessageAskChoiceChanged,
    options.onSessionProcessing,
    options.onSessionReconciled,
    options.clientId,
  ]);
  // Track the currently-viewed session id in a ref so onmessage can
  // route "loose" (outside-a-turn) events to the right consumer.
  const currentAppSessionIdRef = useRef<string | null>(
    options.currentAppSessionId ?? null
  );
  useEffect(() => {
    currentAppSessionIdRef.current = options.currentAppSessionId ?? null;
  }, [options.currentAppSessionId]);
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<WSEvent[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [streamingPhase, setStreamingPhase] = useState<StreamingPhase>(null);
  const [streamingLoadPhase, setStreamingLoadPhase] = useState<StreamingLoadPhase>(null);
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(
    null
  );
  const [streamingAppSessionId, setStreamingAppSessionId] = useState<
    string | null
  >(null);
  // Single source for the 3 terminal transitions (turn_complete,
  // turn_stopped, error) — clears streaming flags and pins the
  // result. Setters are stable React guarantees, so `useCallback`
  // empty deps gives a stable identity across renders.
  const applyTerminalEvent = useCallback(
    (result: Record<string, unknown> | null) => {
      setIsStreaming(false);
      setIsStopping(false);
      setStreamingPhase(null);
      setStreamingLoadPhase(null);
      setLastResult(result);
    },
    [],
  );
  const wsRef = useRef<WebSocket | null>(null);
  const snapshotTransportRef = useRef(new SnapshotTransport());
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Mirror isStreaming into a ref so onmessage can gate the "loose
  // event" path without re-subscribing on every streaming transition.
  const isStreamingRef = useRef(false);
  useEffect(() => {
    isStreamingRef.current = isStreaming;
  }, [isStreaming]);
  // Mirror streamingAppSessionId into a ref for the stale watchdog.
  const streamingSidRef = useRef<string | null>(null);
  useEffect(() => {
    streamingSidRef.current = streamingAppSessionId;
  }, [streamingAppSessionId]);

  // Stale-turn watchdog: if streaming is active and no events arrive
  // for 90s, the orchestrator task likely died silently. Fires
  // onTurnStale so the UI can surface a warning instead of a stuck
  // spinner. Resets on every event or terminal transition.
  const STALE_TIMEOUT_MS = 90_000;
  const lastEventAtRef = useRef<number>(0);
  useEffect(() => {
    if (!isStreaming) return;
    lastEventAtRef.current = Date.now();
    const id = setInterval(() => {
      if (!isStreamingRef.current) return;
      if (Date.now() - lastEventAtRef.current >= STALE_TIMEOUT_MS) {
        const sid = streamingSidRef.current;
        if (sid) onTurnStaleRef.current?.(sid);
      }
    }, 10_000);
    return () => clearInterval(id);
  }, [isStreaming]);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    // Re-resolve via getWsUrl so a bearer token acquired AFTER module
    // load (right after login on native) ends up on the handshake URL.
    // Browser path is the same URL, just without a ?token= suffix.
    void url;
    const ws = new WebSocket(getWsUrl());
    let routingVerifiedSnapshot = false;
    let verifiedRouteResult: void | Promise<void>;

    const routeVerifiedSnapshot = (event: WSEvent) => {
      routingVerifiedSnapshot = true;
      verifiedRouteResult = undefined;
      try {
        ws.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
        return verifiedRouteResult;
      } finally {
        routingVerifiedSnapshot = false;
      }
    };

    ws.onopen = () => {
      setConnected(true);
      snapshotTransportRef.current.resume((frame) => ws.send(JSON.stringify(frame)));
    };

    ws.onclose = (ev) => {
      setConnected(false);
      setIsStreaming(false);
      setIsStopping(false);
      setStreamingPhase(null);
      setStreamingLoadPhase(null);
      setStreamingAppSessionId(null);
      // Code 1008 ("policy violation") is what the backend sends from
      // /ws/chat when the better_agent_session cookie is missing or invalid. Tell
      // the top-level App to swap to <Login /> instead of reconnecting
      // forever against a 401 wall.
      if (ev.code === 1008) {
        window.dispatchEvent(new CustomEvent("better-agent-auth-failed"));
        return;
      }
      reconnectTimer.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => {
      ws.close();
    };

    ws.onmessage = (e) => {
      try {
        const event: WSEvent = JSON.parse(e.data);
        if (!routingVerifiedSnapshot && snapshotTransportRef.current.handle(
          event,
          (frame) => ws.send(JSON.stringify(frame)),
          routeVerifiedSnapshot,
          typeof e.data === "string" ? e.data.length * 4 : 0,
        )) return;

        // Catch-all dispatch (progress bus extenders) BEFORE any typed
        // path so even early-return events (messages_replay etc.) still
        // resolve pending ops waiting on them.
        try {
          onAnyEventRef.current?.(event);
        } catch {
          // never let bus errors break WS routing
        }

        // Pump EVERY frame into the typed eventBus so subscribers
        // (sessionRegistry, SessionStatusBadge consumers, etc.) can
        // converge without prop-drilling another `onXxx` callback.
        // Additive — does not replace the named-callback paths below
        // (those have downstream consumers that haven't migrated yet).
        try {
          eventBus.publish(event.type, event.data ?? {});
        } catch {
          // see onAnyEvent comment
        }

        // Advance the events.jsonl watermark for this session BEFORE
        // dispatching to handlers, so reconnects send the right
        // `events_from_seq`. Backend stamps a top-level `seq` on every
        // frame routed through `BetterAgentJsonlTailer._entry_to_ws_frame`.
        const ev = event as WSEvent & { seq?: number };
        if (typeof ev.seq === "number") {
          const sid =
            (event.data as { app_session_id?: string } | undefined)
              ?.app_session_id ?? currentAppSessionIdRef.current ?? null;
          if (sid) {
            onEventSeqAdvanceRef.current?.(sid, ev.seq);
          }
        }

        // Sequence-cursor replay: backend's response to our
        // subscribe(since_seq=N). Upsert by id into the current
        // session — this is the only path that hydrates the message
        // list, both on cold load (since_seq=0 → full session) and on
        // mid-turn reconnect (since_seq=N → catches up missed
        // messages + the latest in-flight assistant snapshot).
        if (event.type === "messages_replay") {
          const d = event.data as {
            app_session_id: string;
            messages: ChatMessage[];
          };
          if (d.app_session_id && Array.isArray(d.messages)) {
            onMessagesReplayRef.current?.(d.app_session_id, d.messages);
          }
          return;
        }

        // A backend reconcile appended late events to a collapsed
        // historical turn — replace its stale stub so the expanded
        // turn re-fetches fresh full events.
        if (event.type === "stub_invalidated") {
          for (const d of parseStubInvalidations(event.data)) {
            onStubInvalidatedRef.current?.(
              d.app_session_id,
              d.msg_id,
              d.stub,
            );
          }
          return;
        }

        // Per-event message delta — currently fired when the backend
        // lazily creates the assistant message on the first inner
        // event of a turn. Same upsert semantics as messages_replay.
        if (event.type === "messages_delta") {
          const d = event.data as {
            app_session_id: string;
            messages: ChatMessage[];
          };
          if (d.app_session_id && Array.isArray(d.messages)) {
            onMessagesDeltaRef.current?.(d.app_session_id, d.messages);
          }
          return;
        }

        // Backend ack of the user's prompt. Dispatched imperatively so
        // a tight back-to-back burst (user_message_persisted →
        // messages_delta → run_state → turn_start) can't lose the
        // ack to turn_start's `setEvents([])` before React commits
        // the events-buffer effect that would have processed it.
        if (event.type === "user_message_persisted") {
          const d = event.data as {
            session_id?: string;
            user_message?: ChatMessage;
          };
          if (d.session_id && d.user_message) {
            onUserMessagePersistedRef.current?.(d.session_id, d.user_message);
          }
          return;
        }

        if (event.type === "steer_prompt_persisted") {
          const d = event.data as {
            app_session_id?: string;
            client_id?: string | null;
          };
          if (d.app_session_id) {
            onSteerPromptPersistedRef.current?.(
              d.app_session_id,
              d.client_id ?? null,
            );
          }
          return;
        }

        // Async-reconcile progress. Backend emits these ONLY for slow
        // reconciles (>0.3s threshold) — fast reconciles produce zero
        // events to avoid flashing the badge for sub-perceptible work.
        // `started` fires when the 0.3s timer fires; `finished` fires
        // when the reconcile completes (success OR failure).
        if (
          event.type === "session_processing_started" ||
          event.type === "session_processing_finished"
        ) {
          const d = event.data as { root_id?: string };
          if (d.root_id) {
            const kind =
              event.type === "session_processing_started"
                ? "started"
                : "finished";
            onSessionProcessingRef.current?.(d.root_id, kind);
          }
          return;
        }

        // Backend reconcile completed (fast or slow). The initial GET
        // may have returned stale cache; silently refetch if the user
        // is viewing this root's session.
        if (event.type === "session_reconciled") {
          const d = event.data as { root_id?: string; snapshot_refresh_id?: string };
          if (d.root_id) {
            verifiedRouteResult = onSessionReconciledRef.current?.(
              d.root_id,
              typeof d.snapshot_refresh_id === "string",
            );
          }
          return;
        }

        // Backend-owned run_state snapshot. Frontend mirrors; renders
        // labeled "running" badges per active run.
        if (event.type === "run_state") {
          const d = event.data as {
            app_session_id: string;
            runs: RunInfo[];
          };
          if (d.app_session_id && Array.isArray(d.runs)) {
            onRunStateRef.current?.(d.app_session_id, d.runs);
          }
          return;
        }

        // Reset stale-turn watchdog on every turn-scoped event.
        if (isStreamingRef.current) lastEventAtRef.current = Date.now();

        // Live turn frames — route onto the canonical assistant
        // message for the SPECIFIC session the event belongs to. The
        // backend's `_dispatch_raw` annotates `data.app_session_id`
        // on every per-session frame so a client subscribed to N
        // panes can route each frame to the right one. Ownerless
        // render frames are ignored instead of being grafted onto
        // whichever pane is focused.
        // `pr-link` is a no-uuid metadata agent_message (a PR was just
        // created). It never lands on the render tree — surface it only
        // as an ephemeral chat-panel toast, on the LIVE push. Diverted
        // BEFORE the turn-reducer routing below so it can't pollute the
        // in-memory msg.events.
        const prLinkData =
          event.type === "agent_message"
            ? (event.data as { type?: string; prUrl?: string } | undefined)
            : undefined;
        if (prLinkData?.type === "pr-link" && prLinkData.prUrl) {
          const d = event.data as {
            app_session_id?: string;
            sessionId?: string;
            prNumber?: number;
            prUrl: string;
            prRepository?: string;
          };
          onPrLinkRef.current?.({
            sessionId: d.app_session_id ?? d.sessionId,
            prNumber: d.prNumber,
            prUrl: d.prUrl,
            prRepository: d.prRepository,
          });
        } else if (
          event.type === "agent_message" ||
          event.type === "manager_event" ||
          event.type === "model_switched" ||
          event.type === "steer_prompt" ||
          event.type === "worker_event" ||
          event.type === "turn_start" ||
          event.type === "turn_complete" ||
          event.type === "worker_start" ||
          event.type === "worker_complete" ||
          event.type === "worker_prep_start" ||
          event.type === "worker_prep_event" ||
          event.type === "worker_prep_complete" ||
          event.type === "worker_prep_cancelled" ||
          event.type === "todos_snapshot"
        ) {
          const eventSid = resolveLiveFrameSessionId(
            event,
            currentAppSessionIdRef.current,
          );
          if (eventSid) {
            onLiveTurnEventRef.current?.(eventSid, event);
          }
          // intentional fallthrough — the existing `setEvents` buffer
          // below still captures these for non-rendering uses
          // (sidebar refresh signals, etc).
        }

        // User-message lifecycle (5 states emitted by the backend's
        // event bus). All carry `app_session_id` because they were
        // persisted to events.jsonl and broadcast via the wire tailer,
        // which annotates every event with its sid. The caller projects
        // each event's `lifecycle_msg_id` to a chat message's
        // `lifecycle` field so the UI can render queued/sent/received/
        // done/failed states + interrupt cross-refs.
        if (
          event.type === "user_message_queued" ||
          event.type === "user_message_sent" ||
          event.type === "user_message_received" ||
          event.type === "user_message_done" ||
          event.type === "user_message_failed"
        ) {
          const eventSid =
            (event.data as { app_session_id?: string } | undefined)
              ?.app_session_id ?? currentAppSessionIdRef.current ?? null;
          const d = event.data as {
            lifecycle_msg_id?: string;
            client_id?: string;
            kind?: string;
            error?: string;
            reason?: string;
          } | undefined;
          logPromptSend("lifecycle", {
            event: event.type,
            app_session_id: eventSid,
            lifecycle_msg_id: d?.lifecycle_msg_id,
            client_id: d?.client_id,
            kind: d?.kind,
            error: d?.error ?? d?.reason,
          }, event.type === "user_message_failed" ? "warn" : "info");
          if (eventSid) {
            onUserMsgLifecycleRef.current?.(eventSid, event);
          }
          // Lifecycle events are pure observability — don't drop into
          // the generic events buffer (would mistakenly show up under
          // run_state's "live events" counter).
          return;
        }

        // A new turn starts with turn_start — clear prior events.
        if (event.type === "turn_start") {
          setEvents([]);
          setIsStreaming(true);
          setStreamingPhase("manager");
          setStreamingLoadPhase("starting");
          setLastResult(null);
          const managerSid =
            (event.data as { app_session_id?: string })?.app_session_id ?? "";
          if (!managerSid) return;
          setStreamingAppSessionId(managerSid || null);
          onTurnStartedRef.current?.(managerSid);
        }

        // Backend ack that a prompt was queued
        if (event.type === "prompt_queued") {
          const d = event.data as {
            app_session_id?: string;
            queued_id?: string;
            prompt_preview?: string;
            send_mode?: string;
            queue_position?: number;
            client_id?: string;
          };
          logPromptSend("prompt_queued_ack", {
            app_session_id: d.app_session_id,
            queued_id: d.queued_id,
            client_id: d.client_id,
            send_mode: d.send_mode,
            queue_position: d.queue_position,
            preview_length: d.prompt_preview?.length ?? 0,
          });
          if (d.queued_id && d.app_session_id) {
            onPromptQueuedRef.current?.({
              app_session_id: d.app_session_id,
              queued_id: d.queued_id,
              prompt_preview: d.prompt_preview ?? "",
              send_mode: d.send_mode ?? "queue",
              queue_position: d.queue_position ?? 1,
              client_id: d.client_id,
            });
          }
        }

        // Backend consumed a queued prompt — clear stale frontend state
        if (event.type === "queue_consumed") {
          const d = event.data as {
            app_session_id?: string;
            queued_id?: string | null;
          };
          logPromptSend("queue_consumed", {
            app_session_id: d.app_session_id,
            queued_id: d.queued_id ?? null,
          });
          if (d.app_session_id) {
            onQueueConsumedRef.current?.({
              app_session_id: d.app_session_id,
              queued_id: d.queued_id ?? null,
            });
          }
        }

        // Phase follows whatever is actively producing events.
        if (event.type === "worker_start") {
          setStreamingPhase("worker");
        } else if (event.type === "worker_complete") {
          setStreamingPhase("manager");
        }

        // Fine-grained load phase: starting → connected → null
        if (event.type === "agent_message" || event.type === "manager_event") {
          const d = event.data as
            | { type?: string; event?: { type?: string } }
            | undefined;
          // Legacy manager_event wraps inner under data.event
          const innerType =
            event.type === "manager_event" ? d?.event?.type : d?.type;
          if (innerType === "session_discovered") {
            setStreamingLoadPhase("connected");
          } else if (innerType === "assistant" && streamingLoadPhase) {
            setStreamingLoadPhase(null);
          }
        }

        setEvents((prev) => [...prev, event]);

        // Per-pane clear (`onTurnTerminal`/`onTurnDetached`) routes to the
        // session the turn belongs to (`event.data.app_session_id`), NOT
        // the focused pane. The WS subscribes to every open pane, so a turn
        // finishing in a background pane must clear THAT pane's per-message
        // `isStreaming` — using the focused id cleared the wrong pane and
        // left the real one stuck "Running…" until a REST refetch.
        //
        // `applyTerminalEvent` is left UNCONDITIONAL: it resets the SHARED
        // single-stream globals (`isStreaming`, load phase, `lastResult`)
        // that `turn_start`/`sendMessage` also set unconditionally. Gating
        // it would desync that pair — a tracked turn ending in a background
        // pane would leave `isStreaming` stuck true, falsely tripping the
        // stale-turn watchdog and suppressing error surfacing.
        if (event.type === "turn_complete") {
          applyTerminalEvent(event.data);
          const sid =
            (event.data as { app_session_id?: string } | undefined)
              ?.app_session_id ?? currentAppSessionIdRef.current ?? null;
          if (sid) onTurnTerminalRef.current?.(sid);
        }

        if (event.type === "turn_stopped") {
          applyTerminalEvent(event.data);
          const d = event.data as {
            app_session_id?: string;
            stopped_at?: string;
            interrupted_by_msg_id?: string;
          };
          const sid = d?.app_session_id ?? currentAppSessionIdRef.current ?? null;
          if (sid) onTurnTerminalRef.current?.(sid, d?.stopped_at, d?.interrupted_by_msg_id);
        }

        if (event.type === "turn_detached") {
          applyTerminalEvent(null);
          const sid =
            (event.data as { app_session_id?: string } | undefined)
              ?.app_session_id ?? currentAppSessionIdRef.current ?? null;
          if (sid) onTurnDetachedRef.current?.(sid);
        }

        if (event.type === "error") {
          const d = event.data as {
            app_session_id?: string;
            session_id?: string;
            client_id?: string | null;
            error?: string;
          } | undefined;
          const errorText = d?.error ?? "";
          applyTerminalEvent({
            success: false,
            error: errorText,
            client_id: d?.client_id ?? null,
          });
          const sid =
            d?.app_session_id ?? d?.session_id ?? currentAppSessionIdRef.current ?? null;
          logPromptSend("backend_error", {
            app_session_id: sid,
            client_id: d?.client_id ?? null,
            error: errorText,
          }, "error");
          if (sid && d?.client_id) {
            onPromptSendErrorRef.current?.(sid, d.client_id, errorText);
          }
          if (sid) onTurnTerminalRef.current?.(sid);
        }

        if (event.type === "rewind_complete") {
          const canonical = event.data as {
            session_id?: string;
            messages?: ChatMessage[];
          } | undefined;
          const d = canonical?.session_id ? canonical : event as unknown as {
            session_id: string;
            messages: ChatMessage[];
          };
          if (d.session_id && Array.isArray(d.messages)) {
            onRewindCompleteRef.current?.(d.session_id, d.messages);
          }
        }
        if (event.type === "message_recovering_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            value: boolean;
          };
          if (d.session_id && d.msg_id) {
            onMessageRecoveringChangedRef.current?.(
              d.session_id,
              d.msg_id,
              !!d.value
            );
          }
        }
        if (event.type === "message_retrying_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            retry_at: string | null;
            error_text?: string;
          };
          if (d.session_id && d.msg_id) {
            onMessageRetryingChangedRef.current?.(
              d.session_id,
              d.msg_id,
              d.retry_at ?? null,
              d.error_text ?? null
            );
          }
        }
        if (event.type === "message_auto_retry_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            auto_retry: { count: number; kind: string } | null;
          };
          if (d.session_id && d.msg_id) {
            onMessageAutoRetryChangedRef.current?.(
              d.session_id,
              d.msg_id,
              d.auto_retry ?? null
            );
          }
        }
        if (event.type === "message_content_updated") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            content: string;
          };
          if (d.session_id && d.msg_id) {
            onMessageContentUpdatedRef.current?.(
              d.session_id,
              d.msg_id,
              d.content ?? ""
            );
          }
        }
        if (event.type === "message_continuation_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            chain_depth: number | null;
          };
          if (d.session_id && d.msg_id) {
            onMessageContinuationChangedRef.current?.(
              d.session_id,
              d.msg_id,
              d.chain_depth ?? null
            );
          }
        }
        if (event.type === "message_run_meta_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            run_meta: import("../types").ChatMessage["run_meta"];
          };
          if (d.session_id && d.msg_id) {
            onMessageRunMetaChangedRef.current?.(
              d.session_id,
              d.msg_id,
              d.run_meta ?? null
            );
          }
        }
        if (event.type === "message_ask_result_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            ask_result: import("../types").AskResult | null;
          };
          if (d.session_id && d.msg_id) {
            onMessageAskResultChangedRef.current?.(
              d.session_id,
              d.msg_id,
              d.ask_result ?? null
            );
          }
        }
        if (event.type === "message_ask_choice_changed") {
          const d = event.data as {
            session_id: string;
            msg_id: string;
            chosen_session_id: string | null;
          };
          if (d.session_id && d.msg_id) {
            onMessageAskChoiceChangedRef.current?.(
              d.session_id,
              d.msg_id,
              d.chosen_session_id ?? null
            );
          }
        }
        if (event.type === "session_metadata_updated") {
          const d = event.data as {
            session_id: string;
            patch: {
              inline_tags?: InlineTag[];
              adv_sync_overlays?: import("../types").AdvSyncOverlay[];
              open_file_panels?: OpenFilePanel[];
              open_config_panels?: import("../types").OpenConfigPanel[];
              draft_input?: string;
              draft_images?: import("../types").PastedImage[];
              queued_prompts?: import("../types").QueuedPrompt[];
              fork_closed?: boolean;
              model?: string;
              reasoning_effort?: string;
              cwd?: string;
              /** A10: backend-gated provider change broadcast for
               * multi-tab convergence. The 409-on-active-run gate
               * runs server-side; any patch that reaches us is safe
               * to apply unconditionally. */
              provider_id?: string;
              supervisor_enabled?: boolean;
              pinned?: boolean;
              topbar_pinned?: boolean;
              topbar_pinned_at?: string | null;
              archived?: boolean;
              working_mode?: Session["working_mode"];
              working_mode_meta?: Session["working_mode_meta"];
              notes?: import("../types").Note[];
              current_todos?: import("../types").TodoItem[];
              current_tasks?: import("../types").TaskItem[];
              messages?: import("../types").ChatMessage[];
              message_count?: number;
              updated_at?: string;
              last_user_prompt_at?: string;
              last_opened_at?: string;
              pagination?: import("../types").Session["pagination"];
              worker_creation_policy?: import("../types").WorkerCreationPolicy;
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
              right_panel_auto_opened_by?: import("../types").Session["right_panel_auto_opened_by"];
              sidebar_minimized?: boolean;
            };
            originated_by?: string | null;
          };
          if (
            d.session_id &&
            d.patch &&
            (d.originated_by == null || d.originated_by !== clientIdRef.current)
          ) {
            onSessionMetadataUpdatedRef.current?.(d.session_id, d.patch);
          }
        }
        if (event.type === "session_created") {
          // DIV-4: multi-tab convergence for new sessions. The
          // originating tab already added the session via the REST POST
          // response — caller MUST dedup by id (see appendSessionIfNew
          // in useSession).
          const d = event.data as { session: Session };
          if (d.session) {
            onSessionCreatedRef.current?.(d.session);
          }
        }
        if (event.type === "session_deleted") {
          // Multi-tab convergence for deletes. Originating tab
          // already filtered locally after REST DELETE; this covers
          // other tabs + the originating tab on REST↔WS races.
          // Caller MUST dedup-by-id (no-op when already removed).
          const d = event.data as { session_id?: string };
          if (d.session_id) {
            onSessionDeletedRef.current?.(d.session_id);
          }
        }
        if (event.type === "supervisor_event") {
          // Three flavors today: verdict_failed (supervisor errored —
          // we fail open), verdict_capped (loop hit MAX_VERDICTS), and
          // await_user (supervisor agreed the worker is legitimately
          // blocked on user input and surfaces what to answer).
          const d = event.data as {
            session_id?: string;
            kind?: string;
            message?: string;
            error?: string;
            reason?: string;
          };
          console.warn(
            "[supervisor_event]",
            d.kind,
            d.message ?? d.reason ?? "",
            d.error ? `(${d.error})` : "",
            "session=", d.session_id,
          );
          onSupervisorEventRef.current?.({
            sessionId: d.session_id,
            kind: d.kind ?? "unknown",
            message: d.message,
            error: d.error,
            reason: d.reason,
          });
        }
        if (event.type === "session_forked") {
          const d = event.data as {
            session: Session;
            parent_session_id: string | null;
          };
          if (d.session) {
            onSessionForkedRef.current?.(d.session, d.parent_session_id ?? null);
          }
        }
        if (event.type === "session_renamed") {
          const d = event.data as { session_id?: string; name?: string };
          if (d.session_id && d.name) {
            onSessionRenamedRef.current?.(d.session_id, d.name);
          }
        }
        if (event.type === "projects_changed") {
          onProjectsChangedRef.current?.();
        }
        if (event.type === "project_updates_changed") {
          const d = event.data as { project_id: string; unseen_count: number };
          onProjectUpdatesChangedRef.current?.(d);
        }
        if (event.type === "workers_changed") {
          onWorkersChangedRef.current?.();
        }
        if (event.type === "session_organization_changed") {
          onSessionOrganizationChangedRef.current?.();
        }
        if (event.type === "project_mappings_changed") {
          onProjectMappingsChangedRef.current?.();
        }
        // Provider list/active-id changed somewhere — let any open
        // ProvidersModal + every ModelSelector refetch via a global
        // window event. Cheaper than threading another callback prop.
        if (event.type === "provider_changed") {
          window.dispatchEvent(new Event("provider_changed"));
        }
        // Streaming provider-CLI install (Settings → Provider CLI tools).
        // provider_setup streams installer stdout/stderr line-by-line
        // (progress) and a terminal state (finished). useProviderInstalls
        // owns the registry projection.
        if (
          event.type === "provider_install_progress" ||
          event.type === "provider_install_finished"
        ) {
          window.dispatchEvent(
            new CustomEvent(event.type, { detail: event.data }),
          );
        }
        // Per-provider model catalog delta (daily refresher / manual
        // refresh). ModelSelector listens via useModelsCatalogChanged
        // and refetches `/api/models`. Carries the four disjoint
        // transition sets on `detail` for future toast/badge use.
        if (event.type === "models_catalog_changed") {
          window.dispatchEvent(
            new CustomEvent("models_catalog_changed", { detail: event.data }),
          );
        }
        // Backend startup-task lifecycle delta. The `StartupTasksBanner`
        // is the sole consumer — it merges the payload into its local
        // map by id (or empties it on `{cleared: true}`). Authoritative
        // snapshot lives at `GET /api/startup_tasks`; this ping is just
        // a live invalidation. Threaded via window event for the same
        // reason as `provider_changed` — no callback plumbing needed.
        if (event.type === "startup_task_changed") {
          window.dispatchEvent(
            new CustomEvent("startup_task_changed", { detail: event.data }),
          );
        }
        // (Legacy `active_process_counts_changed` rebroadcast removed.
        //  Running / unread state now flows via the typed eventBus →
        //  sessionRegistry → useSessionMeta / useProjectAggregate.)
        // Multi-machine: a worker-node connected or disconnected.
        // Threaded via window event so useMachines / machine extension UI /
        // MachineNodePicker can converge without callback plumbing.
        // Authoritative snapshot lives behind the machine-nodes extension.
        if (event.type === "node_state_changed") {
          window.dispatchEvent(
            new CustomEvent("node_state_changed", { detail: event.data }),
          );
        }
        // Multi-machine: a brand-new worker-node is awaiting operator
        // approval (requested) or its request was resolved (approve/deny).
        // usePendingNodeRegistrations and machine extension UI converge approvals via
        // these window events. Authoritative snapshot lives at
        // the machine-nodes extension's pending-node snapshot.
        if (event.type === "node_registration_requested") {
          window.dispatchEvent(
            new CustomEvent("node_registration_requested", { detail: event.data }),
          );
        }
        if (event.type === "node_registration_resolved") {
          window.dispatchEvent(
            new CustomEvent("node_registration_resolved", { detail: event.data }),
          );
        }
        if (event.type === "user_input_requested") {
          window.dispatchEvent(
            new CustomEvent("user_input_requested", { detail: event.data }),
          );
        }
        if (event.type === "user_input_resolved") {
          window.dispatchEvent(
            new CustomEvent("user_input_resolved", { detail: event.data }),
          );
        }
        // Interactive tool/command approval: a runner (Claude can_use_tool /
        // Codex app-server) needs a human decision mid-turn. Chat renders an
        // Approve/Deny card; the decision POSTs back and unblocks the runner.
        if (event.type === "tool_approval_requested") {
          window.dispatchEvent(
            new CustomEvent("tool_approval_requested", { detail: event.data }),
          );
        }
        if (event.type === "tool_approval_resolved") {
          window.dispatchEvent(
            new CustomEvent("tool_approval_resolved", { detail: event.data }),
          );
        }
      } catch {
        // ignore parse errors
      }
    };

    wsRef.current = ws;
  }, [url]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      const ws = wsRef.current;
      wsRef.current = null;
      if (!ws) return;
      ws.onopen = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      ws.close();
      snapshotTransportRef.current.clear();
    };
  }, [connect]);

  // Subscribe / unsubscribe lifecycle. When the user switches to a new
  // session (or the WS reconnects while one is open) we tell the
  // backend "I'm viewing this app_session_id now; push any
  // SessionWatcher events for it through my ws_callback." When they
  // switch away we unsubscribe the previous id so the backend stops
  // firing into a stale WS callback slot.
  //
  // The existing send_message path already calls register_ws
  // server-side, so this is purely additive: normal prompt flow
  // continues to work; this just covers the "viewing-without-
  // prompting" case (zombie runners, crash-recovered sessions, CLI
  // sessions writing in parallel).
  // Track the full set of subscribed session ids (the focused pane plus
  // any additional pane ids from the split-fork view). Diff-driven: on
  // every change of the desired set, send subscribe frames for new ids
  // and unsubscribe for dropped ids.
  const subscribedIdsRef = useRef<Set<string>>(new Set());
  const targetAppSessionId = options.currentAppSessionId ?? null;
  const additionalIds = options.additionalAppSessionIds;
  // Memoize the joined key so this effect only re-runs when the set
  // actually changes (not on every parent render).
  const desiredSetKey = (() => {
    const ids = new Set<string>();
    if (targetAppSessionId) ids.add(targetAppSessionId);
    for (const id of additionalIds ?? []) {
      if (id) ids.add(id);
    }
    return Array.from(ids).sort().join("|");
  })();
  useEffect(() => {
    if (!connected) {
      // WS went down — drop our local record of subscriptions so that
      // the reconnect re-subscribes the full desired set fresh.
      subscribedIdsRef.current = new Set();
      return;
    }
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const desired = new Set<string>();
    if (targetAppSessionId) desired.add(targetAppSessionId);
    for (const id of additionalIds ?? []) {
      if (id) desired.add(id);
    }
    const prev = subscribedIdsRef.current;
    // Unsubscribe ids that fell out of the desired set.
    for (const id of prev) {
      if (!desired.has(id)) {
        try {
          ws.send(
            JSON.stringify({ type: "unsubscribe", app_session_id: id })
          );
        } catch {
          // ignore
        }
      }
    }
    // Subscribe ids that are newly desired.
    for (const id of desired) {
      if (!prev.has(id)) {
        try {
          const sinceSeq = getSinceSeqRef.current?.(id) ?? 0;
          const eventsFromSeq = getEventsFromSeqRef.current?.(id) ?? 0;
          const eventsCursorKnown = getEventsCursorKnownRef.current?.(id) ?? false;
          ws.send(
            JSON.stringify({
              type: "subscribe",
              app_session_id: id,
              since_seq: sinceSeq,
              events_from_seq: eventsFromSeq,
              events_cursor_known: eventsCursorKnown,
            })
          );
        } catch {
          // ignore
        }
      }
    }
    subscribedIdsRef.current = desired;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, desiredSetKey]);

  const sendMessage = useCallback(
    (
      prompt: string,
      model: string,
      cwd: string,
      claudeSessionId?: string | null,
      appSessionId?: string | null,
      images?: ImagePayload[],
      orchestrationMode?: OrchestrationMode,
      clientId?: string | null,
      sendMode?: SendMode | null,
      sendTarget?: "worker" | "supervisor" | null,
      files?: FilePayload[],
      capabilityContexts?: CapabilityContext[],
    ) => {
      const wsState = wsRef.current?.readyState ?? -1;
      const logData = {
        app_session_id: appSessionId ?? null,
        client_id: clientId ?? null,
        send_mode: sendMode ?? null,
        send_target: sendTarget ?? null,
        orchestration_mode: orchestrationMode ?? null,
        prompt_length: prompt.length,
        image_count: images?.length ?? 0,
        file_count: files?.length ?? 0,
        capability_context_count: capabilityContexts?.length ?? 0,
        ws_state: wsState,
        is_streaming: isStreaming,
      };
      logPromptSend("ws_send_attempt", logData);
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        logPromptSend("ws_not_open", logData, "warn");
        return false;
      }

      setStreamingAppSessionId(appSessionId ?? null);

      // If not currently streaming, start fresh
      if (!isStreaming) {
        setEvents([]);
        setIsStreaming(true);
        setStreamingLoadPhase(null);
        setLastResult(null);
      }
      // If already streaming, don't clear events — the new message is queued
      // and turn_start will clear events when it actually starts processing

      try {
        wsRef.current.send(
          JSON.stringify({
            type: "send_message",
            prompt,
            model,
            cwd,
            session_id: claudeSessionId || null,
            app_session_id: appSessionId || null,
            images: images && images.length > 0 ? images : undefined,
            files: files && files.length > 0 ? files : undefined,
            orchestration_mode: orchestrationMode,
            client_id: clientId || null,
            send_mode: sendMode || undefined,
            send_target: sendTarget || undefined,
            capability_contexts: capabilityContexts && capabilityContexts.length > 0 ? capabilityContexts : undefined,
          })
        );
      } catch (error) {
        logPromptSend("ws_send_throw", {
          ...logData,
          error: error instanceof Error ? error.message : String(error),
        }, "error");
        return false;
      }
      logPromptSend("ws_send_ok", logData);
      return true;
    },
    [isStreaming]
  );

  const stopStreaming = useCallback((appSessionId: string): boolean => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;
    setIsStopping(true);
    wsRef.current.send(
      JSON.stringify({
        type: "stop_message",
        app_session_id: appSessionId,
      })
    );
    return true;
  }, []);

  const sendPromoteQueued = useCallback((
    appSessionId: string,
    action: "interrupt" | "steer" = "interrupt",
    queuedId?: string,
    queuedIds?: string[],
  ) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(
      JSON.stringify({
        type: "promote_queued",
        app_session_id: appSessionId,
        action,
        queued_id: queuedId,
        queued_ids: queuedIds,
      })
    );
    return true;
  }, []);

  const sendCancelQueued = useCallback((appSessionId: string, queuedId?: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(
      JSON.stringify({
        type: "cancel_queued",
        app_session_id: appSessionId,
        queued_id: queuedId,
      })
    );
    return true;
  }, []);

  const sendUpdateQueued = useCallback(
    (appSessionId: string, queuedId: string, content: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;
      wsRef.current.send(
        JSON.stringify({
          type: "update_queued",
          app_session_id: appSessionId,
          queued_id: queuedId,
          content,
        })
      );
      return true;
    },
    []
  );

  const sendBeginQueuedEdit = useCallback((appSessionId: string, queuedId: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(
      JSON.stringify({
        type: "begin_queued_edit",
        app_session_id: appSessionId,
        queued_id: queuedId,
      })
    );
    return true;
  }, []);

  const sendFinishQueuedEdit = useCallback((appSessionId: string, queuedId: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(
      JSON.stringify({
        type: "finish_queued_edit",
        app_session_id: appSessionId,
        queued_id: queuedId,
      })
    );
    return true;
  }, []);

  return {
    connected,
    sendMessage,
    stopStreaming,
    sendPromoteQueued,
    sendCancelQueued,
    sendUpdateQueued,
    sendBeginQueuedEdit,
    sendFinishQueuedEdit,
    events,
    isStreaming,
    isStopping,
    streamingPhase,
    streamingLoadPhase,
    lastResult,
    streamingAppSessionId,
  };
}
