import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback } from "react";
import { LayoutGroup, motion, MotionConfig, useReducedMotion } from "framer-motion";
import { mergeMessagesSorted, oldestNumericSeq } from "../utils/mergeMessages";
import { useThrottledValue } from "../hooks/useThrottledValue";
import { isGroupRunning } from "../utils/groupRunning";
import { isUnanchoredRun } from "../utils/runTargets";
import { runMetaMarkedMessageIds } from "../chat/runMetaMarkers";
import { useTranslation } from "react-i18next";
import { scrollToLatest, useScrollLoadOlder } from "../hooks/useScrollLoadOlder";
import type {
  CapabilityContext,
  ChatMessage,
  FileFocus,
  PendingApproval,
  CredentialConsent,
  UserInputRequest,
  ToolApproval,
  Provider,
  RunInfo,
  Session,
  WSEvent,
} from "../types";
import type { InlineTag } from "../types/inlineTag";
import type { StreamingLoadPhase } from "../hooks/useWebSocket";
import { TurnGroup, MessageBubble } from "./MessageBubble";
import { HistoricalTurnDetails } from "./HistoricalTurnDetails";
import { InputArea } from "./InputArea";
import type { ScheduleSendPayload } from "./ScheduleSendPopover";
import { ExtensionModuleSlot, useExtensionFrontendModules } from "./ExtensionSlots";
import { JsonNode } from "./JsonNode";
import Icon from "./Icon";
import { RewindPopover } from "./RewindPopover";
import { SelectionPopup } from "./SelectionPopup";
import { userFacingForks } from "../hooks/useSession";
import { eventBus } from "../lib/eventBus";
import { buildThreadColorMap } from "../threadColors";
import { ForkSplitView } from "./ForkSplitView";
import { SessionTabs } from "./SessionTabs";
import { VoiceActivation } from "./VoiceActivation";
import { SessionBackgroundStrip } from "./SessionBackgroundStrip";
import { ShortcutResponses } from "./ShortcutResponses";
import { useSessionMeta } from "../lib/sessionRegistry";
import { registerMobileHandlers, clearMobileHandlers } from "../contexts/MobileHandlersContext";
import { fileToPastedImage, insertTextAtSelection, promptClipboardPayload } from "../utils/imageAttach";
import {
  extractAssistantOutputTextFromEvents,
  extractAssistantTextFromEvents,
} from "../utils/agentMessages";
import { perfId, perfRecord, perfSpan } from "../lib/renderProfiler";
import { logTiming } from "../lib/frontendLogger";

/** Stable empty-runs singleton so groups with no targeted runs hand a
 *  referentially identical array to TurnGroup across renders — a
 *  fresh `[]` per render would defeat the downstream `memo(TurnGroup)`.
 *  Frozen so an accidental `.push` on the shared instance throws loudly
 *  rather than silently corrupting every other group's array. */
const EMPTY_CHAT_RUNS: RunInfo[] = Object.freeze([]) as unknown as RunInfo[];
const EMPTY_MODEL_SWITCH_EVENTS: WSEvent[] = Object.freeze([]) as unknown as WSEvent[];
const NO_ENTERING: ReadonlySet<string> = new Set();
const ASSISTANT_SPEECH_LIMIT = 4000;

function isEditablePasteTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
}

function insertTextIntoPrompt(text: string): string | null {
  const textarea = document.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]');
  if (!textarea || textarea.disabled) return null;
  textarea.focus();
  const next = insertTextAtSelection(
    textarea.value,
    text,
    textarea.selectionStart,
    textarea.selectionEnd,
  );
  const valueSetter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  if (!valueSetter) return null;
  valueSetter.call(textarea, next.value);
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  textarea.setSelectionRange(next.cursor, next.cursor);
  return next.value;
}

function assistantSpeechText(message: ChatMessage | undefined): string {
  if (!message || message.isStreaming) return "";
  const fromEvents = message.events
    ? extractAssistantOutputTextFromEvents(message.events, ASSISTANT_SPEECH_LIMIT)
    : "";
  if (fromEvents.trim()) return fromEvents.trim();
  return typeof message.content === "string"
    ? message.content.trim().slice(0, ASSISTANT_SPEECH_LIMIT)
    : "";
}

function speakAssistantText(text: string) {
  const synth = window.speechSynthesis;
  if (!synth || !text.trim()) return;
  synth.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = navigator.language || "en-US";
  synth.speak(utterance);
}

import {
  runThreeStateSync,
  useOpProgress,
} from "../progress/store";

import { API, createSessionSchedule, fetchSessionSchedules } from "../api";
import { extBackendBase, resolvedExtBackendBase } from "../extensionIds";

const teamOrchestrationApi = () => extBackendBase("team");

function UserInputCard({
  request,
  onDone,
}: {
  request: UserInputRequest;
  onDone: (requestId: string) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const q of request.questions) {
      initial[q.id] = q.options?.[0]?.label ?? "";
    }
    return initial;
  });
  const [submitting, setSubmitting] = useState(false);
  const textRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const canSubmit = request.questions.every((q) => (answers[q.id] || "").trim());

  const pickOption = (questionId: string, label: string) => {
    setAnswers((prev) => ({ ...prev, [questionId]: label }));
    const el = textRefs.current[questionId];
    if (el) requestAnimationFrame(() => { el.focus(); el.select(); });
  };

  const submit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${API}/api/user-input/${encodeURIComponent(request.request_id)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ app_session_id: request.app_session_id, answers }),
      });
      if (res.ok) onDone(request.request_id);
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${API}/api/user-input/${encodeURIComponent(request.request_id)}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ app_session_id: request.app_session_id }),
      });
      if (res.ok) onDone(request.request_id);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="user-input-card">
      <div className="user-input-card__title">Input needed</div>
      <div className="user-input-card__questions">
        {request.questions.map((q) => (
          <div className="user-input-card__question" key={q.id}>
            <div className="user-input-card__header">{q.header}</div>
            <div className="user-input-card__body">{q.question}</div>
            {q.options && q.options.length > 0 ? (
              <div className="user-input-card__options">
                {q.options.map((option) => (
                  <label className="user-input-card__option" key={option.label}>
                    <input
                      type="radio"
                      name={`${request.request_id}:${q.id}`}
                      checked={answers[q.id] === option.label}
                      onChange={() => pickOption(q.id, option.label)}
                      disabled={submitting}
                    />
                    <span>
                      <strong>{option.label}</strong>
                      {option.description ? <small>{option.description}</small> : null}
                    </span>
                  </label>
                ))}
                <input
                  ref={(el) => { textRefs.current[q.id] = el; }}
                  className="user-input-card__text"
                  value={answers[q.id] ?? ""}
                  onChange={(e) => setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
                  onFocus={(e) => e.target.select()}
                  disabled={submitting}
                  placeholder="Other answer"
                />
              </div>
            ) : (
              <textarea
                className="user-input-card__textarea"
                value={answers[q.id] ?? ""}
                onChange={(e) => setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
                disabled={submitting}
                rows={3}
              />
            )}
          </div>
        ))}
      </div>
      <div className="user-input-card__actions">
        <button type="button" onClick={cancel} disabled={submitting}>Cancel</button>
        <button type="button" className="primary" onClick={submit} disabled={!canSubmit || submitting}>
          Send
        </button>
      </div>
    </div>
  );
}

/** Max chars rendered per argument value in the approval card. The backend
 *  already caps each value, but a runner that bypasses the shared helper (or a
 *  future provider) might not — defend the UI so one huge field can't blow up
 *  the card. Generous enough to show a full command / path / short patch. */
const TOOL_APPROVAL_VALUE_LIMIT = 2000;

/** Normalize a tool-call summary into ordered [label, value] rows covering
 *  EVERY argument, so the user sees exactly what they're approving. Tolerant
 *  of the unified `summary.input` shape and the legacy `summary.args` shape
 *  (older runners / replayed records), and of non-string values. */
export function toolApprovalArgRows(
  summary: Record<string, unknown> | undefined,
): Array<{ key: string; value: string }> {
  const bag =
    (summary?.input as Record<string, unknown> | undefined) ??
    (summary?.args as Record<string, unknown> | undefined) ??
    undefined;
  if (!bag || typeof bag !== "object") return [];
  const rows: Array<{ key: string; value: string }> = [];
  for (const [key, raw] of Object.entries(bag)) {
    let value: string;
    if (typeof raw === "string") {
      value = raw;
    } else if (raw === null || raw === undefined) {
      value = String(raw);
    } else {
      try {
        value = JSON.stringify(raw);
      } catch {
        value = String(raw);
      }
    }
    if (value.length > TOOL_APPROVAL_VALUE_LIMIT) {
      value = value.slice(0, TOOL_APPROVAL_VALUE_LIMIT) + "…";
    }
    rows.push({ key, value });
  }
  return rows;
}

function ToolApprovalCard({
  approval,
  sessionId,
  onResolved,
}: {
  approval: ToolApproval;
  sessionId: string;
  onResolved: (approvalId: string) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const toolName =
    approval.tool_name ||
    (typeof approval.summary?.tool === "string" ? (approval.summary.tool as string) : "") ||
    t("toolApproval.unknownTool");
  const rows = toolApprovalArgRows(approval.summary);
  const decide = async (approved: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      await runThreeStateSync<Response, ToolApproval[]>({
        operationId: `approval:tool:${approval.approval_id}`,
        action: t("toolApproval.title"),
        reconcile: () => {},
        mutate: async () => {
          const response = await fetch(
            `${API}/api/sessions/${encodeURIComponent(sessionId)}/tool-approvals/${encodeURIComponent(approval.approval_id)}/decide`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              credentials: "include",
              body: JSON.stringify({ approved }),
            },
          );
          if (!response.ok) throw new Error(await response.text());
          return response;
        },
      });
      onResolved(approval.approval_id);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="user-input-card" data-testid="tool-approval-card" data-approval-id={approval.approval_id}>
      <div className="user-input-card__title">{t("toolApproval.title")}</div>
      <div className="user-input-card__question">
        <div className="user-input-card__header">
          {t("toolApproval.tool", { tool: toolName })}
        </div>
        {approval.provider_kind ? (
          <div className="tool-approval-card__provider">
            {t("toolApproval.provider", { provider: approval.provider_kind })}
          </div>
        ) : null}
        {rows.length > 0 ? (
          <dl className="tool-approval-card__args">
            {rows.map((row) => (
              <div key={row.key} className="tool-approval-card__arg">
                <dt className="tool-approval-card__arg-key">{row.key}</dt>
                <dd className="tool-approval-card__arg-value">{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="user-input-card__body tool-approval-card__no-args">
            {t("toolApproval.noArgs")}
          </div>
        )}
      </div>
      <div className="user-input-card__actions">
        <button type="button" onClick={() => decide(false)} disabled={busy}>
          {t("toolApproval.deny")}
        </button>
        <button type="button" className="primary" onClick={() => decide(true)} disabled={busy}>
          {t("toolApproval.approve")}
        </button>
      </div>
    </div>
  );
}

/** One rendered turn group: an initiating turn message (User/Ask/Message/
 * Provisioning/etc.) paired with its assistant response (if any), the runs
 * targeting that turn, and whether it is the latest turn group. Exposed so
 * callers can inject a per-turn footer via `renderTurnFooter`. */
export interface TurnGroupData {
  initiatorMessage: ChatMessage;
  responseMessage?: ChatMessage;
  turnRuns: RunInfo[];
  isLatest: boolean;
  precedingModelSwitchEvents: WSEvent[];
  trailingModelSwitchEvents: WSEvent[];
}

function turnGroupRenderKey(group: TurnGroupData, sessionId = ""): string {
  const turnId = group.responseMessage?.id
    ?? group.initiatorMessage.client_id
    ?? group.initiatorMessage.id;
  return `${sessionId}:${turnId}`;
}

function modelSwitchEvents(message?: ChatMessage): WSEvent[] {
  const events = message?.events?.filter((event) => event.type === "model_switched") ?? [];
  return events.length > 0 ? events : EMPTY_MODEL_SWITCH_EVENTS;
}

function joinModelSwitchEvents(left: WSEvent[], right: WSEvent[]): WSEvent[] {
  if (left.length === 0) return right;
  if (right.length === 0) return left;
  return [...left, ...right];
}

function assistantHasVisibleTurnBody(message: ChatMessage, activeTargetIds: ReadonlySet<string>): boolean {
  if (message.role !== "assistant") return false;
  if (message.isStreaming) return true;
  if (activeTargetIds.has(message.id)) return true;
  if (typeof message.content === "string" && message.content.trim()) return true;
  if (message.error || message.status === "error") return true;
  if (message.ask_result || message.chosen_session_id) return true;
  if ((message.workers?.length ?? 0) > 0) return true;
  if (message.historical_hydration_root) return true;
  return (message.events ?? []).some((event) => event.type !== "model_switched");
}

interface Props {
  messages: ChatMessage[];
  pendingMessages: ChatMessage[];
  /** Backend-owned run-state for this session. Drives the "running"
   * badges that replaced the synthetic-streaming-bubble cursor. */
  runs: RunInfo[];
  streamingEvents: WSEvent[];
  isStreaming: boolean;
  isStopping: boolean;
  /** Fine-grained loading phase while the CLI subprocess starts. Null once content flows. */
  streamingLoadPhase: StreamingLoadPhase;
  onSend: (prompt: string, images: import("./InputArea").PastedImage[], files: import("./InputArea").FileAttachment[]) => boolean | Promise<boolean>;
  onSteer?: (prompt: string, images: import("./InputArea").PastedImage[], files: import("./InputArea").FileAttachment[]) => boolean | Promise<boolean>;
  onInterrupt?: (prompt: string, images: import("./InputArea").PastedImage[], files: import("./InputArea").FileAttachment[]) => boolean | Promise<boolean>;
  onAlterUserMessage?: (message: ChatMessage, content: string) => boolean | Promise<boolean>;
  canSteer?: boolean;
  onStop?: () => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (assistantMessage: ChatMessage) => void;
  onContinueRateLimitOnAnotherProvider?: (assistantMessage: ChatMessage) => void;
  rateLimitFallbackLabel?: string | null;
  onChooseAnotherProviderForRateLimit?: (assistantMessage: ChatMessage) => void;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  disabled: boolean;
  session?: Session | null;
  onRename?: (id: string, name: string) => void;
  tags?: InlineTag[];
  onAddTag?: (text: string, comment: string, messageId: string) => void;
  onAdvSync?: (text: string, messageId: string) => void;
  onAdvSyncClick?: (overlay: import("../types").AdvSyncOverlay) => void;
  onRemoveTag?: (id: string) => void;
  /** Backend-backed draft input for this session. The `value` is owned
   * by the backend session record and synced via the
   * `session_metadata_updated` WS event so multiple tabs converge. */
  draft: string;
  onDraftChange: (value: string) => void;
  /** Backend-backed draft images for this session. Mirror of `draft`
   * for pasted/attached images so multiple tabs converge. */
  draftImages?: import("./InputArea").PastedImage[];
  onImagesChange?: (images: import("./InputArea").PastedImage[], text: string) => void;
  /** Optional: when provided, the InputArea renders a "⚙ Engineer"
   * button. Click forwards the trimmed draft up to App.tsx, which opens
   * the fork/new picker and starts the prompt-eng overlay. */
  onEngineer?: (draft: string) => void;
  onSendToNewSession?: (
    prompt: string,
    images: import("./InputArea").PastedImage[],
    files: import("./InputArea").FileAttachment[],
  ) => boolean | Promise<boolean>;
  /** Optional: full root tree for split-pane fork view. When this has
   * a non-empty `forks` array, the linear chat list is replaced by a
   * `ForkSplitView` (shared messages above the fork point, N+1 columns
   * below). When omitted or `forks` is empty, behavior matches the
   * pre-fork single-session layout. */
  tree?: Session | null;
  /** All optimistic pending messages keyed by session id — needed by
   * ForkSplitView so each pane can render its own pending bubbles. */
  pendingBySession?: Record<string, ChatMessage[]>;
  /** Currently focused pane id in the split view. Defaults to the
   * root id when no fork is open. */
  focusedSessionId?: string;
  onSetForkFocus?: (sessionId: string) => void;
  onCloseFork?: (sessionId: string) => void;
  onReopenFork?: (sessionId: string) => void;
  onDeleteFork?: (sessionId: string) => void;
  /** Per-session run state, keyed by session id. ForkSplitView reads
   * its own slice for each pane so multiple panes can show "running"
   * concurrently. */
  runStateBySession?: Record<string, RunInfo[]>;
  /** Fork-and-send: typed prompt creates a new fork (auto-focused). */
  onForkAndSend?: (
    prompt: string,
    images: import("./InputArea").PastedImage[]
  ) => boolean | Promise<boolean>;
  canForkSession?: boolean;
  /** Currently queued prompt packages (shown as independent banners). */
  queuedPrompts?: { id: string; preview: string; images?: import("./InputArea").PastedImage[]; imagesCount?: number; files?: import("./InputArea").FileAttachment[]; filesCount?: number }[];
  onPromoteQueued: (queuedId?: string) => void;
  /** Interrupt with a selected/all set of queued items in one atomic reorder. */
  onPromoteQueuedMulti?: (queuedIds: string[]) => void;
  onSteerQueued?: (queuedId?: string) => void;
  onCancelQueued?: (queuedId?: string) => void;
  onQueuedTextEdit?: (text: string, queuedId?: string) => void;
  onQueuedEditStart?: (queuedId?: string) => void;
  onQueuedEditFinish?: (queuedId?: string) => void;
  /** When the supervisor toggle is on, renders a "Review" button. */
  onReviewLastWork?: () => void;
  /** Flip the supervisor toggle on the focused session. */
  onToggleSupervisor?: (enabled: boolean) => void;
  /** Reopen the supervisor prompt modal to edit the custom prompt
   *  while supervisor is already enabled. */
  onEditSupervisorPrompt?: () => void;
  /** Graduate the supervisor's claude session into a new native BC root
   *  and re-back the supervisor on this session as a fork of it. */
  onSeparateSupervisor?: () => void;
  /** Send target when supervisor is on: "worker" (default, sends to the
   *  primary agent) or "supervisor" (direct chat with the judge).
   *  Only meaningful when `supervisor_enabled === true`. */
  sendTarget?: "worker" | "supervisor";
  onSendTargetChange?: (target: "worker" | "supervisor") => void;
  /** Load older messages on scroll-up. Takes session id + beforeSeq. */
  onLoadOlderMessages?: (sessionId: string, beforeSeq: number) => Promise<void>;
  /** Whether the focused session has older messages to load. */
  hasOlderMessages?: boolean;
  /** True while REST fetch for the session is in flight. */
  sessionLoading?: boolean;
  /** Set when the session REST fetch failed — renders an error state with retry. */
  sessionLoadError?: { sessionId: string; message: string } | null;
  /** Retry loading the session after a failed fetch. */
  onRetrySessionLoad?: (sessionId: string) => void;
  /** Save the current draft as a note. */
  onAddNote?: (text: string) => void;
  onAddCapabilityToNextTurn?: () => void;
  nextTurnCapabilities?: CapabilityContext[];
  onRemoveNextTurnCapability?: (sourceId: string) => void;
  /** Move a single queued prompt to notes (and cancel just that item). */
  onQueuedToNote?: (text: string, queuedId: string) => void;
  /** Cross-project session tabs. */
  openSessions?: Session[];
  /** Whether the open-session tabs bar is shown. */
  sessionTabsVisible?: boolean;
  /** Active tabs sort field — its timestamp shows on each tab. */
  sessionTabsSort?: string;
  providers?: Provider[];
  onCloseTab?: (id: string) => void;
  onCloseOtherTabs?: (id: string) => void;
  onSelectTab?: (id: string) => void;
  onToggleTopbarPin?: (id: string, pinned: boolean) => void;
  /** Optional node rendered at the TOP of the message scroll area,
   * above the first group. Used by the Ask view for its greeting box. */
  headerNode?: import("react").ReactNode;
  composerHeaderNode?: import("react").ReactNode;
  composerOverflowNode?: import("react").ReactNode;
  /** Optional node rendered BELOW each turn group. Used by
   * the Ask view to inject the inline session picker for any turn whose
   * assistant message carries an `ask_result` — rendered outside the
   * group so it stays visible even when the group is collapsed. */
  renderTurnFooter?: (group: TurnGroupData) => import("react").ReactNode;
  /** Optional per-group CSS class. When provided, each group is wrapped
   * in a div (instead of a Fragment) with the returned class. */
  getTurnGroupClassName?: (group: TurnGroupData) => string | undefined;
  /** Hide the per-session toolbar (name + Trace/Raw/Tree toggles).
   * The Ask view has no use for it. */
  hideToolbar?: boolean;
  /** Switch right panel to Notes tab and open it. */
  onShowNotes?: () => void;
  /** Switch right panel to Comments tab and open it. */
  onShowComments?: () => void;
  /** Extension-owned action nodes rendered in the session-view chat toolbar. */
  toolbarActionsNode?: import("react").ReactNode;
  /** Toggle the desktop right panel. Rendered to the right of the Ask
   * button. Reflects the persisted `right_panel_open` state via the
   * `rightPanelOpen` prop so the button can show an "active" style. */
  onToggleRightPanel?: () => void;
  rightPanelOpen?: boolean;
  /** Main-panel view mode for this session (`"chat"` full-height,
   * `"status"` docks chat bottom-left with a status workspace).
   * When omitted, the view-mode toggle is hidden. */
  viewMode?: import("../types").ViewMode;
  onToggleViewMode?: () => void;
  /** Configured shortcut responses from user prefs. */
  shortcutResponses?: string[];
  /** Projects available for @mention in the prompt input. */
  projects?: import("../types").Project[];
  /** Sessions available for @mention in the prompt input. */
  sessions?: import("../types").Session[];
  /** Node the user is currently on — shows a badge on items from other machines. */
  currentNodeId?: string;
  /** Machine snapshots for resolving node_id → display name. */
  machines?: import("../types").NodeSnapshot[];
  userDisplayName?: string | null;
  credentialBrokerEnabled?: boolean;
}

export function Chat({
  messages,
  pendingMessages,
  runs,
  streamingEvents,
  isStreaming,
  isStopping,
  onSend,
  onSteer,
  onInterrupt,
  onAlterUserMessage,
  canSteer,
  onStop,
  onRetry,
  onRetryStopped,
  onContinueRateLimitOnAnotherProvider,
  rateLimitFallbackLabel,
  onChooseAnotherProviderForRateLimit,
  onFileClick,
  onViewDiff,
  disabled,
  session,
  onRename,
  onAddTag,
  onAdvSync,
  tags,
  onAdvSyncClick,
  onRemoveTag,
  draft,
  onDraftChange,
  draftImages,
  onImagesChange,
  onEngineer,
  onSendToNewSession,
  tree,
  pendingBySession,
  focusedSessionId,
  onSetForkFocus,
  onCloseFork,
  onReopenFork,
  onDeleteFork,
  runStateBySession,
  onForkAndSend,
  canForkSession = false,
  queuedPrompts,
  onPromoteQueued,
  onPromoteQueuedMulti,
  onSteerQueued,
  onCancelQueued,
  onQueuedTextEdit,
  onQueuedEditStart,
  onQueuedEditFinish,
  onReviewLastWork,
  onToggleSupervisor,
  onEditSupervisorPrompt,
  onSeparateSupervisor,
  sendTarget,
  onSendTargetChange,
  onLoadOlderMessages,
  hasOlderMessages,
  sessionLoading = false,
  sessionLoadError = null,
  onRetrySessionLoad,
  onAddNote,
  onAddCapabilityToNextTurn,
  nextTurnCapabilities,
  onRemoveNextTurnCapability,
  onQueuedToNote,
  openSessions = [],
  sessionTabsVisible = true,
  sessionTabsSort = "last_opened_at",
  providers = [],
  onCloseTab,
  onCloseOtherTabs,
  onSelectTab,
  onToggleTopbarPin,
  headerNode,
  composerHeaderNode,
  composerOverflowNode,
  renderTurnFooter,
  getTurnGroupClassName,
  hideToolbar,
  onShowNotes,
  onShowComments,
  toolbarActionsNode,
  onToggleRightPanel,
  rightPanelOpen,
  viewMode,
  onToggleViewMode,
  shortcutResponses = [],
  projects = [],
  sessions = [],
  currentNodeId = "primary",
  machines = [],
  userDisplayName = null,
  credentialBrokerEnabled = false,
}: Props) {
  const { t } = useTranslation();
  const chatInlineActionModules = useExtensionFrontendModules("chat-inline-actions");
  const {
    is_running: sessionRunning,
    monitoring_state: monitoringState,
  } = useSessionMeta(session?.id);
  const visibleRuns = runs;
  const [stickToBottom, setStickToBottom] = useState(true);
  const initialScrollSessionRef = useRef<string | null>(null);
  const [_inputFocused, setInputFocused] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(session?.name ?? "");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setEditing(false);
    setEditName(session?.name ?? "");
  }, [session?.id, session?.name]);

  const startEdit = () => {
    if (session && onRename) {
      setEditName(session.name);
      setEditing(true);
    }
  };

  const commitEdit = () => {
    setEditing(false);
    const trimmed = editName.trim();
    if (session && onRename && trimmed && trimmed !== session.name) {
      onRename(session.id, trimmed);
    }
  };

  const cancelEdit = () => {
    setEditing(false);
    setEditName(session?.name ?? "");
  };

  const handleChatPanelPaste = useCallback((event: React.ClipboardEvent<HTMLElement>) => {
    if (disabled || isEditablePasteTarget(event.target)) return;
    const payload = promptClipboardPayload(event.clipboardData);
    if (!payload.text && payload.imageFiles.length === 0) return;
    event.preventDefault();
    const textarea = document.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]');
    const nextText = payload.text
      ? insertTextIntoPrompt(payload.text)
      : textarea?.value ?? draft;
    if (nextText === null) return;
    if (payload.imageFiles.length > 0 && onImagesChange) {
      void Promise.all(payload.imageFiles.map(fileToPastedImage)).then((nextImages) => {
        onImagesChange([...(draftImages ?? []), ...nextImages], nextText);
      });
    }
    textarea?.focus();
  }, [disabled, draft, draftImages, onImagesChange]);

  const loadOlderOpId = `chat:loadOlder:${session?.id ?? "none"}`;
  const { inflight: loadingOlder } = useOpProgress(loadOlderOpId);

  const loadOlderFn = useCallback(async () => {
    if (!onLoadOlderMessages || !session?.id) return;
    const oldest = oldestNumericSeq(messages);
    if (oldest !== null && oldest > 0) {
      await onLoadOlderMessages(session.id, oldest);
    }
  }, [onLoadOlderMessages, session?.id, messages]);

  const {
    scrollRef,
    handleScroll: scrollLoadHandler,
    handleWheel: handleLoadOlderWheel,
    handleTouchStart: handleLoadOlderTouchStart,
    handleTouchMove: handleLoadOlderTouchMove,
    handleTouchEnd: handleLoadOlderTouchEnd,
    handleTouchCancel: handleLoadOlderTouchCancel,
    handlePointerDown: handleLoadOlderPointerDown,
    handlePointerUp: handleLoadOlderPointerUp,
    handleKeyDown: handleLoadOlderKeyDown,
    handleKeyUp: handleLoadOlderKeyUp,
    handleScrollEnd: handleLoadOlderScrollEnd,
    triggerLoadOlder: triggerChatLoadOlder,
    justPrepended,
  } = useScrollLoadOlder(
    loadOlderOpId,
    !!hasOlderMessages,
    onLoadOlderMessages ? loadOlderFn : undefined,
  );
  const [showRaw, setShowRaw] = useState(false);
  const [rawJsonCollapseSignal, setRawJsonCollapseSignal] = useState(0);
  const [toolbarMenuOpen, setToolbarMenuOpen] = useState(false);
  const [voicePlaybackEnabled, setVoicePlaybackEnabled] = useState(false);
  // Close toolbar overflow menu on outside clicks + Escape
  useEffect(() => {
    if (!toolbarMenuOpen) return;
    const mouseHandler = (e: MouseEvent) => {
      const wrapper = (e.target as HTMLElement).closest(".chat-toolbar-overflow-wrapper");
      if (!wrapper) setToolbarMenuOpen(false);
    };
    const keyHandler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setToolbarMenuOpen(false);
    };
    document.addEventListener("mousedown", mouseHandler);
    document.addEventListener("keydown", keyHandler);
    return () => {
      document.removeEventListener("mousedown", mouseHandler);
      document.removeEventListener("keydown", keyHandler);
    };
  }, [toolbarMenuOpen]);
  const [rewindTarget, setRewindTarget] = useState<{
    message: ChatMessage;
    pos: { x: number; y: number };
  } | null>(null);

  // Register handlers for the unified context menu / mobile action sheet.
  // Kept in useEffect to guarantee commit-time execution (not during
  // aborted concurrent renders).
  useEffect(() => {
    registerMobileHandlers({
      rewind: (messageId: string, pos: { x: number; y: number }) => {
        const msg = messages?.find((m) => m.id === messageId);
        if (msg) setRewindTarget({ message: msg, pos });
      },
      addTag: onAddTag,
      advSync: onAdvSync,
    });
    return () => { clearMobileHandlers(); };
  });

  // Pending fresh-worker approvals for the current session. Populated
  // from `worker_creation_requested` WS events AND (on mount / cwd
  // change) from the Team Orchestration extension to rehydrate after a
  // reconnect.
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  useEffect(() => {
    const cwd = session?.cwd;
    if (!cwd) {
      setPendingApprovals([]);
      return;
    }
    const fetchApprovals = async () => {
      try {
        const res = await fetch(
          `${teamOrchestrationApi()}/pending_approvals?cwd=${encodeURIComponent(cwd)}`,
        );
        if (!res.ok) return;
        const data = await res.json();
        setPendingApprovals(data.approvals || []);
      } catch {
        // ignore
      }
    };
    fetchApprovals();
  }, [session?.cwd]);

  useEffect(() => {
    // Live WS delta path. useWebSocket pumps every frame into the typed
    // eventBus; the REST pull above stays the rehydrate/reconcile path.
    const onResolved = ({ delegation_id }: { delegation_id: string }) => {
      setPendingApprovals((prev) => prev.filter((a) => a.delegation_id !== delegation_id));
    };
    const offRequested = eventBus.subscribe("worker_creation_requested", (approval) => {
      if (approval.cwd !== session?.cwd) return;
      setPendingApprovals((prev) => [
        ...prev.filter((a) => a.delegation_id !== approval.delegation_id),
        approval,
      ]);
    });
    const offApproved = eventBus.subscribe("worker_creation_approved", onResolved);
    const offFailed = eventBus.subscribe("worker_creation_failed", onResolved);
    return () => {
      offRequested();
      offApproved();
      offFailed();
    };
  }, [session?.cwd]);
  // Pending credential-broker consents for this session. Backend is the
  // source of truth (consent_store); we pull on mount/session-change and
  // refetch on the `credential_consent_changed` WS invalidation ping.
  const [pendingCredentials, setPendingCredentials] = useState<CredentialConsent[]>([]);
  const credentialBrokerBase = credentialBrokerEnabled
    ? resolvedExtBackendBase("credentialBroker")
    : null;
  const credentialContextRef = useRef({
    sid: session?.id ?? null,
    base: credentialBrokerBase,
  });
  credentialContextRef.current = {
    sid: session?.id ?? null,
    base: credentialBrokerBase,
  };
  const refetchCredentials = useCallback(async (signal?: AbortSignal) => {
    const sid = session?.id;
    const base = credentialBrokerBase;
    if (!sid || !base) {
      setPendingCredentials([]);
      return;
    }
    try {
      const res = await fetch(
        `${base}/credentials/pending?app_session_id=${encodeURIComponent(sid)}`,
        { signal },
      );
      if (!res.ok) return;
      const data = await res.json();
      if (
        credentialContextRef.current.sid !== sid ||
        credentialContextRef.current.base !== base
      ) return;
      setPendingCredentials(data.consents || []);
    } catch {
      // ignore
    }
  }, [credentialBrokerBase, session?.id]);
  useEffect(() => {
    const controller = new AbortController();
    void refetchCredentials(controller.signal);
    return () => {
      controller.abort();
    };
  }, [refetchCredentials]);
  useEffect(() => {
    if (!streamingEvents || streamingEvents.length === 0) return;
    const last = streamingEvents[streamingEvents.length - 1];
    if (last.type !== "credential_consent_changed") return;
    refetchCredentials();
  }, [streamingEvents, refetchCredentials]);
  const [pendingUserInputs, setPendingUserInputs] = useState<UserInputRequest[]>([]);
  const pendingUserInputsSessionRef = useRef<string | null>(null);
  const pendingUserInputsFetchSeqRef = useRef(0);
  pendingUserInputsSessionRef.current = session?.id ?? null;
  useEffect(() => {
    // <Chat> is a long-lived singleton across session switches. Clear
    // immediately so a pending card from the previously viewed session never
    // paints in the newly selected session while its REST snapshot loads.
    setPendingUserInputs([]);
  }, [session?.id]);
  const removePendingUserInput = useCallback((requestId: string) => {
    setPendingUserInputs((prev) => prev.filter((req) => req.request_id !== requestId));
  }, []);
  const refetchUserInputs = useCallback(async () => {
    const sid = session?.id;
    const fetchSeq = ++pendingUserInputsFetchSeqRef.current;
    if (!sid) {
      setPendingUserInputs([]);
      return;
    }
    try {
      const res = await fetch(
        `${API}/api/user-input/pending?app_session_id=${encodeURIComponent(sid)}`,
        { credentials: "include" },
      );
      if (!res.ok) return;
      const data = await res.json();
      if (fetchSeq !== pendingUserInputsFetchSeqRef.current || pendingUserInputsSessionRef.current !== sid) return;
      const fetched = Array.isArray(data.requests) ? (data.requests as UserInputRequest[]) : [];
      setPendingUserInputs(fetched.filter((req) => req.app_session_id === sid));
    } catch {
      // ignore
    }
  }, [session?.id]);
  useEffect(() => {
    refetchUserInputs();
  }, [refetchUserInputs]);
  useEffect(() => {
    const onRequested = (e: Event) => {
      const detail = (e as CustomEvent<UserInputRequest>).detail;
      const sid = pendingUserInputsSessionRef.current;
      if (!sid || detail?.app_session_id !== sid) return;
      setPendingUserInputs((prev) => {
        const rest = prev.filter((req) => req.request_id !== detail.request_id);
        return [...rest, detail];
      });
    };
    const onResolved = (e: Event) => {
      const detail = (e as CustomEvent<{ request_id?: string; app_session_id?: string }>).detail;
      const sid = pendingUserInputsSessionRef.current;
      if (!sid || detail?.app_session_id !== sid || !detail.request_id) return;
      removePendingUserInput(detail.request_id);
    };
    window.addEventListener("user_input_requested", onRequested);
    window.addEventListener("user_input_resolved", onResolved);
    return () => {
      window.removeEventListener("user_input_requested", onRequested);
      window.removeEventListener("user_input_resolved", onResolved);
    };
  }, [removePendingUserInput]);
  const visiblePendingUserInputs = useMemo(() => {
    const sid = session?.id;
    return sid ? pendingUserInputs.filter((req) => req.app_session_id === sid) : [];
  }, [pendingUserInputs, session?.id]);
  // Interactive tool/command approvals (Claude can_use_tool / Codex app-server).
  // Backend holds them in-memory with a fail-closed timeout; rehydrate on
  // mount/reconnect so a missed WS event doesn't silently become a denial.
  const [pendingToolApprovals, setPendingToolApprovals] = useState<ToolApproval[]>([]);
  const removeToolApproval = useCallback((approvalId: string) => {
    setPendingToolApprovals((prev) => prev.filter((a) => a.approval_id !== approvalId));
  }, []);
  const refetchToolApprovals = useCallback(async () => {
    const sid = session?.id;
    if (!sid) {
      setPendingToolApprovals([]);
      return;
    }
    try {
      const res = await fetch(`${API}/api/sessions/${encodeURIComponent(sid)}/tool-approvals/pending`, {
        credentials: "include",
      });
      if (!res.ok) return;
      const data = await res.json();
      const fetched = Array.isArray(data.approvals) ? (data.approvals as ToolApproval[]) : [];
      // Merge, don't replace: a late REST snapshot (taken before a WS-added
      // approval existed) must not clobber a card the live WS event already
      // added — otherwise the user can't approve and the backend denies.
      setPendingToolApprovals((prev) => {
        const byId = new Map(prev.map((a) => [a.approval_id, a]));
        for (const f of fetched) byId.set(f.approval_id, f);
        return [...byId.values()];
      });
    } catch {
      // ignore
    }
  }, [session?.id]);
  useEffect(() => {
    refetchToolApprovals();
  }, [refetchToolApprovals]);
  useEffect(() => {
    const onRequested = (e: Event) => {
      const detail = (e as CustomEvent<ToolApproval>).detail;
      if (!detail || detail.app_session_id !== session?.id) return;
      setPendingToolApprovals((prev) => [
        ...prev.filter((a) => a.approval_id !== detail.approval_id),
        detail,
      ]);
    };
    const onResolved = (e: Event) => {
      const detail = (e as CustomEvent<{ approval_id?: string }>).detail;
      if (detail?.approval_id) removeToolApproval(detail.approval_id);
    };
    window.addEventListener("tool_approval_requested", onRequested);
    window.addEventListener("tool_approval_resolved", onResolved);
    return () => {
      window.removeEventListener("tool_approval_requested", onRequested);
      window.removeEventListener("tool_approval_resolved", onResolved);
    };
  }, [session?.id, removeToolApproval]);
  const chatInlineActionContext = useMemo(
    () => ({
      workerApprovals: pendingApprovals,
      approveWorker: async (delegationId: string, description: string, orchestrationMode: string) => {
        await runThreeStateSync<Response, PendingApproval[]>({
          operationId: `approval:worker:${delegationId}`,
          action: t("toolApproval.title"),
          reconcile: () => {},
          mutate: async () => {
            const response = await fetch(
              `${teamOrchestrationApi()}/pending_approvals/${delegationId}/approve`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ description, orchestration_mode: orchestrationMode }),
              },
            );
            if (!response.ok) throw new Error(await response.text());
            return response;
          },
        });
        setPendingApprovals((prev) =>
          prev.filter((approval) => approval.delegation_id !== delegationId),
        );
      },
      denyWorker: async (delegationId: string) => {
        await runThreeStateSync<Response, PendingApproval[]>({
          operationId: `approval:worker:${delegationId}`,
          action: t("toolApproval.title"),
          reconcile: () => {},
          mutate: async () => {
            const response = await fetch(
              `${teamOrchestrationApi()}/pending_approvals/${delegationId}/deny`,
              { method: "POST" },
            );
            if (!response.ok) throw new Error(await response.text());
            return response;
          },
        });
        setPendingApprovals((prev) =>
          prev.filter((approval) => approval.delegation_id !== delegationId),
        );
      },
      credentialConsents: pendingCredentials,
      approveCredential: async (consentId: string, secrets: Record<string, string>) => {
        if (!credentialBrokerBase) return;
        const body = Object.keys(secrets).length ? { secrets } : {};
        await runThreeStateSync<Response, CredentialConsent[]>({
          operationId: `approval:credential:${consentId}`,
          action: t("toolApproval.title"),
          reconcile: refetchCredentials,
          mutate: async () => {
            const res = await fetch(`${credentialBrokerBase}/credentials/${encodeURIComponent(consentId)}/approve`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            if (!res.ok) {
              const detail = await res.json().catch(() => ({}));
              throw new Error(detail.detail || "approve failed");
            }
            return res;
          },
        });
        setPendingCredentials((prev) => prev.filter((consent) => consent.consent_id !== consentId));
      },
      denyCredential: async (consentId: string) => {
        if (!credentialBrokerBase) return;
        await runThreeStateSync<Response, CredentialConsent[]>({
          operationId: `approval:credential:${consentId}`,
          action: t("toolApproval.title"),
          reconcile: refetchCredentials,
          mutate: async () => {
            const res = await fetch(`${credentialBrokerBase}/credentials/${encodeURIComponent(consentId)}/deny`, {
              method: "POST",
            });
            if (!res.ok) {
              const detail = await res.json().catch(() => ({}));
              throw new Error(detail.detail || "deny failed");
            }
            return res;
          },
        });
        setPendingCredentials((prev) => prev.filter((consent) => consent.consent_id !== consentId));
      },
    }),
    [credentialBrokerBase, pendingApprovals, pendingCredentials, refetchCredentials, t],
  );

  // On session switch: re-stick to bottom and snap there. The Chat
  // component is reused across sessions (no key={session.id}), so
  // stickToBottom from a previous session would otherwise carry over
  // and prevent the new session from rendering scrolled to the end.
  useEffect(() => {
    initialScrollSessionRef.current = session?.id ?? null;
    setStickToBottom(true);
  }, [session?.id]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 50px threshold for sticking to bottom.
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setStickToBottom(isAtBottom);
    scrollLoadHandler();
  }, [scrollLoadHandler]);

  const handleRewindConfirm = useCallback(() => {
    if (!rewindTarget || !session) return;
    const msg = rewindTarget.message;
    if (!msg.agent_message_uuid) return;
    const previousTarget = rewindTarget;
    setRewindTarget(null);
    void runThreeStateSync<Response, Session>({
      operationId: `session:rewind:${session.id}`,
      action: t("rewind.rewindWithFiles"),
      reconcile: () => setRewindTarget(previousTarget),
      mutate: async () => {
        const res = await fetch(`${API}/api/sessions/${session.id}/rewind`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_uuid: msg.agent_message_uuid }),
        });
        if (!res.ok) throw new Error(await res.text());
        return res;
      },
    }).catch(() => {});
  }, [rewindTarget, session, t]);

  const handleSchedule = useCallback(
    async (payload: ScheduleSendPayload): Promise<boolean> => {
      if (!session) return false;
      try {
        await runThreeStateSync({
          operationId: `schedule:create:${session.id}`,
          action: t("schedule.scheduleSend"),
          reconcile: async () => { await fetchSessionSchedules(session.id); },
          mutate: () => createSessionSchedule(session.id, payload),
        });
        return true;
      } catch (e) {
        throw e instanceof Error ? e : new Error(String(e));
      }
    },
    [session, t],
  );

  const allMessages = useMemo(() => {
    const startedAt = performance.now();
    const merged = mergeMessagesSorted(messages, pendingMessages);
    logTiming("chat", "merge_messages", startedAt, {
      messages: messages.length,
      pending: pendingMessages.length,
      merged: merged.length,
    }, 25);
    return merged;
  }, [messages, pendingMessages]);

  const lastAssistantText = useMemo(() => {
    const startedAt = performance.now();
    let text = "";
    for (let i = allMessages.length - 1; i >= 0; i--) {
      const m = allMessages[i];
      if (m.role === "assistant" && m.events) {
        text = extractAssistantTextFromEvents(m.events);
        break;
      }
    }
    logTiming("chat", "last_assistant_text", startedAt, {
      messages: allMessages.length,
      text_length: text.length,
    }, 25);
    return text;
  }, [allMessages]);

  // Stable identity while the set of message ids is unchanged: a streaming
  // token mutates the last message's content, not its id, so `threadIdKey`
  // holds and the Map keeps the same reference. Without this the Map was
  // rebuilt on every token and its new identity broke memo() on every
  // TurnGroup, re-rendering the whole chat instead of the streaming turn group.
  const threadIdKey = allMessages.map((m) => m.id).join("\n");
  const threadColorMap = useMemo(
    () => buildThreadColorMap(allMessages.map((m) => m.id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [threadIdKey],
  );
  const turnGroups = useMemo(() => {
    const startedAt = performance.now();
    const finishProfile = perfSpan("chat_projection", {
      session: perfId(session?.id), messages: allMessages.length, runs: visibleRuns.length,
    });
    const activeTargetIds = new Set(
      visibleRuns
        .map((run) => run.target_message_id)
        .filter((id): id is string => typeof id === "string" && id.length > 0),
    );
    // Pair consecutive turn initiators + assistant messages into turn groups.
    const pairs: {
      initiatorMessage: ChatMessage;
      responseMessage?: ChatMessage;
      precedingModelSwitchEvents: WSEvent[];
    }[] = [];
    let pendingUser: ChatMessage | null = null;
    let pendingUserPrecedingModelSwitchEvents: WSEvent[] = EMPTY_MODEL_SWITCH_EVENTS;
    let unclaimedModelSwitchEvents: WSEvent[] = EMPTY_MODEL_SWITCH_EVENTS;

    const consumeUnclaimedModelSwitchEvents = () => {
      const events = unclaimedModelSwitchEvents;
      unclaimedModelSwitchEvents = EMPTY_MODEL_SWITCH_EVENTS;
      return events;
    };

    for (const m of allMessages) {
      if (m.role === "user") {
        if (pendingUser) {
          pairs.push({
            initiatorMessage: pendingUser,
            precedingModelSwitchEvents: pendingUserPrecedingModelSwitchEvents,
          });
        }
        pendingUser = m;
        pendingUserPrecedingModelSwitchEvents = consumeUnclaimedModelSwitchEvents();
      } else if (m.role === "assistant") {
        if (!assistantHasVisibleTurnBody(m, activeTargetIds)) {
          const events = modelSwitchEvents(m);
          if (events.length > 0) {
            unclaimedModelSwitchEvents =
              unclaimedModelSwitchEvents.length > 0
                ? [...unclaimedModelSwitchEvents, ...events]
                : events;
          }
          continue;
        }
        if (pendingUser) {
          pairs.push({
            initiatorMessage: pendingUser,
            responseMessage: m,
            precedingModelSwitchEvents: pendingUserPrecedingModelSwitchEvents,
          });
          pendingUser = null;
          pendingUserPrecedingModelSwitchEvents = EMPTY_MODEL_SWITCH_EVENTS;
        } else {
          // Orphan assistant (user msg was cancelled / never persisted).
          // Synthesize an empty user stub so the assistant renders in
          // its proper slot instead of being mislabeled as "User".
          pairs.push({
            initiatorMessage: {
              id: `__synth-${m.id}`,
              role: "user" as const,
              content: "",
              events: [],
              timestamp: m.timestamp,
              isStreaming: false,
            },
            responseMessage: m,
            precedingModelSwitchEvents: consumeUnclaimedModelSwitchEvents(),
          });
        }
      }
    }
    if (pendingUser) {
      pairs.push({
        initiatorMessage: pendingUser,
        precedingModelSwitchEvents: pendingUserPrecedingModelSwitchEvents,
      });
    }

    const lastGroupIdx = pairs.length - 1;
    const projected = pairs.map((pair, idx) => {
      const mids = new Set<string>();
      mids.add(pair.initiatorMessage.id);
      if (pair.responseMessage) mids.add(pair.responseMessage.id);

      const collected = visibleRuns.filter((r) => r.target_message_id && mids.has(r.target_message_id));

      // In-flight run (no target yet) belongs to the last group.
      if (idx === lastGroupIdx) {
        collected.push(...visibleRuns.filter(isUnanchoredRun));
      }
      return {
        ...pair,
        turnRuns: collected.length > 0 ? collected : EMPTY_CHAT_RUNS,
        isLatest: idx === lastGroupIdx,
        precedingModelSwitchEvents: joinModelSwitchEvents(
          pair.precedingModelSwitchEvents,
          idx > 0 ? modelSwitchEvents(pairs[idx - 1].responseMessage) : EMPTY_MODEL_SWITCH_EVENTS,
        ),
        trailingModelSwitchEvents:
          idx === lastGroupIdx
            ? joinModelSwitchEvents(modelSwitchEvents(pair.responseMessage), unclaimedModelSwitchEvents)
            : EMPTY_MODEL_SWITCH_EVENTS,
      };
    });
    finishProfile();
    logTiming("chat", "turn_groups", startedAt, {
      messages: allMessages.length,
      runs: visibleRuns.length,
      groups: projected.length,
    }, 25);
    return projected;
  }, [allMessages, visibleRuns, session?.id]);

  // Coalesce streaming-driven re-renders so the chat's layout animations
  // animate in chunks instead of re-triggering on every token. Idle sessions
  // pass through immediately so user interactions stay snappy.
  const displayTurnGroups = useThrottledValue(turnGroups, sessionRunning && isStreaming ? 140 : 0);

  const runMetaMarkedIds = useMemo(
    () => runMetaMarkedMessageIds(displayTurnGroups, session),
    [displayTurnGroups, session],
  );

  // Sync scroll to bottom when the RENDERED content changes (if stickToBottom).
  // Keyed on displayTurnGroups (the throttled render data), not raw messages, so
  // the snap runs in the same commit that grows the DOM — otherwise throttling
  // would scroll to the stale pre-update height. Skip the snap on a prepend
  // render (older messages loaded); the hook's layout effect already restored
  // the pre-prepend position.
  useLayoutEffect(() => {
    if (justPrepended.current) {
      justPrepended.current = false;
      return;
    }
    const initialOpen = !!session?.id
      && initialScrollSessionRef.current === session.id
      && !sessionLoading
      && displayTurnGroups.length > 0;
    if (!initialOpen && !stickToBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    const startedAt = performance.now();
    void el.scrollHeight;
    const readMs = performance.now() - startedAt;
    scrollToLatest(el);
    if (initialOpen) initialScrollSessionRef.current = null;
    perfRecord("scroll_dom", {
      session: perfId(session?.id), read_ms: Math.round(readMs * 10) / 10,
      groups: displayTurnGroups.length,
    });
  }, [displayTurnGroups, stickToBottom, pendingMessages, streamingEvents, visiblePendingUserInputs, justPrepended, session?.id, sessionLoading]);

  const latestTurnGroup = turnGroups[turnGroups.length - 1];
  const latestTurnGroupRunning =
    !!latestTurnGroup &&
    sessionRunning &&
    (isStreaming ||
      (latestTurnGroup.responseMessage
        ? isGroupRunning(latestTurnGroup.turnRuns)
        : latestTurnGroup.turnRuns.length > 0));
  const latestResponseSpeech = assistantSpeechText(latestTurnGroup?.responseMessage);
  const previousLatestTurnRef = useRef<{
    sessionId?: string;
    initiatorMessageId?: string;
    responseMessageId?: string;
    running: boolean;
  } | null>(null);
  useEffect(() => {
    const current = {
      sessionId: session?.id,
      initiatorMessageId: latestTurnGroup?.initiatorMessage.id,
      responseMessageId: latestTurnGroup?.responseMessage?.id,
      running: latestTurnGroupRunning,
    };
    const previous = previousLatestTurnRef.current;
    previousLatestTurnRef.current = current;
    if (
      !previous ||
      previous.sessionId !== current.sessionId ||
      previous.initiatorMessageId !== current.initiatorMessageId ||
      previous.responseMessageId !== current.responseMessageId ||
      !previous.running ||
      current.running ||
      !voicePlaybackEnabled ||
      !latestResponseSpeech
    ) {
      return;
    }
    speakAssistantText(latestResponseSpeech);
  }, [
    latestResponseSpeech,
    latestTurnGroup?.responseMessage?.id,
    latestTurnGroup?.initiatorMessage.id,
    latestTurnGroupRunning,
    session?.id,
    voicePlaybackEnabled,
  ]);

  useEffect(() => {
    if (voicePlaybackEnabled) return;
    window.speechSynthesis?.cancel();
  }, [voicePlaybackEnabled]);

  // Groups freshly prepended by "load older" — they animate in on mount.
  // A top-prepend pushes the previous first group down; everything above
  // its new position is new. Anchor missing (session switch) or at index 0
  // (plain append) → nothing animates. Read of the ref is intentional: it
  // still holds the PRE-prepend first id during this render; the layout
  // effect below advances it only after commit.
  const prevFirstGroupIdRef = useRef<string | undefined>(undefined);
  const reduceMotion = useReducedMotion();
  const enteringGroupIds = useMemo(() => {
    if (reduceMotion) return NO_ENTERING;
    const prevFirst = prevFirstGroupIdRef.current;
    if (!prevFirst) return NO_ENTERING;
    const anchorIdx = displayTurnGroups.findIndex((g) => turnGroupRenderKey(g, session?.id) === prevFirst);
    if (anchorIdx <= 0) return NO_ENTERING;
    return new Set(displayTurnGroups.slice(0, anchorIdx).map((g) => turnGroupRenderKey(g, session?.id)));
  }, [displayTurnGroups, reduceMotion, session?.id]);
  useLayoutEffect(() => {
    prevFirstGroupIdRef.current = displayTurnGroups[0] ? turnGroupRenderKey(displayTurnGroups[0], session?.id) : undefined;
  }, [displayTurnGroups, session?.id]);

  return (
    <MotionConfig reducedMotion="user" transition={{ duration: 0.55, ease: "easeInOut" }}>
    <div
      className="chat-container"
      data-testid="chat-container"
      data-session-running={sessionRunning ? "true" : "false"}
    >
      {sessionTabsVisible && openSessions.length > 0 && onSelectTab && onCloseTab && onCloseOtherTabs && onToggleTopbarPin && (
        <SessionTabs
          sessions={openSessions}
          providers={providers ?? []}
          currentSessionId={session?.id}
          sortField={sessionTabsSort}
          onSelect={onSelectTab}
          onClose={onCloseTab}
          onCloseOthers={onCloseOtherTabs}
          onToggleTopbarPin={onToggleTopbarPin}
        />
      )}
      {session && !hideToolbar && (
        <div className="chat-toolbar">
          {editing && onRename ? (
            <input
              ref={(el) => { inputRef.current = el; }}
              className="chat-rename-input"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") commitEdit();
                if (e.key === "Escape") cancelEdit();
              }}
              onBlur={commitEdit}
              onClick={(e) => e.stopPropagation()}
              autoFocus
            />
          ) : (
            <div
              className="chat-toolbar-title"
              title={session.name}
              onDoubleClick={startEdit}
            >
              {session.name}
            </div>
          )}
          {onShowNotes && (session.notes?.length ?? 0) > 0 && (
            <button
              className="chat-toolbar-badge-btn"
              onClick={onShowNotes}
            >
              <Icon name="memo" size={13} /> {(session.notes ?? []).length}
            </button>
          )}
          {onShowComments && (tags?.length ?? 0) > 0 && (
            <button
              className="chat-toolbar-badge-btn"
              onClick={onShowComments}
            >
              <Icon name="chat" size={13} /> {(tags ?? []).length}
            </button>
          )}
          {toolbarActionsNode}
          {onToggleRightPanel && (
            <button
              className={
                "chat-toolbar-right-panel-toggle" +
                (rightPanelOpen ? " active" : "")
              }
              onClick={onToggleRightPanel}
              title={rightPanelOpen ? t("app.closeFiles") : t("app.toggleFiles")}
              aria-label={
                rightPanelOpen ? t("app.closeFiles") : t("app.toggleFiles")
              }
              aria-pressed={rightPanelOpen ? true : false}
            >
              <Icon name="memo" size={18} />
            </button>
          )}
          {onToggleViewMode && (
            <button
              className={
                "chat-toolbar-view-mode-toggle" +
                (viewMode === "status" ? " active" : "")
              }
              onClick={onToggleViewMode}
              title={
                viewMode === "status"
                  ? t("statusView.switchToChat")
                  : t("statusView.switchToStatus")
              }
              aria-label={
                viewMode === "status"
                  ? t("statusView.switchToChat")
                  : t("statusView.switchToStatus")
              }
              aria-pressed={viewMode === "status"}
            >
              <Icon name="activity" size={18} />
            </button>
          )}
          <VoiceActivation onEnabledChange={setVoicePlaybackEnabled} />
          <div className="chat-toolbar-overflow-wrapper">
            <button
              className="chat-toolbar-overflow-trigger"
              onClick={() => setToolbarMenuOpen((v) => !v)}
            >
              ⋯
            </button>
            {toolbarMenuOpen && (
              <div className="chat-toolbar-overflow-menu">
                <button
                  className={`raw-toggle ${showRaw ? "active" : ""}`}
                  onClick={() => {
                    setShowRaw((v) => !v);
                    setToolbarMenuOpen(false);
                  }}
                >
                  {showRaw ? t("chat.chatButton") : t("chat.rawJsonButton")}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      <motion.div
        layoutScroll
        className="chat-messages"
        data-testid="chat-messages"
        ref={scrollRef}
        onScroll={handleScroll}
        onScrollEnd={handleLoadOlderScrollEnd}
        onWheel={handleLoadOlderWheel}
        onTouchStart={handleLoadOlderTouchStart}
        onTouchMove={handleLoadOlderTouchMove}
        onTouchEnd={handleLoadOlderTouchEnd}
        onTouchCancel={handleLoadOlderTouchCancel}
        onPointerDown={handleLoadOlderPointerDown}
        onPointerUp={handleLoadOlderPointerUp}
        onKeyDown={handleLoadOlderKeyDown}
        onKeyUp={handleLoadOlderKeyUp}
        onPaste={handleChatPanelPaste}
        tabIndex={0}
      >
        {headerNode}

        {hasOlderMessages && !sessionLoading && (
          <div className="load-older-wrapper">
            {loadingOlder ? (
              <div className="load-older-spinner">
                {t("chat.loadingOlderMessages")}
                <span className="load-older-dots" aria-hidden="true">
                  <i /><i /><i />
                </span>
              </div>
            ) : (
              <button className="load-older-link" onClick={triggerChatLoadOlder}>
                {t("chat.loadOlderMessages")}
              </button>
            )}
          </div>
        )}

        {showRaw && (
          <div className="raw-events-viewer">
            <div className="raw-events-toolbar">
              <button
                className="raw-toggle json-tree-collapse"
                onClick={() => setRawJsonCollapseSignal((v) => v + 1)}
              >
                {t("chat.collapseJsonTreeButton", { defaultValue: "Collapse tree" })}
              </button>
            </div>
            {streamingEvents.map((e, i) => (
              <JsonNode key={i} value={e} collapseSignal={rawJsonCollapseSignal} />
            ))}
          </div>
        )}

        {!showRaw && (
          <>
            {sessionLoadError && sessionLoadError.sessionId === (focusedSessionId ?? tree?.id) ? (
              <div className="chat-load-error" role="alert">
                <span className="chat-load-error-text">
                  {t("chat.sessionLoadFailed", { detail: sessionLoadError.message })}
                </span>
                {onRetrySessionLoad && (
                  <button
                    type="button"
                    className="chat-load-error-retry"
                    onClick={() => onRetrySessionLoad(sessionLoadError.sessionId)}
                  >
                    {t("chat.sessionLoadRetry")}
                  </button>
                )}
              </div>
            ) : sessionLoading && displayTurnGroups.length === 0 ? (
              <div className="chat-loading-skeleton">
                <div className="chat-loading-pulse" />
              </div>
            ) : tree && userFacingForks(tree).length > 0 ? (
              <ForkSplitView
                tree={tree}
                focusedSessionId={focusedSessionId ?? tree.id}
                pendingBySession={pendingBySession ?? {}}
                runStateBySession={runStateBySession ?? {}}
                userDisplayName={userDisplayName}
                onSetFocus={onSetForkFocus ?? (() => {})}
                onCloseFork={onCloseFork ?? (() => {})}
                onReopenFork={onReopenFork ?? (() => {})}
                onDeleteFork={onDeleteFork}
                onLoadOlderMessages={onLoadOlderMessages}
              />
            ) : (
              <LayoutGroup>
              {displayTurnGroups.map((g) => {
                const groupCls = getTurnGroupClassName?.(g);
                const Wrapper = groupCls ? "div" : Fragment;
                const wrapperProps = groupCls ? { className: groupCls } : {};
                const groupKey = turnGroupRenderKey(g, session?.id);
                return (
                  <Wrapper key={groupKey} {...wrapperProps}>
                    <TurnGroup
                      enterAnimation={enteringGroupIds.has(groupKey)}
                      initiatorMessage={g.initiatorMessage}
                      responseMessage={g.responseMessage}
                      precedingModelSwitchEvents={g.precedingModelSwitchEvents}
                      trailingModelSwitchEvents={g.trailingModelSwitchEvents}
                      runs={g.turnRuns}
                      sessionRunning={g.isLatest ? sessionRunning : false}
                      activelyStreaming={g.isLatest && isStreaming && sessionRunning}
                      fallbackRunMeta={
                        g.isLatest && session
                          ? {
                              providerId: session.provider_id ?? null,
                              model: session.model ?? null,
                              reasoningEffort: session.reasoning_effort ?? null,
                            }
                          : undefined
                      }
                      showRunMeta={
                        !!g.responseMessage && runMetaMarkedIds.has(g.responseMessage.id)
                      }
                      // Never auto-collapse a group that is still running.
                      defaultCollapsed={
                        !!g.responseMessage &&
                        !(
                          g.isLatest &&
                          sessionRunning &&
                          (isStreaming || isGroupRunning(g.turnRuns))
                        )
                      }
                      threadColorMap={threadColorMap}
                      onRetry={onRetry}
                      onRetryStopped={onRetryStopped}
                      onContinueRateLimitOnAnotherProvider={onContinueRateLimitOnAnotherProvider}
                      rateLimitFallbackLabel={rateLimitFallbackLabel}
                      onChooseAnotherProviderForRateLimit={onChooseAnotherProviderForRateLimit}
                      onAlterTurnMessage={
                        onAlterUserMessage &&
                        g.isLatest &&
                        !g.initiatorMessage.id.startsWith("pending-")
                          ? onAlterUserMessage
                          : undefined
                      }
                      onFileClick={onFileClick}
                      onViewDiff={onViewDiff}
                      tags={tags}
                      onRemoveTag={onRemoveTag}
                      onAdvSyncClick={onAdvSyncClick}
                      scrollEl={scrollRef.current}
                      sessionId={session?.id}
                      userDisplayName={userDisplayName}
                      renderWorkDetails={session?.id && g.responseMessage?.historical_hydration_root
                        ? (active, onTerminal) => (
                            <HistoricalTurnDetails
                              sessionId={session.id}
                              messageId={g.responseMessage!.id}
                              manifest={g.responseMessage!.historical_hydration_root!}
                              active={active}
                              onTerminal={onTerminal}
                            />
                          )
                        : undefined}
                      historicalDirectChildCount={g.responseMessage?.historical_hydration_root?.direct_child_count ?? 0}
                    />
                    {renderTurnFooter?.(g)}
                  </Wrapper>
                );
              })}
              </LayoutGroup>
            )}
            {!sessionLoading && session?.root_events && session.root_events.length > 0 && (
              <div className="root-events">
                <div className="root-events__label">{t("chat.rootEvents")}</div>
                <MessageBubble
                  message={{
                    id: "__root__",
                    role: "assistant",
                    events: session.root_events,
                    content: "",
                    workers: [],
                    timestamp: session.updated_at,
                    isStreaming: false,
                  } as ChatMessage}
                  sessionId={session.id}
                  orchestrationMode={session.orchestration_mode}
                  threadColorMap={threadColorMap}
                  onFileClick={onFileClick}
                  onViewDiff={onViewDiff}
                  userDisplayName={userDisplayName}
                />
              </div>
            )}
          </>
        )}

        {(pendingApprovals.length > 0 || pendingCredentials.length > 0) &&
          chatInlineActionModules.map((module) => (
            <ExtensionModuleSlot
              key={`${module.extension_id}:${module.id}`}
              module={module}
              context={chatInlineActionContext}
            />
          ))}
        {visiblePendingUserInputs.map((request) => (
          <UserInputCard
            key={request.request_id}
            request={request}
            onDone={removePendingUserInput}
          />
        ))}
        {pendingToolApprovals.map((approval) => (
          <ToolApprovalCard
            key={approval.approval_id}
            approval={approval}
            sessionId={session?.id ?? ""}
            onResolved={removeToolApproval}
          />
        ))}
      </motion.div>

      <SessionBackgroundStrip key={session?.id ?? "none"} sessionId={session?.id} />

      {(() => {
        const effectiveSessionRunning = isStreaming || sessionRunning || visibleRuns.length > 0;
        const effectiveIsStreaming =
          effectiveSessionRunning && monitoringState !== "waiting_on_background";
        return (
          <>
            <ShortcutResponses
              onSend={(prompt) => onSend(prompt, [], [])}
              isStreaming={effectiveIsStreaming}
              disabled={disabled}
              lastAssistantText={lastAssistantText}
              shortcuts={shortcutResponses}
            />

            <InputArea
              onSend={onSend}
              onSteer={onSteer}
              onInterrupt={onInterrupt}
              canSteer={!!canSteer}
              onFork={onForkAndSend}
              canFork={!!onForkAndSend && canForkSession}
              onEngineer={onEngineer}
              onSendToNewSession={onSendToNewSession}
              disabled={disabled}
              isStreaming={effectiveIsStreaming}
              isStopping={isStopping}
              onStop={effectiveSessionRunning ? onStop : undefined}
              showStop={effectiveSessionRunning}
              sessionId={session?.id}
              onSchedule={session ? handleSchedule : undefined}
              draft={draft}
              onDraftChange={onDraftChange}
              draftImages={draftImages}
              onImagesChange={onImagesChange}
              queuedPrompts={queuedPrompts}
              onPromoteQueued={onPromoteQueued}
              onPromoteQueuedMulti={onPromoteQueuedMulti}
              onSteerQueued={onSteerQueued}
              onCancelQueued={onCancelQueued}
              onQueuedTextEdit={onQueuedTextEdit}
              onQueuedEditStart={onQueuedEditStart}
              onQueuedEditFinish={onQueuedEditFinish}
              onReviewLastWork={onReviewLastWork}
              tagCount={tags?.length ?? 0}
              sendTarget={sendTarget}
              onSendTargetChange={onSendTargetChange}
              supervisorEnabled={!!session?.supervisor_enabled}
              onToggleSupervisor={onToggleSupervisor}
              onEditSupervisorPrompt={onEditSupervisorPrompt}
              onSeparateSupervisor={onSeparateSupervisor}
              onAddNote={onAddNote}
              onAddCapabilityToNextTurn={onAddCapabilityToNextTurn}
              nextTurnCapabilities={nextTurnCapabilities}
              onRemoveNextTurnCapability={onRemoveNextTurnCapability}
              onQueuedToNote={onQueuedToNote}
              onFocusChange={setInputFocused}
              projects={projects}
              sessions={sessions}
              currentNodeId={currentNodeId}
              machines={machines}
              headerNode={composerHeaderNode}
              overflowPanelNode={composerOverflowNode}
            />
          </>
        );
      })()}
      {onAddTag && (
        <SelectionPopup onAdd={onAddTag} onAdvSync={onAdvSync} />
      )}
      {rewindTarget && (
        <RewindPopover
          x={rewindTarget.pos.x}
          y={rewindTarget.pos.y}
          enabled={!!rewindTarget.message.agent_message_uuid}
          onConfirm={handleRewindConfirm}
          onClose={() => setRewindTarget(null)}
        />
      )}
    </div>
    </MotionConfig>
  );
}
