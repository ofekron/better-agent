import { useState, useCallback, useEffect, useRef } from "react";
import type {
  OpenFilePanel,
  OrchestrationMode,
  ProviderRunner,
  RunInfo,
  Session,
  SessionProcessingUpdate,
  ChatMessage,
  CapabilityContext,
  WSEvent,
} from "../types";
import type { InlineTag } from "../types/inlineTag";
import { applyLiveTurnEvent } from "../utils/applyLiveTurnEvent";
import { startOp, completeOp, failOp } from "../progress/store";
import { fetchWithTimeout, responseError } from "src/utils/offlineRequest";

import { API } from "../api";
import { useLocalStorage } from "./useLocalStorage";
import { sortSessionsForList } from "../lib/sessionSort";
import { sessionRegistry, statusRankForRow } from "../lib/sessionRegistry";
import { subscribeMany } from "../lib/eventBus";
import {
  applyOlderMessagePage,
  parseOlderMessagePage,
} from "../lib/messagePagination";
import { SingleFlight } from "../lib/singleFlight";

export { sortSessionsForList };

export type SessionProcessingState = {
  epoch: string | null;
  revision: number;
  roots: Record<string, boolean>;
};

export function reduceSessionProcessing(
  previous: SessionProcessingState,
  update: SessionProcessingUpdate,
): SessionProcessingState {
  if (previous.epoch === update.epoch && update.revision < previous.revision) return previous;
  return {
    epoch: update.epoch,
    revision: update.revision,
    roots: Object.fromEntries(update.rootIds.map((rootId) => [rootId, true])),
  };
}

export interface CreateSessionOptions {
  name: string;
  model: string;
  cwd: string;
  orchestrationMode?: OrchestrationMode;
  browserHarnessEnabled?: boolean;
  providerId?: string;
  browserHarnessHeadless?: boolean;
  fileEditEnabled?: boolean;
  fileEditPath?: string;
  nodeId?: string;
  reasoningEffort?: string;
  runner?: ProviderRunner;
  permission?: Record<string, string>;
  clientSessionId?: string;
  capabilityContexts?: CapabilityContext[];
  folderId?: string | null;
  preset?: string;
}

export type SessionMetadataPatch = {
  inline_tags?: InlineTag[];
  adv_sync_overlays?: Session["adv_sync_overlays"];
  open_file_panels?: OpenFilePanel[];
  open_config_panels?: import("../types").OpenConfigPanel[];
  draft_input?: string;
  draft_images?: import("../types").PastedImage[];
  draft_input_seq?: number;
  fork_closed?: boolean;
  model?: string;
  reasoning_effort?: string;
  cwd?: string;
  provider_id?: string;
  permission?: Session["permission"];
  supervisor_enabled?: boolean;
  supervisor_custom_prompt?: string;
  pinned?: boolean;
  topbar_pinned?: boolean;
  topbar_pinned_at?: string | null;
  archived?: boolean;
  worker_eligible?: boolean;
  agent_rename_allowed?: boolean;
  working_mode?: Session["working_mode"];
  working_mode_meta?: Session["working_mode_meta"];
  notes?: import("../types").Note[];
  current_todos?: import("../types").TodoItem[];
  current_tasks?: import("../types").TaskItem[];
  messages?: ChatMessage[];
  message_count?: number;
  updated_at?: string;
  last_user_prompt_at?: string;
  last_opened_at?: string;
  pagination?: Session["pagination"];
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
  right_panel_auto_opened_by?: Session["right_panel_auto_opened_by"];
  sidebar_minimized?: boolean;
};

type SessionMetadataUpdater =
  | SessionMetadataPatch
  | ((session: Session) => SessionMetadataPatch);

type ReconcilePreserveRegistry = Record<string, Record<string, SessionMetadataUpdater>>;

export type SessionListFilters = {
  projectPath?: string;
  search?: string;
  searchFields?: string[];
  showArchived?: boolean;
  fileEditMode?: "any" | "yes" | "no";
  folderIds?: string[];
  folderView?: boolean;
  tagIds?: string[];
  providerIds?: string[];
  modelIds?: string[];
  modes?: string[];
  sources?: string[];
  sortBy?: string;
  /** Status-bucket grouping as the strongest sort key (below empty-new +
   * pinned). Backend-owned (pref `session_status_sort`); the value here
   * mirrors the `/api/sessions` response so the local re-sort matches the
   * backend page order. */
  statusSort?: boolean;
};

function sameStringList(a?: string[], b?: string[]): boolean {
  const left = a ?? [];
  const right = b ?? [];
  if (left.length !== right.length) return false;
  return left.every((value, index) => value === right[index]);
}

/** True only when this list fetch covers the full, unfiltered global
 * session universe (no narrowing filter active). The sessionRegistry is
 * the ALL-projects source of truth for per-project running/unread
 * aggregates, so only a global fetch may `replaceFromRows` (which evicts
 * everything not in the page). A fetch narrowed to one project (or search/
 * tag/folder/etc.) is a subset — replacing from it would wipe every OTHER
 * project's sessions out of the registry, zeroing their aggregate badges
 * until a fresh WS delta happened to re-materialize them. Narrowed fetches
 * therefore `seedFromRows` (fill-only) instead. */
export function isGlobalUnfilteredFetch(f: SessionListFilters): boolean {
  return (
    !f.projectPath &&
    !(f.search ?? "").trim() &&
    !f.showArchived &&
    (f.fileEditMode ?? "any") === "any" &&
    !(f.folderIds?.length) &&
    !(f.tagIds?.length) &&
    !(f.providerIds?.length) &&
    !(f.modelIds?.length) &&
    !(f.modes?.length) &&
    !(f.sources?.length)
  );
}

/** Which locally-held sessions a REPLACE page must PRESERVE rather than
 * evict. A replace page is a point-in-time backend snapshot taken when the
 * fetch was dispatched. The ONLY replace fetch that ever applies is the
 * newest-dispatched one (older in-flight replace fetches are discarded by
 * the `requestSeq !== current` guard on resolve), so a page can be stale
 * only relative to local inserts that happened AFTER that fetch was
 * dispatched. Preserve a `prev` session absent from the page when it is an
 * unacknowledged offline session, or when it was inserted locally after the
 * fetch's dispatch watermark (`insertGenById[id] > dispatchInsertGen`).
 * Everything else absent from the page is a genuine backend removal and is
 * evicted. This closes the race where a new server-confirmed session
 * (e.g. one whose first prompt failed, so no turn-complete refetch re-adds
 * it) is dropped from the sidebar by an in-flight pre-creation list fetch. */
export function preservedLocalSessionsForReplace(
  prev: Session[],
  pageIds: Set<string>,
  opts: { dispatchInsertGen: number; insertGenById: Map<string, number> },
): Session[] {
  return prev.filter((s) => {
    if (pageIds.has(s.id)) return false;
    if (s.offline_pending) return true;
    const gen = opts.insertGenById.get(s.id);
    return gen !== undefined && gen > opts.dispatchInsertGen;
  });
}

function sameSessionListFilters(
  a: SessionListFilters,
  b: SessionListFilters,
): boolean {
  return (
    (a.projectPath ?? "") === (b.projectPath ?? "") &&
    (a.search ?? "") === (b.search ?? "") &&
    sameStringList(a.searchFields, b.searchFields) &&
    Boolean(a.showArchived) === Boolean(b.showArchived) &&
    (a.fileEditMode ?? "any") === (b.fileEditMode ?? "any") &&
    sameStringList(a.folderIds, b.folderIds) &&
    Boolean(a.folderView) === Boolean(b.folderView) &&
    sameStringList(a.tagIds, b.tagIds) &&
    sameStringList(a.providerIds, b.providerIds) &&
    sameStringList(a.modelIds, b.modelIds) &&
    sameStringList(a.modes, b.modes) &&
    sameStringList(a.sources, b.sources) &&
    (a.sortBy ?? "") === (b.sortBy ?? "") &&
    Boolean(a.statusSort) === Boolean(b.statusSort)
  );
}

const SESSION_TREE_CACHE_LIMIT = 20;
const SESSION_LIST_PAGE_SIZE = 50;

/** Return only the user-facing forks of `node` — filters out internal
 * Better Agent sessions like delegate forks (manager-mode per-pair threads).
 * The backend embeds those in the same `forks` array as user-facing
 * forks; the frontend should never render them in the sidebar. */
export function userFacingForks(node: Session): Session[] {
  // INVARIANT: adv_sync_fork is intentionally EXCLUDED here. Those forks
  // exist as embedded children but the user only sees them on demand
  // — `handleAdvSyncClick` opens a separate window (?adv_sync_overlay=…)
  // that renders them via AdvSyncWindow. The default session view stays
  // linear regardless of any in-flight or converged adv-sync runs.
  return (node.forks ?? []).filter((f) => (f.kind ?? "user") === "user");
}

/** Forks that need WS subscriptions. Aliased to `userFacingForks` —
 * the main-window WS subscriptions follow the same set. AdvSyncWindow
 * opens its own connection and subscribes to the two adv-sync forks
 * directly. */
export const wsSubscribableForks = userFacingForks;

function isSidebarVisibleSession(session: Session): boolean {
  return (
    !session.working_mode ||
    (session.working_mode === "file_editing" &&
      session.working_mode_meta?.persistent === true)
  );
}

function canLocallyInsertIntoSessionList(
  session: Session,
  filters: SessionListFilters,
): boolean {
  if (!isSidebarVisibleSession(session)) return false;
  // Mirrors backend session_matches_project: all_projects sessions (e.g. the
  // assistant singleton) belong to every project regardless of cwd.
  if (
    filters.projectPath &&
    !session.all_projects &&
    session.cwd !== filters.projectPath
  )
    return false;
  if (filters.search?.trim()) return false;
  if (!filters.showArchived && session.archived) return false;
  if (filters.fileEditMode && filters.fileEditMode !== "any") return false;
  if (filters.folderIds?.length) return false;
  if (filters.tagIds?.length) return false;
  if (filters.providerIds?.length) return false;
  if (filters.modelIds?.length) return false;
  if (filters.modes?.length) return false;
  if (filters.sources?.length) return false;
  return true;
}

/** Return the two forks bound to an adv-sync overlay, in
 * (supportive, adversarial) order. Used by AdvSyncWindow. Returns
 * empty array if either fork is missing from the tree (e.g. one was
 * deleted out from under the overlay). */
export function advSyncForksFor(
  tree: Session,
  overlay: { supportive_fork_id: string; adversarial_fork_id: string },
): Session[] {
  const byId = new Map<string, Session>();
  const visit = (n: Session) => {
    byId.set(n.id, n);
    for (const f of n.forks ?? []) visit(f);
  };
  visit(tree);
  const s = byId.get(overlay.supportive_fork_id);
  const a = byId.get(overlay.adversarial_fork_id);
  return s && a ? [s, a] : [];
}

/** Count total events on a message — primary msg.events plus all
 * worker panel events. Used by mergeReplayIntoNode's streaming
 * protection guard so manager-mode messages (where events live in
 * workers[].events) aren't incorrectly considered "empty". */
function totalEventCount(msg: ChatMessage): number {
  let n = msg.events?.length ?? 0;
  if (msg.workers) {
    for (const w of msg.workers) {
      n += w.events?.length ?? 0;
    }
  }
  return n;
}

export function mergeProjectedMessageDelta(
  current: ChatMessage,
  incoming: ChatMessage,
): ChatMessage {
  if (!incoming.omitted_payloads?.events) return incoming;
  const next: ChatMessage = { ...incoming };
  if (incoming.events === undefined && current.events !== undefined) {
    next.events = current.events;
  }
  if (incoming.workers && current.workers) {
    const currentWorkers = new Map(
      current.workers.map((worker) => [worker.delegation_id, worker]),
    );
    next.workers = incoming.workers.map((worker) => {
      const currentWorker = currentWorkers.get(worker.delegation_id);
      if (!currentWorker || worker.events !== undefined) return worker;
      return { ...worker, events: currentWorker.events };
    });
  }
  return next;
}

export function mergeIncomingMessageSnapshot(
  current: ChatMessage,
  incoming: ChatMessage,
): ChatMessage | null {
  if (
    current.role === "assistant" &&
    current.isStreaming &&
    incoming.role === "assistant"
  ) {
    const replayEvents = totalEventCount(incoming);
    const liveEvents = totalEventCount(current);
    if (incoming.isStreaming === true && replayEvents <= liveEvents) {
      return null;
    }
    if (incoming.isStreaming !== true) {
      if (incoming.omitted_payloads?.events) {
        return mergeProjectedMessageDelta(current, incoming);
      }
      const replayText = incoming.content ?? "";
      const liveText = current.content ?? "";
      const replayTextLen = replayText.length;
      const liveTextLen = liveText.length;
      const replayContinuesLiveText =
        liveTextLen > 0 &&
        replayTextLen >= liveTextLen &&
        replayText.startsWith(liveText);
      const incomingIsTerminal =
        !!incoming.completed_at || !!incoming.stopped_at || !!incoming.error;
      if (
        replayEvents <= liveEvents &&
        !replayContinuesLiveText &&
        !incomingIsTerminal
      ) {
        return null;
      }
    }
  }
  return mergeProjectedMessageDelta(current, incoming);
}

/** Walk the tree rooted at `tree` and apply `mutate` to the node whose
 * id matches `sessionId`. Returns a new tree (sharing untouched
 * subtrees) if a node changed; the same tree reference if no node
 * matched. Used by every WS reducer below to update either the root
 * itself or any embedded fork in one consistent way. */
function updateNodeById(
  tree: Session,
  sessionId: string,
  mutate: (node: Session) => Session
): Session {
  if (tree.id === sessionId) return mutate(tree);
  const forks = tree.forks;
  if (!forks || forks.length === 0) return tree;
  let changed = false;
  const next: Session[] = forks.map((f) => {
    const r = updateNodeById(f, sessionId, mutate);
    if (r !== f) changed = true;
    return r;
  });
  return changed ? { ...tree, forks: next } : tree;
}

/** Find a node by id anywhere in the tree, or null. Read-only. */
function findNode(tree: Session, sessionId: string): Session | null {
  if (tree.id === sessionId) return tree;
  for (const f of tree.forks ?? []) {
    const hit = findNode(f, sessionId);
    if (hit) return hit;
  }
  return null;
}

/** Resolve which existing assistant message a live WS turn-event should
 * be applied to, or -1 if none matches (caller then spawns a streaming
 * placeholder). Pure so the routing rule is unit-testable.
 *
 * Order: the frame's owning `msg_id` (authoritative — annotated by the
 * wire tailer from events.jsonl) → the active run's target_message_id →
 * the last streaming assistant. Routing by `msg_id` first keeps a LATE
 * event — one the provider re-emits AFTER its turn completed and the run
 * was cleared — on its real, finalized message instead of spawning a
 * duplicate placeholder bubble (or grafting onto a newer turn). */
export function resolveLiveEventTargetIndex(
  msgs: ChatMessage[],
  event: WSEvent,
  activeRunTargetId: string | null,
): number {
  const frameMsgId = (event.data as { msg_id?: string } | undefined)?.msg_id;
  if (frameMsgId) {
    const idx = msgs.findIndex(
      (m) => m.role === "assistant" && m.id === frameMsgId,
    );
    if (idx !== -1) return idx;
  }
  if (activeRunTargetId) {
    const idx = msgs.findIndex(
      (m) => m.role === "assistant" && m.id === activeRunTargetId,
    );
    if (idx !== -1) return idx;
  }
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant" && msgs[i].isStreaming) return i;
  }
  return -1;
}

/** Extract the canonical event UUID from a live WS turn-event, handling
 *  the direct agent_message shape and the legacy manager_event wrapper. */
export function extractLiveEventUuid(event: WSEvent): string | undefined {
  const d = event.data as Record<string, unknown> | undefined;
  if (!d) return undefined;
  if (typeof d.uuid === "string") return d.uuid;
  const inner = d.event as Record<string, unknown> | undefined;
  if (inner) {
    if (typeof inner.uuid === "string") return inner.uuid;
    const innerData = inner.data as Record<string, unknown> | undefined;
    if (innerData && typeof innerData.uuid === "string") return innerData.uuid;
  }
  return undefined;
}

/** Check whether a finalized assistant message already contains an event
 *  with the given UUID in any of its event stores (primary events or
 *  worker panel events). */
export function messageHasUuid(msg: ChatMessage, uuid: string): boolean {
  const evs = msg.events;
  if (evs) {
    for (const e of evs) if (extractLiveEventUuid(e) === uuid) return true;
  }
  const workers = msg.workers;
  if (workers) {
    for (const w of workers) {
      const wEvs = w.events;
      if (wEvs) {
        for (const e of wEvs) if (extractLiveEventUuid(e) === uuid) return true;
      }
    }
  }
  return false;
}

function isLivePlaceholder(msg: ChatMessage): boolean {
  return (
    msg.role === "assistant" &&
    msg.isStreaming === true &&
    msg.id.startsWith("live-") &&
    typeof msg.seq !== "number"
  );
}

function mergeEventsByUuid(
  placeholderEvents: WSEvent[] | undefined,
  canonicalEvents: WSEvent[] | undefined,
): WSEvent[] | undefined {
  if (!placeholderEvents?.length) return canonicalEvents;
  if (!canonicalEvents?.length) return placeholderEvents;
  const merged = [...placeholderEvents];
  const byUuid = new Map<string, number>();
  merged.forEach((event, index) => {
    const uuid = extractLiveEventUuid(event);
    if (uuid) byUuid.set(uuid, index);
  });
  for (const event of canonicalEvents) {
    const uuid = extractLiveEventUuid(event);
    if (!uuid) {
      merged.push(event);
      continue;
    }
    const existing = byUuid.get(uuid);
    if (existing === undefined) {
      byUuid.set(uuid, merged.length);
      merged.push(event);
    } else {
      merged[existing] = event;
    }
  }
  return merged;
}

function mergePlaceholderWorkers(
  placeholderWorkers: ChatMessage["workers"],
  canonicalWorkers: ChatMessage["workers"],
): ChatMessage["workers"] {
  if (!placeholderWorkers?.length) return canonicalWorkers;
  if (!canonicalWorkers?.length) return placeholderWorkers;
  const merged = placeholderWorkers.map((worker) => ({ ...worker }));
  const byDelegation = new Map<string, number>();
  merged.forEach((worker, index) => {
    if (worker.delegation_id) byDelegation.set(worker.delegation_id, index);
  });
  for (const worker of canonicalWorkers) {
    const idx = worker.delegation_id
      ? byDelegation.get(worker.delegation_id)
      : undefined;
    if (idx === undefined) {
      merged.push(worker);
      if (worker.delegation_id) byDelegation.set(worker.delegation_id, merged.length - 1);
      continue;
    }
    const placeholder = merged[idx];
    merged[idx] = {
      ...placeholder,
      ...worker,
      events: mergeEventsByUuid(placeholder.events, worker.events) ?? [],
    };
  }
  return merged;
}

function shouldAdoptLivePlaceholder(
  placeholder: ChatMessage,
  canonical: ChatMessage,
): boolean {
  if (!isLivePlaceholder(placeholder) || canonical.role !== "assistant") return false;
  for (const event of canonical.events ?? []) {
    const uuid = extractLiveEventUuid(event);
    if (uuid && messageHasUuid(placeholder, uuid)) return true;
  }
  const canonicalDelegations = new Set(
    (canonical.workers ?? [])
      .map((worker) => worker.delegation_id)
      .filter((id): id is string => !!id),
  );
  if (canonicalDelegations.size > 0) {
    for (const worker of placeholder.workers ?? []) {
      if (worker.delegation_id && canonicalDelegations.has(worker.delegation_id)) {
        return true;
      }
    }
  }
  return false;
}

function findLivePlaceholderToAdopt(
  messages: ChatMessage[],
  canonical: ChatMessage,
  canonicalIndex?: number,
): number {
  const sameGroupCandidates: number[] = [];
  for (let i = messages.length - 1; i >= 0; i--) {
    if (i === canonicalIndex) continue;
    const sameGroup = samePromptGroupForAdoption(messages, i, canonical);
    if (sameGroup && isLivePlaceholder(messages[i])) sameGroupCandidates.push(i);
    if (
      shouldAdoptLivePlaceholder(messages[i], canonical) &&
      (sameGroup || typeof canonical.seq !== "number")
    ) {
      return i;
    }
  }
  if (
    canonical.isStreaming === true &&
    totalEventCount(canonical) === 0 &&
    typeof canonical.seq === "number" &&
    sameGroupCandidates.length === 1
  ) {
    return sameGroupCandidates[0];
  }
  return -1;
}

function previousUserSeqForIndex(
  messages: ChatMessage[],
  beforeIndex: number,
): number | null {
  for (let i = beforeIndex - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role === "user" && typeof message.seq === "number") {
      return message.seq;
    }
  }
  return null;
}

function previousUserSeqForCanonical(
  messages: ChatMessage[],
  canonical: ChatMessage,
): number | null {
  if (typeof canonical.seq !== "number") return null;
  let seq: number | null = null;
  for (const message of messages) {
    if (
      message.role === "user" &&
      typeof message.seq === "number" &&
      message.seq < canonical.seq &&
      (seq === null || message.seq > seq)
    ) {
      seq = message.seq;
    }
  }
  return seq;
}

function samePromptGroupForAdoption(
  messages: ChatMessage[],
  placeholderIndex: number,
  canonical: ChatMessage,
): boolean {
  const canonicalUserSeq = previousUserSeqForCanonical(messages, canonical);
  if (canonicalUserSeq === null) return false;
  const placeholderUserSeq = previousUserSeqForIndex(messages, placeholderIndex);
  return placeholderUserSeq === canonicalUserSeq;
}

function adoptLivePlaceholder(
  placeholder: ChatMessage,
  canonical: ChatMessage,
): ChatMessage {
  return {
    ...canonical,
    content: canonical.content || placeholder.content,
    events: mergeEventsByUuid(placeholder.events, canonical.events) ?? [],
    workers: mergePlaceholderWorkers(placeholder.workers, canonical.workers),
  };
}

export function mergeIncomingMessagesForNode(
  existing: ChatMessage[],
  messages: ChatMessage[],
): ChatMessage[] {
  const byId = new Map<string, number>();
  existing.forEach((m, i) => byId.set(m.id, i));
  const merged = [...existing];
  for (const m of messages) {
    const idx = byId.get(m.id);
    let next: ChatMessage | null = m;
    if (idx !== undefined) {
      const ex = merged[idx];
      next = mergeIncomingMessageSnapshot(ex, m);
      if (next === null) continue;
    }
    if (next.role === "assistant") {
      const placeholderIdx = findLivePlaceholderToAdopt(merged, next, idx);
      if (placeholderIdx !== -1) {
        next = adoptLivePlaceholder(merged[placeholderIdx], next);
        merged.splice(placeholderIdx, 1);
        byId.clear();
        merged.forEach((message, index) => byId.set(message.id, index));
      }
    }
    const nextIdx = byId.get(next.id);
    if (nextIdx !== undefined) {
      merged[nextIdx] = next;
    } else {
      byId.set(next.id, merged.length);
      merged.push(next);
    }
  }
  merged.sort((a, b) => {
    const sa =
      typeof a.seq === "number" ? a.seq : Number.MAX_SAFE_INTEGER;
    const sb =
      typeof b.seq === "number" ? b.seq : Number.MAX_SAFE_INTEGER;
    return sa - sb;
  });
  return merged;
}

/** Pure reducer for turn-terminal WS frames (turn_complete /
 * turn_stopped / error) over a node's message list.
 *
 * - Normal terminal: flip `isStreaming` off on the in-flight assistant
 *   and stamp `stopped_at` / `interrupted_by_msg_id`.
 * - Error terminal (`errorText !== undefined`): the backend's exception
 *   path in `_finalize_turn_messages` marks the USER message errored
 *   (`mark_user_error`) AND removes the persisted assistant message,
 *   then emits the `error` frame with NO `client_id` and NO
 *   `messages_delta`. Mirror that here: stamp the turn's initiating user
 *   message with the failure (so the error card renders in-chat instead
 *   of vanishing) and drop the orphaned streaming placeholder (which
 *   would otherwise linger as an empty "No output" bubble that hides
 *   the failure). Any later `messages_delta` / replay re-adds the
 *   canonical message, and the stamped user error matches the persisted
 *   `mark_user_error`, so reload stays consistent.
 *
 * Returns the SAME `msgs` reference when nothing changed. */
export function finalizeTerminalAssistant(
  msgs: ChatMessage[],
  opts: {
    stoppedAt?: string;
    interruptedByMsgId?: string | null;
    errorText?: string;
  },
): ChatMessage[] {
  const lastIdx = msgs.length - 1;
  const last = msgs[lastIdx];
  if (!last || last.role !== "assistant" || !last.isStreaming) return msgs;
  if (opts.errorText !== undefined) {
    // Find the user message that initiated this failed turn (the most
    // recent user message preceding the in-flight assistant).
    let userIdx = -1;
    for (let i = lastIdx - 1; i >= 0; i--) {
      if (msgs[i].role === "user") {
        userIdx = i;
        break;
      }
    }
    const withoutPlaceholder = msgs.slice(0, lastIdx);
    if (userIdx === -1) return withoutPlaceholder;
    const userMsg = withoutPlaceholder[userIdx];
    // A send-level error (client_id present) already marked this via
    // onPromptSendError — don't clobber a richer existing error.
    if (userMsg.status === "error" && userMsg.errorText) {
      return withoutPlaceholder;
    }
    const stamped: ChatMessage = {
      ...userMsg,
      status: "error",
      errorText: opts.errorText,
    };
    return [
      ...withoutPlaceholder.slice(0, userIdx),
      stamped,
      ...withoutPlaceholder.slice(userIdx + 1),
    ];
  }
  const updated: ChatMessage = {
    ...last,
    isStreaming: false,
    isDetached: false,
    ...(opts.stoppedAt ? { stopped_at: opts.stoppedAt } : {}),
    ...(opts.interruptedByMsgId ? { interrupted_by_msg_id: opts.interruptedByMsgId } : {}),
  };
  return [...msgs.slice(0, lastIdx), updated];
}

/** Pure reducer for one live WS turn-event over a node's message list.
 *
 * Routes the event onto the in-flight assistant (via
 * `resolveLiveEventTargetIndex`), applying cross-message UUID dedup so a
 * late replay event for a PRIOR finalized turn is dropped. When no
 * assistant message owns the event yet, a streaming placeholder is
 * spawned — BUT ONLY if the event actually contributes render content.
 * Content-less framing frames (`turn_start`/`turn_complete`, which carry
 * only `agent_session_id`) must NOT mint a placeholder: it would finalize
 * empty and render a phantom collapsed "No output" turn. `newId` is
 * injected so the placeholder id is deterministic in tests.
 *
 * Returns the SAME `msgs` reference when nothing changed. */
export function applyLiveEventToMessages(
  msgs: ChatMessage[],
  event: WSEvent,
  activeRunTargetId: string | null,
  mode: OrchestrationMode | undefined,
  newId: string,
): ChatMessage[] {
  const targetIdx = resolveLiveEventTargetIndex(msgs, event, activeRunTargetId);
  const target = targetIdx >= 0 ? msgs[targetIdx] : undefined;
  if (target && target.role === "assistant") {
    let base = target;

    const evUuid = extractLiveEventUuid(event);
    if (evUuid) {
      for (let mi = 0; mi < msgs.length; mi++) {
        if (mi === targetIdx) continue;
        const m = msgs[mi];
        if (m.role !== "assistant" || m.isStreaming) continue;
        if (messageHasUuid(m, evUuid)) return msgs;
      }
    }

    const nextTarget = applyLiveTurnEvent(base, event, mode);
    if (nextTarget === base) return msgs;
    const nextMessages = [...msgs];
    nextMessages[targetIdx] = nextTarget;
    return nextMessages;
  }

  // No assistant message yet — create a streaming placeholder, but only
  // for events that carry render content. Content-less framing frames
  // (turn_start/turn_complete) would spawn an empty turn that finalizes
  // into a phantom "No output" box.
  const placeholder: ChatMessage = {
    id: newId,
    role: "assistant",
    content: "",
    events: [],
    isStreaming: true,
    timestamp: new Date().toISOString(),
  };
  const applied = applyLiveTurnEvent(placeholder, event, mode);
  // Render-bearing = anything the turn would actually show: primary
  // events, assistant text, OR a worker panel (worker_start adds a panel
  // without touching events/content). turn_start/turn_complete touch only
  // agent_session_id, so they fall through and DON'T spawn a turn.
  const hasRenderContent =
    (applied.events?.length ?? 0) > 0 ||
    !!applied.content ||
    (applied.workers?.length ?? 0) > 0;
  if (!hasRenderContent) return msgs;
  return [...msgs, applied];
}

function treeHasStreamingAssistant(node: Session): boolean {
  if ((node.messages ?? []).some((m) => m.role === "assistant" && m.isStreaming)) {
    return true;
  }
  return (node.forks ?? []).some(treeHasStreamingAssistant);
}

export function useSession(authStatus?: string) {
  const [exchangePageSize] = useLocalStorage("bc_exchange_page_size", 3);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionListFilters, setSessionListFilters] =
    useState<SessionListFilters>({});
  const [sessionsHasMore, setSessionsHasMore] = useState(true);
  const [sessionsLoadingMore, setSessionsLoadingMore] = useState(false);
  const [sessionsSearching, setSessionsSearching] = useState(false);
  // True once the initial /api/sessions response has resolved.
  // Callers (e.g. SessionView's deep-link gate) need to wait until
  // this flips before treating a missing id as "unknown" — without
  // it, the first render incorrectly bounces every direct URL load
  // because `sessions` is the initial empty array.
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [currentSession, setCurrentSession] = useState<Session | null>(null);
  // True while REST fetch for the selected session is in flight.
  // Prevents flash-of-empty-content when switching sessions.
  const [sessionLoading, setSessionLoading] = useState(false);
  // Non-null when the most recent selectSession REST fetch failed
  // (timeout, network, non-OK). Rendered as an error state with a
  // retry so a failed select is never a silent dead click.
  const [sessionLoadError, setSessionLoadError] = useState<{
    sessionId: string;
    message: string;
  } | null>(null);
  // WS subscription target — set ONLY after REST resolves and seq cursors
  // are seeded. Prevents the WS subscribe from firing during the
  // optimistic swap (which has since_seq=0 and events_from_seq=0,
  // causing the backend to flood us with all messages and events).
  const [wsTargetSessionId, setWsTargetSessionId] = useState<string | null>(null);
  const selectRequestIdRef = useRef(0);
  const selectInFlightIdRef = useRef<string | null>(null);
  // Per-session highest seq we have applied locally. Sent as
  // `since_seq` on every WS subscribe so backend can replay only
  // what's new. Ref so reads are always fresh inside callbacks
  // without triggering re-renders / WS resubscribes.
  const lastSeqBySessionRef = useRef<Record<string, number>>({});
  // Watermark cursor for events.jsonl, keyed by app_session_id. Seeded
  // from the REST snapshot's `max_seq_by_sid` and passed back to the
  // backend on every WS subscribe as `events_from_seq` — the backend's
  // wire tailer drains the gap before live events flow.
  const lastEventSeqBySessionRef = useRef<Record<string, number>>({});
  // Pending replay queue: WS messages_replay can arrive before the
  // REST selectSession resolves (C1/C5). We stash them here and
  // flush after the REST tree lands in currentSession.
  const pendingReplayRef = useRef<
    { sessionId: string; messages: ChatMessage[] }[]
  >([]);
  const olderMessageRequestsRef = useRef(new SingleFlight<string>());
  const sessionTreeCacheRef = useRef<Map<string, Session>>(new Map());
  const sessionTreeRootByNodeRef = useRef<Map<string, string>>(new Map());

  const collectTreeNodeIds = useCallback((tree: Session): string[] => {
    const ids: string[] = [];
    const visit = (node: Session) => {
      ids.push(node.id);
      for (const f of wsSubscribableForks(node)) visit(f);
    };
    visit(tree);
    return ids;
  }, []);

  const forgetSessionTree = useCallback((sessionId: string) => {
    const rootId = sessionTreeRootByNodeRef.current.get(sessionId) ?? sessionId;
    const old = sessionTreeCacheRef.current.get(rootId);
    if (old) {
      for (const nodeId of collectTreeNodeIds(old)) {
        sessionTreeRootByNodeRef.current.delete(nodeId);
      }
    }
    sessionTreeCacheRef.current.delete(rootId);
  }, [collectTreeNodeIds]);

  const rememberSessionTree = useCallback((tree: Session) => {
    forgetSessionTree(tree.id);
    const cache = sessionTreeCacheRef.current;
    cache.set(tree.id, tree);
    for (const nodeId of collectTreeNodeIds(tree)) {
      sessionTreeRootByNodeRef.current.set(nodeId, tree.id);
    }
    while (cache.size > SESSION_TREE_CACHE_LIMIT) {
      const oldestRootId = cache.keys().next().value as string | undefined;
      if (!oldestRootId) break;
      forgetSessionTree(oldestRootId);
    }
  }, [collectTreeNodeIds, forgetSessionTree]);

  const cachedSessionTreeFor = useCallback((sessionId: string): Session | null => {
    const rootId = sessionTreeRootByNodeRef.current.get(sessionId);
    if (!rootId) return null;
    const tree = sessionTreeCacheRef.current.get(rootId);
    if (!tree) return null;
    sessionTreeCacheRef.current.delete(rootId);
    sessionTreeCacheRef.current.set(rootId, tree);
    return tree;
  }, []);

  const renameSessionNode = useCallback((node: Session, sessionId: string, name: string): Session => {
    const forks = node.forks;
    let forksChanged = false;
    const nextForks = forks?.map((fork) => {
      const next = renameSessionNode(fork, sessionId, name);
      if (next !== fork) forksChanged = true;
      return next;
    });
    const ownChanged = node.id === sessionId && node.name !== name;
    if (!ownChanged && !forksChanged) return node;
    return {
      ...node,
      ...(ownChanged ? { name } : {}),
      ...(forksChanged ? { forks: nextForks } : {}),
    };
  }, []);

  const updateCachedSessionName = useCallback(
    (sessionId: string, name: string) => {
      const rootId = sessionTreeRootByNodeRef.current.get(sessionId);
      if (!rootId) return;
      const cached = sessionTreeCacheRef.current.get(rootId);
      if (!cached) return;
      const renamed = renameSessionNode(cached, sessionId, name);
      if (renamed === cached) return;
      sessionTreeCacheRef.current.set(rootId, renamed);
    },
    [renameSessionNode],
  );

  // ── session-open latency probe ───────────────────────────────────
  // Measures `click → in-sync-with-tip` per session open. "In sync"
  // is detected via a quiet window: after the REST snapshot resolves,
  // every applied replay or live event for the open sid restarts a
  // 250 ms timer. When the timer fires (no traffic for QUIET_MS), we
  // log `{rest_ms, tip_ms}` to console. Cleared if the user switches
  // to a different session before the timer fires.
  const OPEN_QUIET_MS = 250;
  const openTimingRef = useRef<{
    sid: string;
    t0: number;
    restMs: number | null;
    quietTimer: number | null;
  } | null>(null);
  const clearOpenQuietTimer = () => {
    const t = openTimingRef.current;
    if (t?.quietTimer != null) {
      window.clearTimeout(t.quietTimer);
      t.quietTimer = null;
    }
  };
  const armOpenQuietTimer = () => {
    const t = openTimingRef.current;
    if (!t || t.restMs === null) return;
    if (t.quietTimer != null) window.clearTimeout(t.quietTimer);
    t.quietTimer = window.setTimeout(() => {
      const cur = openTimingRef.current;
      if (!cur) return;
      const tipMs = Math.round(performance.now() - cur.t0);
      // Structured single-line log so it's grep-able from devtools.
      console.info("[perf] session.open", {
        sid: cur.sid,
        rest_ms: cur.restMs,
        tip_ms: tipMs,
      });
      openTimingRef.current = null;
    }, OPEN_QUIET_MS);
  };
  // INVARIANT: must only read REFS, never reactive state. This function
  // is captured inside `useCallback` closures (applyMessagesReplay,
  // applyLiveEvent) and the closures are memoized on first render —
  // a stale closure over reactive state would silently misroute or
  // drop probe events. Refs are always fresh, so this stays safe.
  const markOpenTimingEvent = (sessionId: string) => {
    const t = openTimingRef.current;
    if (!t || t.sid !== sessionId) return;
    armOpenQuietTimer();
  };
  useEffect(() => () => {
    clearOpenQuietTimer();
    openTimingRef.current = null;
  }, []);
  // Per-session list of currently-running CLI runs as reported by
  // the backend's `run_state` event. Backend is the source of truth
  // for "is something running" — frontend just mirrors. Stored as
  // state (not ref) because UI badges read it on render.
  const [runStateBySession, setRunStateBySession] = useState<
    Record<string, RunInfo[]>
  >({});
  const runStateBySessionRef = useRef<Record<string, RunInfo[]>>({});
  // Per-root-id reconcile-in-progress flag. Backend fires
  // `session_processing_started/finished` ONLY when the async
  // reconcile crosses its 0.3s threshold — fast reconciles never
  // touch this state (no UI flash). State (not ref) so badges
  // re-render. Keyed by root_id, NOT app_session_id, because
  // reconcile is per-root-tree.
  const [processingState, setProcessingState] = useState<SessionProcessingState>({
    epoch: null,
    revision: 0,
    roots: {},
  });
  const processingByRoot = processingState.roots;

  // Ref mirrors so callbacks with [] deps can read fresh state without
  // re-creating themselves (selectSession in particular needs to peek
  // at sessions[] / currentSession synchronously to optimistically swap
  // before the REST round-trip resolves — see selectSession below).
  const sessionsRef = useRef<Session[]>([]);
  sessionsRef.current = sessions;
  const sessionListFiltersRef = useRef<SessionListFilters>({});
  sessionListFiltersRef.current = sessionListFilters;
  const sessionsHasMoreRef = useRef(true);
  sessionsHasMoreRef.current = sessionsHasMore;
  const sessionsLoadedRef = useRef(false);
  sessionsLoadedRef.current = sessionsLoaded;
  const sessionsNextOffsetRef = useRef(0);
  const sessionsLoadingPageRef = useRef(false);
  const sessionListRequestSeqRef = useRef(0);
  // Monotonic watermark for local structural inserts into the session
  // list. A replace page dispatched before an insert must not evict that
  // insert — see preservedLocalSessionsForReplace.
  const sessionInsertGenRef = useRef(0);
  const sessionInsertGenByIdRef = useRef<Map<string, number>>(new Map());
  const sessionListFiltersReadyRef = useRef(false);
  const currentSessionRef = useRef<Session | null>(null);
  currentSessionRef.current = currentSession;

  // Single sort entry-point for the sidebar list. Reads folderView/sortBy/
  // statusSort/search from the live filters ref so every call site stays a
  // one-arg call. When status sort is on (and not searching), injects the
  // registry-backed rank; otherwise behaves exactly as the time-only sort.
  const sortForList = useCallback((list: Session[]) => {
    const f = sessionListFiltersRef.current;
    const folderView = f.folderView ?? false;
    const sortBy = f.sortBy ?? "updated_at";
    const searchActive = Boolean(f.search?.trim());
    const rankOf = f.statusSort && !searchActive ? statusRankForRow : undefined;
    return sortSessionsForList(list, folderView, sortBy, rankOf);
  }, []);

  // Stamp a session as locally inserted at the current generation so an
  // older in-flight replace fetch (dispatched before this insert) cannot
  // evict it. Bumps the monotonic generation and records it per-id.
  const markSessionInserted = useCallback((id: string) => {
    sessionInsertGenRef.current += 1;
    sessionInsertGenByIdRef.current.set(id, sessionInsertGenRef.current);
  }, []);

  const applySessionPatchEverywhere = useCallback((
    sessionId: string,
    patchOrUpdater: SessionMetadataUpdater,
  ) => {
    const apply = (session: Session): Session => {
      const patch =
        typeof patchOrUpdater === "function"
          ? patchOrUpdater(session)
          : patchOrUpdater;
      const next: Session = { ...session };
      if (patch.inline_tags !== undefined) next.inline_tags = patch.inline_tags;
      if (patch.adv_sync_overlays !== undefined)
        next.adv_sync_overlays = patch.adv_sync_overlays;
      if (patch.open_file_panels !== undefined)
        next.open_file_panels = patch.open_file_panels;
      if (patch.open_config_panels !== undefined)
        next.open_config_panels = patch.open_config_panels;
      if (patch.draft_input !== undefined) next.draft_input = patch.draft_input;
      if (patch.draft_images !== undefined) next.draft_images = patch.draft_images;
      if (patch.draft_input_seq !== undefined) next.draft_input_seq = patch.draft_input_seq;
      if (patch.fork_closed !== undefined) next.fork_closed = patch.fork_closed;
      if (patch.model !== undefined) next.model = patch.model;
      if (patch.reasoning_effort !== undefined) {
        next.reasoning_effort = patch.reasoning_effort as Session["reasoning_effort"];
      }
      if (patch.cwd !== undefined) next.cwd = patch.cwd;
      if (patch.provider_id !== undefined) next.provider_id = patch.provider_id;
      if (patch.permission !== undefined) next.permission = patch.permission;
      if (patch.supervisor_enabled !== undefined) next.supervisor_enabled = patch.supervisor_enabled;
      if (patch.supervisor_custom_prompt !== undefined) next.supervisor_custom_prompt = patch.supervisor_custom_prompt;
      if (patch.pinned !== undefined) next.pinned = patch.pinned;
      if (patch.topbar_pinned !== undefined) next.topbar_pinned = patch.topbar_pinned;
      if (patch.topbar_pinned_at !== undefined) next.topbar_pinned_at = patch.topbar_pinned_at;
      if (patch.archived !== undefined) next.archived = patch.archived;
      if (patch.worker_eligible !== undefined) next.worker_eligible = patch.worker_eligible;
      if (patch.agent_rename_allowed !== undefined) next.agent_rename_allowed = patch.agent_rename_allowed;
      if (patch.working_mode !== undefined) next.working_mode = patch.working_mode;
      if (patch.working_mode_meta !== undefined) next.working_mode_meta = patch.working_mode_meta;
      if (patch.notes !== undefined) next.notes = patch.notes;
      if (patch.current_todos !== undefined) next.current_todos = patch.current_todos;
      if (patch.current_tasks !== undefined) next.current_tasks = patch.current_tasks;
      if (patch.messages !== undefined) next.messages = patch.messages;
      if (patch.message_count !== undefined) next.message_count = patch.message_count;
      if (patch.updated_at !== undefined) next.updated_at = patch.updated_at;
      if (patch.last_user_prompt_at !== undefined) next.last_user_prompt_at = patch.last_user_prompt_at;
      if (patch.last_opened_at !== undefined) next.last_opened_at = patch.last_opened_at;
      if (patch.pagination !== undefined) next.pagination = patch.pagination;
      if (patch.right_panel_open !== undefined)
        next.right_panel_open = patch.right_panel_open;
      if (patch.right_panel_active_tab !== undefined)
        next.right_panel_active_tab = patch.right_panel_active_tab;
      if (patch.right_panel_width !== undefined)
        next.right_panel_width = patch.right_panel_width;
      if (patch.right_panel_mobile_height !== undefined)
        next.right_panel_mobile_height = patch.right_panel_mobile_height;
      if (patch.right_panel_todos_dismissed !== undefined)
        next.right_panel_todos_dismissed = patch.right_panel_todos_dismissed;
      if (patch.right_panel_auto_opened_by !== undefined)
        next.right_panel_auto_opened_by = patch.right_panel_auto_opened_by;
      if (patch.sidebar_minimized !== undefined)
        next.sidebar_minimized = patch.sidebar_minimized;
      const keys = Object.keys(patch) as (keyof SessionMetadataPatch)[];
      return keys.some(
        (key) =>
          (session as unknown as Record<string, unknown>)[key] !==
          (next as unknown as Record<string, unknown>)[key],
      ) ? next : session;
    };

    setSessions((prev) => {
      let changed = false;
      const patched = prev
        .map((s) => {
          if (s.id !== sessionId) return s;
          const next = apply(s);
          if (next !== s) changed = true;
          return next;
        })
        .filter(isSidebarVisibleSession);
      if (!changed && patched.length === prev.length) return prev;
      const sorted = sortForList(patched);
      if (
        sorted.length === prev.length &&
        sorted.every((session, index) => session === prev[index])
      ) {
        return prev;
      }
      return sorted;
    });
    setCurrentSession((prev) =>
      prev ? updateNodeById(prev, sessionId, apply) : prev
    );

    const rootId = sessionTreeRootByNodeRef.current.get(sessionId);
    if (rootId) {
      const cached = sessionTreeCacheRef.current.get(rootId);
      if (cached) {
        const updated = updateNodeById(cached, sessionId, apply);
        if (updated !== cached) sessionTreeCacheRef.current.set(rootId, updated);
      }
    }
  }, [sortForList]);

  const stampSessionLastOpened = useCallback((sessionId: string, at: string) => {
    applySessionPatchEverywhere(sessionId, { last_opened_at: at });
  }, [applySessionPatchEverywhere]);

  const markSessionOpened = useCallback((sessionId: string, at = new Date().toISOString()) => {
    stampSessionLastOpened(sessionId, at);
    void fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/opened`, {
      method: "POST",
      credentials: "include",
    }).catch(() => {});
    return at;
  }, [stampSessionLastOpened]);

  useEffect(() => {
    if (!currentSession || wsTargetSessionId === null) return;
    rememberSessionTree(currentSession);
  }, [currentSession, rememberSessionTree, wsTargetSessionId]);

  const mergeSessionPage = useCallback(
    (prev: Session[], page: Session[], replace: boolean, dispatchInsertGen: number) => {
      // Pure sort/merge ONLY. Do NOT mutate `sessionRegistry` here — this
      // runs as a `setSessions` updater, which React executes during its
      // render pass. Mutating an external store that `useSyncExternalStore`
      // subscribes to (SessionStatusBadge) mid-render triggers React's
      // "getSnapshot should be cached" infinite loop (#185). Registry
      // seeding happens in `fetchSessionPage`, outside the updater.
      if (replace) {
        const backendIds = new Set(page.map((s) => s.id));
        // Preserve unacknowledged offline sessions AND sessions inserted
        // locally after this fetch was dispatched (server-confirmed rows a
        // pre-creation snapshot can't know about). See
        // preservedLocalSessionsForReplace.
        const preserved = preservedLocalSessionsForReplace(prev, backendIds, {
          dispatchInsertGen,
          insertGenById: sessionInsertGenByIdRef.current,
        });
        return sortForList([...preserved, ...page]);
      }
      const existingIds = new Set(prev.map((s) => s.id));
      return sortForList(
        [...prev, ...page.filter((s) => !existingIds.has(s.id))],
      );
    },
    [sortForList],
  );

  const fetchSessionPage = useCallback(
    async (
      offset: number,
      replace: boolean,
      filterSnapshot: SessionListFilters = sessionListFiltersRef.current,
      limitOverride?: number,
      silent = false,
    ) => {
      if (sessionsLoadingPageRef.current && !replace) return;
      const requestSeq = ++sessionListRequestSeqRef.current;
      // Snapshot the insert watermark at DISPATCH time: rows inserted
      // after this are newer than this page and must survive its replace.
      const dispatchInsertGen = sessionInsertGenRef.current;
      sessionsLoadingPageRef.current = true;
      if (!replace) setSessionsLoadingMore(true);
      // Silent background refreshes (status-churn refetch) must not flash
      // the search spinner — it is reserved for user-initiated fetches.
      if (replace && !silent && sessionsLoadedRef.current) setSessionsSearching(true);
      startOp(replace ? "session:list" : "session:list:more");
      let incompleteSnapshot = false;
      try {
        const params = new URLSearchParams({
          offset: String(offset),
          limit: String(limitOverride ?? SESSION_LIST_PAGE_SIZE),
        });
        const filters = filterSnapshot;
        if (filters.projectPath) params.set("project_path", filters.projectPath);
        const searchQuery = filters.search?.trim() ?? "";
        if (searchQuery) {
          params.set("search", searchQuery);
          if (filters.searchFields) params.set("search_fields", filters.searchFields.join(","));
        }
        if (filters.showArchived) params.set("show_archived", "true");
        if (filters.fileEditMode === "yes") params.set("file_edit_mode", "true");
        if (filters.fileEditMode === "no") params.set("file_edit_mode", "false");
        if (filters.folderIds?.length) params.set("folder_ids", filters.folderIds.join(","));
        if (filters.folderView !== undefined) params.set("folder_view", String(filters.folderView));
        if (filters.tagIds?.length) params.set("tag_ids", filters.tagIds.join(","));
        if (filters.providerIds?.length) params.set("provider_ids", filters.providerIds.join(","));
        if (filters.modelIds?.length) params.set("model_ids", filters.modelIds.join(","));
        if (filters.modes?.length) params.set("modes", filters.modes.join(","));
        if (filters.sources?.length) params.set("sources", filters.sources.join(","));
        if (filters.sortBy) params.set("sort_by", filters.sortBy);
        const res = await fetch(`${API}/api/sessions?${params}`, {
          credentials: "include",
        });
        if (!res.ok) {
          if (res.status === 401) {
            window.dispatchEvent(new CustomEvent("better-agent-auth-failed"));
          }
          return;
        }
        const data = await res.json();
        if (replace && requestSeq !== sessionListRequestSeqRef.current) return;
        if (replace && offset === 0 && data?.snapshot_complete === false) {
          incompleteSnapshot = true;
          window.setTimeout(() => {
            void fetchSessionPage(0, true, filterSnapshot, limitOverride, silent);
          }, 150);
          return;
        }
        const rawPage = data.sessions || [];
        const page = rawPage.filter(isSidebarVisibleSession);
        if (!replace && requestSeq !== sessionListRequestSeqRef.current) return;
        if (replace && offset === 0 && isGlobalUnfilteredFetch(filters)) {
          // Only a full, unfiltered global page may replace the registry
          // (the ALL-projects aggregate source of truth). A project- or
          // otherwise-narrowed replace would evict every other project's
          // sessions, zeroing their running/unread badges — the bug this
          // guard closes. See isGlobalUnfilteredFetch.
          sessionRegistry.replaceFromRows(page);
        } else {
          // Seed the registry from this page (async callback — NOT during
          // render) so deeper-page rows have a live entry for both the
          // status rank and the running/unread badge. Only fills missing
          // sids; never clobbers a fresher live entry. Also the path for
          // every narrowed replace fetch (project/search/tag/…), so a
          // project switch never wipes background projects' aggregates.
          sessionRegistry.seedFromRows(page);
        }
        if (replace) {
          // A session present in a backend page is acknowledged — drop its
          // local-insert protection so the watermark map stays bounded.
          for (const row of page) sessionInsertGenByIdRef.current.delete(row.id);
        }
        setSessions((prev) => mergeSessionPage(prev, page, replace, dispatchInsertGen));
        sessionsNextOffsetRef.current = offset + rawPage.length;
        setSessionsHasMore(Boolean(data.has_more));
      } catch {
        // ignore
      } finally {
        if (!replace) setSessionsLoadingMore(false);
        if (requestSeq === sessionListRequestSeqRef.current) {
          if (replace && !incompleteSnapshot) setSessionsLoaded(true);
          if (replace) setSessionsSearching(false);
          sessionsLoadingPageRef.current = false;
        }
        completeOp(replace ? "session:list" : "session:list:more");
      }
    },
    [mergeSessionPage],
  );

  // Programmatic sidebar refreshes (turn completion, WS deltas, reconnect,
  // metadata updates) are silent — the search spinner is reserved for
  // user-initiated fetches (the filter-change effect).
  const fetchSessions = useCallback(async (filterSnapshot?: SessionListFilters) => {
    await fetchSessionPage(0, true, filterSnapshot, undefined, true);
  }, [fetchSessionPage]);

  const loadMoreSessions = useCallback(async () => {
    if (!sessionsLoaded || !sessionsHasMoreRef.current) return;
    await fetchSessionPage(sessionsNextOffsetRef.current, false);
  }, [fetchSessionPage, sessionsLoaded]);

  // Re-paginate the FULL loaded span (not just page 0) so a status-churn
  // refetch doesn't collapse the user's scroll depth back to one page.
  // Capped at the backend's max page size (le=200): beyond 200 loaded rows
  // the refetch covers only the top 200 — acceptable since the top status
  // buckets are what the user is sorting toward.
  const refetchLoadedSpan = useCallback(() => {
    const loaded = sessionsRef.current.length;
    const span = Math.min(Math.max(loaded, SESSION_LIST_PAGE_SIZE), 200);
    void fetchSessionPage(0, true, sessionListFiltersRef.current, span, true);
  }, [fetchSessionPage]);

  // Status sort only: keep the list fresh against live status churn. On any
  // status-affecting delta, (a) re-sort loaded rows off the live registry
  // (immediate, authoritative interim view) and (b) debounce a full-span
  // re-pagination so sessions on unloaded/deeper pages bubble in. The
  // refetch debounce (2.5s) clears the backend's 2s monitoring-snapshot
  // tick so the live re-sort and the refetch don't fight.
  useEffect(() => {
    if (!sessionListFilters.statusSort) return;
    let resortTimer: number | undefined;
    let refetchTimer: number | undefined;
    const onDelta = () => {
      window.clearTimeout(resortTimer);
      resortTimer = window.setTimeout(() => {
        setSessions((prev) => sortForList([...prev]));
      }, 60);
      window.clearTimeout(refetchTimer);
      refetchTimer = window.setTimeout(() => {
        refetchLoadedSpan();
      }, 2500);
    };
    const unsub = subscribeMany([
      ["session_monitoring_changed", onDelta],
      ["session_running_changed", onDelta],
      ["session_unread_changed", onDelta],
      ["session_user_input_changed", onDelta],
      ["session_marker_changed", onDelta],
    ]);
    return () => {
      unsub();
      window.clearTimeout(resortTimer);
      window.clearTimeout(refetchTimer);
    };
  }, [sessionListFilters.statusSort, sortForList, refetchLoadedSpan]);

  useEffect(() => {
    // Fire on mount + whenever we transition to 'authed'. If the mount-time
    // fetch gets a 401, this transition ensures we try again once logged in.
    if (authStatus === "authed" || !authStatus) {
      fetchSessions();
    }
  }, [fetchSessions, authStatus]);

  useEffect(() => {
    if (!sessionsLoaded) return;
    if (!sessionListFiltersReadyRef.current) {
      sessionListFiltersReadyRef.current = true;
      return;
    }
    sessionsNextOffsetRef.current = 0;
    void fetchSessionPage(0, true, sessionListFilters);
  }, [fetchSessionPage, sessionListFilters, sessionsLoaded]);

  const updateSessionListFilters = useCallback((next: SessionListFilters) => {
    setSessionListFilters((prev) =>
      sameSessionListFilters(prev, next) ? prev : next,
    );
  }, []);

  useEffect(() => {
    if (!sessionsLoaded) return;
    setSessions((prev) => {
      const sorted = sortForList([...prev]);
      if (
        sorted.length === prev.length &&
        sorted.every((session, index) => session === prev[index])
      ) {
        return prev;
      }
      return sorted;
    });
  }, [
    sessionListFilters.folderView,
    sessionListFilters.sortBy,
    sessionListFilters.statusSort,
    sessionListFilters.search,
    sessionsLoaded,
    sortForList,
  ]);

  const createSession = useCallback(
    async (opts: CreateSessionOptions) => {
      const {
        name,
        model,
        cwd,
        orchestrationMode = "team",
        browserHarnessEnabled = true,
        providerId,
        browserHarnessHeadless = true,
        fileEditEnabled = false,
        fileEditPath,
        nodeId = "primary",
        reasoningEffort,
        runner,
        permission,
        clientSessionId,
        capabilityContexts,
        folderId,
        preset,
      } = opts;
      startOp("session:create");
      try {
        const res = await fetchWithTimeout(`${API}/api/sessions`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name,
            model,
            cwd,
            orchestration_mode: orchestrationMode,
            browser_harness_enabled: browserHarnessEnabled,
            provider_id: providerId,
            browser_harness_headless: browserHarnessHeadless,
            file_edit_enabled: fileEditEnabled,
            file_edit_path: fileEditPath,
            node_id: nodeId,
            reasoning_effort: reasoningEffort,
            runner,
            permission: permission && Object.keys(permission).length > 0 ? permission : undefined,
            client_session_id: clientSessionId,
            capability_contexts: capabilityContexts && capabilityContexts.length > 0 ? capabilityContexts : undefined,
            folder_id: folderId || undefined,
            preset: preset || undefined,
          }),
        });
        if (!res.ok) {
          throw await responseError(res);
        }
        const session = await res.json();
        const listFilters = sessionListFiltersRef.current;
        if (!canLocallyInsertIntoSessionList(session, listFilters)) {
          void fetchSessionPage(0, true, listFilters);
        }
        // Dedup: the backend's `session_created` WS broadcast can land
        // on this same tab before this POST `await` resolves, in which
        // case `appendSessionIfNew` already inserted it. Without this
        // check the sidebar shows the new session twice.
        if (canLocallyInsertIntoSessionList(session, listFilters)) {
          markSessionInserted(session.id);
          setSessions((prev) =>
            sortForList(
              prev.some((s) => s.id === session.id)
                ? prev.map((s) => (s.id === session.id ? session : s))
                : [session, ...prev],
            ),
          );
        }
        lastEventSeqBySessionRef.current = {
          ...lastEventSeqBySessionRef.current,
          [session.id]: 0,
        };
        return session;
      } finally {
        completeOp("session:create");
      }
    },
    [sortForList, markSessionInserted]
  );

  const addOfflineSession = useCallback((session: Session, select: boolean) => {
    markSessionInserted(session.id);
    setSessions((prev) =>
      prev.some((s) => s.id === session.id)
        ? prev
        : sortForList([session, ...prev]),
    );
    if (!select) return;
    selectRequestIdRef.current++;
    selectInFlightIdRef.current = null;
    setCurrentSession(session);
    setWsTargetSessionId(null);
  }, [sortForList, markSessionInserted]);

  const restoreOfflineSession = useCallback((session: Session) => {
    markSessionInserted(session.id);
    setSessions((prev) =>
      prev.some((s) => s.id === session.id)
        ? prev
        : sortForList([session, ...prev]),
    );
  }, [sortForList, markSessionInserted]);

  const forkSession = useCallback(
    async (parentId: string, name?: string) => {
      const opId = `session:fork:${parentId}`;
      startOp(opId);
      const res = await fetch(`${API}/api/sessions/${parentId}/fork`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const text = await res.text();
        failOp(opId, text);
        throw new Error(text);
      }
      const child = (await res.json()) as Session;
      completeOp(opId);
      await fetchSessions();
      selectRequestIdRef.current++;
      selectInFlightIdRef.current = null;
      // Functional update: the parent session's draft may have changed
      // between the fork POST and this state commit. Since the fork
      // switches focus to the child (new session, empty draft), there's
      // nothing to carry forward — but using a functional update is
      // consistent with the pattern in createSession/selectSession and
      // avoids discarding any intermediate state.
      setCurrentSession((prev) => prev && prev.id === child.id
        ? { ...child, draft_input: prev.draft_input, draft_images: prev.draft_images, draft_input_seq: prev.draft_input_seq }
        : child);
      // New fork — no REST gap, enable WS subscribe immediately.
      lastEventSeqBySessionRef.current = {
        ...lastEventSeqBySessionRef.current,
        [child.id]: 0,
      };
      setWsTargetSessionId(child.id);
      return child;
    },
    [fetchSessions]
  );

  /** Draft-preservation merge: carries draft_input, draft_images,
   * draft_input_seq and fork drafts from an old tree into a new one.
   * Shared by selectSession and applySessionReconciled. */
  const carryDrafts = useCallback(
    (oldNode: Session, newNode: Session): Session => {
      const result = { ...newNode };
      if (oldNode.draft_input !== undefined && oldNode.draft_input !== newNode.draft_input) {
        result.draft_input = oldNode.draft_input;
        result.draft_images = oldNode.draft_images;
        result.draft_input_seq = oldNode.draft_input_seq;
      }
      if (oldNode.forks && newNode.forks) {
        const mergedForks: Session[] = [];
        for (const nf of newNode.forks) {
          const of_ = oldNode.forks.find((f) => f.id === nf.id);
          mergedForks.push(of_ ? carryDrafts(of_, nf) : nf);
        }
        result.forks = mergedForks;
      }
      return result;
    },
    []
  );

  const reconcilePreservesRef = useRef<ReconcilePreserveRegistry>({});
  const preserveSessionMetadataThroughReconcile = useCallback(
    (sessionId: string, key: string, patchOrUpdater: SessionMetadataUpdater) => {
      reconcilePreservesRef.current = {
        ...reconcilePreservesRef.current,
        [sessionId]: {
          ...(reconcilePreservesRef.current[sessionId] ?? {}),
          [key]: patchOrUpdater,
        },
      };
    },
    [],
  );
  const clearSessionMetadataReconcilePreserve = useCallback(
    (sessionId: string, key: string) => {
      const byKey = reconcilePreservesRef.current[sessionId];
      if (!byKey || !(key in byKey)) return;
      const nextByKey = { ...byKey };
      delete nextByKey[key];
      const next = { ...reconcilePreservesRef.current };
      if (Object.keys(nextByKey).length === 0) delete next[sessionId];
      else next[sessionId] = nextByKey;
      reconcilePreservesRef.current = next;
    },
    [],
  );
  const applyReconcilePreserves = useCallback((node: Session): Session => {
    const applyOne = (current: Session): Session => {
      const preserves = reconcilePreservesRef.current[current.id];
      let next = current;
      if (preserves) {
        for (const patchOrUpdater of Object.values(preserves)) {
          const patch = typeof patchOrUpdater === "function"
            ? patchOrUpdater(next)
            : patchOrUpdater;
          next = { ...next, ...patch } as Session;
        }
      }
      if (next.forks?.length) {
        next = { ...next, forks: next.forks.map(applyOne) };
      }
      return next;
    };
    return applyOne(node);
  }, []);

  const selectSession = useCallback(async (id: string) => {
    if (selectInFlightIdRef.current === id) return;
    selectInFlightIdRef.current = id;
    const myReqId = ++selectRequestIdRef.current;
    const opId = `session:select:${id}`;
    startOp(opId);
    setSessionLoadError(null);
    // Drop WS target immediately so the WS hook unsubscribes from the
    // old session. The new target is set only after REST resolves and
    // seq cursors are seeded — prevents since_seq=0 flood.
    setWsTargetSessionId(null);
    // Start the open-latency probe. If a previous open was still
    // waiting for its quiet window, drop it — the user has moved on.
    clearOpenQuietTimer();
    openTimingRef.current = {
      sid: id,
      t0: performance.now(),
      restMs: null,
      quietTimer: null,
    };
    // Optimistic swap: flip currentSession to a sidebar-summary stub
    // BEFORE awaiting REST so the sidebar highlight + chat header move
    // in the same tick as the click. REST resolve replaces the stub
    // with the canonical tree (full messages, forks, max_seq_by_sid).
    // Skip when the cached entry is missing (deep-link before sidebar
    // load), when it's the already-focused session (refetch should not
    // wipe loaded messages), or when a stub for this id is already in
    // place from a prior in-flight selectSession for the same id.
    setSessionLoading(true);
    const cached = sessionsRef.current.find((s) => s.id === id);
    const cur = currentSessionRef.current;
    const cachedTree = cur?.id === id ? null : cachedSessionTreeFor(id);
    if (cachedTree) {
      const viewedSessionId = cachedTree.id;
      const openedAt = markSessionOpened(viewedSessionId);
      const cachedTreeWithOpenedAt = updateNodeById(cachedTree, viewedSessionId, (node) => {
        const incomingMs = node.last_opened_at ? Date.parse(node.last_opened_at) : NaN;
        const openedMs = Date.parse(openedAt);
        if (!Number.isNaN(incomingMs) && incomingMs >= openedMs) return node;
        return { ...node, last_opened_at: openedAt };
      });
      setCurrentSession(cachedTreeWithOpenedAt);
      const t = openTimingRef.current;
      if (t && t.sid === id) {
        t.restMs = 0;
        armOpenQuietTimer();
      }
      setWsTargetSessionId(id);
      selectInFlightIdRef.current = null;
      setSessionLoading(false);
      completeOp(opId);
      return;
    }
    if (cached && cur?.id !== id) {
      setCurrentSession({
        ...cached,
        messages: [],
        forks: [],
      });
    }
    try {
      const res = await fetch(`${API}/api/sessions/${id}?exchange_count=${exchangePageSize}`, {
        credentials: "include",
      });
      if (!res.ok) {
        if (res.status === 401) {
          window.dispatchEvent(new CustomEvent("better-agent-auth-failed"));
          return;
        }
        const err = await responseError(res);
        if (myReqId === selectRequestIdRef.current) {
          setSessionLoadError({ sessionId: id, message: err.message });
        }
        return;
      }
      // Backend returns the FULL root tree containing `id` (id may be
      // a fork — get_root_tree resolves to its root). The frontend
      // stores the whole tree in currentSession; the split-pane UI
      // reads forks from `currentSession.forks`.
      const tree = (await res.json()) as Session;
      if (myReqId !== selectRequestIdRef.current) return;
      const viewedSessionId = tree.id;
      const openedAt = markSessionOpened(viewedSessionId);
      const treeWithOpenedAt = updateNodeById(tree, viewedSessionId, (node) => {
        const incomingMs = node.last_opened_at ? Date.parse(node.last_opened_at) : NaN;
        const openedMs = Date.parse(openedAt);
        if (!Number.isNaN(incomingMs) && incomingMs >= openedMs) return node;
        return { ...node, last_opened_at: openedAt };
      });
      // Record REST-resolve checkpoint for the open-latency probe.
      // The quiet timer only arms once restMs is set, so any replay/
      // live event that lands before this point is folded into the
      // tip wait by the next `markOpenTimingEvent` call.
      {
        const t = openTimingRef.current;
        if (t && t.sid === id) {
          t.restMs = Math.round(performance.now() - t.t0);
          armOpenQuietTimer();
        }
      }
      // Draft-preservation: the user may have been typing while the REST
      // fetch was in flight. The debounced PATCH (300ms) can lag behind
      // the fetch, so the backend's draft_input may be stale (empty or
      // outdated). Use a FUNCTIONAL update to read the LATEST state
      // (including any keystrokes applied by applySessionMetadata during
      // the fetch) and carry forward any draft that differs from the
      // REST response. A direct setCurrentSession(tree) would discard
      // those intermediate functional updates. Also merge any messages
      // that were added by WS events (user_message_persisted, etc.)
      // during the fetch — the REST response was generated before those
      // messages existed, so carryDrafts alone would lose them.
      setCurrentSession((prev) => {
        if (!prev || prev.id !== treeWithOpenedAt.id) {
          console.info(
            "[stale-dbg] selectSession %s: direct tree (prev=%s tree=%s)",
            id.slice(0, 8), prev?.id?.slice(0, 8) ?? "null", treeWithOpenedAt.id.slice(0, 8),
          );
          return treeWithOpenedAt;
        }
        if (
          treeHasStreamingAssistant(prev) &&
          treeHasStreamingAssistant(treeWithOpenedAt)
        ) {
          console.info("[stale-dbg] selectSession %s: kept prev (streaming)", id.slice(0, 8));
          return prev;
        }
        const carried = carryDrafts(prev, treeWithOpenedAt);
        const treeAsst = treeWithOpenedAt.messages?.filter((m: ChatMessage) => m.role === "assistant").at(-1);
        const prevAsst = prev.messages?.filter((m: ChatMessage) => m.role === "assistant").at(-1);
        console.info(
          "[stale-dbg] selectSession %s: merging tree_msgs=%d prev_msgs=%d tree_last_evts=%d prev_last_evts=%d",
          id.slice(0, 8),
          treeWithOpenedAt.messages?.length ?? 0, prev.messages?.length ?? 0,
          treeAsst?.events?.length ?? 0, prevAsst?.events?.length ?? 0,
        );
        if (prev.messages?.length) {
          return addMissingMessages(carried, prev.id, prev.messages);
        }
        return carried;
      });
      // Flush any WS replays that arrived while REST was in flight.
      const pending = pendingReplayRef.current;
      if (pending.length > 0) {
        pendingReplayRef.current = [];
        for (const { sessionId, messages } of pending) {
          setCurrentSession((prev) =>
            prev ? mergeReplayIntoNode(prev, sessionId, messages) : prev
          );
          bumpLastSeq(sessionId, messages);
        }
      }
      // Seed the seq cursor for the root AND every embedded fork —
      // each pane's WS subscribe sends its own since_seq.
      const updates: Record<string, number> = {};
      const visit = (node: Session) => {
        let highest = -1;
        for (const m of node.messages || []) {
          if (typeof m.seq === "number" && m.seq > highest) highest = m.seq;
        }
        updates[node.id] = highest;
        for (const f of wsSubscribableForks(node)) visit(f);
      };
      visit(tree);
      lastSeqBySessionRef.current = {
        ...lastSeqBySessionRef.current,
        ...updates,
      };
      // Seed event-watermark cursors from the REST snapshot. The
      // backend stamps `max_seq_by_sid` on the tree response: a
      // per-sid map of the highest events.jsonl seq present at REST
      // time. Passing this back as `events_from_seq` on subscribe
      // closes the REST↔WS gap with no uuid-dedup reliance.
      // CRITICAL: merge with Math.max — a refetch may return a STALE
      // max_seq if the request was queued behind live WS frames that
      // have already advanced our cursor. Spread-overwrite would rewind
      // the watermark and ask the backend to redeliver events we
      // already applied.
      const maxSeqByMid = (tree as Session & {
        max_seq_by_sid?: Record<string, number>;
      }).max_seq_by_sid;
      {
        const prev = lastEventSeqBySessionRef.current;
        const next: Record<string, number> = { ...prev };
        for (const sid of Object.keys(updates)) {
          if (typeof next[sid] !== "number") next[sid] = 0;
        }
        if (maxSeqByMid && typeof maxSeqByMid === "object") {
          for (const [sid, seq] of Object.entries(maxSeqByMid)) {
            if (typeof seq !== "number") continue;
            const cur = next[sid];
            if (typeof cur !== "number" || seq > cur) {
              next[sid] = seq;
            }
          }
        }
        lastEventSeqBySessionRef.current = next;
      }
      // All seq cursors are seeded — safe to let the WS subscribe now.
      setWsTargetSessionId(id);
    } catch (e) {
      if (myReqId === selectRequestIdRef.current) {
        const timedOut = e instanceof DOMException && e.name === "AbortError";
        setSessionLoadError({
          sessionId: id,
          message: timedOut
            ? "timeout"
            : e instanceof Error
              ? e.message
              : String(e),
        });
      }
    } finally {
      if (myReqId === selectRequestIdRef.current) {
        selectInFlightIdRef.current = null;
      }
      setSessionLoading(false);
      completeOp(opId);
    }
  }, [cachedSessionTreeFor, exchangePageSize, markSessionOpened]);

  const removeSessionLocally = useCallback((id: string) => {
    forgetSessionTree(id);
    sessionInsertGenByIdRef.current.delete(id);
    setSessions((prev) => {
      if (!prev.some((s) => s.id === id)) return prev;
      return prev.filter((s) => s.id !== id);
    });
    setCurrentSession((prev) => {
      if (!prev) return prev;
      if (prev.id === id) return null;
      const dropFork = (node: Session): Session => {
        const forks = node.forks;
        if (!forks || forks.length === 0) return node;
        let changed = false;
        const next: Session[] = [];
        for (const f of forks) {
          if (f.id === id) {
            changed = true;
            continue;
          }
          const recursed = dropFork(f);
          if (recursed !== f) changed = true;
          next.push(recursed);
        }
        return changed ? { ...node, forks: next } : node;
      };
      const updated = dropFork(prev);
      return updated === prev ? prev : updated;
    });
    setWsTargetSessionId((prev) => prev === id ? null : prev);
  }, [forgetSessionTree]);

  const deleteSession = useCallback(
    async (id: string) => {
      const opId = `session:delete:${id}`;
      const wasCurrentSession = currentSessionRef.current?.id === id;
      startOp(opId);
      removeSessionLocally(id);
      try {
        const response = await fetch(`${API}/api/sessions/${id}`, {
          method: "DELETE",
          credentials: "include",
        });
        if (!response.ok) {
          throw await responseError(response);
        }
      } catch (err: unknown) {
        failOp(opId, err instanceof Error ? err.message : String(err));
        await fetchSessions();
        if (wasCurrentSession) {
          await selectSession(id);
        }
        throw err;
      } finally {
        completeOp(opId);
      }
    },
    [fetchSessions, removeSessionLocally, selectSession]
  );

  const bumpLastSeq = useCallback(
    (sessionId: string, messages: ChatMessage[]) => {
      const prev = lastSeqBySessionRef.current[sessionId] ?? -1;
      let next = prev;
      for (const m of messages) {
        if (typeof m.seq === "number" && m.seq > next) next = m.seq;
      }
      if (next !== prev) {
        lastSeqBySessionRef.current = {
          ...lastSeqBySessionRef.current,
          [sessionId]: next,
        };
      }
    },
    []
  );

  const addMessages = useCallback(
    (sessionId: string, messages: ChatMessage[]) => {
      const freshRef: ChatMessage[] = [];
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const existing = new Set((node.messages || []).map((m) => m.id));
          const fresh = messages.filter((m) => !existing.has(m.id));
          if (fresh.length === 0) return node;
          freshRef.push(...fresh);
          return {
            ...node,
            messages: [...(node.messages || []), ...fresh],
          };
        });
      });
      if (freshRef.length > 0) bumpLastSeq(sessionId, freshRef);
    },
    [bumpLastSeq]
  );

  const replaceMessages = useCallback(
    (sessionId: string, messages: ChatMessage[]) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => ({
          ...node,
          messages,
        }));
      });
      // Replace = wholesale truth swap (currently used by rewind_complete).
      // Reset our seq cursor to the highest seq in the new list (or -1).
      let next = -1;
      for (const m of messages) {
        if (typeof m.seq === "number" && m.seq > next) next = m.seq;
      }
      lastSeqBySessionRef.current = {
        ...lastSeqBySessionRef.current,
        [sessionId]: next,
      };
    },
    []
  );

  /** Merge replay messages into a node inside the tree. Shared by
   * applyMessagesReplay and flushPendingReplays. */
  const mergeReplayIntoNode = useCallback(
    (
      prev: Session,
      sessionId: string,
      messages: ChatMessage[]
    ): Session => {
      return updateNodeById(prev, sessionId, (node) => {
        const existing = node.messages || [];
        return { ...node, messages: mergeIncomingMessagesForNode(existing, messages) };
      });
    },
    []
  );

  /** Add messages from `source` into the tree node ONLY if their id
   * doesn't already exist in the node. Used by selectSession and
   * applySessionReconciled to preserve optimistic messages
   * (user_message_persisted during fetch) without overwriting the
   * canonical REST data with stale local versions. */
  const addMissingMessages = useCallback(
    (
      tree: Session,
      sessionId: string,
      source: ChatMessage[]
    ): Session => {
      return updateNodeById(tree, sessionId, (node) => {
        const existing = node.messages || [];
        const existingIds = new Set(existing.map((m) => m.id));
        const missing = source.filter((m) => !existingIds.has(m.id));
        if (missing.length === 0) return node;
        const merged = [...existing, ...missing];
        merged.sort((a, b) => {
          const sa =
            typeof a.seq === "number" ? a.seq : Number.MAX_SAFE_INTEGER;
          const sb =
            typeof b.seq === "number" ? b.seq : Number.MAX_SAFE_INTEGER;
          return sa - sb;
        });
        return { ...node, messages: merged };
      });
    },
    []
  );

  /** Apply a `messages_replay` payload from the backend. Upserts each
   * message by `id` (replaces if present, appends if new) and bumps
   * `lastSeqBySession`. If the target session isn't in the tree yet
   * (REST fetch still in flight), queues the replay for later flush. */
  const applyMessagesReplay = useCallback(
    (sessionId: string, messages: ChatMessage[]) => {
      if (messages.length === 0) return;
      let applied = false;
      setCurrentSession((prev) => {
        if (!prev || !findNode(prev, sessionId)) {
          // Tree not loaded yet — queue for flush after selectSession.
          pendingReplayRef.current.push({ sessionId, messages });
          return prev;
        }
        applied = true;
        return mergeReplayIntoNode(prev, sessionId, messages);
      });
      if (applied) bumpLastSeq(sessionId, messages);
      // Replay frame counts as activity for the open-latency probe —
      // restart the quiet timer so we wait through the full drain.
      markOpenTimingEvent(sessionId);
    },
    [mergeReplayIntoNode, bumpLastSeq]
  );

  /** Apply a `stub_invalidated` payload: a backend reconcile appended
   * late events to a collapsed historical turn, so its stale stub (and
   * any previously-fetched full events the bubble cached) must be
   * dropped. Re-stub the message + empty all its event lists and bump
   * `stubVersion` so an already-expanded bubble busts its fetch cache
   * and re-fetches fresh. */
  const applyStubInvalidated = useCallback(
    (
      sessionId: string,
      msgId: string,
      stub: { event_count: number; last_events: WSEvent[] }
    ) => {
      setCurrentSession((prev) => {
        if (!prev || !findNode(prev, sessionId)) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const messages = node.messages || [];
          const idx = messages.findIndex((m) => m.id === msgId);
          if (idx === -1) return node;
          const m = messages[idx];
          const next: ChatMessage = {
            ...m,
            stub,
            stubVersion: (m.stubVersion ?? 0) + 1,
            events: [],
            workers: m.workers
              ? m.workers.map((w) => ({ ...w, events: [] }))
              : m.workers,
          };
          const merged = [...messages];
          merged[idx] = next;
          return { ...node, messages: merged };
        });
      });
    },
    []
  );

  /** Extract the claude event UUID from a WS event, handling both
   * agent_message, legacy manager_event, and worker_event wrappers
   * (mirrors the backend's _event_uuid in orchs/base.py). */

  const activeRunTargetMessageId = useCallback((sessionId: string): string | null => {
    const runs = runStateBySessionRef.current[sessionId] ?? [];
    const primaryRun = [...runs]
      .reverse()
      .find((run) => run.kind !== "worker" && run.target_message_id);
    return primaryRun?.target_message_id ?? null;
  }, []);

  /** Apply one live WS turn-event onto the backend-owned active assistant
   * message. If the backend has not reported a target yet, fall back to a
   * streaming tail assistant or create a placeholder. */
  const applyLiveEvent = useCallback(
    (sessionId: string, event: WSEvent) => {
      // Live event = activity on the open session; restart the
      // open-latency probe's quiet window so we wait through the
      // current burst before declaring "in sync".
      markOpenTimingEvent(sessionId);
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const nextMessages = applyLiveEventToMessages(
            msgs,
            event,
            activeRunTargetMessageId(sessionId),
            node.orchestration_mode,
            `live-${Date.now()}`,
          );
          if (nextMessages === msgs) return node;
          return { ...node, messages: nextMessages };
        });
      });
    },
    [activeRunTargetMessageId]
  );

  /** Replace the run-state list for a session with the backend's
   * authoritative snapshot. Empty array → no runs active. */
  const applyRunState = useCallback(
    (sessionId: string, runs: RunInfo[]) => {
      runStateBySessionRef.current = {
        ...runStateBySessionRef.current,
        [sessionId]: runs,
      };
      setRunStateBySession((all) => {
        if (runs.length === 0) {
          const { [sessionId]: _refDrop, ...refRest } =
            runStateBySessionRef.current;
          void _refDrop;
          runStateBySessionRef.current = refRest;
          if (!(sessionId in all)) return all;
          const { [sessionId]: _drop, ...rest } = all;
          void _drop;
          return rest;
        }
        return { ...all, [sessionId]: runs };
      });
    },
    []
  );

  // Self-healing reconciliation for stuck "Running…" badges: the
  // backend can remove a run_state entry and drop the paired
  // `run_state` WS notification (e.g. broadcast throws, or removal
  // happens via the passive background prune, which never emits) —
  // the frontend then mirrors a stale non-empty run list forever,
  // since `run_state` is push-only with no periodic re-poll. Every
  // 15s, any run older than the grace period gets a one-shot check
  // against the backend's authoritative per-run snapshot; a 404
  // means the backend already forgot it, so we drop it locally too.
  const runReconcileInFlightRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const RECONCILE_GRACE_MS = 30_000;
    const id = setInterval(() => {
      const now = Date.now();
      for (const [sessionId, runs] of Object.entries(
        runStateBySessionRef.current
      )) {
        for (const run of runs) {
          const key = `${sessionId}:${run.run_id}`;
          if (runReconcileInFlightRef.current.has(key)) continue;
          const startedAt = Date.parse(run.started_at);
          if (!Number.isFinite(startedAt) || now - startedAt < RECONCILE_GRACE_MS)
            continue;
          runReconcileInFlightRef.current.add(key);
          fetch(
            `${API}/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(run.run_id)}/details`,
            { credentials: "include" }
          )
            .then((r) => {
              if (r.status === 404) {
                const current = runStateBySessionRef.current[sessionId] ?? [];
                applyRunState(
                  sessionId,
                  current.filter((r2) => r2.run_id !== run.run_id)
                );
              }
            })
            .catch(() => {})
            .finally(() => {
              runReconcileInFlightRef.current.delete(key);
            });
        }
      }
    }, 15_000);
    return () => clearInterval(id);
  }, [applyRunState]);

  /** Mark the last streaming assistant message as terminal (turn
   * completed or stopped). Sets `isStreaming: false` and optionally
   * stamps `stopped_at` so the "Running…" indicator disappears
   * immediately without waiting for a REST refetch. */
  const markTurnTerminal = useCallback(
    (
      sessionId: string,
      stoppedAt?: string,
      interruptedByMsgId?: string | null,
      errorText?: string,
    ) => {
      console.debug(
        "[stale-dbg] markTurnTerminal %s stoppedAt=%s interruptedBy=%s error=%s",
        sessionId.slice(0, 8), stoppedAt ?? "none", interruptedByMsgId ?? "none",
        errorText !== undefined ? "yes" : "no",
      );
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const nextMessages = finalizeTerminalAssistant(msgs, {
            stoppedAt,
            interruptedByMsgId,
            errorText,
          });
          if (nextMessages === msgs) return node;
          return { ...node, messages: nextMessages };
        });
      });
    },
    []
  );

  /** Mark the last streaming assistant message as detached (backend
   * restarted but runner is still alive externally). Stamps
   * `isDetached: true` so the bubble renders "Reconnecting…" instead
   * of a stuck spinner. Clears on reconnect when REST replay
   * overwrites the message. */
  const markTurnDetached = useCallback(
    (sessionId: string) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const lastIdx = msgs.length - 1;
          const last = msgs[lastIdx];
          if (!last || last.role !== "assistant" || !last.isStreaming)
            return node;
          const updated: ChatMessage = {
            ...last,
            isStreaming: false,
            isDetached: true,
          };
          return {
            ...node,
            messages: [...msgs.slice(0, lastIdx), updated],
          };
        });
      });
    },
    []
  );


  /** Flip the `isRecovering` flag on a specific assistant message in
   * response to backend `message_recovering_changed` WS frames. The
   * backend owns the truth (transient in-memory set in session_manager);
   * we mirror it onto the message so MessageBubble can render the pill. */
  const applyMessageRecovering = useCallback(
    (sessionId: string, msgId: string, value: boolean) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          let idx = -1;
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].id === msgId) {
              idx = i;
              break;
            }
          }
          if (idx === -1) return node;
          const current = msgs[idx];
          if (!!current.isRecovering === value) return node;
          const next: ChatMessage = { ...current, isRecovering: value };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  /** Stamp `retrying_until` (and optional `error`/`errorText`) on an
   * assistant message in response to backend `message_retrying_changed`
   * WS frames. `retryAt=null` clears both the pill and the error —
   * fired the instant the next attempt re-spawns. */
  const applyMessageRetrying = useCallback(
    (
      sessionId: string,
      msgId: string,
      retryAt: string | null,
      errorText: string | null,
    ) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          let idx = -1;
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].id === msgId) {
              idx = i;
              break;
            }
          }
          if (idx === -1) return node;
          const current = msgs[idx];
          if ((current.retrying_until ?? null) === retryAt) return node;
          const next: ChatMessage = {
            ...current,
            retrying_until: retryAt,
            ...(retryAt === null
              ? { error: undefined, errorText: undefined }
              : errorText
                ? { error: true, errorText }
                : {}),
          };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    [],
  );

  /** Stamp the per-turn picker payload (`ask_result`) on an assistant
   * message in response to `message_ask_result_changed` WS frames. */
  const applyMessageAskResult = useCallback(
    (
      sessionId: string,
      msgId: string,
      askResult: import("../types").AskResult | null,
    ) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          let idx = -1;
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].id === msgId) {
              idx = i;
              break;
            }
          }
          if (idx === -1) return node;
          const next: ChatMessage = { ...msgs[idx], ask_result: askResult };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  const applyMessageContent = useCallback(
    (sessionId: string, msgId: string, content: string) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const idx = msgs.findIndex((m) => m.id === msgId);
          if (idx === -1) return node;
          if ((msgs[idx].content ?? "") === content) return node;
          const next: ChatMessage = {
            ...msgs[idx],
            content,
            isDetached: false,
          };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  const applyMessageContinuation = useCallback(
    (sessionId: string, msgId: string, chainDepth: number | null) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const idx = msgs.findIndex((m) => m.id === msgId);
          if (idx === -1) return node;
          if ((msgs[idx].continuation_active ?? null) === chainDepth)
            return node;
          const next: ChatMessage = { ...msgs[idx] };
          if (chainDepth == null) {
            delete next.continuation_active;
          } else {
            next.continuation_active = chainDepth;
          }
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  /** Patch `run_meta` (per-turn provider/model/effort actually used) on an
   *  assistant message in response to `message_run_meta_changed` WS frames.
   *  Re-stamped by the backend each retry iteration, so a mid-message
   *  selector switch (rate-limit 'continue on another provider') updates the
   *  badge live to match the provider running the succeeding attempt. */
  const applyMessageRunMeta = useCallback(
    (
      sessionId: string,
      msgId: string,
      runMeta: ChatMessage["run_meta"],
    ) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const idx = msgs.findIndex((m) => m.id === msgId);
          if (idx === -1) return node;
          const current = msgs[idx].run_meta ?? null;
          const incoming = runMeta ?? null;
          if (
            JSON.stringify(current) === JSON.stringify(incoming)
          )
            return node;
          const next: ChatMessage = { ...msgs[idx] };
          if (incoming) {
            next.run_meta = runMeta ?? undefined;
          } else {
            delete next.run_meta;
          }
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  /** Stamp the user's pick (`chosen_session_id`) on an assistant message in
   * response to `message_ask_choice_changed` WS frames — keeps the chosen
   * picker row highlighted across reloads / tabs / previous turns. */
  const applyMessageAutoRetry = useCallback(
    (
      sessionId: string,
      msgId: string,
      autoRetry: { count: number; kind: string } | null,
    ) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const idx = msgs.findIndex((m) => m.id === msgId);
          if (idx === -1) return node;
          const next: ChatMessage = { ...msgs[idx], auto_retry: autoRetry };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    [],
  );

  const applyMessageAskChoice = useCallback(
    (sessionId: string, msgId: string, chosenSessionId: string | null) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          let idx = -1;
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].id === msgId) {
              idx = i;
              break;
            }
          }
          if (idx === -1) return node;
          if ((msgs[idx].chosen_session_id ?? null) === chosenSessionId)
            return node;
          const next: ChatMessage = {
            ...msgs[idx],
            chosen_session_id: chosenSessionId,
          };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  /** Read the highest seq we have for `sessionId`, suitable to send
   * as `since_seq` on the next subscribe. We return the highest seen
   * seq (NOT +1) so the backend's `seq >= since_seq` filter re-sends
   * the last message — that's how the in-flight assistant message
   * (whose seq doesn't change as content streams in) gets refreshed
   * on reconnect. Upsert by id makes the duplicate harmless. */
  const getSinceSeq = useCallback((sessionId: string | null): number => {
    if (!sessionId) return 0;
    const v = lastSeqBySessionRef.current[sessionId];
    return typeof v === "number" && v >= 0 ? v : 0;
  }, []);

  /** Highest events.jsonl seq we've already received for `sessionId`,
   * passed to the backend on subscribe as `events_from_seq` so the
   * wire tailer drains the REST↔WS gap before live events flow. */
  const getEventsFromSeq = useCallback(
    (sessionId: string | null): number => {
      if (!sessionId) return 0;
      const v = lastEventSeqBySessionRef.current[sessionId];
      return typeof v === "number" && v >= 0 ? v : 0;
    },
    []
  );

  const getEventsCursorKnown = useCallback(
    (sessionId: string | null): boolean => {
      if (!sessionId) return false;
      return typeof lastEventSeqBySessionRef.current[sessionId] === "number";
    },
    []
  );

  /** Bump the events.jsonl watermark for `sessionId` to `seq` (no-op if
   * we already have a higher one). Called from useWebSocket on every
   * incoming frame that carries a top-level `seq`, so reconnects ask
   * the backend to resume from the right place. */
  const advanceEventSeq = useCallback(
    (sessionId: string, seq: number) => {
      const cur = lastEventSeqBySessionRef.current[sessionId] ?? -1;
      if (seq > cur) {
        lastEventSeqBySessionRef.current = {
          ...lastEventSeqBySessionRef.current,
          [sessionId]: seq,
        };
      }
    },
    []
  );

  const updateSessionName = useCallback(
    (sessionId: string, name: string) => {
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, name } : s))
      );
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return renameSessionNode(prev, sessionId, name);
      });
      updateCachedSessionName(sessionId, name);
    },
    [renameSessionNode, updateCachedSessionName]
  );

  const togglePin = useCallback(
    async (sessionId: string, pinned: boolean) => {
      const response = await fetch(`${API}/api/sessions/${sessionId}/pin`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned }),
      });
      if (!response.ok) return;
      const data = await response.json();
      const nextPinned = Boolean(data.pinned);
      applySessionPatchEverywhere(sessionId, { pinned: nextPinned });
    },
    [applySessionPatchEverywhere]
  );

  const unpinOtherSessions = useCallback(
    async (keepId: string) => {
      const response = await fetch(`${API}/api/sessions/${keepId}/unpin-others`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
      });
      if (!response.ok) return;
      const data = await response.json();
      const unpinnedIds = new Set<string>(
        Array.isArray(data.unpinned_ids)
          ? data.unpinned_ids.filter((id: unknown): id is string => typeof id === "string")
          : [],
      );
      if (!unpinnedIds.size) return;
      for (const id of unpinnedIds) {
        applySessionPatchEverywhere(id, { pinned: false });
      }
    },
    [applySessionPatchEverywhere]
  );

  const archiveSession = useCallback(
    async (sessionId: string, archived: boolean) => {
      await fetch(`${API}/api/sessions/${sessionId}/archive`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archived }),
      });
      setSessions((prev) =>
        sortForList(
          prev
            .map((s) => (s.id === sessionId ? { ...s, archived } : s))
            .filter(isSidebarVisibleSession),
        )
      );
    },
    [sortForList]
  );

  const moveSessionToProject = useCallback(
    async (sessionId: string, cwd: string): Promise<Session> => {
      const opId = `session:move:${sessionId}`;
      startOp(opId);
      try {
        const res = await fetch(`${API}/api/sessions/${sessionId}/move-to-project`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cwd }),
        });
        if (!res.ok) {
          const detail = (await res.json().catch(() => null))?.detail;
          throw new Error(
            typeof detail === "string" ? detail : `move failed (${res.status})`,
          );
        }
        const created: Session = await res.json();
        setSessions((prev) =>
          sortForList(
            prev
              .map((s) =>
                s.id === sessionId
                  ? { ...s, archived: true, moved_to_session_id: created.id }
                  : s,
              )
              .filter(isSidebarVisibleSession),
          )
        );
        return created;
      } finally {
        completeOp(opId);
      }
    },
    [sortForList]
  );

  const toggleWorkerEligible = useCallback(
    async (sessionId: string, worker_eligible: boolean) => {
      await fetch(`${API}/api/sessions/${sessionId}/worker_eligible`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ worker_eligible }),
      });
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, worker_eligible } : s))
      );
    },
    []
  );

  const toggleAgentRenameAllowed = useCallback(
    async (sessionId: string, agent_rename_allowed: boolean) => {
      await fetch(`${API}/api/sessions/${sessionId}/agent_rename_allowed`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_rename_allowed }),
      });
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, agent_rename_allowed } : s))
      );
    },
    []
  );

  const renameSession = useCallback(
    async (sessionId: string, name: string) => {
      const opId = `session:rename:${sessionId}`;
      startOp(opId);
      try {
        await fetch(`${API}/api/sessions/${sessionId}/rename`, {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
      } finally {
        completeOp(opId);
      }
      updateSessionName(sessionId, name);
    },
    [updateSessionName]
  );

  /** Merge a partial metadata patch ({inline_tags?, draft_input?, fork_closed?})
   * into every local copy of the session record. Used both as the optimistic
   * local updater (typing, tag add/remove) AND as the WS broadcast applier
   * (cross-tab sync). The shared reducer also re-sorts the sidebar list by
   * the active sort field so frontend-only patches take effect immediately. */
  const applySessionMetadata = useCallback(
    (
      sessionId: string,
      patchOrUpdater: SessionMetadataUpdater
    ) => {
      applySessionPatchEverywhere(sessionId, patchOrUpdater);
    },
    [applySessionPatchEverywhere]
  );

  /** Prepend a freshly-born NON-fork session (from a WS
   * `session_created` event) into the sidebar list. Dedup by id — the
   * originating tab already inserted via the REST POST response, so
   * this only adds for OTHER tabs (INV-3 / DIV-4 multi-tab
   * convergence). No-op for ephemeral sessions (they're filtered
   * out backend-side). */
  const appendSessionIfNew = useCallback((session: Session) => {
    if (!isSidebarVisibleSession(session)) return;
    if (!canLocallyInsertIntoSessionList(session, sessionListFiltersRef.current)) {
      refetchLoadedSpan();
      return;
    }
    markSessionInserted(session.id);
    setSessions((prev) => {
      if (prev.some((s) => s.id === session.id)) return prev;
      return sortForList([session, ...prev]);
    });
  }, [refetchLoadedSpan, sortForList, markSessionInserted]);

  const dropSessionIfPresent = useCallback((id: string) => {
    removeSessionLocally(id);
  }, [removeSessionLocally]);

  /** Append a freshly-born fork (from a WS `session_forked` event) into
   * the live tree. The fork is added under its `parent_session_id`'s
   * node — which may be the root or any nested fork. No-op if the
   * parent isn't in the current tree (the event is for some other
   * tree). */
  const appendFork = useCallback(
    (childSession: Session, parentSessionId: string | null) => {
      if (!parentSessionId) return;
      setCurrentSession((prev) => {
        if (!prev) return prev;
        if (!findNode(prev, parentSessionId)) return prev;
        // Skip if we already have this fork (race: forker tab + ws echo).
        if (findNode(prev, childSession.id)) return prev;
        return updateNodeById(prev, parentSessionId, (parent) => ({
          ...parent,
          forks: [...(parent.forks ?? []), childSession],
        }));
      });
      // Seed the seq cursor for the new fork.
      let highest = -1;
      for (const m of childSession.messages || []) {
        if (typeof m.seq === "number" && m.seq > highest) highest = m.seq;
      }
      lastSeqBySessionRef.current = {
        ...lastSeqBySessionRef.current,
        [childSession.id]: highest,
      };
      lastEventSeqBySessionRef.current = {
        ...lastEventSeqBySessionRef.current,
        [childSession.id]: 0,
      };
    },
    []
  );

  /** Collect every session id reachable in the current tree (root +
   * forks). */
  const allOpenSessionIds = useCallback((): string[] => {
    if (!currentSession) return [];
    const ids: string[] = [];
    const visit = (node: Session) => {
      ids.push(node.id);
      for (const f of wsSubscribableForks(node)) visit(f);
    };
    visit(currentSession);
    return ids;
  }, [currentSession]);

  /** Look up a node within the current tree (read-only). */
  const getNode = useCallback(
    (sessionId: string): Session | null => {
      if (!currentSession) return null;
      return findNode(currentSession, sessionId);
    },
    [currentSession]
  );

  /** Load older messages for a session node (scroll-up pagination).
   * Prepends them to the node's messages array and updates pagination. */
  const loadOlderMessages = useCallback(
    (sessionId: string, beforeSeq: number) =>
      olderMessageRequestsRef.current.run(`${sessionId}:${beforeSeq}`, async () => {
        const res = await fetch(
          `${API}/api/sessions/${encodeURIComponent(sessionId)}/messages?before_seq=${beforeSeq}&exchange_count=${exchangePageSize}`
        );
        if (!res.ok) throw await responseError(res);
        const page = parseOlderMessagePage(await res.json());
        const currentNode = currentSessionRef.current
          ? findNode(currentSessionRef.current, sessionId)
          : null;
        const prepended = currentNode?.pagination?.oldest_loaded_seq === beforeSeq
          && page.messages.length > 0;
        setCurrentSession((prev) => {
          if (!prev) return prev;
          return updateNodeById(prev, sessionId, (node) =>
            applyOlderMessagePage(node, beforeSeq, page)
          );
        });
        return prepended;
      }),
    [exchangePageSize]
  );

  const clearCurrentSession = useCallback(() => {
    selectRequestIdRef.current++;
    selectInFlightIdRef.current = null;
    setCurrentSession(null);
    setWsTargetSessionId(null);
  }, []);

  /** AI-driven sidebar search. Posts the user's natural-language query
   * to the backend, which runs a one-shot headless claude invocation
   * and returns ids ranked by relevance. The `signal` argument lets the
   * caller (SessionList) cancel a stale request via AbortController when
   * a new query is fired before the prior reply lands. Returns `null`
   * when the request was aborted so the caller can ignore stale state. */
  const searchSessions = useCallback(
    async (
      query: string,
      signal?: AbortSignal,
    ): Promise<{
      results: Session[];
      reasoning: string;
      error: string | null;
    } | null> => {
      try {
        const res = await fetch(`${API}/api/extensions/ofek-dev.ask/backend/sessions/search`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query }),
          signal,
        });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const body = await res.json();
            if (body && typeof body.detail === "string") detail = body.detail;
          } catch {
            // best-effort detail extraction
          }
          return {
            results: [],
            reasoning: "",
            error: detail,
          };
        }
        const data = await res.json();
        return {
          results: Array.isArray(data.results) ? data.results : [],
          reasoning: typeof data.reasoning === "string" ? data.reasoning : "",
          error: typeof data.error === "string" ? data.error : null,
        };
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return null;
        return {
          results: [],
          reasoning: "",
          error: e instanceof Error ? e.message : "search_failed",
        };
      }
    },
    [],
  );

  /** WS handler for backend async-reconcile progress. Backend fires
   * `session_processing_started/finished` ONLY for reconciles that
   * cross the 0.3s threshold; fast reconciles never reach this
   * handler, so the badge doesn't flash for sub-perceptible work. */
  const applySessionProcessing = useCallback(
    (update: SessionProcessingUpdate) => {
      setProcessingState((previous) => reduceSessionProcessing(previous, update));
    },
    []
  );

  /** Backend reconcile completed — silently refetch the session tree
   * if the user is currently viewing it. The initial GET may have
   * returned stale cache; this replaces it with the reconciled state
   * without a loading indicator or optimistic swap. */
  const applySessionReconciled = useCallback(
    (rootId: string, authoritative = false) => {
      const cur = currentSessionRef.current;
      // Only refetch if the user is viewing this root (or a fork in it).
      if (!cur) return;
      if (cur.id !== rootId && !findNode(cur, rootId)) return;
      // Don't clobber a live streaming turn.
      const isStreaming = cur.messages?.some(
        (m) => m.role === "assistant" && m.isStreaming
      );
      if (isStreaming && !authoritative) {
        console.info("[stale-dbg] applySessionReconciled %s: skipped (streaming)", rootId.slice(0, 8));
        return;
      }
      const id = cur.id;
      const prevMsgCount = cur.messages?.length ?? 0;
      const lastAsst = cur.messages?.filter(m => m.role === "assistant").at(-1);
      console.info(
        "[stale-dbg] applySessionReconciled %s: refetching (prev msgs=%d last_asst_evts=%d)",
        rootId.slice(0, 8), prevMsgCount, lastAsst?.events?.length ?? 0,
      );
      return fetch(`${API}/api/sessions/${id}?exchange_count=${exchangePageSize}`, {
        credentials: "include",
      })
        .then((res) => (res.ok ? res.json() : null))
        .then((tree: Session | null) => {
          if (!tree) return;
          const treeMsgCount = tree.messages?.length ?? 0;
          const treeAsst = tree.messages?.filter((m: ChatMessage) => m.role === "assistant").at(-1) as ChatMessage | undefined;
          console.info(
            "[stale-dbg] applySessionReconciled %s: REST tree msgs=%d last_asst_evts=%s last_asst_stub=%s",
            rootId.slice(0, 8), treeMsgCount,
            treeAsst?.events?.length ?? "none",
            (treeAsst as unknown as Record<string, unknown>)?.stub ? JSON.stringify((treeAsst as unknown as Record<string, unknown>).stub) : "none",
          );
          setCurrentSession((prev) => {
            if (!prev || prev.id !== tree.id) return prev;
            let carried = carryDrafts(prev, tree);
            if (!authoritative && prev.messages?.length) {
              carried = addMissingMessages(carried, prev.id, prev.messages);
            }
            return applyReconcilePreserves(carried);
          });
          return undefined;
        });
    },
    [addMissingMessages, applyReconcilePreserves, carryDrafts, exchangePageSize]
  );

  /** Update a user message's `status` field, located by `lifecycle_msg_id`.
   * Driven by the 5-state user-message lifecycle WS events so the
   * MessageStatus component reflects sent/received/done/failed. */
  const patchMessageStatus = useCallback(
    (
      sessionId: string,
      lifecycleMsgId: string,
      status: ChatMessage["status"],
      errorText?: string,
    ) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          let idx = -1;
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].lifecycle_msg_id === lifecycleMsgId) {
              idx = i;
              break;
            }
          }
          if (idx === -1) return node;
          const current = msgs[idx];
          if (current.status === status && current.errorText === errorText) return node;
          const next: ChatMessage = { ...current, status, errorText };
          return {
            ...node,
            messages: [...msgs.slice(0, idx), next, ...msgs.slice(idx + 1)],
          };
        });
      });
    },
    []
  );

  return {
    sessions,
    sessionsLoaded,
    sessionsHasMore,
    sessionsLoadingMore,
    sessionsSearching,
    currentSession,
    wsTargetSessionId,
    createSession,
    addOfflineSession,
    restoreOfflineSession,
    forkSession,
    selectSession,
    markSessionOpened,
    clearCurrentSession,
    deleteSession,
    addMessages,
    replaceMessages,
    applyMessagesReplay,
    applyStubInvalidated,
    getSinceSeq,
    getEventsFromSeq,
    getEventsCursorKnown,
    advanceEventSeq,
    updateSessionName,
    renameSession,
    togglePin,
    unpinOtherSessions,
    archiveSession,
    moveSessionToProject,
    toggleWorkerEligible,
    toggleAgentRenameAllowed,
    applySessionMetadata,
    preserveSessionMetadataThroughReconcile,
    clearSessionMetadataReconcilePreserve,
    appendSessionIfNew,
    refreshSessions: fetchSessions,
    setSessionListFilters: updateSessionListFilters,
    loadMoreSessions,
    dropSessionIfPresent,
    runStateBySession,
    applyRunState,
    applyLiveEvent,
    markTurnTerminal,
    markTurnDetached,
    applyMessageRecovering,
    applyMessageRetrying,
    applyMessageAutoRetry,
    applyMessageContent,
    applyMessageContinuation,
    applyMessageRunMeta,
    applyMessageAskResult,
    applyMessageAskChoice,
    processingByRoot,
    applySessionProcessing,
    applySessionReconciled,
    patchMessageStatus,
    appendFork,
    allOpenSessionIds,
    getNode,
    loadOlderMessages,
    sessionLoading,
    sessionLoadError,
    searchSessions,
  };
}
