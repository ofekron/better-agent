import { useState, useCallback, useEffect, useRef } from "react";
import type {
  OpenFilePanel,
  OrchestrationMode,
  RearrangerStats,
  RearrangerTree,
  RunInfo,
  Session,
  ChatMessage,
  CapabilityContext,
  TokenUsage,
  WSEvent,
} from "../types";
import type { InlineTag } from "../types/inlineTag";
import { applyLiveTurnEvent } from "../utils/applyLiveTurnEvent";
import { startOp, completeOp, failOp } from "../progress/store";
import { fetchWithTimeout, responseError } from "src/utils/offlineRequest";

import { API } from "../api";
import { useLocalStorage } from "./useLocalStorage";
import { sortSessionsForList } from "../lib/sessionSort";

export { sortSessionsForList };

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
  supervisor_enabled?: boolean;
  supervisor_custom_prompt?: string;
  pinned?: boolean;
  archived?: boolean;
  worker_eligible?: boolean;
  working_mode?: Session["working_mode"];
  working_mode_meta?: Session["working_mode_meta"];
  notes?: import("../types").Note[];
  current_todos?: import("../types").TodoItem[];
  current_tasks?: import("../types").TaskItem[];
  messages?: ChatMessage[];
  message_count?: number;
  updated_at?: string;
  pagination?: Session["pagination"];
  right_panel_open?: boolean;
  right_panel_active_tab?:
    | "files"
    | "notes"
    | "canvas"
    | "comments"
    | "todos"
    | null;
};

type SessionMetadataUpdater =
  | SessionMetadataPatch
  | ((session: Session) => SessionMetadataPatch);

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
};

function sameStringList(a?: string[], b?: string[]): boolean {
  const left = a ?? [];
  const right = b ?? [];
  if (left.length !== right.length) return false;
  return left.every((value, index) => value === right[index]);
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
    (a.sortBy ?? "") === (b.sortBy ?? "")
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

export function mergeEventlessMessageDelta(
  current: ChatMessage,
  incoming: ChatMessage,
): ChatMessage {
  if (!incoming.event_payload_omitted) return incoming;
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
      if (incoming.event_payload_omitted) {
        return mergeEventlessMessageDelta(current, incoming);
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
  return mergeEventlessMessageDelta(current, incoming);
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
    if (base.isStale) base = { ...base, isStale: false };

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
  // WS subscription target — set ONLY after REST resolves and seq cursors
  // are seeded. Prevents the WS subscribe from firing during the
  // optimistic swap (which has since_seq=0 and events_from_seq=0,
  // causing the backend to flood us with all messages and events).
  const [wsTargetSessionId, setWsTargetSessionId] = useState<string | null>(null);
  const selectRequestIdRef = useRef(0);
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
  const [processingByRoot, setProcessingByRoot] = useState<
    Record<string, boolean>
  >({});

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
  const currentSessionRef = useRef<Session | null>(null);
  currentSessionRef.current = currentSession;

  useEffect(() => {
    if (!currentSession || wsTargetSessionId === null) return;
    rememberSessionTree(currentSession);
  }, [currentSession, rememberSessionTree, wsTargetSessionId]);

  const mergeSessionPage = useCallback(
    (prev: Session[], page: Session[], replace: boolean) => {
      const filters = sessionListFiltersRef.current;
      const folderView = filters.folderView ?? false;
      const sortBy = filters.sortBy ?? "updated_at";
      if (replace) {
        const backendIds = new Set(page.map((s) => s.id));
        const pendingOffline = prev.filter(
          (s) => s.offline_pending && !backendIds.has(s.id),
        );
        return sortSessionsForList([...pendingOffline, ...page], folderView, sortBy);
      }
      const existingIds = new Set(prev.map((s) => s.id));
      return sortSessionsForList(
        [...prev, ...page.filter((s) => !existingIds.has(s.id))],
        folderView,
        sortBy,
      );
    },
    [],
  );

  const fetchSessionPage = useCallback(
    async (
      offset: number,
      replace: boolean,
      filterSnapshot: SessionListFilters = sessionListFiltersRef.current,
    ) => {
      if (sessionsLoadingPageRef.current && !replace) return;
      const requestSeq = ++sessionListRequestSeqRef.current;
      sessionsLoadingPageRef.current = true;
      if (!replace) setSessionsLoadingMore(true);
      if (replace && sessionsLoadedRef.current) setSessionsSearching(true);
      startOp(replace ? "session:list" : "session:list:more");
      try {
        const params = new URLSearchParams({
          offset: String(offset),
          limit: String(SESSION_LIST_PAGE_SIZE),
        });
        const filters = filterSnapshot;
        if (filters.projectPath) params.set("project_path", filters.projectPath);
        if (filters.search?.trim()) params.set("search", filters.search.trim());
        if (filters.searchFields) params.set("search_fields", filters.searchFields.join(","));
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
        const rawPage = data.sessions || [];
        const page = rawPage.filter(isSidebarVisibleSession);
        if (!replace && requestSeq !== sessionListRequestSeqRef.current) return;
        setSessions((prev) => mergeSessionPage(prev, page, replace));
        sessionsNextOffsetRef.current = offset + rawPage.length;
        setSessionsHasMore(Boolean(data.has_more));
      } catch {
        // ignore
      } finally {
        if (!replace) setSessionsLoadingMore(false);
        if (requestSeq === sessionListRequestSeqRef.current) {
          if (replace) setSessionsLoaded(true);
          if (replace) setSessionsSearching(false);
          sessionsLoadingPageRef.current = false;
        }
        completeOp(replace ? "session:list" : "session:list:more");
      }
    },
    [mergeSessionPage],
  );

  const fetchSessions = useCallback(async (filterSnapshot?: SessionListFilters) => {
    await fetchSessionPage(0, true, filterSnapshot);
  }, [fetchSessionPage]);

  const loadMoreSessions = useCallback(async () => {
    if (!sessionsLoaded || !sessionsHasMoreRef.current) return;
    await fetchSessionPage(sessionsNextOffsetRef.current, false);
  }, [fetchSessionPage, sessionsLoaded]);

  useEffect(() => {
    // Fire on mount + whenever we transition to 'authed'. If the mount-time
    // fetch gets a 401, this transition ensures we try again once logged in.
    if (authStatus === "authed" || !authStatus) {
      fetchSessions();
    }
  }, [fetchSessions, authStatus]);

  useEffect(() => {
    if (!sessionsLoaded) return;
    sessionsNextOffsetRef.current = 0;
    void fetchSessionPage(0, true, sessionListFilters);
  }, [fetchSessionPage, sessionListFilters, sessionsLoaded]);

  const updateSessionListFilters = useCallback((next: SessionListFilters) => {
    setSessionListFilters((prev) =>
      sameSessionListFilters(prev, next) ? prev : next,
    );
  }, []);

  const createSession = useCallback(
    async (
      name: string,
      model: string,
      cwd: string,
      orchestrationMode: OrchestrationMode = "team",
      browserHarnessEnabled: boolean = true,
      providerId?: string,
      browserHarnessHeadless: boolean = true,
      fileEditEnabled: boolean = false,
      fileEditPath?: string,
      nodeId: string = "primary",
      reasoningEffort?: string,
      clientSessionId?: string,
      capabilityContexts?: CapabilityContext[],
      folderId?: string | null,
    ) => {
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
            client_session_id: clientSessionId,
            capability_contexts: capabilityContexts && capabilityContexts.length > 0 ? capabilityContexts : undefined,
            folder_id: folderId || undefined,
          }),
        });
        if (!res.ok) {
          throw await responseError(res);
        }
        const session = await res.json();
        const filters = sessionListFiltersRef.current;
        // Dedup: the backend's `session_created` WS broadcast can land
        // on this same tab before this POST `await` resolves, in which
        // case `appendSessionIfNew` already inserted it. Without this
        // check the sidebar shows the new session twice.
        setSessions((prev) =>
          sortSessionsForList(
            prev.some((s) => s.id === session.id)
              ? prev.map((s) => (s.id === session.id ? session : s))
              : [session, ...prev],
            filters.folderView ?? false,
            filters.sortBy ?? "updated_at",
          ),
        );
        lastEventSeqBySessionRef.current = {
          ...lastEventSeqBySessionRef.current,
          [session.id]: 0,
        };
        return session;
      } finally {
        completeOp("session:create");
      }
    },
    [fetchSessions]
  );

  const addOfflineSession = useCallback((session: Session) => {
    setSessions((prev) =>
      prev.some((s) => s.id === session.id)
        ? prev
        : sortSessionsForList(
            [session, ...prev],
            sessionListFiltersRef.current.folderView ?? false,
            sessionListFiltersRef.current.sortBy ?? "updated_at",
          ),
    );
    selectRequestIdRef.current++;
    setCurrentSession(session);
    setWsTargetSessionId(null);
  }, []);

  const restoreOfflineSession = useCallback((session: Session) => {
    setSessions((prev) =>
      prev.some((s) => s.id === session.id)
        ? prev
        : sortSessionsForList(
            [session, ...prev],
            sessionListFiltersRef.current.folderView ?? false,
            sessionListFiltersRef.current.sortBy ?? "updated_at",
          ),
    );
  }, []);

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

  const selectSession = useCallback(async (id: string) => {
    const myReqId = ++selectRequestIdRef.current;
    const opId = `session:select:${id}`;
    startOp(opId);
    // Stamp last-opened on the backend (fire-and-forget) so the "last
    // opened" session sort reflects this selection. Does not gate the
    // open flow; backend bumps last_opened_at without touching updated_at.
    void fetch(`${API}/api/sessions/${encodeURIComponent(id)}/opened`, {
      method: "POST",
      credentials: "include",
    }).catch(() => {});
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
      setCurrentSession(cachedTree);
      const t = openTimingRef.current;
      if (t && t.sid === id) {
        t.restMs = 0;
        armOpenQuietTimer();
      }
      setWsTargetSessionId(id);
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
        }
        return;
      }
      // Backend returns the FULL root tree containing `id` (id may be
      // a fork — get_root_tree resolves to its root). The frontend
      // stores the whole tree in currentSession; the split-pane UI
      // reads forks from `currentSession.forks`.
      const tree = (await res.json()) as Session;
      if (myReqId !== selectRequestIdRef.current) return;
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
        if (!prev || prev.id !== tree.id) {
          console.info(
            "[stale-dbg] selectSession %s: direct tree (prev=%s tree=%s)",
            id.slice(0, 8), prev?.id?.slice(0, 8) ?? "null", tree.id.slice(0, 8),
          );
          return tree;
        }
        if (
          treeHasStreamingAssistant(prev) &&
          treeHasStreamingAssistant(tree)
        ) {
          console.info("[stale-dbg] selectSession %s: kept prev (streaming)", id.slice(0, 8));
          return prev;
        }
        const carried = carryDrafts(prev, tree);
        const treeAsst = tree.messages?.filter((m: ChatMessage) => m.role === "assistant").at(-1);
        const prevAsst = prev.messages?.filter((m: ChatMessage) => m.role === "assistant").at(-1);
        console.info(
          "[stale-dbg] selectSession %s: merging tree_msgs=%d prev_msgs=%d tree_last_evts=%d prev_last_evts=%d",
          id.slice(0, 8),
          tree.messages?.length ?? 0, prev.messages?.length ?? 0,
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
    } catch {
      // ignore
    } finally {
      setSessionLoading(false);
      completeOp(opId);
    }
  }, [cachedSessionTreeFor]);

  const deleteSession = useCallback(
    async (id: string) => {
      const opId = `session:delete:${id}`;
      startOp(opId);
      try {
        await fetch(`${API}/api/sessions/${id}`, {
          method: "DELETE",
          credentials: "include",
        });
      } finally {
        completeOp(opId);
      }
      setSessions((prev) => prev.filter((s) => s.id !== id));
      forgetSessionTree(id);
      // If we deleted a fork inside the open tree, splice it out of
      // its parent's `forks` array. If we deleted the root the user
      // is currently viewing, clear `currentSession` entirely. Either
      // way, focus + multi-WS subscribe fall through naturally on
      // next render via the tree-walking effect.
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
        return dropFork(prev);
      });
      setWsTargetSessionId((prev) => prev === id ? null : prev);
    },
    [forgetSessionTree]
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
        const byId = new Map<string, number>();
        existing.forEach((m, i) => byId.set(m.id, i));
        const merged = [...existing];
        for (const m of messages) {
          const idx = byId.get(m.id);
          if (idx !== undefined) {
            const ex = merged[idx];
            const next = mergeIncomingMessageSnapshot(ex, m);
            if (next !== null) merged[idx] = next;
          } else {
            byId.set(m.id, merged.length);
            merged.push(m);
          }
        }
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

  /** Mark the last streaming assistant message as terminal (turn
   * completed or stopped). Sets `isStreaming: false` and optionally
   * stamps `stopped_at` so the "Running…" indicator disappears
   * immediately without waiting for a REST refetch. */
  const markTurnTerminal = useCallback(
    (sessionId: string, stoppedAt?: string, interruptedByMsgId?: string | null) => {
      console.debug(
        "[stale-dbg] markTurnTerminal %s stoppedAt=%s interruptedBy=%s",
        sessionId.slice(0, 8), stoppedAt ?? "none", interruptedByMsgId ?? "none",
      );
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
            isStale: false,
            isDetached: false,
            ...(stoppedAt ? { stopped_at: stoppedAt } : {}),
            ...(interruptedByMsgId ? { interrupted_by_msg_id: interruptedByMsgId } : {}),
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
            isStale: false,
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

  /** Mark the last assistant message as stale — no events arrived for
   * STALE_TIMEOUT_MS while streaming. The orchestrator task likely died
   * silently. Stamps `isStale: true` so the bubble shows a warning
   * instead of a stuck spinner. Clears on the next event or terminal. */
  const markTurnStale = useCallback(
    (sessionId: string) => {
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const msgs = node.messages || [];
          const lastIdx = msgs.length - 1;
          const last = msgs[lastIdx];
          if (!last || last.role !== "assistant" || !last.isStreaming)
            return node;
          if (last.isStale) return node;
          const updated: ChatMessage = { ...last, isStale: true };
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
      setSessions((prev) =>
        sortSessionsForList(
          prev.map((s) => (s.id === sessionId ? { ...s, pinned: nextPinned } : s)),
          sessionListFiltersRef.current.folderView ?? false,
          sessionListFiltersRef.current.sortBy ?? "updated_at",
        )
      );
      void fetchSessions();
    },
    [fetchSessions]
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
      const unpinnedIds = new Set(
        Array.isArray(data.unpinned_ids)
          ? data.unpinned_ids.filter((id: unknown): id is string => typeof id === "string")
          : [],
      );
      if (!unpinnedIds.size) return;
      setSessions((prev) =>
        sortSessionsForList(
          prev.map((s) => (unpinnedIds.has(s.id) ? { ...s, pinned: false } : s)),
          sessionListFiltersRef.current.folderView ?? false,
          sessionListFiltersRef.current.sortBy ?? "updated_at",
        )
      );
      void fetchSessions();
    },
    [fetchSessions]
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
        prev.map((s) => (s.id === sessionId ? { ...s, archived } : s))
      );
    },
    []
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

  /** Patch the rearranger-related fields on the current session. Used by
   * App to apply `rearranger_updated` / `rearranger_state` WS events. */
  const updateRearranger = useCallback(
    (
      sessionId: string,
      patch: {
        enabled?: boolean;
        tree?: RearrangerTree | null;
        rearranger_session_id?: string | null;
        last_message_count?: number;
        rearranger_stats?: RearrangerStats | null;
        token_usage_total?: TokenUsage | null;
        token_usage_last?: TokenUsage | null;
      }
    ) => {
      const apply = (s: Session): Session => {
        const next: Session = { ...s };
        if (patch.enabled !== undefined) next.rearranger_enabled = patch.enabled;
        if (patch.tree !== undefined) next.rearranger_tree = patch.tree;
        if (patch.rearranger_session_id !== undefined) {
          next.rearranger_session_id = patch.rearranger_session_id;
        }
        if (patch.last_message_count !== undefined) {
          next.rearranger_last_message_count = patch.last_message_count;
        }
        if (patch.rearranger_stats !== undefined) {
          next.rearranger_stats = patch.rearranger_stats;
        }
        if (patch.token_usage_total !== undefined && patch.token_usage_total) {
          next.token_usage_total = patch.token_usage_total;
        }
        if (patch.token_usage_last !== undefined && patch.token_usage_last) {
          next.token_usage_last = patch.token_usage_last;
        }
        return next;
      };
      setSessions((prev) =>
        sortSessionsForList(
          prev.map((s) => (s.id === sessionId ? apply(s) : s)),
          sessionListFiltersRef.current.folderView ?? false,
          sessionListFiltersRef.current.sortBy ?? "updated_at",
        )
      );
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, apply);
      });
    },
    []
  );

  /** Merge a partial metadata patch ({inline_tags?, draft_input?, fork_closed?})
   * into the session record. Used both as the optimistic local
   * updater (typing, tag add/remove) AND as the WS broadcast applier
   * (cross-tab sync). The two are unified so a single reducer owns
   * the merge — no skew between optimistic and broadcast paths. */
  const applySessionMetadata = useCallback(
    (
      sessionId: string,
      patchOrUpdater: SessionMetadataUpdater
    ) => {
      const apply = (s: Session): Session => {
        const patch =
          typeof patchOrUpdater === "function"
            ? patchOrUpdater(s)
            : patchOrUpdater;
        const next: Session = { ...s };
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
        if (patch.supervisor_enabled !== undefined) next.supervisor_enabled = patch.supervisor_enabled;
        if (patch.supervisor_custom_prompt !== undefined) next.supervisor_custom_prompt = patch.supervisor_custom_prompt;
        if (patch.pinned !== undefined) next.pinned = patch.pinned;
        if (patch.archived !== undefined) next.archived = patch.archived;
        if (patch.worker_eligible !== undefined) next.worker_eligible = patch.worker_eligible;
        if (patch.working_mode !== undefined) next.working_mode = patch.working_mode;
        if (patch.working_mode_meta !== undefined) next.working_mode_meta = patch.working_mode_meta;
        if (patch.notes !== undefined) next.notes = patch.notes;
        if (patch.current_todos !== undefined) next.current_todos = patch.current_todos;
        if (patch.current_tasks !== undefined) next.current_tasks = patch.current_tasks;
        if (patch.messages !== undefined) next.messages = patch.messages;
        if (patch.message_count !== undefined) next.message_count = patch.message_count;
        if (patch.updated_at !== undefined) next.updated_at = patch.updated_at;
        if (patch.pagination !== undefined) next.pagination = patch.pagination;
        if (patch.right_panel_open !== undefined)
          next.right_panel_open = patch.right_panel_open;
        if (patch.right_panel_active_tab !== undefined)
          next.right_panel_active_tab = patch.right_panel_active_tab;
        return next;
      };
      setSessions((prev) =>
        sortSessionsForList(
          prev
            .map((s) => (s.id === sessionId ? apply(s) : s))
            .filter(isSidebarVisibleSession),
          sessionListFiltersRef.current.folderView ?? false,
          sessionListFiltersRef.current.sortBy ?? "updated_at",
        )
      );
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, apply);
      });
    },
    []
  );

  /** Prepend a freshly-born NON-fork session (from a WS
   * `session_created` event) into the sidebar list. Dedup by id — the
   * originating tab already inserted via the REST POST response, so
   * this only adds for OTHER tabs (INV-3 / DIV-4 multi-tab
   * convergence). No-op for ephemeral sessions (they're filtered
   * out backend-side). */
  const appendSessionIfNew = useCallback((session: Session) => {
    if (!isSidebarVisibleSession(session)) return;
    const filters = sessionListFiltersRef.current;
    setSessions((prev) => {
      if (prev.some((s) => s.id === session.id)) return prev;
      return sortSessionsForList(
        [session, ...prev],
        filters.folderView ?? false,
        filters.sortBy ?? "updated_at",
      );
    });
  }, []);

  /** Drop a session by id (from a WS `session_deleted` event). Mirrors
   * the optimistic removal in `deleteSession`, but driven by the WS
   * event so other tabs converge AND so the originating tab still
   * cleans up if the REST response was lost / the request was
   * cancelled mid-flight. Idempotent by id. */
  const dropSessionIfPresent = useCallback((id: string) => {
    forgetSessionTree(id);
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
  }, [forgetSessionTree]);

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
    async (sessionId: string, beforeSeq: number) => {
      const res = await fetch(
        `${API}/api/sessions/${sessionId}/messages?before_seq=${beforeSeq}&exchange_count=${exchangePageSize}`
      );
      if (!res.ok) return;
      const data = await res.json();
      const older: ChatMessage[] = data.messages || [];
      if (older.length === 0) return;
      setCurrentSession((prev) => {
        if (!prev) return prev;
        return updateNodeById(prev, sessionId, (node) => {
          const existing = node.messages || [];
          const oldest = older[0]?.seq ?? null;
          return {
            ...node,
            messages: [...older, ...existing],
            pagination: {
              total_messages: data.total_messages,
              oldest_loaded_seq: oldest,
              has_older: data.has_older,
            },
          };
        });
      });
    },
    [exchangePageSize]
  );

  const clearCurrentSession = useCallback(() => {
    selectRequestIdRef.current++;
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
      session_ids: string[];
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
            session_ids: [],
            reasoning: "",
            error: detail,
          };
        }
        const data = await res.json();
        return {
          session_ids: Array.isArray(data.session_ids) ? data.session_ids : [],
          reasoning: typeof data.reasoning === "string" ? data.reasoning : "",
          error: typeof data.error === "string" ? data.error : null,
        };
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return null;
        return {
          session_ids: [],
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
    (rootId: string, kind: "started" | "finished") => {
      setProcessingByRoot((prev) => {
        const active = kind === "started";
        if (!!prev[rootId] === active) return prev;
        const next = { ...prev };
        if (active) next[rootId] = true;
        else delete next[rootId];
        return next;
      });
    },
    []
  );

  /** Backend reconcile completed — silently refetch the session tree
   * if the user is currently viewing it. The initial GET may have
   * returned stale cache; this replaces it with the reconciled state
   * without a loading indicator or optimistic swap. */
  const applySessionReconciled = useCallback(
    (rootId: string) => {
      const cur = currentSessionRef.current;
      // Only refetch if the user is viewing this root (or a fork in it).
      if (!cur) return;
      if (cur.id !== rootId && !findNode(cur, rootId)) return;
      // Don't clobber a live streaming turn.
      const isStreaming = cur.messages?.some(
        (m) => m.role === "assistant" && m.isStreaming
      );
      if (isStreaming) {
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
      fetch(`${API}/api/sessions/${id}?exchange_count=${exchangePageSize}`, {
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
            const carried = carryDrafts(prev, tree);
            if (prev.messages?.length) {
              return addMissingMessages(carried, prev.id, prev.messages);
            }
            return carried;
          });
        });
    },
    [exchangePageSize]
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
    toggleWorkerEligible,
    updateRearranger,
    applySessionMetadata,
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
    markTurnStale,
    applyMessageRecovering,
    applyMessageRetrying,
    applyMessageAutoRetry,
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
    searchSessions,
  };
}
