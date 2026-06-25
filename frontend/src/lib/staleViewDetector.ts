/**
 * Stale-view detector — proactive, real-time detection of divergence
 * between the RENDERED sessions chat panel and the CANONICAL in-memory
 * session state.
 *
 * Background
 * ----------
 * The chat panel is rendered from an in-memory `currentSession` tree that
 * is seeded by REST and then mutated by a stream of WebSocket frames
 * (messages_delta, worker_event, messages_replay, reconcile, …). A large
 * amount of code exists to keep that tree convergent (see the many
 * `[stale-dbg]` logs in useSession/useWebSocket), but bugs in that merge
 * logic surface as a STALE VIEW: the panel shows messages that no longer
 * match the canonical state (missing turns, extra turns, wrong order, or
 * wrong text).
 *
 * There is already an ON-DEMAND validator that an external TestApe browser
 * adapter can invoke (frontend `extractVisibleChatPanelTree` + backend
 * `testape_chat_panel_detector.validate_chat_panel`). This module adds the
 * missing piece: a SELF-CONTAINED comparator the running app can call on
 * itself, continuously, to catch mismatches the moment they happen — no
 * external adapter, no round trip.
 *
 * `compareRenderedTreeToSession` mirrors the backend comparator's rules so
 * a mismatch found here is the same class of mismatch the backend would
 * report:
 *   - the rendered messages in each region must be an in-ORDER SUBSEQUENCE
 *     of the canonical messages for that region's node (virtualization /
 *     scroll / lazy-load legitimately hide messages, so subset is OK;
 *     extra / reordered / unknown ids are NOT),
 *   - roles must match,
 *   - rendered text must be contained in the canonical message's text
 *     (markdown rendering and truncation mean exact equality is wrong).
 *
 * Everything is pure and dependency-light so it is trivially unit-testable
 * and safe to run on a hot path when debug mode is on.
 */

import type { Session, ChatMessage } from "src/types";

export type StaleViewMismatchKind =
  | "extractor_missing"
  | "panel_not_visible"
  | "no_session_snapshot"
  | "no_regions"
  | "region_no_node"
  | "unexpected_message"
  | "missing_from_snapshot"
  | "role_mismatch"
  | "text_mismatch"
  | "out_of_order";

export interface StaleViewMismatch {
  kind: StaleViewMismatchKind;
  detail: string;
  region_index?: number;
  session_id?: string | null;
  message_id?: string | null;
}

export interface StaleViewReport {
  ok: boolean;
  checked_at: string;
  session_id: string | null;
  region_count: number;
  rendered_message_count: number;
  mismatches: StaleViewMismatch[];
  /** True when the check was skipped (e.g. streaming in flight) rather
   * than run — callers must not treat a skip as "ok". */
  skipped?: boolean;
  skip_reason?: string;
}

/* ----------------------------- Rendered tree ---------------------------- */
/* These mirror the shape produced by testapeConsumer.extractVisibleChatPanelTree. */

export interface RenderedMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
}

export interface RenderedRegion {
  kind: "linear" | "fork_shared" | "fork_pane";
  session_id: string | null;
  focused?: boolean;
  messages: RenderedMessage[];
}

export interface RenderedTree {
  visible: boolean;
  session_id: string | null;
  title: string | null;
  regions: RenderedRegion[];
}

/* ------------------------------- Helpers -------------------------------- */

function normalizeText(value: string | null | undefined): string {
  return (value ?? "").split(/\s+/).filter(Boolean).join(" ");
}

function stripMarkdown(value: string): string {
  let text = value.replace(/`([^`]*)`/g, "$1");
  text = text.replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  text = text.replace(/[*_~>#-]+/g, " ");
  return text;
}

/** Candidate display texts for a canonical message: the raw content and a
 * markdown-stripped variant, plus any assistant event text. The rendered
 * DOM text must be a substring of one of these. */
export function messageTextCandidates(message: ChatMessage): string[] {
  const out: string[] = [];
  const push = (raw: string | null | undefined) => {
    if (!raw) return;
    const normalized = normalizeText(raw);
    const stripped = normalizeText(stripMarkdown(raw));
    for (const c of [normalized, stripped]) {
      if (c && !out.includes(c)) out.push(c);
    }
  };
  push(typeof message.content === "string" ? message.content : "");
  // Assistant text also lives inside events (agent_message frames).
  for (const ev of message.events || []) {
    const data = (ev as unknown as { data?: unknown }).data;
    if (data && typeof data === "object") {
      const d = data as Record<string, unknown>;
      const inner = (d.message ?? undefined) as Record<string, unknown> | undefined;
      const content = inner?.content;
      if (typeof content === "string") {
        push(content);
      } else if (Array.isArray(content)) {
        const parts = content
          .map((p) => (p && typeof p === "object" ? (p as Record<string, unknown>).text : undefined))
          .filter((t): t is string => typeof t === "string");
        if (parts.length) push(parts.join(" "));
      }
    }
  }
  return out;
}

interface ExpectedMessage {
  id: string;
  role: "user" | "assistant";
  texts: string[];
}

function collectNodesById(root: Session): Map<string, Session> {
  const out = new Map<string, Session>();
  const visit = (node: Session) => {
    // Only "user"-kind nodes are addressable roots/forks in the panel;
    // this mirrors the backend `_nodes_by_id`.
    const kind = (node as unknown as { kind?: string }).kind ?? "user";
    if (node.id && kind === "user") out.set(node.id, node);
    for (const child of node.forks || []) visit(child);
  };
  visit(root);
  return out;
}

function seqOf(message: ChatMessage): number | null {
  return typeof message.seq === "number" ? message.seq : null;
}

function messageEntries(
  node: Session,
  include?: (m: ChatMessage) => boolean,
): ExpectedMessage[] {
  const out: ExpectedMessage[] = [];
  for (const m of node.messages || []) {
    if (include && !include(m)) continue;
    if ((m.role === "user" || m.role === "assistant") && m.id) {
      out.push({ id: m.id, role: m.role, texts: messageTextCandidates(m) });
    }
  }
  return out;
}

function earliestForkPoint(root: Session): number | null {
  let earliest: number | null = null;
  const visit = (node: Session) => {
    const kind = (node as unknown as { kind?: string }).kind ?? "user";
    if (kind !== "user") return;
    const fp = node.fork_point_seq;
    if (typeof fp === "number" && (earliest === null || fp < earliest)) {
      earliest = fp;
    }
    for (const child of node.forks || []) visit(child);
  };
  visit(root);
  return earliest;
}

function expectedMessagesForRegion(
  region: RenderedRegion,
  currentSessionId: string | null,
  root: Session,
  nodes: Map<string, Session>,
): ExpectedMessage[] | null {
  const kind = region.kind || "linear";
  if (kind === "fork_shared") {
    const forkPoint = earliestForkPoint(root);
    return messageEntries(root, (m) => {
      const s = seqOf(m);
      return forkPoint === null || s === null || s <= forkPoint;
    });
  }
  const sessionId = region.session_id || currentSessionId || root.id;
  const node = nodes.get(sessionId || "");
  if (!node) return null;
  if (kind === "fork_pane") {
    const forkPoint = earliestForkPoint(root);
    return messageEntries(node, (m) => {
      const s = seqOf(m);
      return forkPoint === null || (s !== null && s > forkPoint);
    });
  }
  return messageEntries(node);
}

function findMessageIndex(
  messages: ExpectedMessage[],
  messageId: string,
  start: number,
): number | null {
  for (let i = start; i < messages.length; i++) {
    if (messages[i].id === messageId) return i;
  }
  return null;
}

function compareRegion(
  index: number,
  region: RenderedRegion,
  expected: ExpectedMessage[],
): StaleViewMismatch[] {
  const out: StaleViewMismatch[] = [];
  const expectedById = new Map(expected.map((m) => [m.id, m]));
  let cursor = 0;
  region.messages.forEach((item, renderedIndex) => {
    const messageId = item.id || null;
    if (!messageId) {
      out.push({
        kind: "unexpected_message",
        region_index: index,
        session_id: region.session_id,
        detail: `region ${index} message ${renderedIndex} has no id`,
      });
      return;
    }
    const exp = expectedById.get(messageId);
    if (!exp) {
      out.push({
        kind: "unexpected_message",
        region_index: index,
        session_id: region.session_id,
        message_id: messageId,
        detail: `region ${index} rendered a message (${messageId}) that is not in the canonical session snapshot`,
      });
      return;
    }
    if (item.role !== exp.role) {
      out.push({
        kind: "role_mismatch",
        region_index: index,
        session_id: region.session_id,
        message_id: messageId,
        detail: `region ${index} message ${messageId} role ${item.role} != canonical ${exp.role}`,
      });
    }
    const renderedText = normalizeText(item.text);
    if (renderedText && exp.texts.length && !exp.texts.some((t) => t.includes(renderedText) || renderedText.includes(t))) {
      out.push({
        kind: "text_mismatch",
        region_index: index,
        session_id: region.session_id,
        message_id: messageId,
        detail: `region ${index} message ${messageId} rendered text does not match canonical content`,
      });
    }
    const next = findMessageIndex(expected, messageId, cursor);
    if (next === null) {
      out.push({
        kind: "out_of_order",
        region_index: index,
        session_id: region.session_id,
        message_id: messageId,
        detail: `region ${index} message ${messageId} is out of canonical order`,
      });
    } else {
      cursor = next + 1;
    }
  });
  return out;
}

/**
 * Compare a rendered chat-panel tree against the canonical in-memory
 * session. Pure; safe to unit test. Returns a structured report.
 *
 * `now` is injectable for deterministic tests.
 */
export function compareRenderedTreeToSession(
  tree: RenderedTree | null,
  session: Session | null,
  now: () => Date = () => new Date(),
): StaleViewReport {
  const checkedAt = now().toISOString();
  const base: Omit<StaleViewReport, "ok" | "mismatches"> = {
    checked_at: checkedAt,
    session_id: tree?.session_id ?? session?.id ?? null,
    region_count: tree?.regions?.length ?? 0,
    rendered_message_count:
      tree?.regions?.reduce((n, r) => n + (r.messages?.length ?? 0), 0) ?? 0,
  };
  const fail = (m: StaleViewMismatch[]): StaleViewReport => ({
    ...base,
    ok: m.length === 0,
    mismatches: m,
  });

  if (!tree) {
    return fail([{ kind: "extractor_missing", detail: "chat panel extractor returned no tree" }]);
  }
  if (tree.visible !== true) {
    // Not an error — the panel simply is not showing (e.g. sidebar-only
    // view). Report as a skip so callers don't alarm.
    return { ...base, ok: true, mismatches: [], skipped: true, skip_reason: "panel not visible" };
  }
  if (!session) {
    return fail([{ kind: "no_session_snapshot", detail: "no canonical session snapshot available" }]);
  }
  const regions = tree.regions;
  if (!Array.isArray(regions) || regions.length === 0) {
    return fail([{ kind: "no_regions", detail: "chat panel has no rendered regions" }]);
  }

  const nodes = collectNodesById(session);
  const mismatches: StaleViewMismatch[] = [];
  regions.forEach((region, index) => {
    const expected = expectedMessagesForRegion(region, tree.session_id, session, nodes);
    if (expected === null) {
      mismatches.push({
        kind: "region_no_node",
        region_index: index,
        session_id: region.session_id,
        detail: `region ${index} has no matching session node for ${String(region.session_id)}`,
      });
      return;
    }
    mismatches.push(...compareRegion(index, region, expected));
  });
  return fail(mismatches);
}

/** True if any region shows a streaming/in-flight assistant turn — the
 * canonical tree is legitimately mid-mutation, so we should skip the
 * check to avoid false positives. */
export function sessionIsStreaming(session: Session | null): boolean {
  if (!session) return false;
  const anyStreaming = (node: Session): boolean => {
    if (node.messages?.some((m) => m.role === "assistant" && (m.isStreaming || m.isRecovering))) {
      return true;
    }
    return (node.forks || []).some(anyStreaming);
  };
  return anyStreaming(session);
}
