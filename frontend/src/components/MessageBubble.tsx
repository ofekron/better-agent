import { Fragment, memo, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { turnGroupPropsEqual } from "./turnGroupPropsEqual";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import { turnMessageHeader } from "../lib/turnMessageHeader";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import MarkdownPreview from "@uiw/react-markdown-preview";
import "@uiw/react-markdown-preview/markdown.css";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import type { ChatMessage, EntityBlock, FileFocus, OrchestrationMode, RunInfo, TodoItem, WorkerPanel, WSEvent } from "../types";
import { TodoItemRow } from "./TodosPanel";
import type { InlineTag } from "../types/inlineTag";
import { ThinkingBlock } from "./ThinkingBlock";
import { JsonNode } from "./JsonNode";
import { RunBadgeStack } from "./RunBadge";
import Icon from "./Icon";
import { applyAdvSyncOverlays } from "../utils/advSyncOverlays";
import { useMessageDecorations } from "../hooks/useMessageDecorations";
import type { AdvSyncOverlay } from "../types";
import { linkifyFilePaths, markdownLinkifyComponents, sessionLinkMarker, sessionMarkersToMarkdown } from "../utils/linkifyFilePaths";
import {
  parseArtificialSections,
  hasArtificialSections,
  prettyTagLabel,
  tagPreview,
  UNWRAP_TAG,
  type Segment,
} from "../utils/artificialSections";
import { parseInlineTagsBody } from "../utils/inlineTagsPrompt";
import { getStrategy } from "../strategies";
import { isGroupRunning } from "../utils/groupRunning";
import { isUnanchoredRun } from "../utils/runTargets";
import { dedupeWorkerPanels, isCreationPanelKind, panelKindLabel } from "../utils/mergeEvents";
import { API } from "../api";
import { isSaveShortcutEvent } from "../hooks/useSaveShortcut";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { flattenClaudeMessages } from "../utils/agentMessages";
import { formatWholeJsonMessage } from "../utils/formatWholeJsonMessage";
import { buildMessageImageUrl } from "../utils/messageImages";
import { unwrapTypedAgentMessageEnvelope, unwrapWorkerEventEnvelope } from "../utils/workerEventEnvelope";
import { providerNameForId } from "../utils/providerCache";

/** Stable empty-array singleton so AssistantMessage's memo shallow
 *  compare holds when a group has no runs targeting it. A fresh `[]`
 *  per render would defeat the memo and force re-render on every
 *  parent re-render. Frozen so an accidental `.push` throws loudly
 *  rather than silently leaking entries into every other group. */
const EMPTY_RUNS: RunInfo[] = Object.freeze(
  [],
) as unknown as RunInfo[];
const EMPTY_ACTIVE_WORKER_IDS: ReadonlySet<string> = Object.freeze(new Set<string>()) as ReadonlySet<string>;
const EMPTY_WORKER_DEFAULT_OPEN: ReadonlyMap<string, boolean> = Object.freeze(new Map<string, boolean>()) as ReadonlyMap<string, boolean>;

const ToolCall = lazyWithRetry(() =>
  import("./ToolCall").then((m) => ({ default: m.ToolCall })),
);

type ModelRunMeta = {
  providerId?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
};

function buildRunMetaParts(meta?: ModelRunMeta): Array<{ key: string; label: string; value: string }> {
  if (!meta) return [];
  const parts: Array<{ key: string; label: string; value: string }> = [];
  const providerName = providerNameForId(meta.providerId);
  const model = meta.model?.trim();
  const reasoningEffort = meta.reasoningEffort?.trim();
  if (providerName) parts.push({ key: "provider", label: "message.provider", value: providerName });
  if (model) parts.push({ key: "model", label: "message.model", value: model });
  if (reasoningEffort) parts.push({ key: "effort", label: "message.effort", value: reasoningEffort });
  return parts;
}

function RunMetaChips({ meta }: { meta?: ModelRunMeta }) {
  const { t } = useTranslation();
  const parts = buildRunMetaParts(meta);
  if (parts.length === 0) return null;
  return (
    <span className="run-meta-chips" title={parts.map((part) => `${t(part.label)}: ${part.value}`).join(" / ")}>
      {parts.map((part) => (
        <span className="run-meta-chip" key={part.key}>
          <span className="run-meta-chip-label">{t(part.label)}</span>
          <span className="run-meta-chip-value">{part.value}</span>
        </span>
      ))}
    </span>
  );
}

function workerPanelComplete(worker: WorkerPanel): boolean {
  return (
    worker.success !== undefined ||
    worker.error != null ||
    worker.jsonl_path !== undefined ||
    worker.new_byte_offset !== undefined ||
    worker.token_usage !== undefined
  );
}

function workerPanelDefaultOpen(worker: WorkerPanel, activeWorkerIds: ReadonlySet<string>): boolean {
  if (isCreationPanelKind(worker.panel_kind)) return false;
  return activeWorkerIds.has(worker.delegation_id) && !workerPanelComplete(worker);
}

/** Walk up the DOM tree from `el` and return the nearest ancestor
 *  whose computed `overflow-y` makes it a scroll container. Used by
 *  the collapse-toggle anchor logic when the parent hasn't threaded
 *  an explicit `scrollEl` prop — works for every container regardless
 *  of class name. Returns null if nothing in the chain scrolls. */
function findScrollParent(el: HTMLElement): HTMLElement | null {
  let parent = el.parentElement;
  while (parent) {
    const overflowY = getComputedStyle(parent).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return parent;
    parent = parent.parentElement;
  }
  return null;
}

/** Returns true when the text has no rendering payload (only whitespace,
 *  zero-width chars, BOM, etc.). MessageBox uses this to bail. */
function isEffectivelyEmpty(text: string): boolean {
  // Strip ASCII whitespace, line breaks, zero-width chars, BOM, and the
  // word-joiner / soft-hyphen that JS .trim() leaves alone.
  const stripped = text.replace(/[\s\u200B-\u200D\u2060\uFEFF\u00AD]/g, "");
  return stripped.length === 0;
}

function previewEventsForMessage(message: ChatMessage | undefined, mode?: OrchestrationMode): WSEvent[] {
  if (!message) return [];
  const stubEvents = message.stub?.last_events;
  if (stubEvents && stubEvents.length > 0) return stubEvents;
  return getStrategy(mode).getEvents(message);
}

function decodeEscapedUnicodeForDisplay(text: string): string {
  return text.replace(/\\u([0-9a-fA-F]{4})/g, (_match, hex: string) => {
    const codePoint = Number.parseInt(hex, 16);
    if (!Number.isFinite(codePoint) || codePoint < 0x20) return _match;
    return String.fromCharCode(codePoint);
  });
}

const COLLAPSE_ELLIPSIS = "• • •";

/** First non-empty line, trimmed to ~80 chars, for the collapsed preview. */
function firstLineSummary(text: string, max = 80): string {
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    return line.length > max ? line.slice(0, max - 1) + "\u2026" : line;
  }
  return "";
}

function TeamMessageFrom({ message }: { message: ChatMessage }) {
  const { t } = useTranslation();
  const senderSessionId = message.team_message?.metadata?.sender_session_id?.trim();
  if (!senderSessionId) return null;
  const senderName = message.team_message?.metadata?.sender_name?.trim() || senderSessionId;
  return (
    <div className="team-message-from">
      <span className="team-message-from-label">{t("message.fromSender")}</span>
      {linkifyFilePaths(sessionLinkMarker(senderSessionId, senderName))}
    </div>
  );
}

function eventAssistantText(event: WSEvent): string {
  const data = event.data as Record<string, unknown> | undefined;
  const message = data?.message as Record<string, unknown> | undefined;
  if (data?.type !== "assistant" || message?.role !== "assistant") return "";
  const content = message.content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (!part || typeof part !== "object") return "";
      const item = part as Record<string, unknown>;
      return item.type === "text" && typeof item.text === "string"
        ? item.text
        : "";
    })
    .filter(Boolean)
    .join("\n")
    .trim();
}

function normalizeAssistantContentText(text: string): string {
  return cleanOutput(text)
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n")
    .trim();
}

function visibleAssistantOutputTexts(events: WSEvent[]): string[] {
  const { flat } = flattenClaudeMessages(events);
  const texts: string[] = [];
  for (const event of flat) {
    if (event.type !== "output") continue;
    const parentToolUseId = event.data?.parent_tool_use_id;
    if (typeof parentToolUseId === "string" && parentToolUseId) continue;
    const clean = cleanOutput(String(event.data?.output ?? ""));
    if (!clean || classifyOutput(clean) !== "text") continue;
    texts.push(clean);
  }
  return texts;
}

function visibleEventsRepresentAssistantContent(events: WSEvent[], content: string): boolean {
  const normalizedContent = normalizeAssistantContentText(content);
  if (!normalizedContent) return false;
  const normalizedOutputs = visibleAssistantOutputTexts(events)
    .map(normalizeAssistantContentText)
    .filter(Boolean);
  if (normalizedOutputs.some((text) => text === normalizedContent)) return true;
  return normalizeAssistantContentText(normalizedOutputs.join("\n")) === normalizedContent;
}

function eventTailContainsAssistantContent(events: WSEvent[], content: string): boolean {
  return visibleEventsRepresentAssistantContent(events, content) ||
    events.some((event) => normalizeAssistantContentText(eventAssistantText(event)) === normalizeAssistantContentText(content));
}

/**
 * Live-countdown pill rendered while the orchestrator sleeps between a
 * rate-limited (429) attempt and the next retry. `retryAt` is an
 * absolute ISO timestamp; the component ticks the rendered seconds-
 * remaining locally on a 1s interval so we don't need per-second WS
 * traffic. Disappears once the timestamp passes — the backend then
 * clears the field via `message_retrying_changed { retry_at: null }`.
 */
function RetryingPill({ retryAt }: { retryAt: string }) {
  const { t } = useTranslation();
  const target = useMemo(() => new Date(retryAt).getTime(), [retryAt]);
  const compute = () =>
    Math.max(0, Math.ceil((target - Date.now()) / 1000));
  const [secondsLeft, setSecondsLeft] = useState<number>(compute);
  useEffect(() => {
    setSecondsLeft(compute());
    const id = window.setInterval(() => setSecondsLeft(compute()), 1000);
    return () => window.clearInterval(id);
    // `compute` closes over `target`; depending on `target` is enough.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);
  const label =
    secondsLeft < 90
      ? t("message.retryingIn", { seconds: secondsLeft })
      : secondsLeft < 5400
        ? `Retrying in ${Math.ceil(secondsLeft / 60)}m`
        : `Retrying in ${Math.ceil(secondsLeft / 3600)}h`;
  return (
    <div
      className="retrying-pill"
      data-testid="message-retrying-pill"
      role="status"
      aria-live="polite"
    >
      <span className="retrying-spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/** Inline banner shown while the backend starts a fresh subprocess for a
 * context-window continuation. Displays once and disappears when the
 * new subprocess begins producing output. */
function ContinuationPill({ chainDepth }: { chainDepth: number }) {
  const { t } = useTranslation();
  return (
    <div
      className="continuation-pill"
      data-testid="message-continuation-pill"
      role="status"
      aria-live="polite"
    >
      <span className="continuation-spinner" aria-hidden="true" />
      <span>
        {t("message.autoContinuing")}
        {chainDepth > 1 ? ` (${chainDepth})` : ""}
      </span>
    </div>
  );
}

// Inline-style sentinel emitted by the backend tag-rule pass
// (file_ref_resolver). `⁣[[bcstyle:ATTRS]]…[[/bcstyle]]⁣` where ATTRS is
// `key=value` pairs joined by `;` — `s=SCALE` (font scale) and
// `bg=HEX` + `a=ALPHA` (transparent background highlight). ASCII + bracketed
// so it round-trips through markdown untouched. Rendered here as a styled
// block callout (NEVER raw HTML — the markdown pipeline escapes that by
// design): a background highlight + font scale paint reliably only on a
// block box, not an inline span wrapping block-level markdown. Styled
// segments render their inner markdown in a nested MarkdownPreview wrapped
// in a `.bc-font-scaled` block.
const STYLE_SENTINEL_RE = /⁣\[\[bcstyle:([^\]]*)\]\]([\s\S]*?)\[\[\/bcstyle\]\]⁣/;
const STYLE_SENTINEL_STRIP_RE = /⁣\[\[bcstyle:[^\]]*\]\]|\[\[\/bcstyle\]\]⁣/g;

function parseStyleAttrs(raw: string): {
  fontSize?: string;
  background?: string;
  fontWeight?: string;
} {
  const out: { fontSize?: string; background?: string; fontWeight?: string } = {};
  let bg: string | undefined;
  let alpha = 0.2;
  let hasBg = false;
  for (const part of raw.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    const k = part.slice(0, eq).trim();
    const v = part.slice(eq + 1).trim();
    if (k === "b") {
      if (v === "1") out.fontWeight = "bold";
    } else if (k === "s") {
      const n = Number(v);
      if (Number.isFinite(n)) out.fontSize = `${Math.min(3, Math.max(1, n))}em`;
    } else if (k === "bg") {
      bg = v;
      hasBg = true;
    } else if (k === "a") {
      const n = Number(v);
      if (Number.isFinite(n)) alpha = Math.min(1, Math.max(0, n));
    }
  }
  if (hasBg && bg) out.background = hexAlphaToRgba(bg, alpha);
  return out;
}

function hexAlphaToRgba(hex: string, alpha: number): string {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(hex.trim());
  if (!m) return hex;
  const r = parseInt(m[1].slice(0, 2), 16);
  const g = parseInt(m[1].slice(2, 4), 16);
  const b = parseInt(m[1].slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function ScaledMarkdown({
  source,
  components,
}: {
  source: string;
  components: ReturnType<typeof markdownLinkifyComponents>;
}) {
  const md = (key: string, text: string) => (
    <MarkdownPreview
      key={key}
      source={sessionMarkersToMarkdown(text)}
      wrapperElement={{ "data-color-mode": "dark" }}
      components={components}
      urlTransform={(url) => url}
    />
  );
  if (!STYLE_SENTINEL_RE.test(source)) return md("md", source);

  const nodes: ReactNode[] = [];
  let rest = source;
  let i = 0;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const m = rest.match(STYLE_SENTINEL_RE);
    if (!m || m.index === undefined) {
      if (rest) nodes.push(md(`t${i}`, rest));
      break;
    }
    const before = rest.slice(0, m.index);
    if (before) nodes.push(md(`t${i}`, before));
    nodes.push(
      <div key={`s${i}`} style={parseStyleAttrs(m[1])} className="bc-font-scaled">
        {md(`si${i}`, m[2])}
      </div>,
    );
    rest = rest.slice(m.index + m[0].length);
    i += 1;
  }
  return <>{nodes}</>;
}

/**
 * A collapsible boxed prose container with a "💬 Message" header — gives
 * assistant text the same visual structure as ToolCall blocks. Click the
 * header to collapse/expand. Markdown body is rendered via
 * @uiw/react-markdown-preview which ships its own dark theme + code
 * highlighting + table styling. Memoized on `text` so historical messages
 * don't re-parse markdown on every WS tick from the streaming turn.
 */
const MessageBox = memo(function MessageBox({
  text,
  defaultOpen = true,
  collapsible = true,
  onFileClick,
}: {
  text: string;
  defaultOpen?: boolean;
  collapsible?: boolean;
  onFileClick?: (path: string, focus?: FileFocus) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(defaultOpen);
  const renderedText = decodeEscapedUnicodeForDisplay(text);
  if (isEffectivelyEmpty(renderedText)) return null;
  const preview = firstLineSummary(
    renderedText.replace(STYLE_SENTINEL_STRIP_RE, ""),
  );
  // Pretty-print messages whose entire body is JSON/JSONL (e.g. reviewer
  // verdict payloads) into a fenced code block for the markdown renderer.
  const mdSource = formatWholeJsonMessage(renderedText);
  const mdComponents = markdownLinkifyComponents(onFileClick);
  if (!collapsible) {
    return (
      <div className="message-box message-box-static">
        <div className="message-box-body" data-color-mode="dark">
          <ScaledMarkdown source={mdSource} components={mdComponents} />
        </div>
      </div>
    );
  }
  return (
    <div className={`message-box${open ? " open" : ""}`}>
      <button
        type="button"
        className="message-box-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? t("message.collapseMessageAria") : t("message.expandMessageAria")}
      >
        <span className="collapse-arrow">{open ? "\u25BC" : "\u25B6"}</span>
      </button>
      {open ? (
        <div className="message-box-body" data-color-mode="dark">
          <ScaledMarkdown source={mdSource} components={mdComponents} />
        </div>
      ) : (
        <button
          type="button"
          className="message-box-collapsed-body"
          onClick={() => setOpen(true)}
        >
          {preview}
        </button>
      )}
    </div>
  );
});

interface Props {
  message: ChatMessage;
  sessionId?: string;
  userDisplayName?: string | null;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: () => void;
  threadColorMap?: Map<string, string>;
  defaultCollapsed?: boolean;
  orchestrationMode?: OrchestrationMode;
  /** Backend-owned active runs targeting this specific message
   * (`run.target_message_id === message.id`). Renders one labeled
   * animated badge per entry. */
  runs?: import("../types").RunInfo[];
}

/** Try to extract a human-readable error from raw JSON/text output */
function parseErrorMessage(raw: string): string | null {
  // Try to parse JSON error objects
  const jsonMatch = raw.match(/\{[^{}]*"message"\s*:\s*"([^"]+)"[^{}]*\}/);
  if (jsonMatch) return jsonMatch[1];
  // Detect common error patterns
  if (raw.includes("API Error:")) {
    const after = raw.split("API Error:")[1]?.trim();
    if (after) {
      const msg = after.match(/"message"\s*:\s*"([^"]+)"/);
      return msg ? msg[1] : after.slice(0, 200);
    }
  }
  return null;
}

/** Classify output events for better rendering */
function classifyOutput(text: string): "session" | "error" | "success" | "text" {
  if (/^📋\s*Session started:/.test(text)) return "session";
  if (/^❌/.test(text) || /^Failed to authenticate/i.test(text) || /^API Error/i.test(text)) return "error";
  if (/^✅/.test(text)) return "success";
  return "text";
}

/** Trim backend-side speech-bubble prefix and invisible characters.
 * The backend (provider_bridge._clean) already strips real ANSI escapes
 * before sending events to the WS — we do NOT strip ANSI here, because
 * the lax `\x1b?\[...` pattern used to eat literal text like "[?25h"
 * when it appeared verbatim in a worker's output. */
function cleanOutput(text: string): string {
  const cleaned = text
    // Zero-width chars, BOM, soft hyphen, word joiner — survive .trim().
    .replace(/[\u200B-\u200D\u2060\uFEFF\u00AD]/g, "")
    // nns prefixes assistant text with "💬 " — strip it so the markdown
    // body renders cleanly. Keep ✅ ❌ 📋 since those drive classification.
    .replace(/^\s*\u{1F4AC}\s*/u, "")
    .trim();
  const displayText = decodeEscapedUnicodeForDisplay(cleaned);
  return displayText.replace(/\s/g, "").length === 0 ? "" : displayText;
}

/** Try to parse text as JSON, returning the parsed value or null */
function tryParseJson(text: string): unknown | null {
  const trimmed = text.trim();
  // Must start with { or [ to be JSON
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

/** Detect if text is a tool/terminal result (not prose) */
function isToolResult(text: string): boolean {
  // Starts with Result: or emoji + Result
  if (/^[\p{Emoji_Presentation}\p{Emoji}\uFE0F\u200D]*\s*Result:/u.test(text)) return true;
  // Has numbered lines (e.g. "1→ ..." or "  1\t...")
  if (/^\s*\d+[→\t]/m.test(text)) return true;
  // Has many file paths
  if ((text.match(/\/[\w.-]+\/[\w.-]+/g) || []).length > 3) return true;
  // Has terminal-style output (drwx, -rw-, total N)
  if (/^total \d+|^[d-][rwx-]{9}/m.test(text)) return true;
  return false;
}

/** Format byte count as human-readable */
function fmtSize(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return n + "";
}

/** Collapsible output block with a header summary */
function CollapsibleOutput({ label, children, defaultOpen = false }: {
  label: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="collapsible-output">
      <button className="collapsible-output-header" onClick={() => setOpen(!open)}>
        <span className="collapsible-arrow">{open ? "\u25BC" : "\u25B6"}</span>
        <span className="collapsible-label">{label}</span>
      </button>
      {open && <div className="collapsible-output-body">{children}</div>}
    </div>
  );
}

/** Shared collapse/expand block for timeline entities (workers, sub-agents,
 *  delegates). Shows header with arrow + label, renders last event when
 *  collapsed, and full children when expanded. */
function CollapsibleTimelineBlock({
  anchorId,
  label,
  labelColor,
  chipLabel,
  chipClass,
  events,
  onFileClick,
  onViewDiff,
  defaultOpen = false,
  parentMessageId,
  parentTargetId,
  sessionId,
  created = false,
  modelMeta,
}: {
  anchorId?: string;
  label: string;
  labelColor?: string;
  chipLabel: string;
  chipClass?: string;
  events: WSEvent[];
  onFileClick?: (p: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  defaultOpen?: boolean;
  parentMessageId?: string;
  parentTargetId?: string;
  sessionId?: string;
  created?: boolean;
  modelMeta?: ModelRunMeta;
}) {
  const [openState, setOpenState] = useState({ open: defaultOpen, userToggled: false });
  const open = openState.userToggled ? openState.open : defaultOpen;

  const lastEventPreview = useMemo(() => {
    if (open || events.length === 0) return null;
    return renderLastEventPreview(events, onFileClick, onViewDiff, undefined, sessionId);
  }, [open, events, onFileClick, onViewDiff, sessionId]);

  const filtered = events.filter(
    (e) => !["complete", "session_discovered"].includes(e.type),
  );
  const canExpand = filtered.length > 0;

  return (
    <div
      className={`timeline-block collapsible-timeline-block${created ? " timeline-block-created" : ""}`}
      {...(anchorId ? { id: anchorId } : {})}
    >
      {canExpand ? (
        <button
          className="timeline-entity-header timeline-toggle-header"
          onClick={() => {
            setOpenState((state) => ({
              open: !(state.userToggled ? state.open : defaultOpen),
              userToggled: true,
            }));
          }}
          aria-expanded={open}
        >
          <span className="collapse-arrow">{open ? "\u25BC" : "\u25B6"}</span>
          {chipClass ? <span className={chipClass}>{chipLabel}</span> : null}
          {labelColor && <span className="thread-dot" style={{ background: labelColor }} />}
          <span className="timeline-toggle-label" style={{ color: labelColor }}>{label}</span>
          <RunMetaChips meta={modelMeta} />
          {!open && (
            <span className="sub-agent-collapsed-count">
              {filtered.length} event{filtered.length !== 1 ? "s" : ""}
            </span>
          )}
        </button>
      ) : (
        <div className="timeline-entity-header timeline-static-header">
          <span className="timeline-static-spacer" aria-hidden="true" />
          {chipClass ? <span className={chipClass}>{chipLabel}</span> : null}
          {labelColor && <span className="thread-dot" style={{ background: labelColor }} />}
          <span className="timeline-toggle-label" style={{ color: labelColor }}>{label}</span>
          <RunMetaChips meta={modelMeta} />
        </div>
      )}
      {canExpand && open && (
        <div className="timeline-block-body">
          {renderGroupedEvents(filtered, onFileClick, onViewDiff, parentMessageId, parentTargetId, sessionId)}
        </div>
      )}
      {canExpand && !open && lastEventPreview && (
        <>
          <div className="collapse-ellipsis">{COLLAPSE_ELLIPSIS}</div>
          {lastEventPreview}
        </>
      )}
    </div>
  );
}

/** Render the last renderable event for collapsed preview.
 *  Applies the same pipeline as the expanded view
 *  (flatten → partition → dedup) so the preview matches
 *  what the user sees at the bottom of the expanded timeline. */
const COLLAPSED_PREVIEW_NON_USER_FACING = new Set([
  "complete", "session_discovered",
  "run_state", "command_received", "messages_delta",
  "turn_start", "turn_complete",
  "turn_started", "turn_stopped", "turn_detached",
  "trace_step", "steer_prompt",
  "lifecycle_notice",
  "model_switched",
]);

function renderLastEventPreviewFromLevel(
  events: WSEvent[],
  children: ChildrenMap,
  toolResultById: Map<string, string>,
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  sessionId?: string,
): ReactNode {
  const renderable = events.filter((e) =>
    !COLLAPSED_PREVIEW_NON_USER_FACING.has(e.type) && e.type !== "diagnostic"
  );
  const groups = groupEvents(renderable, toolResultById);
  for (let i = groups.length - 1; i >= 0; i--) {
    const last = groups[i];
    if (last.kind === "tool") {
      const toolUseId = last.event.data.tool_use_id as string | undefined;
      const childEvents = toolUseId ? children.get(toolUseId) : undefined;
      if (childEvents && childEvents.length > 0) {
        const childPreview = renderLastEventPreviewFromLevel(
          childEvents,
          children,
          toolResultById,
          onFileClick,
          onViewDiff,
          sessionId,
        );
        if (childPreview) return childPreview;
      }
      return wrapWithTs(
        <Suspense fallback={null}>
          <ToolCall
            tool={last.event.data.tool as string}
            args={last.event.data.args as string | Record<string, unknown> | null | undefined}
            result={last.result}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
          />
        </Suspense>,
        "last-event",
        last.event._ts,
      );
    }
    const node = renderSingleEvent(last.event, 0, onFileClick, onViewDiff, false, sessionId);
    if (node) return wrapWithTs(node, "last-event", last.event._ts);
  }
  return null;
}

function renderLastEventPreview(
  events: WSEvent[],
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  outerToolResultById?: Map<string, string>,
  sessionId?: string,
): ReactNode {
  const { flat, toolResultById } = flattenClaudeMessages(events);
  // Pre-flattened child streams (SubAgentBlock) have no tool_result
  // carriers of their own — their results live in the caller's map.
  if (outerToolResultById) {
    for (const [k, v] of outerToolResultById) {
      if (!toolResultById.has(k)) toolResultById.set(k, v);
    }
  }
  // Mirror the expanded view's pipeline: partition by parent, then
  // group (which deduplicates and pairs tool_call + result).
  const { topLevel, children } = partitionEventsByParent(flat);
  return renderLastEventPreviewFromLevel(
    topLevel,
    children,
    toolResultById,
    onFileClick,
    onViewDiff,
    sessionId,
  );
}

function OutputEvent({
  text,
  nested = false,
  collapsible = true,
  onFileClick,
}: {
  text: string;
  nested?: boolean;
  collapsible?: boolean;
  onFileClick?: (path: string, focus?: FileFocus) => void;
}) {
  const clean = cleanOutput(text);
  if (!clean) return null;

  const kind = classifyOutput(clean);

  if (kind === "session") return null;

  if (kind === "error") {
    const friendlyMsg = parseErrorMessage(clean);
    const mdComponents = markdownLinkifyComponents(onFileClick);
    return (
      <div className="event-error">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={mdComponents}
          urlTransform={(url) => url}
        >
          {sessionMarkersToMarkdown(friendlyMsg || clean)}
        </ReactMarkdown>
      </div>
    );
  }

  if (kind === "success") {
    return <div className="event-success">{linkifyFilePaths(clean, onFileClick)}</div>;
  }

  // Try JSON parsing FIRST so a stringified JSON object doesn't get
  // wrapped in a Message box.
  const jsonVal = tryParseJson(clean);
  if (jsonVal !== null) {
    return (
      <CollapsibleOutput label={`JSON (${fmtSize(clean.length)} chars)`}>
        <div className="raw-json output-json">
          <JsonNode value={jsonVal} />
        </div>
      </CollapsibleOutput>
    );
  }

  // Tool/terminal result — collapsible pre block (not prose).
  // Only apply this heuristic to events nested under a tool call; top-level
  // manager `output` events are the assistant's reply by construction, and
  // the path-counting rule in `isToolResult` produces false positives on
  // prose that happens to mention several file paths.
  if (nested && isToolResult(clean)) {
    const firstLine = clean.split("\n")[0].slice(0, 80);
    const label = clean.length >= 1000
      ? `${firstLine}... (${fmtSize(clean.length)} chars)`
      : firstLine;
    return (
      <CollapsibleOutput label={label}>
        <pre className="output-pre">{linkifyFilePaths(clean, onFileClick)}</pre>
      </CollapsibleOutput>
    );
  }

  // Everything else is assistant prose — render in a labeled MessageBox
  // with the proper markdown viewer widget.
  return <MessageBox text={clean} collapsible={collapsible} onFileClick={onFileClick} />;
}

function fmt(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return n.toString();
}

function TodosSnapshotEvent({ todos }: { todos: TodoItem[] }) {
  if (!todos || todos.length === 0) return null;
  return (
    <div className="todos-snapshot">
      {todos.map((t, i) => (
        <TodoItemRow key={i} item={t} />
      ))}
    </div>
  );
}

function CompleteEvent({ data }: { data: Record<string, unknown> }) {
  const success = data.success as boolean;
  const error = data.error as string | undefined;
  const tu = data.token_usage as Record<string, number> | undefined;
  const rateLimited = data.rate_limited_until as string | undefined;

  if (success && tu) {
    const inTok = tu.input_tokens || 0;
    const outTok = tu.output_tokens || 0;
    const cacheRead = tu.cache_read_input_tokens || 0;
    if (inTok + outTok === 0) return null;
    return (
      <div className="event-complete-success">
        <span>In: {fmt(inTok)}</span>
        <span className="token-sep">/</span>
        <span>Out: {fmt(outTok)}</span>
        {cacheRead > 0 && (
          <>
            <span className="token-sep">/</span>
            <span>Cache: {fmt(cacheRead)}</span>
          </>
        )}
      </div>
    );
  }

  if (success) return null;

  return (
    <div className="event-complete-error">
      <div className="error-title">Request failed</div>
      {error && <div className="error-detail">{error}</div>}
      {rateLimited && (
        <div className="error-detail">
          Rate limited until {new Date(rateLimited).toLocaleTimeString()}
        </div>
      )}
    </div>
  );
}

function ModelSwitchedEvent({ data }: { data: Record<string, unknown> }) {
  const model = typeof data.model === "string" ? data.model : "";
  const providerId = typeof data.provider_id === "string" ? data.provider_id : "";
  const providerName = typeof data.provider_name === "string" ? data.provider_name : "";
  const providerKind = typeof data.provider_kind === "string" ? data.provider_kind : "";
  const previousModel = typeof data.previous_model === "string" ? data.previous_model : "";
  const previousProviderId = typeof data.previous_provider_id === "string" ? data.previous_provider_id : "";
  const previousProviderName = typeof data.previous_provider_name === "string" ? data.previous_provider_name : "";
  const previousProviderKind = typeof data.previous_provider_kind === "string" ? data.previous_provider_kind : "";
  const reasoningEffort = typeof data.reasoning_effort === "string" ? data.reasoning_effort : "";
  const previousReasoningEffort = typeof data.previous_reasoning_effort === "string" ? data.previous_reasoning_effort : "";
  const changed = Array.isArray(data.changed) ? data.changed : [];
  const hasModelChange = changed.includes("model") || changed.includes("provider_id");
  const hasReasoningChange = changed.includes("reasoning_effort");
  if (!model && !providerId && !reasoningEffort) return null;
  const fromProvider = previousProviderName || previousProviderKind || previousProviderId;
  const toProvider = providerName || providerKind || providerId;
  const includeEffort = hasReasoningChange || Boolean(reasoningEffort || previousReasoningEffort);
  const from = [fromProvider, previousModel, includeEffort ? previousReasoningEffort : ""].filter(Boolean).join(" / ");
  const to = [toProvider, model, includeEffort ? reasoningEffort : ""].filter(Boolean).join(" / ");
  const reasoning = previousReasoningEffort && reasoningEffort
    ? `${previousReasoningEffort} to ${reasoningEffort}`
    : reasoningEffort;
  const label = hasModelChange || !hasReasoningChange ? "Model switched" : "Reasoning changed";
  return (
    <div className="event-model-switched">
      <span>{label}</span>
      {hasModelChange || !hasReasoningChange ? (
        <span>{from && to ? `${from} to ${to}` : to}</span>
      ) : (
        <span>{reasoning}</span>
      )}
    </div>
  );
}

function isModelSwitchedEvent(event: WSEvent): boolean {
  return event.type === "model_switched";
}

function ModelSwitchBoundaryEvents({
  events,
  testId,
}: {
  events: WSEvent[];
  testId: string;
}) {
  if (events.length === 0) return null;
  return (
    <div className="model-switch-boundary-events" data-testid={testId}>
      {events.map((event, idx) => (
        <ModelSwitchedEvent key={(event.data?.uuid as string | undefined) ?? idx} data={event.data ?? {}} />
      ))}
    </div>
  );
}

function ModelFallbackEvent({ data }: { data: Record<string, unknown> }) {
  const { t } = useTranslation();
  const fromModel = typeof data.from_model === "string" ? data.from_model : "";
  const toModel = typeof data.to_model === "string" ? data.to_model : "";
  if (!fromModel && !toModel) return null;
  return (
    <div className="event-model-switched">
      <span>{t("message.modelFallback")}</span>
      <span>
        {fromModel && toModel
          ? t("message.modelFallbackFromTo", { from: fromModel, to: toModel })
          : fromModel || toModel}
      </span>
    </div>
  );
}

/** Normalize text for dedup comparison (strip leading emoji/whitespace) */
function normalizeForDedup(text: string): string {
  return text.replace(/^[\p{Emoji_Presentation}\p{Emoji}\uFE0F\u200D]+\s*/u, "").trim();
}

/**
 * Pre-process events: pair tool_call with following output, and deduplicate
 * output/thinking events that share the same text (CLI often emits both).
 *
 * `toolResultById` (optional): a map from `tool_use_id` to the tool's
 * rendered result text. Produced by `flattenClaudeMessages` when the
 * upstream event stream is claude's native shape — where tool_results
 * live in a separate `user` message and aren't adjacent to the matching
 * `tool_use`. We look up by id FIRST, and fall back to the legacy
 * "next event is an output" pairing for pre-refactor persisted sessions.
 */
const TODO_TOOLS = new Set(["TodoWrite", "TaskCreate", "TaskUpdate"]);
const STANDALONE_TOOL_CALLS = new Set(["WebSearch"]);
// An action group this large is a burst — collapse it by default into a
// single "N actions" header instead of rendering every tool card. Smaller
// groups stay open so normal multi-step turns read as before.
const AUTO_ACTION_OPEN_MAX = 3;

function isTodoToolCall(ev: WSEvent): boolean {
  return ev.type === "tool_call" && TODO_TOOLS.has(ev.data?.tool as string);
}

function isStandaloneToolCall(ev: WSEvent): boolean {
  return ev.type === "tool_call" && STANDALONE_TOOL_CALLS.has(ev.data?.tool as string);
}

function todosKey(todos: unknown): string | null {
  if (!Array.isArray(todos)) return null;
  return JSON.stringify(todos.map((todo) => {
    if (!todo || typeof todo !== "object") return todo;
    const item = todo as Record<string, unknown>;
    return {
      content: item.content ?? "",
      status: item.status ?? "pending",
      activeForm: item.activeForm ?? null,
      source_id: item.source_id ?? null,
    };
  }));
}

function groupEvents(
  events: WSEvent[],
  toolResultById?: Map<string, string>,
): Array<
  | { kind: "tool"; idx: number; event: WSEvent; result?: string }
  | { kind: "event"; idx: number; event: WSEvent }
> {
  const groups: ReturnType<typeof groupEvents> = [];
  const seenTexts = new Set<string>();
  let i = 0;

  // When toolResultById has entries, events are in native Claude SDK
  // shape (tool_results in user messages). In this mode the positional
  // "next is output" fallback must NEVER fire — the next output after a
  // tool_call is assistant text, not a tool result. The fallback exists
  // only for legacy pre-refactor sessions that lacked tool_result blocks.
  const hasNativeResults = !!toolResultById && toolResultById.size > 0;

  // Track a run of consecutive todo tool_calls. When a non-todo event
  // breaks the run, flush the accumulated run as a single synthetic
  // todos_snapshot event. The last snapshot in the run carries the
  // final todo state (args contain the full list for TodoWrite, or the
  // individual item for TaskCreate/TaskUpdate).
  let todoRunStart = -1;
  let lastTodoArgs: Record<string, unknown> | null = null;
  let lastRenderedTodosKey: string | null = null;

  function pushTodosSnapshot(idx: number, event: WSEvent): void {
    const key = todosKey(event.data?.todos);
    if (key && key === lastRenderedTodosKey) return;
    groups.push({ kind: "event", idx, event });
    lastRenderedTodosKey = key;
  }

  function flushTodoRun() {
    if (todoRunStart === -1 || !lastTodoArgs) return;
    // For TodoWrite, args.todos is the full list. For TaskCreate/TaskUpdate,
    // we don't have the compiled list here — the todos_snapshot event
    // (injected by the backend) carries the compiled state. Fall back to
    // showing what we have.
    const todos = lastTodoArgs.todos as Array<Record<string, unknown>> | undefined;
    if (todos && Array.isArray(todos)) {
      // idx is used as a React key downstream — todoRunStart is unique
      // (the consumed todo tool_calls never pushed their own groups).
      const event = { type: "todos_snapshot", data: { todos } } as WSEvent;
      pushTodosSnapshot(todoRunStart, event);
    }
    todoRunStart = -1;
    lastTodoArgs = null;
  }

  while (i < events.length) {
    const ev = events[i];

    if (ev.type === "todos_snapshot") {
      const pendingKey = lastTodoArgs ? todosKey(lastTodoArgs.todos) : null;
      if (todoRunStart !== -1 && pendingKey && pendingKey === todosKey(ev.data?.todos)) {
        todoRunStart = -1;
        lastTodoArgs = null;
        pushTodosSnapshot(i, ev);
      } else {
        flushTodoRun();
        pushTodosSnapshot(i, ev);
      }
      i++;
      continue;
    }

    if (isTodoToolCall(ev)) {
      if (todoRunStart === -1) todoRunStart = i;
      lastTodoArgs = (ev.data?.args as Record<string, unknown>) ?? null;
      // Skip the tool_call + its paired result
      const tuid = ev.data?.tool_use_id as string | undefined;
      if (tuid && toolResultById?.has(tuid)) {
        i++;
      } else if (!hasNativeResults && i + 1 < events.length && events[i + 1].type === "output") {
        i += 2;
      } else {
        i++;
      }
      continue;
    }

    // Non-todo event — flush pending todo run before processing.
    flushTodoRun();

    if (ev.type === "tool_result" && ev.data?.paired_tool_result) {
      i++;
      continue;
    }

    if (ev.type === "tool_call") {
      // Prefer the id-based lookup (native claude shape); fall back to
      // the positional "next is output" pairing (legacy translator shape).
      let result: string | undefined;
      const tuid = ev.data?.tool_use_id as string | undefined;
      // idx is used as a React key downstream — stamp the group with the
      // tool_call's own index, not the post-consumption cursor (which
      // equals the NEXT group's index and collides).
      const startIdx = i;
      if (tuid && toolResultById?.has(tuid)) {
        result = toolResultById.get(tuid);
        i++;
      } else if (
        !hasNativeResults &&
        !isStandaloneToolCall(ev) &&
        i + 1 < events.length &&
        events[i + 1].type === "output"
      ) {
        result = events[i + 1].data.output as string;
        i += 2; // skip both
      } else {
        i++;
      }
      groups.push({ kind: "tool", idx: startIdx, event: ev, result });
    } else {
      // Deduplicate output/thinking events with identical text
      if (ev.type === "output" || ev.type === "thinking" || ev.type === "tool_result") {
        const raw = (
          ev.type === "output"
            ? ev.data.output
            : ev.type === "thinking"
              ? ev.data.thought
              : ev.data.output
        ) as string;
        const normalized = normalizeForDedup(raw || "");
        if (normalized && seenTexts.has(normalized)) {
          i++;
          continue; // skip duplicate
        }
        if (normalized) seenTexts.add(normalized);
      }
      groups.push({ kind: "event", idx: i, event: ev });
      i++;
    }
  }
  // Flush any trailing todo run.
  flushTodoRun();
  return groups;
}

type EventRenderGroup = ReturnType<typeof groupEvents>[number];
type EventRenderGroups = ReturnType<typeof groupEvents>;

function isActionLeadGroup(group: EventRenderGroup): boolean {
  return group.kind === "event" && (
    group.event.type === "output" ||
    group.event.type === "thinking"
  );
}

// Text of an output/thinking lead, or "" for anything else.
function leadText(group: EventRenderGroup): string {
  if (group.kind !== "event") return "";
  const ev = group.event;
  if (ev.type === "output") return (ev.data.output as string) || "";
  if (ev.type === "thinking") return (ev.data.thought as string) || "";
  return "";
}

// A lead with no visible headline text. These must not spawn their own
// "no text" action group — their actions merge into the running group.
function isHeadlessLead(group: EventRenderGroup): boolean {
  return isActionLeadGroup(group) && !leadText(group).trim();
}

function isAutoGroupedAction(group: EventRenderGroup): boolean {
  return group.kind === "tool";
}

// A tool group that dispatched a sub-agent (has child events). These render as
// SubAgentBlock — already self-contained containers — so they must never be
// wrapped inside an AutoActionGroup.
function isSubAgentAction(group: EventRenderGroup, childrenMap: ChildrenMap): boolean {
  if (group.kind !== "tool") return false;
  const toolUseId = group.event.data.tool_use_id as string | undefined;
  const children = toolUseId ? childrenMap.get(toolUseId) : undefined;
  return !!children && children.length > 0;
}

function hasLaterActionLead(groups: EventRenderGroups, startIdx: number): boolean {
  for (let i = startIdx; i < groups.length; i++) {
    if (isActionLeadGroup(groups[i])) return true;
  }
  return false;
}

/**
 * Partition a flat event stream into top-level events and a map of
 * `tool_use_id -> child events`. Sub-agent activity (Task tool, Skill,
 * etc.) carries `parent_tool_use_id` matching the dispatching tool's
 * `tool_use_id`, so we can render the children nested under their parent.
 */
type ChildrenMap = Map<string, WSEvent[]>;

function partitionEventsByParent(events: WSEvent[]): {
  topLevel: WSEvent[];
  children: ChildrenMap;
} {
  const topLevel: WSEvent[] = [];
  const children: ChildrenMap = new Map();
  // First pass: collect tool_use_ids that exist in this stream so we only
  // nest under known parents (avoids dangling stale parent_tool_use_id from
  // resumed sessions).
  const knownToolUseIds = new Set<string>();
  for (const e of events) {
    if (e.type === "tool_call") {
      const tuid = (e.data?.tool_use_id as string | undefined) || undefined;
      if (tuid) knownToolUseIds.add(tuid);
    }
  }
  for (const e of events) {
    const parentId = (e.data?.parent_tool_use_id as string | null | undefined) || null;
    if (parentId && knownToolUseIds.has(parentId)) {
      const arr = children.get(parentId);
      if (arr) arr.push(e);
      else children.set(parentId, [e]);
    } else {
      topLevel.push(e);
    }
  }
  return { topLevel, children };
}

function renderSingleEvent(
  event: WSEvent,
  idx: number,
  onFileClick?: (p: string, focus?: FileFocus) => void,
  _onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  nested: boolean = false,
  sessionId?: string,
  collapsibleProse: boolean = true,
) {
  switch (event.type) {
    case "thinking": {
      const thought = event.data.thought as string;
      // No error sniffing here — real errors arrive via `case "error"`
      // (orchestrator/provider) or via assistant_msg.error/errorText
      // (`.message-status.status-error` chrome). Pattern-matching on
      // thinking prose only ever false-positived on text that mentioned
      // error keywords.
      const looksLikeThinking = thought.length > 200 || /\b(let me|I need to|I should|I'll|thinking|consider|analyzing)\b/i.test(thought);
      if (!looksLikeThinking) {
        return <OutputEvent key={idx} text={thought} nested={nested} collapsible={collapsibleProse} onFileClick={onFileClick} />;
      }
      return <ThinkingBlock key={idx} thought={thought} onFileClick={onFileClick} />;
    }
    case "output":
      return <OutputEvent key={idx} text={event.data.output as string} nested={nested} collapsible={collapsibleProse} onFileClick={onFileClick} />;
    case "tool_result":
      return <OutputEvent key={idx} text={event.data.output as string} nested={nested} collapsible={collapsibleProse} onFileClick={onFileClick} />;
    case "steer_prompt":
      return (
        <SteerPromptEvent
          key={idx}
          prompt={String(event.data.prompt ?? "")}
          images={event.data.images as ChatMessage["images"] | undefined}
          sessionId={sessionId}
          onFileClick={onFileClick}
        />
      );
    case "session_discovered":
      return null;
    // Internal lifecycle events — not rendered.
    case "turn_start":
    case "turn_complete":
    case "turn_started":
    case "turn_stopped":
    case "turn_detached":
    case "trace_step":
    case "run_state":
    case "messages_delta":
    case "command_received":
    // User-message lifecycle observability — status is shown on the
    // user message bubble via MessageStatus, not as assistant content.
    case "user_message_queued":
    case "user_message_sent":
    case "user_message_received":
    case "user_message_done":
    case "user_message_failed":
      return null;
    case "complete":
      return <CompleteEvent key={idx} data={event.data} />;
    case "model_switched":
      return <ModelSwitchedEvent key={idx} data={event.data ?? {}} />;
    case "model_fallback":
      return <ModelFallbackEvent key={idx} data={event.data ?? {}} />;
    case "lifecycle_notice":
      return <LifecycleNotice key={idx} data={event.data ?? {}} />;
    case "pr_link":
      return (
        <PrLinkEvent
          key={idx}
          prNumber={event.data?.prNumber as number | undefined}
          prUrl={event.data?.prUrl as string | undefined}
          prRepository={event.data?.prRepository as string | undefined}
        />
      );
    case "todos_snapshot":
      return <TodosSnapshotEvent key={idx} todos={event.data.todos as TodoItem[]} />;
    case "worker_event": {
      const inner = (event.data as { event?: WSEvent } | undefined)?.event;
      if (!inner) return null;
      return (
        <Fragment key={idx}>
          {renderGroupedEvents([inner], onFileClick, _onViewDiff, undefined, undefined, sessionId)}
        </Fragment>
      );
    }
    case "error":
      return (
        <div key={idx} className="event-error">
          {linkifyFilePaths(event.data.error as string, onFileClick)}
        </div>
      );
    case "diagnostic":
      return (
        <DiagnosticEvent
          key={idx}
          kind={(event.data?.kind as string) || "unknown"}
          raw={event.data?.raw}
        />
      );
    default:
      // Unknown top-level event type — render rather than silently
      // drop so future provider events (gemini stream-json types,
      // claude jsonl additions) stay visible.
      return (
        <DiagnosticEvent
          key={idx}
          kind={`event.${event.type || "(none)"}`}
          raw={event.data}
        />
      );
  }
}

function SteerPromptEvent({
  prompt,
  images,
  sessionId,
  onFileClick,
}: {
  prompt: string;
  images?: ChatMessage["images"];
  sessionId?: string;
  onFileClick?: (path: string, focus?: FileFocus) => void;
}) {
  const hasArtificial = hasArtificialSections(prompt);
  return (
    <div className="event-steer-prompt">
      <span className="event-steer-label">Steer</span>
      <span className="event-steer-text">
        <UserImages images={images} sessionId={sessionId} />
        {hasArtificial ? (
          <UserContentSegments
            segments={parseArtificialSections(prompt)}
            onFileClick={onFileClick}
          />
        ) : (
          linkifyFilePaths(prompt, onFileClick)
        )}
      </span>
    </div>
  );
}

function PrLinkEvent({
  prNumber,
  prUrl,
  prRepository,
}: {
  prNumber?: number;
  prUrl?: string;
  prRepository?: string;
}) {
  if (!prUrl) return null;
  const label = prNumber ? `Pull request #${prNumber}` : "Pull request";
  return (
    <a
      className="event-pr-link"
      href={prUrl}
      target="_blank"
      rel="noopener noreferrer"
      title={prUrl}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 16 16"
        fill="currentColor"
        aria-hidden="true"
        style={{ flexShrink: 0 }}
      >
        <path d="M3.25 1A2.25 2.25 0 0 0 2.5 5.372V10.628a2.25 2.25 0 1 0 1.5 0V5.372A2.25 2.25 0 0 0 3.25 1Zm0 1.5a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5Zm0 9.25a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5ZM12.75 3a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm-2.25.75a2.25 2.25 0 1 1 3 2.122v4.756a2.25 2.25 0 1 1-1.5 0V5.872A2.25 2.25 0 0 1 10.5 3.75Zm2.25 8a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
      </svg>
      <span className="event-pr-link-label">{label}</span>
      {prRepository && (
        <span className="event-pr-link-repo">{prRepository}</span>
      )}
    </a>
  );
}

function DiagnosticEvent({ kind, raw }: { kind: string; raw: unknown }) {
  const [open, setOpen] = useState(false);
  let pretty: string;
  try {
    pretty = JSON.stringify(raw, null, 2);
  } catch {
    pretty = String(raw);
  }
  return (
    <div className="event-diagnostic" style={{
      fontSize: "0.8em",
      opacity: 0.7,
      border: "1px dashed currentColor",
      padding: "4px 8px",
      margin: "4px 0",
      borderRadius: 4,
    }}>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: 0,
          color: "inherit",
          font: "inherit",
        }}
      >
        {open ? <Icon name="chevron-down" size={12} /> : <Icon name="chevron-right" size={12} />} unknown event: <code>{kind}</code>
      </button>
      {open && (
        <pre style={{
          marginTop: 4,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: 200,
          overflow: "auto",
        }}>{pretty}</pre>
      )}
    </div>
  );
}

function LifecycleNotice({ data }: { data: Record<string, unknown> }) {
  const message = String(data.message ?? "");
  if (!message) return null;
  const replacementHistory = Array.isArray(data.replacement_history)
    ? data.replacement_history
    : [];
  const replacementText = replacementHistory
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const row = item as { role?: unknown; text?: unknown };
      const role = typeof row.role === "string" ? row.role : "message";
      const text = typeof row.text === "string" ? row.text : "";
      return text ? `${role}:\n${text}` : "";
    })
    .filter(Boolean)
    .join("\n\n---\n\n");
  return (
    <div>
      <div style={{
        fontSize: "0.8em",
        opacity: 0.72,
        padding: "3px 0",
        margin: "2px 0",
        fontStyle: "italic",
      }}>
        {message}
      </div>
      {replacementText ? (
        <CollapsibleOutput label="Compacted prompt" defaultOpen={false}>
          <pre className="tool-result-pre">{replacementText}</pre>
        </CollapsibleOutput>
      ) : null}
    </div>
  );
}

/**
 * A tool call whose `tool_use_id` has children — i.e. the tool dispatched
 * a sub-agent (Task, Skill, custom subagent_type) and its inner activity
 * is streamed under it. We render the tool header as usual and indent the
 * children below it with the same renderGroupedEvents pipeline so they
 * look identical to the main session view.
 */
function SubAgentBlock({
  toolEvent,
  result,
  childEvents,
  childrenMap,
  toolResultById,
  onFileClick,
  onViewDiff,
  parentMessageId,
  parentTargetId,
  sessionId,
  defaultOpen,
}: {
  toolEvent: WSEvent;
  result?: string;
  childEvents: WSEvent[];
  childrenMap: ChildrenMap;
  toolResultById?: Map<string, string>;
  onFileClick?: (p: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  parentMessageId?: string;
  parentTargetId?: string;
  sessionId?: string;
  defaultOpen: boolean;
}) {
  const [openState, setOpenState] = useState({ open: defaultOpen, userToggled: false });
  const open = openState.userToggled ? openState.open : defaultOpen;
  const childCount = childEvents.length;

  const lastEventPreview = useMemo(() => {
    if (open || childCount === 0) return null;
    return renderLastEventPreview(childEvents, onFileClick, onViewDiff, toolResultById, sessionId);
  }, [open, childCount, childEvents, onFileClick, onViewDiff, toolResultById, sessionId]);

  return (
    <div className="sub-agent-block">
      <div
        className="sub-agent-header"
        role="button"
        tabIndex={0}
        onClick={() => {
          setOpenState((state) => ({
            open: !(state.userToggled ? state.open : defaultOpen),
            userToggled: true,
          }));
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpenState((state) => ({
              open: !(state.userToggled ? state.open : defaultOpen),
              userToggled: true,
            }));
          }
        }}
        aria-expanded={open}
      >
        <span className="collapse-arrow">{open ? "\u25BC" : "\u25B6"}</span>
        <Suspense fallback={null}>
          <ToolCall
            tool={toolEvent.data.tool as string}
            args={toolEvent.data.args as string | Record<string, unknown> | null | undefined}
            result={result}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
          />
        </Suspense>
        {!open && (
          <span className="sub-agent-collapsed-count">
            {childCount} event{childCount !== 1 ? "s" : ""}
          </span>
        )}
      </div>
      {open && (
        <div className="sub-agent-children">
          {renderTreeLevel(childEvents, childrenMap, onFileClick, onViewDiff, true, toolResultById, parentMessageId, parentTargetId)}
        </div>
      )}
      {!open && lastEventPreview && (
        <>
          <div className="collapse-ellipsis">{COLLAPSE_ELLIPSIS}</div>
          {lastEventPreview}
        </>
      )}
    </div>
  );
}

function jumpToParentEl(el: HTMLElement) {
  const container = el.closest(".chat-messages, .supervisor-timeline") as HTMLElement | null;
  if (container) {
    const elBox = el.getBoundingClientRect();
    const containerBox = container.getBoundingClientRect();
    const scrollOffset = container.scrollTop + elBox.top - containerBox.top - 20;
    container.scrollTo({ top: scrollOffset, behavior: "smooth" });
  }
  el.classList.add("highlight-flash");
  setTimeout(() => el.classList.remove("highlight-flash"), 1500);
}

function wrapWithTs(
  node: ReactNode,
  key: string,
  _ts?: string,
  opts?: { targetId?: string; parentId?: string },
): ReactNode {
  if (node === null || node === undefined) return null;
  const ts = fmtTime(_ts);
  const { targetId, parentId } = opts ?? {};
  if (!ts && !targetId && !parentId)
    return <Fragment key={key}>{node}</Fragment>;
  return (
    <div
      key={key}
      className="timeline-event-row"
      {...(targetId ? { id: targetId } : {})}
    >
      {ts && <span className="timeline-event-time">{ts}</span>}
      <div className="timeline-event-content">{node}</div>
      {parentId && (
        <button
          type="button"
          className="jump-to-parent-btn"
          title="Jump to parent"
          onClick={() => {
            const el = document.getElementById(parentId);
            if (el) jumpToParentEl(el);
          }}
        >
          <span className="collapse-arrow"><Icon name="chevron-up" size={12} /></span>
        </button>
      )}
    </div>
  );
}

function AutoActionGroup({
  lead,
  actions,
  childrenMap,
  toolResultById,
  defaultOpen,
  onFileClick,
  onViewDiff,
  nested,
  parentMessageId,
  sessionId,
}: {
  lead: EventRenderGroup;
  actions: EventRenderGroups;
  childrenMap: ChildrenMap;
  toolResultById?: Map<string, string>;
  defaultOpen: boolean;
  onFileClick?: (p: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  nested: boolean;
  parentMessageId?: string;
  sessionId?: string;
}) {
  const [openState, setOpenState] = useState({ open: defaultOpen, userToggled: false });
  const [bodyMounted, setBodyMounted] = useState(defaultOpen);
  const open = openState.userToggled ? openState.open : defaultOpen;
  const leadTargetId = `action-lead-${lead.idx}`;
  const time = fmtTime(lead.event._ts);

  useEffect(() => {
    if (open) setBodyMounted(true);
  }, [open]);

  const count = actions.length;

  return (
    <div className={`auto-action-group${open ? " open" : ""}`} data-testid="auto-action-group">
      <div
        className="auto-action-group-header"
        role="button"
        tabIndex={0}
        onClick={() => {
          setOpenState((state) => ({
            open: !(state.userToggled ? state.open : defaultOpen),
            userToggled: true,
          }));
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          setOpenState((state) => ({
            open: !(state.userToggled ? state.open : defaultOpen),
            userToggled: true,
          }));
        }}
        aria-expanded={open}
      >
        <span className="collapse-arrow">{"\u25B6"}</span>
        <div className="auto-action-group-lead" id={leadTargetId}>
          {renderSingleEvent(lead.event, lead.idx, onFileClick, onViewDiff, nested, sessionId, false)}
        </div>
        <span className="auto-action-group-meta">
          <span className="auto-action-group-count">
            {count} action{count !== 1 ? "s" : ""}
          </span>
          {time && <span className="auto-action-group-time">{time}</span>}
        </span>
      </div>
      {bodyMounted ? (
        <div
          className="auto-action-group-body-shell"
          aria-hidden={!open}
          onTransitionEnd={(event) => {
            if (event.target !== event.currentTarget || open) return;
            setBodyMounted(false);
          }}
        >
          <div className="auto-action-group-body">
            {renderTreeEntries(
              actions,
              childrenMap,
              onFileClick,
              onViewDiff,
              nested,
              toolResultById,
              parentMessageId,
              leadTargetId,
              sessionId,
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function renderTreeEntry(
  g: EventRenderGroup,
  childrenMap: ChildrenMap,
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  nested: boolean = false,
  toolResultById?: Map<string, string>,
  parentMessageId?: string,
  parentTargetId?: string,
  sessionId?: string,
) {
  let node: ReactNode;
  if (g.kind === "tool") {
    const toolUseId = g.event.data.tool_use_id as string | undefined;
    const children = toolUseId ? childrenMap.get(toolUseId) : undefined;
    if (children && children.length > 0) {
      node = (
        <SubAgentBlock
          toolEvent={g.event}
          result={g.result}
          childEvents={children}
          childrenMap={childrenMap}
          toolResultById={toolResultById}
          onFileClick={onFileClick}
          onViewDiff={onViewDiff}
          parentMessageId={parentMessageId}
          parentTargetId={parentTargetId}
          sessionId={sessionId}
          defaultOpen={g.result === undefined}
        />
      );
    } else {
      node = (
        <Suspense fallback={null}>
          <ToolCall
            tool={g.event.data.tool as string}
            args={g.event.data.args as string | Record<string, unknown> | null | undefined}
            result={g.result}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
          />
        </Suspense>
      );
    }
  } else {
    node = renderSingleEvent(g.event, g.idx, onFileClick, onViewDiff, nested, sessionId);
  }
  const tuid = g.kind === "tool" ? (g.event.data.tool_use_id as string | undefined) : undefined;
  const ptuid = (g.event.data.parent_tool_use_id as string | null | undefined) || undefined;
  return wrapWithTs(node, `row-${g.idx}`, g.event._ts, {
    targetId: tuid ? `tu-${tuid}` : undefined,
    parentId: ptuid ? `tu-${ptuid}` : parentTargetId ?? (parentMessageId ? `msg-${parentMessageId}` : undefined),
  });
}

function renderTreeEntries(
  groups: EventRenderGroups,
  childrenMap: ChildrenMap,
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  nested: boolean = false,
  toolResultById?: Map<string, string>,
  parentMessageId?: string,
  parentTargetId?: string,
  sessionId?: string,
) {
  return groups.map((g) => renderTreeEntry(
    g,
    childrenMap,
    onFileClick,
    onViewDiff,
    nested,
    toolResultById,
    parentMessageId,
    parentTargetId,
    sessionId,
  ));
}

function renderTreeLevel(
  events: WSEvent[],
  childrenMap: ChildrenMap,
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  nested: boolean = false,
  toolResultById?: Map<string, string>,
  parentMessageId?: string,
  parentTargetId?: string,
  sessionId?: string,
) {
  const groups = groupEvents(events, toolResultById);
  const rows: ReactNode[] = [];
  let i = 0;
  while (i < groups.length) {
    const lead = groups[i];
    if (!isActionLeadGroup(lead)) {
      rows.push(renderTreeEntry(
        lead,
        childrenMap,
        onFileClick,
        onViewDiff,
        nested,
        toolResultById,
        parentMessageId,
        parentTargetId,
        sessionId,
      ));
      i++;
      continue;
    }

    const actions: EventRenderGroups = [];
    let j = i + 1;
    while (j < groups.length) {
      const g = groups[j];
      if (isAutoGroupedAction(g) && !isSubAgentAction(g, childrenMap)) {
        actions.push(g);
        j++;
        continue;
      }
      // A headless lead (empty output/thinking) does not break an already
      // running group; swallow it so its following actions merge into the
      // current group instead of forming a separate "no text" box. Only do
      // this once the group has actions — otherwise a final text message
      // followed by a headless lead would absorb trailing actions and render
      // as a collapsible header instead of a standalone text box.
      if (actions.length > 0 && isHeadlessLead(g)) {
        j++;
        continue;
      }
      break;
    }
    if (actions.length === 0) {
      rows.push(renderTreeEntry(
        lead,
        childrenMap,
        onFileClick,
        onViewDiff,
        nested,
        toolResultById,
        parentMessageId,
        parentTargetId,
        sessionId,
      ));
      i++;
      continue;
    }

    rows.push(
      <AutoActionGroup
        key={`auto-action-${lead.idx}`}
        lead={lead}
        actions={actions}
        childrenMap={childrenMap}
        toolResultById={toolResultById}
        defaultOpen={!hasLaterActionLead(groups, j) && actions.length <= AUTO_ACTION_OPEN_MAX}
        onFileClick={onFileClick}
        onViewDiff={onViewDiff}
        nested={nested}
        parentMessageId={parentMessageId}
        sessionId={sessionId}
      />,
    );
    i = j;
  }
  return rows;
}

function renderGroupedEvents(
  events: WSEvent[],
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  parentMessageId?: string,
  parentTargetId?: string,
  sessionId?: string,
) {
  const { flat, toolResultById } = flattenClaudeMessages(events);
  const { topLevel, children } = partitionEventsByParent(flat);
  return renderTreeLevel(
    topLevel, children, onFileClick, onViewDiff, false, toolResultById, parentMessageId, parentTargetId, sessionId,
  );
}

function workerPanelEvents(worker: WorkerPanel): WSEvent[] {
  return Array.isArray(worker.events) ? worker.events : [];
}

function eventUuid(event: WSEvent): string | undefined {
  const data = event.data as Record<string, unknown> | undefined;
  return typeof data?.uuid === "string" ? data.uuid : undefined;
}

function routeLeakedWorkerEvents(message: ChatMessage): ChatMessage {
  const events = message.events ?? [];
  if (!events.some((e) => e.type === "worker_event" || unwrapTypedAgentMessageEnvelope(e))) return message;

  let workers = message.workers ?? [];
  let workersChanged = false;
  const cleanEvents: WSEvent[] = [];

  const ensureMutableWorkers = () => {
    if (workersChanged) return;
    workers = workers.map((worker) => ({ ...worker, events: workerPanelEvents(worker) }));
    workersChanged = true;
  };

  for (const event of events) {
    const typedEvent = unwrapTypedAgentMessageEnvelope(event);
    const workerEvent = event.type === "worker_event" ? event : unwrapWorkerEventEnvelope(event);
    if (!workerEvent) {
      cleanEvents.push(typedEvent ?? event);
      continue;
    }

    if (workerEvent.type === "worker_start") {
      const data = workerEvent.data as {
        delegation_id?: string;
        worker_session_id?: string | null;
        worker_description?: string;
        panel_kind?: WorkerPanel["panel_kind"];
        started_at?: string;
        insert_at?: number;
        is_new?: boolean;
        instructions_preview?: string;
        orchestration_mode?: OrchestrationMode;
        provider_id?: string | null;
        model?: string | null;
        reasoning_effort?: WorkerPanel["reasoning_effort"];
        run_mode?: string;
      };
      if (!data.delegation_id || workers.some((worker) => worker.delegation_id === data.delegation_id)) {
        continue;
      }
      ensureMutableWorkers();
      workers = [...workers, {
        delegation_id: data.delegation_id,
        worker_session_id: data.worker_session_id ?? "",
        worker_description: data.worker_description ?? "",
        panel_kind: data.panel_kind,
        started_at: data.started_at,
        insert_at: data.insert_at,
        is_new: data.is_new ?? false,
        instructions_preview: data.instructions_preview ?? "",
        orchestration_mode: data.orchestration_mode,
        provider_id: data.provider_id,
        model: data.model,
        reasoning_effort: data.reasoning_effort,
        run_mode: data.run_mode,
        events: [],
      }];
      continue;
    }

    if (workerEvent.type === "worker_complete") {
      const data = workerEvent.data as {
        delegation_id?: string;
        worker_session_id?: string | null;
        jsonl_path?: string | null;
        new_byte_offset?: number;
        token_usage?: WorkerPanel["token_usage"];
        success?: boolean;
        error?: string | null;
        fork_agent_sid?: string | null;
        run_mode?: string;
      };
      if (!data.delegation_id) continue;
      const idx = workers.findIndex((worker) => worker.delegation_id === data.delegation_id);
      if (idx === -1) continue;
      ensureMutableWorkers();
      workers[idx] = {
        ...workers[idx],
        worker_session_id: data.worker_session_id ?? workers[idx].worker_session_id,
        jsonl_path: data.jsonl_path ?? null,
        new_byte_offset: data.new_byte_offset,
        token_usage: data.token_usage,
        success: data.success,
        error: data.error ?? null,
        fork_agent_sid: data.fork_agent_sid ?? workers[idx].fork_agent_sid,
        run_mode: data.run_mode ?? workers[idx].run_mode,
      };
      continue;
    }

    const data = workerEvent.data as { delegation_id?: string; event?: WSEvent };
    if (!data?.delegation_id || !data.event) continue;

    const idx = workers.findIndex((worker) => worker.delegation_id === data.delegation_id);
    if (idx === -1) continue;

    ensureMutableWorkers();
    workers[idx] = {
      ...workers[idx],
      events: (() => {
        const current = workerPanelEvents(workers[idx]);
        const uuid = eventUuid(data.event);
        if (!uuid) return [...current, data.event];
        const existingIdx = current.findIndex((inner) => eventUuid(inner) === uuid);
        if (existingIdx === -1) return [...current, data.event];
        const next = [...current];
        next[existingIdx] = data.event;
        return next;
      })(),
    };
  }

  return {
    ...message,
    events: cleanEvents,
    workers: workersChanged ? workers : message.workers,
  };
}

/** Build a short summary from manager events for the collapsed view */
function buildTurnSummary(managerEvents: WSEvent[], workerCount: number, contentFallback?: string): string {
  // Flatten agent_message events to legacy shape so the tool_call /
  // output / thinking counts and previews work on both pre- and
  // post-refactor persisted sessions.
  const { flat } = flattenClaudeMessages(managerEvents);
  const toolCalls = flat.filter((e) => e.type === "tool_call");
  const outputs = flat.filter((e) => e.type === "output" || e.type === "thinking");

  let lastOutput = "";
  for (let i = outputs.length - 1; i >= 0; i--) {
    const text = (outputs[i].data.output || outputs[i].data.thought || "") as string;
    const clean = cleanOutput(text);
    const kind = classifyOutput(clean);
    if (kind === "text" && clean.length > 10) {
      lastOutput = clean.length > 120 ? clean.slice(0, 120) + "..." : clean;
      break;
    }
  }

  const parts: string[] = [];
  if (workerCount > 0) parts.push(`${workerCount} worker${workerCount > 1 ? "s" : ""}`);
  if (toolCalls.length > 0) parts.push(`${toolCalls.length} tool call${toolCalls.length > 1 ? "s" : ""}`);
  if (lastOutput) parts.push(lastOutput);
  const joined = parts.join(" — ");
  if (joined) return joined;
  if (contentFallback && contentFallback.length > 10) {
    return contentFallback.length > 120 ? contentFallback.slice(0, 120) + "..." : contentFallback;
  }
  return contentFallback ? "Response" : "No output";
}

/** Format a timestamp string. Shows HH:MM:SS for today, MM/DD HH:MM:SS
 *  for older dates. Returns null on falsy input. */
function fmtTime(ts: string | undefined): string | null {
  if (!ts) return null;
  try {
    const d = new Date(ts);
    const now = new Date();
    const isToday =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (isToday) {
      return d.toLocaleTimeString(undefined, { hour12: false });
    }
    const time = d.toLocaleTimeString(undefined, { hour12: false });
    const date = `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
    return `${date} ${time}`;
  } catch {
    return null;
  }
}

/** Label for a turn's primary agent — used by the collapse-toggle header
 * and (manager mode only) the manager-scope chip. Only manager mode has a
 * delegating "Manager"; every other mode — native, and the undefined→native
 * default in getStrategy — IS the assistant itself, so it must not be
 * mislabeled "Manager". */
function primaryEntityLabel(mode?: OrchestrationMode): string {
  return mode === "team" ? "Team" : "";
}

function renderEntityBlock(
  block: EntityBlock,
  colorMap: Map<string, string> | undefined,
  onFileClick: ((p: string, focus?: FileFocus) => void) | undefined,
  onViewDiff: ((path: string, oldStr: string, newStr: string) => void) | undefined,
  key: string,
  /** When true, manager blocks render their events flat without an outer
   * `timeline-block` wrapper or "Manager" header — used when the
   * surrounding AssistantMessage already provides a manager-scope chip,
   * so we don't get a double "Manager > Manager" visual nesting. Worker
   * blocks always render with the wrapper regardless of this flag. */
  flattenManager: boolean = false,
  orchestrationMode?: OrchestrationMode,
  initiatorMessageId?: string,
  sessionId?: string,
  workerDefaultOpenById?: ReadonlyMap<string, boolean>,
): ReactNode {
  const color = colorMap?.get(block.entityId);
  const filteredEvents: WSEvent[] = [];
  block.events.forEach((e, i) => {
    if (["complete", "session_discovered", "worker_start"].includes(e.type)) return;
    filteredEvents.push({ ...e, _ts: block.timestamps[i] });
  });

  if (flattenManager && block.entityType === "manager") {
    return (
      <div className="timeline-block-body" key={key}>
        {renderGroupedEvents(filteredEvents, onFileClick, onViewDiff, initiatorMessageId, undefined, sessionId)}
      </div>
    );
  }

  const isWorker = block.entityType === "worker";
  const chipClass = isWorker
    ? "role-chip role-chip-worker"
    : "role-chip";
  const chipLabel = isWorker ? panelKindLabel(block.panelKind) : primaryEntityLabel(orchestrationMode);

  // Workers use the shared collapsible block; manager stays always-open
  // (its scope chip already wraps from the outer AssistantMessage).
  if (isWorker) {
    const anchorId = `timeline-entity-${block.entityId}`;
    return (
      <CollapsibleTimelineBlock
        key={key}
        anchorId={anchorId}
        label={block.entityLabel}
        labelColor={color}
        chipLabel={chipLabel}
        chipClass={chipClass}
        events={filteredEvents}
        onFileClick={onFileClick}
        onViewDiff={onViewDiff}
        parentMessageId={initiatorMessageId}
        parentTargetId={anchorId}
        sessionId={sessionId}
        created={isCreationPanelKind(block.panelKind)}
        modelMeta={{
          providerId: block.providerId,
          model: block.model,
          reasoningEffort: block.reasoningEffort,
        }}
        defaultOpen={workerDefaultOpenById?.get(block.entityId) ?? false}
      />
    );
  }

  return (
    <div
      className="timeline-block"
      style={color ? { borderInlineStartColor: color } : undefined}
      key={key}
    >
      <div className="timeline-entity-header">
        <span className={chipClass}>{chipLabel}</span>
        {color && <span className="thread-dot" style={{ background: color }} />}
        <span style={{ color }}>{block.entityLabel}</span>
      </div>
      <div className="timeline-block-body">
        {renderGroupedEvents(filteredEvents, onFileClick, onViewDiff, initiatorMessageId, undefined, sessionId)}
      </div>
    </div>
  );
}

function renderTimeline(
  entityBlocks: EntityBlock[] | undefined,
  managerEvents: WSEvent[],
  workers: WorkerPanel[],
  colorMap: Map<string, string> | undefined,
  onFileClick: ((p: string, focus?: FileFocus) => void) | undefined,
  onViewDiff: ((path: string, oldStr: string, newStr: string) => void) | undefined,
  activeWorkerIds: ReadonlySet<string> = EMPTY_ACTIVE_WORKER_IDS,
  flattenManager = false,
  orchestrationMode?: OrchestrationMode,
  initiatorMessageId?: string,
  sessionId?: string,
): ReactNode[] {
  const workerDefaultOpenById = new Map(
    workers.map((worker) => [
      worker.delegation_id,
      workerPanelDefaultOpen(worker, activeWorkerIds),
    ]),
  );
  // Worker preparation events (one-time context-loading run on a fresh
  // worker Better Agent session) are surfaced via worker_prep_* frames. Pull them
  // out of the linear stream and render them in a dedicated collapsible
  // block per worker so the user can audit the prep without it cluttering
  // the manager's own timeline.
  const prepByWorker = new Map<
    string,
    { description: string; events: WSEvent[]; complete: boolean; cancelled: boolean }
  >();
  const cleanManagerEvents: WSEvent[] = [];
  for (const e of managerEvents) {
    if (e.type === "worker_prep_start") {
      const wid = e.data?.worker_agent_session_id as string | undefined;
      if (!wid) continue;
      if (!prepByWorker.has(wid)) {
        prepByWorker.set(wid, {
          description: (e.data?.description as string) || "worker",
          events: [],
          complete: false,
          cancelled: false,
        });
      }
    } else if (e.type === "worker_prep_event") {
      const wid = e.data?.worker_agent_session_id as string | undefined;
      const inner = e.data?.event as WSEvent | undefined;
      if (!wid || !inner) continue;
      const slot = prepByWorker.get(wid) ?? {
        description: "worker",
        events: [],
        complete: false,
        cancelled: false,
      };
      slot.events.push(inner);
      prepByWorker.set(wid, slot);
    } else if (e.type === "worker_prep_complete") {
      const wid = e.data?.worker_agent_session_id as string | undefined;
      const slot = wid ? prepByWorker.get(wid) : undefined;
      if (slot) slot.complete = true;
    } else if (e.type === "worker_prep_cancelled") {
      const wid = e.data?.worker_agent_session_id as string | undefined;
      const slot = wid ? prepByWorker.get(wid) : undefined;
      if (slot) slot.cancelled = true;
    } else {
      cleanManagerEvents.push(e);
    }
  }

  const prepBlocks: ReactNode[] = [];
  for (const [wid, slot] of prepByWorker) {
    const status = slot.cancelled
      ? "cancelled"
      : slot.complete
        ? "ready"
        : "preparing…";
    const label = `Worker preparation — ${slot.description} (${status})`;
    prepBlocks.push(
      <div key={`prep-${wid}`} className="worker-prep-block">
        <CollapsibleOutput label={label} defaultOpen={!slot.complete}>
          {renderGroupedEvents(slot.events, onFileClick, onViewDiff, undefined, undefined, sessionId)}
        </CollapsibleOutput>
      </div>
    );
  }

  if (entityBlocks && entityBlocks.length > 0) {
    return [
      ...prepBlocks,
      ...entityBlocks.map((b, i) =>
        renderEntityBlock(b, colorMap, onFileClick, onViewDiff, `block-${b.entityId}-${i}`, flattenManager, orchestrationMode, initiatorMessageId, sessionId, workerDefaultOpenById)
      ),
    ];
  }
  return [
    ...prepBlocks,
    ...renderManagerStreamLegacy(cleanManagerEvents, workers, colorMap, onFileClick, onViewDiff, workerDefaultOpenById, initiatorMessageId, sessionId),
  ];
}

function StoppedIndicator({
  stoppedAt,
  interrupted,
  onRetry,
}: {
  stoppedAt: string;
  interrupted?: boolean;
  onRetry?: () => void;
}) {
  return (
    <div className="stopped-indicator">
      {interrupted ? "Interrupted" : "Stopped"} at {new Date(stoppedAt).toLocaleTimeString()}
      {onRetry && (
        <button
          className="status-retry-btn"
          onClick={(e) => {
            e.stopPropagation();
            onRetry();
          }}
        >
          Retry
        </button>
      )}
    </div>
  );
}

/**
 * Legacy renderManagerStream — kept for backward compat with persisted
 * sessions that don't have entityBlocks.
 */
function renderManagerStreamLegacy(
  managerEvents: WSEvent[],
  workers: WorkerPanel[],
  colorMap?: Map<string, string>,
  onFileClick?: (p: string, focus?: FileFocus) => void,
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void,
  workerDefaultOpenById: ReadonlyMap<string, boolean> = EMPTY_WORKER_DEFAULT_OPEN,
  initiatorMessageId?: string,
  sessionId?: string,
): ReactNode[] {
  const { flat, toolResultById } = flattenClaudeMessages(managerEvents);
  const { topLevel, children } = partitionEventsByParent(flat);
  if (workers.length === 0) {
    return renderTreeLevel(
      topLevel,
      children,
      onFileClick,
      onViewDiff,
      false,
      toolResultById,
      initiatorMessageId,
      undefined,
      sessionId,
    );
  }
  const groups = groupEvents(topLevel, toolResultById);

  const rendered: ReactNode[] = [];
  const consumedWorkerIds = new Set<string>();
  let delegateIdx = 0;

  groups.forEach((g, i) => {
    if (g.kind === "tool") {
      const tool = g.event.data.tool as string;
      const toolUseId = g.event.data.tool_use_id as string | undefined;

      if (tool === "delegate") {
        const worker = workers[delegateIdx];
        delegateIdx += 1;
        if (worker) {
          consumedWorkerIds.add(worker.delegation_id);
          const color = worker.worker_session_id ? colorMap?.get(worker.worker_session_id) : undefined;
          const anchorId = `timeline-entity-${worker.delegation_id}`;
          const filteredEvents = workerPanelEvents(worker).filter(
            (e) => !["complete", "session_discovered"].includes(e.type)
          );
          const block = (
            <CollapsibleTimelineBlock
              anchorId={anchorId}
              label={worker.worker_description || "worker"}
              labelColor={color}
              chipLabel={panelKindLabel(worker.panel_kind)}
              chipClass="role-chip role-chip-worker"
              events={filteredEvents}
              onFileClick={onFileClick}
              onViewDiff={onViewDiff}
              parentMessageId={initiatorMessageId}
              parentTargetId={anchorId}
              sessionId={sessionId}
              created={isCreationPanelKind(worker.panel_kind)}
              modelMeta={{
                providerId: worker.provider_id,
                model: worker.model,
                reasoningEffort: worker.reasoning_effort,
              }}
              defaultOpen={workerDefaultOpenById.get(worker.delegation_id) ?? false}
            />
          );
          rendered.push(wrapWithTs(block, `delegate-${worker.delegation_id}`, g.event._ts, {
            targetId: toolUseId ? `tu-${toolUseId}` : undefined,
            parentId: initiatorMessageId ? `msg-${initiatorMessageId}` : undefined,
          }));
          return;
        }
      }

      const childEvents = toolUseId ? children.get(toolUseId) : undefined;
      if (childEvents && childEvents.length > 0) {
        const node = (
          <SubAgentBlock
            toolEvent={g.event}
            result={g.result}
            childEvents={childEvents}
            childrenMap={children}
            toolResultById={toolResultById}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
            parentMessageId={initiatorMessageId}
            sessionId={sessionId}
            defaultOpen={g.result === undefined}
          />
        );
        rendered.push(wrapWithTs(node, `agent-${i}`, g.event._ts, {
          targetId: toolUseId ? `tu-${toolUseId}` : undefined,
          parentId: initiatorMessageId ? `msg-${initiatorMessageId}` : undefined,
        }));
        return;
      }

      const toolNode = (
        <Suspense fallback={null}>
          <ToolCall
            tool={tool}
            args={g.event.data.args as string | Record<string, unknown> | null | undefined}
            result={g.result}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
          />
        </Suspense>
      );
      rendered.push(wrapWithTs(toolNode, `tool-${i}`, g.event._ts, {
        targetId: toolUseId ? `tu-${toolUseId}` : undefined,
        parentId: initiatorMessageId ? `msg-${initiatorMessageId}` : undefined,
      }));
      return;
    }
    const ptuid = (g.event.data.parent_tool_use_id as string | null | undefined) || undefined;
    rendered.push(
      wrapWithTs(
        renderSingleEvent(g.event, g.idx, onFileClick, onViewDiff, false, sessionId),
        `evt-${g.idx}`,
        g.event._ts,
        {
          parentId: ptuid
            ? `tu-${ptuid}`
            : initiatorMessageId ? `msg-${initiatorMessageId}` : undefined,
        },
      ),
    );
  });

  for (const w of workers) {
    if (consumedWorkerIds.has(w.delegation_id)) continue;
    const color = w.worker_session_id ? colorMap?.get(w.worker_session_id) : undefined;
    const anchorId = `timeline-entity-${w.delegation_id}`;
    const filteredEvents = workerPanelEvents(w).filter(
      (e) => !["complete", "session_discovered"].includes(e.type)
    );
    rendered.push(
      <CollapsibleTimelineBlock
        key={`orphan-${w.delegation_id}`}
        anchorId={anchorId}
        label={w.worker_description || panelKindLabel(w.panel_kind)}
        labelColor={color}
        chipLabel={panelKindLabel(w.panel_kind)}
        chipClass="role-chip role-chip-worker"
        events={filteredEvents}
        onFileClick={onFileClick}
        onViewDiff={onViewDiff}
        parentMessageId={initiatorMessageId}
        parentTargetId={anchorId}
        sessionId={sessionId}
        created={isCreationPanelKind(w.panel_kind)}
        modelMeta={{
          providerId: w.provider_id,
          model: w.model,
          reasoningEffort: w.reasoning_effort,
        }}
        defaultOpen={workerDefaultOpenById.get(w.delegation_id) ?? false}
      />
    );
  }

  return rendered;
}

/**
 * Memoized so persisted assistant messages — whose `message` object is a
 * stable reference — don't re-walk their entire event tree on every WS
 * tick from an in-progress streaming turn above them. Only the streaming
 * message's bubble re-renders when its events grow.
 */
type LazyFetchedMessage = { key: string; message: ChatMessage };

function messageWithHydratedRenderPayload(
  current: ChatMessage,
  hydrated: ChatMessage,
): ChatMessage {
  const next: ChatMessage = {
    ...current,
    events: hydrated.events ?? current.events,
  };
  if (current.workers || hydrated.workers) {
    const hydratedWorkers = new Map(
      (hydrated.workers ?? []).map((worker) => [worker.delegation_id, worker]),
    );
    next.workers = (current.workers ?? hydrated.workers ?? []).map((worker) => {
      const hydratedWorker = hydratedWorkers.get(worker.delegation_id);
      return hydratedWorker?.events
        ? { ...worker, events: hydratedWorker.events }
        : worker;
    });
  }
  return next;
}

const AssistantMessage = memo(function AssistantMessage({
  message,
  sessionId,
  onFileClick,
  onViewDiff,
  onRetry,
  onRetryStopped,
  onContinueRateLimitOnAnotherProvider,
  threadColorMap,
  tags,
  advSyncOverlays,
  onAdvSyncClick,
  orchestrationMode,
  containerRef: externalContainerRef,
  runs = [],
  /** When true, manager entity blocks are relabeled as "Worker". Used for
   * supervisor sub-groups where the manager events are actually worker
   * events emitted by the supervised paired worker. */
  relabelManagerAsWorker = false,
  loadPhase,
  /** Id of the parent turn initiator — level-0 events jump to this. */
  initiatorMessageId,
  lazyFetchedMessage,
  onLazyFetchedMessage,
}: {
  message: ChatMessage;
  /** Session id used to build the lazy event-fetch URL for stubbed
   * messages. */
  sessionId?: string;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: () => void;
  /** Distinct from `onRetry`: used by `StoppedIndicator` to trigger a
   * rewind-then-retry of the discarded turn (deletes the stopped
   * assistant + its user message, then re-sends as a fresh turn). */
  onRetryStopped?: () => void;
  onContinueRateLimitOnAnotherProvider?: () => void;
  threadColorMap?: Map<string, string>;
  tags?: InlineTag[];
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
  orchestrationMode?: OrchestrationMode;
  containerRef?: RefObject<HTMLDivElement | null>;
  runs?: import("../types").RunInfo[];
  relabelManagerAsWorker?: boolean;
  /** Fine-grained loading phase while the CLI subprocess starts. */
  loadPhase?: import("../hooks/useWebSocket").StreamingLoadPhase;
  /** Id of the parent turn initiator — level-0 events jump to this. */
  initiatorMessageId?: string;
  lazyFetchedMessage?: LazyFetchedMessage | null;
  onLazyFetchedMessage?: (entry: LazyFetchedMessage) => void;
}) {
  const internalRef = useRef<HTMLDivElement>(null);
  const containerRef = externalContainerRef ?? internalRef;

  // Cache of the full message fetched lazily when render events are absent.
  const [fetched, setFetched] = useState<ChatMessage | null>(null);
  const fetchedForId = useRef<string | null>(null);
  const onLazyFetchedMessageRef = useRef(onLazyFetchedMessage);

  useEffect(() => {
    onLazyFetchedMessageRef.current = onLazyFetchedMessage;
  }, [onLazyFetchedMessage]);

  const omittedEvents = message.omitted_payloads?.events;
  const fetchKey = `${message.id}:${message.stubVersion ?? 0}:${omittedEvents?.revision ?? ""}`;
  const cachedFetched =
    lazyFetchedMessage?.key === fetchKey
      ? lazyFetchedMessage.message
      : fetchedForId.current === fetchKey
        ? fetched
        : null;
  const needsFetch =
    (!!message.stub || !!omittedEvents) &&
    !cachedFetched;
  useEffect(() => {
    if (!needsFetch || !sessionId) return;
    let cancelled = false;
    fetch(
      `${API}/api/sessions/${encodeURIComponent(sessionId)}` +
        `/messages/${encodeURIComponent(message.id)}/events`,
    )
      .then((r) => r.json())
      .then((full: ChatMessage) => {
        if (cancelled) return;
        fetchedForId.current = fetchKey;
        setFetched(full);
        onLazyFetchedMessageRef.current?.({ key: fetchKey, message: full });
      });
    return () => {
      cancelled = true;
    };
  }, [needsFetch, sessionId, message.id, fetchKey]);

  // The message whose full events drive the expanded timeline: the
  // lazily-fetched full form when available, else the message as-is
  // (non-stub messages already carry full events).
  const effectiveMessage =
    (message.stub || omittedEvents) &&
    cachedFetched
      ? messageWithHydratedRenderPayload(message, cachedFetched)
      : message;
  const decorationRevision =
    tags?.length || advSyncOverlays?.length
      ? messageObjectRevision(effectiveMessage)
      : message.id;

  useMessageDecorations(containerRef, {
    tags,
    advSyncOverlays,
    onAdvSyncClick,
    revision: decorationRevision,
  });
  // Event stream for this assistant bubble. Manager-mode turns keep
  // Resolve the orchestration strategy for this mode.
  const strategy = useMemo(() => getStrategy(orchestrationMode), [orchestrationMode]);

  const routedMessage = useMemo(
    () => routeLeakedWorkerEvents(effectiveMessage),
    [effectiveMessage],
  );

  const filteredManagerEvents = useMemo(() => {
    return strategy.getEvents(routedMessage).filter(
      (e) => !["complete", "session_discovered"].includes(e.type) && !isModelSwitchedEvent(e)
    );
  }, [strategy, routedMessage]);

  // Worker prep events live on `message.events` alongside the primary
  // agent's own events but should NOT enter the entity-block tagger
  // (they have no real entity owner — they're a separate per-worker
  // side-stream rendered as a collapsible at the top of the timeline).
  // Strip them from the events fed to buildEntityBlocks so the manager
  // entity block doesn't accidentally absorb them.
  const messageWithoutPrepEvents = useMemo(() => {
    const prepTypes = new Set([
      "worker_prep_start",
      "worker_prep_event",
      "worker_prep_complete",
      "worker_prep_cancelled",
    ]);
    const evs = routedMessage.events ?? [];
    if (!evs.some((e) => prepTypes.has(e.type))) return routedMessage;
    return { ...routedMessage, events: evs.filter((e) => !prepTypes.has(e.type)) };
  }, [routedMessage]);

  const workers = useMemo(
    () => dedupeWorkerPanels(routedMessage.workers ?? []),
    [routedMessage.workers],
  );
  const activeWorkerIds = useMemo(() => {
    const ids = new Set<string>();
    for (const run of runs) {
      if (run.kind !== "worker" || !run.delegation_id) continue;
      ids.add(run.delegation_id);
    }
    return ids;
  }, [runs]);
  const entityBlocks = useMemo(() => {
    const blocks = strategy.buildEntityBlocks(messageWithoutPrepEvents, workers);
    if (relabelManagerAsWorker && blocks) {
      return blocks.map(b =>
        b.entityType === "manager"
          ? { ...b, entityType: "worker" as const, entityLabel: "Worker" }
          : b
      );
    }
    return blocks;
  }, [strategy, messageWithoutPrepEvents, workers, relabelManagerAsWorker]);
  const hasManagerScope = strategy.hasScopeWrapper(effectiveMessage);
  const flattenPrimaryEntity = hasManagerScope || orchestrationMode !== "team";

  const stream = renderTimeline(
    entityBlocks,
    filteredManagerEvents,
    workers,
    threadColorMap,
    onFileClick,
    onViewDiff,
    activeWorkerIds,
    // Team already has an outer scope chip; native has no manager scope.
    // Flatten primary blocks in both cases while keeping worker/session
    // panels as collapsible blocks.
    flattenPrimaryEntity,
    orchestrationMode,
    initiatorMessageId,
    sessionId,
  );

  const managerSessionShort =
    hasManagerScope && effectiveMessage.agent_session_id
      ? effectiveMessage.agent_session_id.slice(0, 8)
      : null;
  const assistantErrorText =
    message.error && !message.content && !message.retrying_until
      ? message.errorText
      : undefined;
  const assistantContent = typeof effectiveMessage.content === "string"
    ? effectiveMessage.content
    : "";
  const shouldRenderAssistantContent =
    !!assistantContent &&
    !message.error &&
    !message.isStreaming &&
    (stream.length === 0 || !visibleEventsRepresentAssistantContent(filteredManagerEvents, assistantContent));

  return (
    <div className="message assistant-message" data-message-id={message.id} data-testid="assistant-message" ref={containerRef}>
      <div className="message-content" key={decorationRevision}>
        {hasManagerScope ? (
          <div className="manager-scope" aria-label={primaryEntityLabel(orchestrationMode) + " scope"}>
            <div className="role-label role-label-manager">
              {primaryEntityLabel(orchestrationMode) && (
                <span className="role-chip">{primaryEntityLabel(orchestrationMode)}</span>
              )}
              {managerSessionShort && (
                <span className="role-session-id">· {managerSessionShort}</span>
              )}
            </div>
            {stream}
          </div>
        ) : (
          stream
        )}
        {shouldRenderAssistantContent && (
          <MessageBox text={assistantContent} onFileClick={onFileClick} />
        )}
        {stream.length === 0 && assistantErrorText && !message.isStreaming && (
          <MessageBox text={assistantErrorText} onFileClick={onFileClick} />
        )}
        {loadPhase && stream.length === 0 && !message.content && message.isStreaming && !message.stopped_at && (
          <div className="load-phase-indicator" role="status" aria-live="polite">
            <span className="load-phase-spinner" aria-hidden="true" />
            <span>{loadPhase === "starting" ? "Starting session…" : "Loading context…"}</span>
          </div>
        )}
        {(runs.length > 0 || (message.isStreaming && !message.stopped_at && !message.isStale)) && (
          <div className="streaming-footer">
            {runs.length > 0 ? (
              <RunBadgeStack
                runs={runs}
                sessionId={sessionId}
                workerLabelByDelegation={
                  workers.length > 0
                    ? new Map(
                        workers.map((w) => [
                          w.delegation_id,
                          w.worker_description,
                        ])
                      )
                    : undefined
                }
              />
            ) : null}
          </div>
        )}
        {message.error && !message.retrying_until && (
          <>
            <MessageStatus
              status="error"
              errorText={message.errorText ?? message.content}
              onRetry={onRetryStopped ?? onRetry}
            />
            {(() => {
              const ts = fmtTime(message.timestamp);
              if (!ts) return null;
              return (
                <div className="message-box-footer">
                  <span className="user-message-time">{ts}</span>
                </div>
              );
            })()}
          </>
        )}
        {message.error && message.retrying_until && (
          <div className="message-status status-warning" data-testid="message-retry-warning">
            <div className="error-block-header">
              <span className="status-dot" />
              <span className="error-block-label">Retrying</span>
              <span className="status-error-text">
                {message.errorText
                  ? message.errorText.split("\n", 1)[0]
                  : "Rate limit exceeded"}
              </span>
              {onContinueRateLimitOnAnotherProvider && (
                <button
                  className="status-retry-btn"
                  onClick={(e) => {
                    e.stopPropagation();
                    onContinueRateLimitOnAnotherProvider();
                  }}
                >
                  Continue on another provider
                </button>
              )}
            </div>
          </div>
        )}
        {message.stopped_at && (
          <StoppedIndicator
            stoppedAt={message.stopped_at}
            interrupted={!!message.interrupted_by_msg_id}
            onRetry={onRetryStopped}
          />
        )}
        {message.isDetached && !message.isStale && (
          <div className="detached-pill" role="status" aria-live="polite">
            <span className="detached-spinner" aria-hidden="true" />
            <span>Reconnecting — agent still running…</span>
          </div>
        )}
        {message.isStale && (
          <div className="stale-pill" role="status" aria-live="polite">
            Connection lost — no updates for 90s
          </div>
        )}
        {message.isRecovering && (
          <div
            className="recovering-pill"
            data-testid="message-recovering-pill"
            role="status"
            aria-live="polite"
          >
            <span className="recovering-spinner" aria-hidden="true" />
            <span>Updating state…</span>
          </div>
        )}
        {message.retrying_until && (
          <RetryingPill retryAt={message.retrying_until} />
        )}
        {message.continuation_active != null && (
          <ContinuationPill chainDepth={message.continuation_active} />
        )}
        {message.auto_retry && message.auto_retry.count > 0 && (
          <div
            className="auto-retry-pill"
            data-testid="message-auto-retry-pill"
            role="status"
          >
            <span aria-hidden="true">↻</span>
            <span>
              {message.auto_retry.kind === "rate_limit"
                ? "Auto-retried after rate limit"
                : message.auto_retry.kind === "transient"
                  ? "Auto-retried after a transient error"
                  : "Auto-retried"}
              {message.auto_retry.count > 1
                ? ` ×${message.auto_retry.count}`
                : ""}{" "}
              — recovered
            </span>
          </div>
        )}
      </div>
    </div>
  );
});

/** Status indicator for pending user messages */
function MessageStatus({
  status,
  errorText,
  onRetry,
}: {
  status: ChatMessage["status"];
  errorText?: string;
  onRetry?: () => void;
}) {
  const [errorExpanded, setErrorExpanded] = useState(false);
  if (!status) return null;
  const config = {
    sending: { label: "Sending...", className: "status-sending" },
    received: { label: "Received", className: "status-received" },
    running: { label: "Running...", className: "status-running" },
    error: { label: "Failed", className: "status-error" },
    offline: { label: "Queued offline", className: "status-offline" },
  }[status];

  if (status === "error") {
    const firstLine = errorText ? errorText.split("\n", 1)[0] : "";
    return (
      <div className="message-status status-error">
        <div
          className="error-block-header"
          onClick={(e) => {
            e.stopPropagation();
            if (errorText) setErrorExpanded((v) => !v);
          }}
          role={errorText ? "button" : undefined}
        >
          <span className="status-dot" />
          <span className="error-block-label">{config.label}</span>
          {errorText && (
            <span className="collapse-arrow">
              {errorExpanded ? "\u25BC" : "\u25B6"}
            </span>
          )}
          {errorText && !errorExpanded && (
            <span className="status-error-text">{firstLine}</span>
          )}
          {onRetry && (
            <button
              className="status-retry-btn"
              onClick={(e) => { e.stopPropagation(); onRetry(); }}
            >
              Retry
            </button>
          )}
        </div>
        {errorExpanded && errorText && (
          <pre className="error-block-body">{errorText}</pre>
        )}
      </div>
    );
  }

  return (
    <div className={`message-status ${config.className}`}>
      <span className="status-dot" />
      <span>{config.label}</span>
    </div>
  );
}

/** Render attached images for a user message */
function UserImages({ images, sessionId }: { images?: ChatMessage["images"]; sessionId?: string }) {
  const [lightbox, setLightbox] = useState<{ url: string; index: number } | null>(null);
  const [resolvedUrls, setResolvedUrls] = useState<string[]>([]);
  useBackButtonDismiss(lightbox !== null, () => setLightbox(null));

  useEffect(() => {
    let cancelled = false;
    const objectUrls: string[] = [];

    async function loadImages() {
      if (!images || images.length === 0) {
        setResolvedUrls([]);
        return;
      }
      const next = await Promise.all(
        images.map(async (img) => {
          if (img.dataUrl) return img.dataUrl;
          const url = buildMessageImageUrl(sessionId, img.filename);
          if (!url) return "";
          try {
            const res = await fetch(url, { credentials: "include" });
            if (!res.ok) return "";
            const objectUrl = URL.createObjectURL(await res.blob());
            objectUrls.push(objectUrl);
            return objectUrl;
          } catch {
            return "";
          }
        }),
      );
      if (cancelled) {
        objectUrls.forEach((url) => URL.revokeObjectURL(url));
        return;
      }
      setResolvedUrls(next.filter(Boolean));
    }

    void loadImages();
    return () => {
      cancelled = true;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [images, sessionId]);

  if (!images || images.length === 0) return null;

  const urls = resolvedUrls;
  if (urls.length === 0) return null;

  const navigate = (dir: 1 | -1) => {
    if (!lightbox) return;
    const next = (lightbox.index + dir + urls.length) % urls.length;
    setLightbox({ url: urls[next], index: next });
  };

  return (
    <>
      <div className="message-images">
        {urls.map((url, i) => (
          <img
            key={i}
            src={url}
            alt={`Attachment ${i + 1}`}
            className="message-image"
            onClick={() => setLightbox({ url, index: i })}
          />
        ))}
      </div>
      {lightbox && (
        <div className="image-lightbox-overlay" onClick={() => setLightbox(null)}>
          <img src={lightbox.url} className="image-lightbox-img" onClick={e => e.stopPropagation()} />
          {urls.length > 1 && (
            <>
              <button className="image-lightbox-nav image-lightbox-prev" onClick={e => { e.stopPropagation(); navigate(-1); }}>‹</button>
              <button className="image-lightbox-nav image-lightbox-next" onClick={e => { e.stopPropagation(); navigate(1); }}>›</button>
            </>
          )}
          <button className="image-lightbox-close" onClick={() => setLightbox(null)}><Icon name="x" size={18} /></button>
          <div className="image-lightbox-counter">{lightbox.index + 1} / {urls.length}</div>
        </div>
      )}
    </>
  );
}

function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function UserFiles({ files }: { files?: ChatMessage["files"] }) {
  if (!files || files.length === 0) return null;
  return (
    <div className="message-files">
      {files.map((file, i) => (
        <div key={`${file.name}-${i}`} className="message-file-badge">
          <span className="message-file-name">{file.name}</span>
          <span className="message-file-size">{formatAttachmentSize(file.size)}</span>
        </div>
      ))}
    </div>
  );
}

/** A single turn group: the turn initiator (User/Ask/Message/Provisioning/etc.)
 *  paired with its assistant response, collapsible as a unit. Wrapped in
 *  `memo` below so historical turn groups skip re-render on per-frame WS
 *  streaming updates — those mutate only the in-flight assistant message
 *  (last in the list), leaving every earlier turn group's props
 *  referentially stable. */
function TurnGroupImpl({ initiatorMessage, responseMessage, childTurnGroups, sessionId, userDisplayName, onFileClick, onViewDiff, onRetry, onRetryStopped, onContinueRateLimitOnAnotherProvider, onAlterTurnMessage, threadColorMap, defaultCollapsed = false, expandAllTrigger, tags, advSyncOverlays, onAdvSyncClick, scrollEl: scrollElProp, orchestrationMode, runs, sessionRunning = false, loadPhase, enterAnimation, precedingModelSwitchEvents = [], trailingModelSwitchEvents = [] }: {
  initiatorMessage: ChatMessage;
  responseMessage?: ChatMessage;
  precedingModelSwitchEvents?: WSEvent[];
  trailingModelSwitchEvents?: WSEvent[];
  /** Child turn groups nested under the supervisor/main turn. */
  childTurnGroups?: { initiator: ChatMessage; response?: ChatMessage }[];
  sessionId?: string;
  userDisplayName?: string | null;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (responseMessage: ChatMessage) => void;
  onContinueRateLimitOnAnotherProvider?: (responseMessage: ChatMessage) => void;
  onAlterTurnMessage?: (message: ChatMessage, content: string) => boolean | Promise<boolean>;
  threadColorMap?: Map<string, string>;
  defaultCollapsed?: boolean;
  expandAllTrigger?: number;
  tags?: InlineTag[];
  onRemoveTag?: (id: string) => void;
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
  /** Scroll container that owns this group. Used by the collapse-toggle
   *  scroll-anchor logic to compensate scrollTop after the group's
   *  height changes, so the bottom edge of the box stays put. Optional
   *  — when absent we fall back to a closest() lookup. */
  scrollEl?: HTMLElement | null;
  orchestrationMode?: OrchestrationMode;
  /** Backend-owned active runs targeting this group (manager/native
   * for the assistant_msg, plus N workers). Each renders a labeled
   * animated badge — empty array means nothing is running here. */
  runs?: import("../types").RunInfo[];
  sessionRunning?: boolean;
  /** Fine-grained loading phase while the CLI subprocess starts. */
  loadPhase?: import("../hooks/useWebSocket").StreamingLoadPhase;
  /** True only for groups freshly prepended via "load older". Plays a
   * one-shot fade/slide-in on mount. Read once at mount (framer-motion
   * owns the animation), so later re-renders never cancel it. Uses
   * opacity + transform only — no layout shift, so the scroll-restore
   * in useScrollLoadOlder stays exact. */
  enterAnimation?: boolean;
}) {
  const { t } = useTranslation();
  const responseContainerRef = useRef<HTMLDivElement>(null);
  const initiatorContainerRef = useRef<HTMLDivElement>(null);
  // Outer-most node of this group — used to anchor scroll on its bottom
  // edge when the user toggles collapse, so the box "shoves up" instead
  // of pushing the boxes below it down the viewport.
  const groupRef = useRef<HTMLDivElement>(null);
  // Captured pre-toggle state: the group's bottom in viewport coords and
  // the scroll container + its scrollTop at click time. Read in the
  // useLayoutEffect below to compensate scrollTop after the DOM grows
  // (or shrinks). Only populated on user-initiated toggles — auto-toggles
  // from defaultCollapsed / expandAllTrigger leave it null so they don't
  // disturb scroll.
  const pendingAnchorRef = useRef<
    { bottom: number; scrollTop: number; scrollEl: HTMLElement } | null
  >(null);
  // Track whether the user has manually toggled this group. If they haven't,
  // we follow `defaultCollapsed` — so the latest turn auto-expands and
  // previously-latest turns auto-collapse when a new turn arrives. Once the
  // user clicks the header, we stop overriding and respect their choice.
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [userToggled, setUserToggled] = useState(false);
  useEffect(() => {
    if (!userToggled) setCollapsed(defaultCollapsed);
  }, [defaultCollapsed, userToggled]);
  // Two independent collapse surfaces: the group chevron folds the events/
  // response, while the user prompt text has its own chevron. The prompt
  // never auto-collapses — only its own toggle folds it.
  const [promptCollapsed, setPromptCollapsed] = useState(false);
  const initiatorBodyCollapsed = promptCollapsed;
  const responseCollapsed = collapsed;
  const [lazyFetchedResponse, setLazyFetchedResponse] =
    useState<LazyFetchedMessage | null>(null);
  const [lazyFetchedChildResponses, setLazyFetchedChildResponses] =
    useState<Record<string, LazyFetchedMessage>>({});
  const rememberLazyFetchedResponse = useCallback((entry: LazyFetchedMessage) => {
    setLazyFetchedResponse((current) =>
      current?.key === entry.key && current.message === entry.message
        ? current
        : entry,
    );
  }, []);
  const rememberLazyFetchedChildResponse = useCallback(
    (messageId: string, entry: LazyFetchedMessage) => {
      setLazyFetchedChildResponses((current) => {
        const existing = current[messageId];
        if (existing?.key === entry.key && existing.message === entry.message) {
          return current;
        }
        return { ...current, [messageId]: entry };
      });
    },
    [],
  );
  const toggleCollapsed = () => {
    setUserToggled(true);
    const groupEl = groupRef.current;
    // Prefer the prop (parent-owned scroll container, zero DOM walk);
    // fall back to a computed-style walk up the tree for the nearest
    // overflow:auto|scroll ancestor. The walk covers every current
    // host (.chat-messages, .fork-pane-messages, .supervisor-timeline,
    // .adv-sync-window-pane-body) AND every future one without needing
    // a class-list update here. getComputedStyle is fine perf-wise —
    // this fires only on user click, not on every render or scroll.
    const scrollEl =
      scrollElProp ?? (groupEl ? findScrollParent(groupEl) : null);
    if (groupEl && scrollEl) {
      // Anchor on min(box.bottom, scrollEl.bottom) so a box whose body
      // extends below the viewport doesn't push its (visible) header
      // off-screen when it grows. When the bottom is below the viewport,
      // anchoring on the viewport edge gives delta=0 — i.e. let the
      // browser do its default thing, no scroll shift.
      const scrollRect = scrollEl.getBoundingClientRect();
      const boxBottom = groupEl.getBoundingClientRect().bottom;
      const anchorY = Math.min(boxBottom, scrollRect.bottom);
      pendingAnchorRef.current = {
        bottom: anchorY,
        scrollTop: scrollEl.scrollTop,
        scrollEl,
      };
    }
    setCollapsed((v) => !v);
  };
  // After the DOM updates from the toggle (but BEFORE paint), shift
  // scrollTop by the height delta so the group's bottom edge (clamped
  // to viewport bottom) stays at the same viewport y. Everything in
  // the scroll flow below the group is in the same block layout, so
  // anchoring this one edge keeps every box at or below it visually
  // still — the expansion "shoves up" the content above instead of
  // pushing content down.
  useLayoutEffect(() => {
    const anchor = pendingAnchorRef.current;
    if (!anchor) return;
    pendingAnchorRef.current = null;
    const groupEl = groupRef.current;
    // Skip if the group or scroll container detached between toggle and
    // layout commit (route change, tab switch). Writing scrollTop to a
    // disconnected node is a no-op anyway but the math would be
    // meaningless.
    if (!groupEl || !anchor.scrollEl.isConnected) return;
    const scrollRect = anchor.scrollEl.getBoundingClientRect();
    const boxBottom = groupEl.getBoundingClientRect().bottom;
    const afterAnchorY = Math.min(boxBottom, scrollRect.bottom);
    const delta = afterAnchorY - anchor.bottom;
    if (delta !== 0) {
      anchor.scrollEl.scrollTop = anchor.scrollTop + delta;
    }
  }, [collapsed]);
  useEffect(() => {
    if (expandAllTrigger && expandAllTrigger > 0) {
      setCollapsed(false);
    }
  }, [expandAllTrigger]);
  const hasResponse = !!responseMessage || !!(childTurnGroups?.some(sg => sg.response));
  // Ask-flow turns are represented entirely by their inline picker footer
  // (reasoning + matches + actions), rendered by Chat's renderTurnFooter.
  // Their assistant message carries no events and its reasoning already
  // lives in the picker, so rendering the assistant body would duplicate
  // the reasoning and add an empty indented block — suppress it, and make
  // the turn non-expandable. Delegate-approval pickers ride on real turns
  // with their own body, so they are excluded.
  const isAskFlowTurn =
    !!responseMessage?.ask_result &&
    responseMessage.ask_result.purpose !== "delegate_approval";
  const canExpand = hasResponse && !isAskFlowTurn;

  // The last assistant message that carries meaningful content — may be a
  // supervisor sub-group response rather than the initial responseMessage.
  const effectiveResponse = useMemo(() => {
    if (childTurnGroups) {
      for (let i = childTurnGroups.length - 1; i >= 0; i--) {
        if (childTurnGroups[i].response) return childTurnGroups[i].response!;
      }
    }
    return responseMessage;
  }, [responseMessage, childTurnGroups]);
  const initiatorErrorRendersWithResponse =
    initiatorMessage.status === "error" && hasResponse && !effectiveResponse?.error;

  // Only build the summary when we're actually going to render it.
  // On expanded groups this saves a full events walk per render.
  const summary = useMemo(() => {
    if (!responseCollapsed || !hasResponse) return null;
    const src = effectiveResponse;
    // Ask-flow turns with no text are represented entirely by their picker
    // footer (error notice / Create-new / Never-mind). Don't render the
    // generic "No output" summary for them.
    if (src?.ask_result && !src?.content) return null;
    const events = previewEventsForMessage(src, orchestrationMode);
    const workerCount = src?.workers?.length ?? 0;
    return buildTurnSummary(events, workerCount, src?.content);
  }, [responseCollapsed, hasResponse, effectiveResponse, orchestrationMode]);

  // Render the last event fully for collapsed display
  const collapsedLastEvent = useMemo(() => {
    if (!responseCollapsed || !hasResponse) return null;
    const src = effectiveResponse;
    const content = src?.content;
    const events = previewEventsForMessage(src, orchestrationMode);
    if (
      content &&
      !src?.isStreaming &&
      eventTailContainsAssistantContent(events, content)
    ) {
      return wrapWithTs(
        <OutputEvent text={content} onFileClick={onFileClick} />,
        "last-event",
      );
    }
    if (events.length === 0) {
      return null;
    }
    const preview = renderLastEventPreview(events, onFileClick, onViewDiff, undefined, sessionId);
    return preview ?? (() => {
      return null;
    })();
  }, [responseCollapsed, hasResponse, effectiveResponse, onFileClick, onViewDiff, orchestrationMode, sessionId]);

  const collapsedSteerPrompts = useMemo(() => {
    if (!responseCollapsed || !hasResponse) return [];
    const src = effectiveResponse;
    const events = previewEventsForMessage(src, orchestrationMode);
    return events
      .filter((event) => event.type === "steer_prompt")
      .map((event, idx) =>
        wrapWithTs(
          renderSingleEvent(event, idx, onFileClick, onViewDiff, false, sessionId),
          `steer-${idx}`,
          event._ts,
        )
      )
      .filter(Boolean);
  }, [responseCollapsed, hasResponse, effectiveResponse, onFileClick, onViewDiff, orchestrationMode, sessionId]);

  const responseTags = useMemo(
    () => (responseMessage ? tags?.filter((t) => t.messageId === responseMessage.id) ?? [] : []),
    [tags, responseMessage]
  );
  const responseOverlays = useMemo(
    () =>
      responseMessage
        ? advSyncOverlays?.filter((o) => o.message_id === responseMessage.id) ?? []
        : [],
    [advSyncOverlays, responseMessage]
  );
  const initiatorOverlays = useMemo(
    () =>
      advSyncOverlays?.filter((o) => o.message_id === initiatorMessage.id) ?? [],
    [advSyncOverlays, initiatorMessage.id]
  );
  // Bucket runs by target_message_id once per render of this group, then
  // hand each AssistantMessage a stable per-id reference. Inline
  // `runs.filter(...)` would mint a new array on every render and defeat
  // the `AssistantMessage = memo(...)` shallow compare, forcing the
  // entire assistant subtree to re-render on every parent re-render
  // (e.g. every per-token WS frame).
  const runsByTargetId = useMemo(() => {
    const map = new Map<string, import("../types").RunInfo[]>();
    for (const r of runs ?? []) {
      if (!r.target_message_id) continue;
      const arr = map.get(r.target_message_id);
      if (arr) arr.push(r);
      else map.set(r.target_message_id, [r]);
    }
    return map;
  }, [runs]);
  const responseRuns = responseMessage
    ? runsByTargetId.get(responseMessage.id) ?? EMPTY_RUNS
    : EMPTY_RUNS;
  const collapsedResponseErrorText =
    responseCollapsed && effectiveResponse?.error
      ? effectiveResponse.errorText ?? effectiveResponse.content
      : undefined;
  // Apply overlays directly to the user-message-box body. The
  // AssistantMessage component runs its own effect for assistant
  // messages; user messages are rendered inline here so the same
  // effect lives here. Order parity with the assistant case: tag
  // overlays don't currently apply to user messages (tags are
  // assistant-only in this codebase), so the order question is moot.
  useEffect(() => {
    if (!initiatorContainerRef.current || initiatorOverlays.length === 0) return;
    let cleanup: (() => void) | undefined;
    const timer = setTimeout(() => {
      if (!initiatorContainerRef.current || !onAdvSyncClick) return;
      cleanup = applyAdvSyncOverlays(
        initiatorContainerRef.current,
        initiatorOverlays,
        onAdvSyncClick,
      );
    }, 50);
    return () => {
      clearTimeout(timer);
      cleanup?.();
    };
  }, [initiatorOverlays, onAdvSyncClick]);

  const isRunning = sessionRunning || isGroupRunning(responseMessage, runs);
  const rawInitiatorContent =
    typeof initiatorMessage.content === "string" ? initiatorMessage.content : "";
  const hiddenPrompt =
    initiatorMessage.role === "user" &&
    typeof initiatorMessage.cli_prompt === "string" &&
    initiatorMessage.cli_prompt.length > 0 &&
    initiatorMessage.cli_prompt !== rawInitiatorContent
      ? initiatorMessage.cli_prompt
      : "";
  const [isEditingInitiator, setIsEditingInitiator] = useState(false);
  const [showHiddenPrompt, setShowHiddenPrompt] = useState(false);
  const [initiatorEditDraft, setInitiatorEditDraft] = useState(rawInitiatorContent);
  useEffect(() => {
    if (!isEditingInitiator) setInitiatorEditDraft(rawInitiatorContent);
  }, [isEditingInitiator, rawInitiatorContent]);
  const submitInitiatorAlter = useCallback(async () => {
    if (!onAlterTurnMessage) return;
    const next = initiatorEditDraft.trim();
    if (!next || next === rawInitiatorContent.trim()) {
      setIsEditingInitiator(false);
      return;
    }
    const sent = await onAlterTurnMessage(initiatorMessage, next);
    if (sent !== false) setIsEditingInitiator(false);
  }, [onAlterTurnMessage, rawInitiatorContent, initiatorEditDraft, initiatorMessage]);

  return (
    <motion.div
      className="message-row"
      layout="position"
      initial={enterAnimation ? { opacity: 0, y: -8 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.55, ease: "easeInOut" }}
    >
      <div className="turn-group" ref={groupRef}>
      <ModelSwitchBoundaryEvents
        events={precedingModelSwitchEvents}
        testId="model-switch-preceding"
      />
      {/* Synthetic turn-initiator stub (id starts with "__synth-") is created by
          the Chat grouping logic when an orphan assistant message has
          no persisted turn initiator. Hide the user box entirely — the
          assistant message renders in its own slot below. */}
      {!initiatorMessage.id.startsWith("__synth-") && (
      <div
        className="message-box user-message-box"
        id={`msg-${initiatorMessage.id}`}
        data-message-id={initiatorMessage.id}
        data-testid="user-message"
        data-status={initiatorMessage.status ?? ""}
        ref={initiatorContainerRef}
      >
        <div className="message-box-header-row">
          <button
            type="button"
            className="message-box-header message-box-header-main"
            onClick={canExpand ? toggleCollapsed : undefined}
            aria-expanded={!collapsed}
            disabled={!canExpand}
          >
            <span className="collapse-arrow">{collapsed ? "\u25B6" : "\u25BC"}</span>
            <span className={`message-box-icon${initiatorMessage.source ? " orchestration-icon" : ""}`}>
              {turnMessageHeader(initiatorMessage.source).icon}
            </span>
            <span className={`message-box-label${initiatorMessage.source ? " orchestration-label" : ""}`}>
              {turnMessageHeader(initiatorMessage.source, userDisplayName).label}
            </span>
          </button>
          <TeamMessageFrom message={initiatorMessage} />
          <button
            type="button"
            className="prompt-collapse-toggle"
            onClick={() => setPromptCollapsed((v) => !v)}
            aria-expanded={!promptCollapsed}
            aria-label={promptCollapsed ? t("message.expandMessageAria") : t("message.collapseMessageAria")}
            title={promptCollapsed ? t("message.expandMessageAria") : t("message.collapseMessageAria")}
          >
            <span className="collapse-arrow">{promptCollapsed ? "▶" : "▼"}</span>
          </button>
          {onAlterTurnMessage && (
            <div className="message-header-actions">
              {hiddenPrompt && (
                <button
                  type="button"
                  className="message-header-action-btn hidden-prompt-btn"
                  onClick={() => setShowHiddenPrompt(true)}
                  title="Show full prompt"
                  aria-label="Show full prompt"
                >
                  <Icon name="warning" size={13} />
                </button>
              )}
              <button
                type="button"
                className="message-header-action-btn alter-user-message-btn"
                onClick={() => {
                  setInitiatorEditDraft(rawInitiatorContent);
                  setIsEditingInitiator(true);
                }}
                title={t("message.alterTitle")}
              >
                <Icon name="edit" size={13} />
                <span>{t("message.alterButton")}</span>
              </button>
            </div>
          )}
          {!onAlterTurnMessage && hiddenPrompt && (
            <div className="message-header-actions">
              <button
                type="button"
                className="message-header-action-btn hidden-prompt-btn"
                onClick={() => setShowHiddenPrompt(true)}
                title="Show full prompt"
                aria-label="Show full prompt"
              >
                <Icon name="warning" size={13} />
              </button>
            </div>
          )}
          {initiatorMessage.parent_id && (
            <button
              type="button"
              className="jump-to-parent-inline-btn"
              title="Jump to parent"
              onClick={() => {
                const el = document.getElementById(`msg-${initiatorMessage.parent_id!}`);
                if (el) jumpToParentEl(el);
              }}
            >
              <Icon name="chevron-up" size={12} />
            </button>
          )}
        </div>
        {showHiddenPrompt && hiddenPrompt && (
          <div
            className="modal-overlay hidden-prompt-modal-overlay"
            role="dialog"
            aria-modal="true"
            aria-labelledby={`hidden-prompt-title-${initiatorMessage.id}`}
            onClick={() => setShowHiddenPrompt(false)}
          >
            <div
              className="modal-content hidden-prompt-modal"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <h2 id={`hidden-prompt-title-${initiatorMessage.id}`}>Full prompt</h2>
                <button
                  type="button"
                  className="modal-close"
                  onClick={() => setShowHiddenPrompt(false)}
                  aria-label="Close"
                >
                  ×
                </button>
              </div>
              <div className="modal-body hidden-prompt-modal-body">
                <pre>{hiddenPrompt}</pre>
              </div>
            </div>
          </div>
        )}
        {initiatorBodyCollapsed && (
          <button
            type="button"
            className="message-box-collapsed-body"
            onClick={() => setPromptCollapsed(false)}
          >
            {firstLineSummary(rawInitiatorContent)}
          </button>
        )}
        {!initiatorBodyCollapsed && (() => {
          const hasArtificial = hasArtificialSections(rawInitiatorContent);
          return (
            <div className="message-box-body">
              <UserImages images={initiatorMessage.images} sessionId={sessionId} />
              <UserFiles files={initiatorMessage.files} />
              {isEditingInitiator ? (
                <div className="alter-user-message-editor">
                  <textarea
                    className="alter-user-message-textarea"
                    value={initiatorEditDraft}
                    rows={Math.min(10, Math.max(3, initiatorEditDraft.split("\n").length))}
                    onChange={(event) => setInitiatorEditDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") {
                        setInitiatorEditDraft(rawInitiatorContent);
                        setIsEditingInitiator(false);
                      }
                      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                        event.preventDefault();
                        void submitInitiatorAlter();
                      }
                      if (isSaveShortcutEvent(event)) {
                        event.preventDefault();
                        void submitInitiatorAlter();
                      }
                    }}
                  />
                  <div className="alter-user-message-actions">
                    <button
                      type="button"
                      className="alter-user-message-cancel"
                      onClick={() => {
                        setInitiatorEditDraft(rawInitiatorContent);
                        setIsEditingInitiator(false);
                      }}
                    >
                      {t("message.cancelAlterButton")}
                    </button>
                    <button
                      type="button"
                      className="alter-user-message-save"
                      onClick={() => void submitInitiatorAlter()}
                      disabled={!initiatorEditDraft.trim()}
                    >
                      {t("message.saveAlterButton")}
                    </button>
                  </div>
                </div>
              ) : hasArtificial ? (
                <UserContentSegments
                  segments={parseArtificialSections(rawInitiatorContent)}
                  onFileClick={onFileClick}
                />
              ) : (
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={markdownLinkifyComponents(onFileClick)}
                  urlTransform={(url) => url}
                >
                  {rawInitiatorContent}
                </ReactMarkdown>
              )}
              <MessageStatus
                status={
                  isRunning || initiatorErrorRendersWithResponse
                    ? undefined
                    : initiatorMessage.status
                }
                errorText={initiatorMessage.errorText}
                onRetry={onRetry ? () => onRetry(initiatorMessage) : undefined}
              />
              {(() => {
                const ts = fmtTime(initiatorMessage.timestamp);
                if (!ts) return null;
                return (
                  <div className="message-box-footer">
                    <span className="user-message-time">{ts}</span>
                  </div>
                );
              })()}
            </div>
          );
        })()}
        {/* Unanchored runs (no target_message_id) — these are
            in-flight turns that haven't yet lazy-created their
            assistant message. Render the badge directly under the
            user bubble so the user sees "agent is starting" the
            moment they hit send, before any delta lands. */}
        {(() => {
          const unanchored = (runs ?? []).filter(isUnanchoredRun);
          if (unanchored.length === 0) return null;
          return (
            <div className="message-box-body">
              <RunBadgeStack runs={unanchored} sessionId={sessionId} />
            </div>
          );
        })()}
      </div>
      )}
      {responseCollapsed && !isAskFlowTurn && (collapsedResponseErrorText || collapsedLastEvent || collapsedSteerPrompts.length > 0 || summary || effectiveResponse?.stopped_at) && (
        <div
          className="turn-group-children"
          data-message-id={effectiveResponse?.id ?? initiatorMessage.id}
        >
          {collapsedResponseErrorText && (
            <>
              <MessageStatus
                status="error"
                errorText={collapsedResponseErrorText}
                onRetry={
                  effectiveResponse && onRetryStopped
                    ? () => onRetryStopped(effectiveResponse)
                    : onRetry
                      ? () => onRetry(initiatorMessage)
                      : undefined
                }
              />
              {(() => {
                const ts = fmtTime(effectiveResponse?.timestamp);
                if (!ts) return null;
                return (
                  <div className="message-box-footer">
                    <span className="user-message-time">{ts}</span>
                  </div>
                );
              })()}
            </>
          )}
          {collapsedSteerPrompts.length > 0 && collapsedSteerPrompts}
          {collapsedLastEvent ? (
            <>
              <div className="collapse-ellipsis">{COLLAPSE_ELLIPSIS}</div>
              {collapsedLastEvent}
            </>
          ) : summary ? (
            <div className="collapse-summary">{summary}</div>
          ) : null}
          {effectiveResponse?.stopped_at && (
            <StoppedIndicator
              stoppedAt={effectiveResponse.stopped_at}
              interrupted={!!effectiveResponse.interrupted_by_msg_id}
              onRetry={
                onRetryStopped ? () => onRetryStopped(effectiveResponse) : undefined
              }
            />
          )}
          {initiatorErrorRendersWithResponse && (
            <MessageStatus
              status="error"
              errorText={initiatorMessage.errorText}
              onRetry={onRetry ? () => onRetry(initiatorMessage) : undefined}
            />
          )}
        </div>
      )}
      {!responseCollapsed && !isAskFlowTurn && (responseMessage || (childTurnGroups && childTurnGroups.length > 0)) && (
        <div className="turn-group-children">
          {responseMessage && (
            <AssistantMessage
              message={responseMessage}
              sessionId={sessionId}
              onFileClick={onFileClick}
              onViewDiff={onViewDiff}
              onRetry={onRetry ? () => onRetry(initiatorMessage) : undefined}
              onRetryStopped={
                onRetryStopped ? () => onRetryStopped(responseMessage) : undefined
              }
              onContinueRateLimitOnAnotherProvider={
                responseMessage.retrying_until && onContinueRateLimitOnAnotherProvider
                  ? () => onContinueRateLimitOnAnotherProvider(responseMessage)
                  : undefined
              }
              threadColorMap={threadColorMap}
              tags={responseTags.length > 0 ? responseTags : undefined}
              advSyncOverlays={
                responseOverlays.length > 0 ? responseOverlays : undefined
              }
              onAdvSyncClick={onAdvSyncClick}
              orchestrationMode={orchestrationMode}
              containerRef={responseContainerRef}
              relabelManagerAsWorker={false}
              runs={responseRuns}
              loadPhase={loadPhase ?? undefined}
              initiatorMessageId={initiatorMessage.id}
              lazyFetchedMessage={lazyFetchedResponse}
              onLazyFetchedMessage={rememberLazyFetchedResponse}
            />
          )}
          {initiatorErrorRendersWithResponse && (
            <MessageStatus
              status="error"
              errorText={initiatorMessage.errorText}
              onRetry={onRetry ? () => onRetry(initiatorMessage) : undefined}
            />
          )}
          {childTurnGroups?.map((sg) => (
              <div key={sg.initiator.id} className="worker-sub-group">
                <div className="worker-instruction" data-message-id={sg.initiator.id}>
                  <span className="worker-tag">Worker</span>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownLinkifyComponents()}>
                    {sessionMarkersToMarkdown(sg.initiator.content)}
                  </ReactMarkdown>
                  {sg.initiator.parent_id && (
                    <button
                      type="button"
                      className="jump-to-parent-btn"
                      title="Jump to parent"
                      onClick={() => {
                        const el = document.getElementById(`msg-${sg.initiator.parent_id!}`);
                        if (el) jumpToParentEl(el);
                      }}
                    >
                      <span className="collapse-arrow"><Icon name="chevron-up" size={12} /></span>
                    </button>
                  )}
                </div>
                {sg.response && (
                  <AssistantMessage
                    message={sg.response}
                    sessionId={sessionId}
                    onFileClick={onFileClick}
                    onViewDiff={onViewDiff}
                    threadColorMap={threadColorMap}
                    orchestrationMode={orchestrationMode}
                    relabelManagerAsWorker
                    runs={runsByTargetId.get(sg.response.id) ?? EMPTY_RUNS}
                    initiatorMessageId={sg.initiator.id}
                    lazyFetchedMessage={lazyFetchedChildResponses[sg.response.id] ?? null}
                    onLazyFetchedMessage={(entry) =>
                      rememberLazyFetchedChildResponse(sg.response!.id, entry)
                    }
                  />
                )}
              </div>
            ))}
        </div>
      )}
      <ModelSwitchBoundaryEvents
        events={trailingModelSwitchEvents}
        testId="model-switch-trailing"
      />
      </div>
    </motion.div>
  );
}

export const TurnGroup = memo(TurnGroupImpl, turnGroupPropsEqual);

/** Render a list of segments produced by `parseArtificialSections`.
 *  Text segments → ReactMarkdown. Tag segments → ArtificialSectionChip,
 *  except `<user_prompt>` whose inner is the user's real text (unwrapped
 *  and recursed inline). */
function UserContentSegments({
  segments,
  onFileClick,
}: {
  segments: Segment[];
  onFileClick?: (path: string) => void;
}) {
  return (
    <>
      {segments.map((seg, i) => {
        if (seg.kind === "text") {
          if (!seg.text.trim()) return null;
          return (
            <ReactMarkdown
              key={i}
              remarkPlugins={[remarkGfm]}
              components={markdownLinkifyComponents(onFileClick)}
              urlTransform={(url) => url}
            >
              {seg.text}
            </ReactMarkdown>
          );
        }
        if (seg.tag === UNWRAP_TAG) {
          return (
            <UserContentSegments
              key={i}
              segments={parseArtificialSections(seg.inner)}
              onFileClick={onFileClick}
            />
          );
        }
        return (
          <ArtificialSectionChip
            key={i}
            tag={seg.tag}
            attrs={seg.attrs}
            inner={seg.inner}
            onFileClick={onFileClick}
          />
        );
      })}
    </>
  );
}

/** Collapsible chip for a single artificial section (e.g. <system-reminder>,
 *  <verdict-prompt>, <file-comment>). Title = pretty tag label + a hint
 *  from attributes when meaningful (e.g. `role="supportive"`). Body
 *  recursively renders nested allowed tags as sub-chips. */
function ArtificialSectionChip({
  tag,
  attrs,
  inner,
  onFileClick,
}: {
  tag: string;
  attrs: Record<string, string>;
  inner: string;
  onFileClick?: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(tag === "inline-tags");
  const label = prettyTagLabel(tag);
  // Surface attribute hints (path, range, role, mode, verb) as part of
  // the title so the collapsed chip is informative without expanding.
  const hintParts: string[] = [];
  for (const k of ["path", "range", "role", "mode", "verb"]) {
    const v = attrs[k];
    if (v) hintParts.push(k === "path" ? v : `${k}=${v}`);
  }
  const hint = hintParts.join(" ");
  const preview = expanded ? "" : tagPreview(inner);
  const segments = parseArtificialSections(inner);
  return (
    <div
      className={`artificial-section-chip artificial-section-${tag}${
        expanded ? " expanded" : ""
      }`}
    >
      <button
        type="button"
        className="artificial-section-header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="artificial-section-caret">{expanded ? "▾" : "▸"}</span>
        <span className="artificial-section-label">{label}</span>
        {hint && <span className="artificial-section-hint">{hint}</span>}
        {!expanded && preview && (
          <span className="artificial-section-preview">{preview}</span>
        )}
      </button>
      {expanded && (
        <div className="artificial-section-body">
          {tag === "inline-tags" ? (
            <InlineTagsCards body={inner} onFileClick={onFileClick} />
          ) : (
            <UserContentSegments
              segments={segments}
              onFileClick={onFileClick}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** Render the contents of an `<inline-tags>` artificial section as comment cards. */
function InlineTagsCards({
  body,
  onFileClick,
}: {
  body: string;
  onFileClick?: (path: string) => void;
}) {
  const comments = useMemo(() => parseInlineTagsBody(body), [body]);
  if (comments.length === 0) {
    return (
      <UserContentSegments
        segments={parseArtificialSections(body)}
        onFileClick={onFileClick}
      />
    );
  }
  return (
    <div className="inline-tags-cards">
      {comments.map((c, i) => {
        const fileHeader =
          c.file && c.range ? `${c.file}:${c.range}` :
          c.file ? c.file : null;
        return (
          <div key={i} className="comment-card inline-tags-card">
            {fileHeader && (
              <div
                className="inline-tags-card-anchor"
                onClick={() => onFileClick && c.file && onFileClick(c.file)}
                role={onFileClick ? "button" : undefined}
              >
                {fileHeader}
              </div>
            )}
            {c.selected && (
              <div className="inline-tags-card-selected">{c.selected}</div>
            )}
            <div className="comment-card-comment">{c.comment}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Compact collapsible chip for synthetic prompts that the supervisor
 *  verdict loop injects (adversarial prompt sent to the supervisor,
 *  verdict-reasoning fed back to the worker). Renders a one-line
 *  preview by default; click to expand the full text.
 */
function SyntheticPromptChip({
  message,
  kind,
}: {
  message: ChatMessage;
  kind: "supervisor" | "worker";
}) {
  const [expanded, setExpanded] = useState(false);
  const content = typeof message.content === "string" ? message.content : "";
  const firstLine = content.split("\n", 1)[0] ?? "";
  const preview =
    firstLine.length > 96 ? firstLine.slice(0, 93) + "…" : firstLine;
  const label =
    kind === "supervisor" ? "Supervisor prompt" : "Verdict relay";
  return (
    <div
      className={`message synthetic-prompt-chip synthetic-prompt-${kind}${
        expanded ? " expanded" : ""
      }`}
      data-message-id={message.id}
    >
      <button
        type="button"
        className="synthetic-prompt-header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="synthetic-prompt-caret">{expanded ? "▾" : "▸"}</span>
        <span className="synthetic-prompt-label">{label}</span>
        {!expanded && (
          <span className="synthetic-prompt-preview">{preview}</span>
        )}
      </button>
      {expanded && (
        <div className="synthetic-prompt-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownLinkifyComponents()}>{sessionMarkersToMarkdown(content)}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

/** Standalone assistant message (for streaming before pairing) */
export function MessageBubble({ message, sessionId, userDisplayName, onFileClick, onViewDiff, onRetry, threadColorMap, orchestrationMode, runs = [] }: Props) {
  if (message.role === "user") {
    // Synthetic prompts injected by the supervisor verdict loop carry
    // `source` ∈ {"supervisor", "worker"}. These are programmatic
    // adversarial prompts containing embedded worker output as
    // context — verbose and confusing if rendered as a full user
    // bubble. Render them as a collapsible chip so the verdict /
    // worker response remains the visual focus.
    const isSyntheticPrompt =
      message.source === "supervisor" || message.source === "worker";
    if (isSyntheticPrompt) {
      return (
        <SyntheticPromptChip
          message={message}
          kind={message.source as "supervisor" | "worker"}
        />
      );
    }
    const rawContent =
      typeof message.content === "string" ? message.content : "";
    const hasArtificial = hasArtificialSections(rawContent);
    return (
      <div className="message user-message" data-message-id={message.id}>
        {message.source && (
          <div className="message-box-header standalone-user-source">
            <span className="message-box-icon orchestration-icon">
              {turnMessageHeader(message.source).icon}
            </span>
            <span className="message-box-label orchestration-label">
              {turnMessageHeader(message.source, userDisplayName).label}
            </span>
            <TeamMessageFrom message={message} />
          </div>
        )}
        <div className="message-content">
          <UserImages images={message.images} sessionId={sessionId} />
          <UserFiles files={message.files} />
          {hasArtificial ? (
            <UserContentSegments
              segments={parseArtificialSections(rawContent)}
              onFileClick={onFileClick}
            />
          ) : (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={markdownLinkifyComponents(onFileClick)}
              urlTransform={(url) => url}
            >
              {rawContent}
            </ReactMarkdown>
          )}
          <MessageStatus
            status={message.status ?? (runs.length > 0 || (message.isStreaming && !message.stopped_at) ? "running" : undefined)}
            errorText={message.errorText}
          />
          {(() => {
            const t = fmtTime(message.timestamp);
            return t ? (
              <div className="message-box-footer">
                <span className="user-message-time">{t}</span>
              </div>
            ) : null;
          })()}
        </div>
      </div>
    );
  }

  return (
    <AssistantMessage
      message={message}
      sessionId={sessionId}
      onFileClick={onFileClick}
      onViewDiff={onViewDiff}
      onRetry={onRetry}
      threadColorMap={threadColorMap}
      orchestrationMode={orchestrationMode}
      runs={runs}
    />
  );
}
const messageObjectRevisions = new WeakMap<object, number>();
let nextMessageObjectRevision = 1;

function messageObjectRevision(message: object): number {
  const existing = messageObjectRevisions.get(message);
  if (existing) return existing;
  const revision = nextMessageObjectRevision++;
  messageObjectRevisions.set(message, revision);
  return revision;
}
