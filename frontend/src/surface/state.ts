// Native Contract-Node store — the per-session state backing surface/
// components. Unlike adapter/useSurfaceSession.ts (which maps every frame
// down into legacy ChatMessage/WSEvent for the flag-gated ba.surface_v2
// path), this store keeps everything in NodeWire/frame shape end to end:
// components read NodeWire fields directly, nothing Claude-flavored or
// legacy-typed crosses this boundary (ADR 0010 §Precondition; wire.ts is
// the component input contract).
//
// Reuses adapter/turnNodeTable.ts (already shape-native) as the generic
// "one container's direct children, kept sorted by (ts, seq)" primitive —
// not just for a turn's own body, but for EVERY container node in the
// tree (a turn, an explanation, a native_subagent_turn/worker_turn/
// sub_session_turn/session_turn): chat-panel.md's `children(item)` is one
// operation regardless of which kind owns the children, so one generic
// table type serves all of them (adapter/turnNodeTable.ts's own docstring
// already frames it as "one turn's body nodes" — a container's body is
// the same shape either way).
//
// Reuses adapter/client.ts's SurfaceClient/SurfaceSocket verbatim (already
// shape-native transport, ADR 0006's plane 3/4) — no new transport layer.

import { SurfaceClient, type SurfaceSocket } from "../adapter/client";
import {
  createTurnNodeTable,
  getTurnNode,
  hasTurnNode,
  upsertTurnNode,
  type TurnNodeTable,
} from "../adapter/turnNodeTable";
import { uuidv4 } from "../lib/uuid";
import {
  CONTRACT_VERSION,
  type AssistantTextPayloadWire,
  type AttachmentInputWire,
  type ChatFrame,
  type ChildManifestWire,
  type NodeId,
  type NodeWire,
  type SendModeWire,
  type ThinkingPayloadWire,
  type RunWire,
  type SnapshotIdentity,
  type TerminalReasonWire,
  type TransportAckFrame,
  type TurnId,
  type TurnPhaseWire,
  type TypedPromptPayloadWire,
  type UsageWire,
} from "../adapter/wire";

/** Turn phases chat-panel.md's `isLive(turn)` treats as "running" —
 * everything else (`completed | stopped | failed`, or no lifecycle frame
 * observed yet) is not-live. */
const LIVE_PHASES: ReadonlySet<TurnPhaseWire> = new Set([
  "queued",
  "starting",
  "running",
  "awaiting_interaction",
  "reconnecting",
  "stopping",
]);

export function isLivePhase(phase: TurnPhaseWire | null): boolean {
  return phase !== null && LIVE_PHASES.has(phase);
}

/** Client-only send-in-flight state for a turn scaffolded by
 * `SurfaceStore.sendPrompt` before the backend's own confirmed
 * `typed_prompt` node exists — never part of the wire contract (NodeWire
 * stays a pure mirror of the backend, per wire.ts's own docstring). Kept
 * on TurnEntry (a store-internal type) rather than smuggled into a
 * NodeWire field, so components can render "sending"/"failed, retry"
 * chrome without any client-synthesized value pretending to be a real
 * ContentStatusWire. */
export interface ProvisionalSend {
  intentId: string;
  status: "sending" | "error";
  errorMessage: string | null;
}

/** Everything `sendPrompt`/`retrySend` needs to re-scaffold a provisional
 * turn from scratch — the durable record `ProvisionalSendRegistry` keeps,
 * independent of any one `SurfaceStore` instance's lifetime. */
interface ProvisionalSendRecord {
  intentId: string;
  text: string;
  attachments: readonly AttachmentInputWire[];
  sendMode: SendModeWire;
  status: "sending" | "error";
  errorMessage: string | null;
  /** Wall-clock seconds at the ORIGINAL send attempt (not touched by
   * `retrySend`'s fresh-intent-id replacement) — keeps a reseeded
   * provisional turn's tail position stable relative to other provisional
   * entries across a session switch, instead of jumping to "now". */
  createdAtSeconds: number;
}

/** A `SurfaceStore` is disposed and recreated on every session switch
 * (`useSurfaceStore.ts`'s effect) — a provisional send living only on that
 * instance's own `turnsById`/`pendingByIntentId` would vanish the moment the
 * user switches away, even though the backend may still be processing (or
 * may have already rejected) it. This registry is the SOLE durable source of
 * truth for "a send this client issued but has not yet seen reconciled or
 * abandoned" — module-level, keyed by session_id, outliving any one store
 * instance. Every `SurfaceStore` method that mutates a provisional send's
 * state (`sendPrompt`/`failProvisionalSend`/`reconcileProvisionalSend`/
 * `retrySend`) writes through to it; `hydrate()` reads it back via
 * `reseedProvisionalSends` so a session re-selected later shows exactly the
 * entries it left with (pending or "error", Retry-able) minus whatever the
 * fresh snapshot itself already proves resolved (see that method). A
 * store's own `turnsById`/`pendingByIntentId` are just this session's
 * current PROJECTION of the registry, same relationship `childrenTables`
 * has to the backend's own node graph. */
class ProvisionalSendRegistry {
  private bySession = new Map<string, Map<string, ProvisionalSendRecord>>();

  list(sessionId: string): ProvisionalSendRecord[] {
    const m = this.bySession.get(sessionId);
    return m ? [...m.values()].sort((a, b) => a.createdAtSeconds - b.createdAtSeconds) : [];
  }

  get(sessionId: string, intentId: string): ProvisionalSendRecord | undefined {
    return this.bySession.get(sessionId)?.get(intentId);
  }

  set(sessionId: string, record: ProvisionalSendRecord): void {
    let m = this.bySession.get(sessionId);
    if (!m) {
      m = new Map();
      this.bySession.set(sessionId, m);
    }
    m.set(record.intentId, record);
  }

  delete(sessionId: string, intentId: string): void {
    const m = this.bySession.get(sessionId);
    if (!m) return;
    m.delete(intentId);
    if (m.size === 0) this.bySession.delete(sessionId);
  }
}

const provisionalSendRegistry = new ProvisionalSendRegistry();

export interface TurnEntry {
  turnId: TurnId;
  turn: NodeWire;
  prompt: NodeWire | null;
  /** turn.Result's parts (chat-panel.md ResultPart+ / DerivedResult),
   * kept id-deduped, sorted (ts, seq). Never includes body items — the
   * backend's raw `children(turn_id)` scan does NOT distinguish Result
   * nodes from body items by parent_id alone (both carry parent_id ==
   * turn.node_id), so any children fetched via `getChildren`/
   * `ensureChildren` below are filtered through `bodyItemsOf` to strip
   * them back out before rendering as BodyItems. */
  results: NodeWire[];
  manifest: ChildManifestWire;
  runtimeChange: NodeWire | null;
  phase: TurnPhaseWire | null;
  reason: TerminalReasonWire | null;
  usage: UsageWire | null;
  /** Set only for a turn `sendPrompt` scaffolded client-side, cleared the
   * moment the backend's own confirmed `typed_prompt` node reconciles it
   * (see `reconcileProvisionalSend`). Null for every real, backend-born
   * turn. */
  provisionalSend: ProvisionalSend | null;
}

/** `children(turn_id)` mixes Result nodes in with real BodyItems (backend
 * parent_id scan has no kind filter) — Results already render via
 * `TurnEntry.results`/`turn.Result`, so every "get this container's body
 * items" call point filters them back out here. Harmless (no-op) for any
 * other container id, since only a turn ever parents a `result` node. */
export function bodyItemsOf(children: readonly NodeWire[]): NodeWire[] {
  return children.filter((n) => n.kind !== "result");
}

interface NodeLocation {
  table: TurnNodeTable;
}

export interface SurfaceStoreSnapshot {
  identity: SnapshotIdentity | null;
  instructionWidget: NodeWire | null;
  /** turnId, in (ts, seq) order. */
  turnOrder: readonly TurnId[];
  turnsById: ReadonlyMap<TurnId, TurnEntry>;
  runsById: ReadonlyMap<string, RunWire>;
  olderCursor: string | null;
  loadingOlder: boolean;
  /** Set when the most recent `loadOlder()` attempt threw (network error,
   * non-2xx REST response); cleared the moment a retry is attempted. Never
   * clears `olderCursor` on failure, so the same retry affordance keeps
   * working and a later retry can still succeed. */
  olderError: string | null;
  /** True once the first snapshot/resync completes; false while a fresh
   * session is still hydrating (initial REST fetch in flight). */
  hydrated: boolean;
  /** Mirrors the `/ws/v2/surface` socket's own OPEN state (updated via
   * `SurfaceSocketHandlers.onOpen`/`onClose`, event-driven — never
   * polled). `sendPrompt` itself doesn't consult this (submitting while
   * closed still surfaces a real error via the provisional entry, same as
   * legacy) — it exists so a caller with a durable fallback transport
   * (e.g. the legacy offline queue) can decide NOT to attempt the native
   * path at all while offline, instead of racing a doomed submit. */
  socketOpen: boolean;
}

function byTsSeq(a: { ts: number; seq: number }, b: { ts: number; seq: number }): number {
  return a.ts - b.ts || a.seq - b.seq;
}

const EMPTY_MANIFEST: ChildManifestWire = { renderable_child_count: 0, has_children: false };

function scaffoldTurn(promptNode: NodeWire): NodeWire {
  return {
    ...promptNode,
    node_id: `turn:${promptNode.turn_id}`,
    kind: "turn",
    payload: null,
    status: null,
    child_manifest: null,
  };
}

/** One session's native surface store. Not a React hook itself — see
 * `useSurfaceStore` below, which owns the instance's lifecycle and
 * re-renders subscribers via `useSyncExternalStore`. */
export class SurfaceStore {
  private readonly client = new SurfaceClient();
  private socket: SurfaceSocket | null = null;
  private socketOpened = false;
  private cancelled = false;

  private identity: SnapshotIdentity | null = null;
  private instructionWidget: NodeWire | null = null;
  private turnOrder: TurnId[] = [];
  private turnsById = new Map<TurnId, TurnEntry>();
  private runsById = new Map<string, RunWire>();
  private olderCursor: string | null = null;
  private loadingOlder = false;
  private olderError: string | null = null;
  private hydrated = false;
  private socketConnected = false;

  /** containerNodeId -> its direct children, one level (chat-panel.md's
   * `children(item)`). Populated lazily via `ensureChildren`, or eagerly
   * seeded at hydrate time for the trailing/live turn (mirrors backend's
   * `_build_turn_view` eager-bounded-nodes design — see module docstring
   * on why the last window turn always arrives pre-expanded). */
  private childrenTables = new Map<NodeId, TurnNodeTable>();
  /** node_id -> which children table currently holds it, for O(1)
   * text_delta/node_status routing instead of scanning every table. */
  private nodeLocation = new Map<NodeId, NodeLocation>();
  /** containerNodeId -> in-flight fetch, so concurrent `ensureChildren`
   * callers (e.g. two components racing to expand the same node) share
   * one request instead of double-fetching. */
  private pendingFetches = new Map<NodeId, Promise<NodeWire[]>>();
  /** turn_id -> the most recent `turn_lifecycle` frame seen for a turn
   * this store hasn't birthed yet (e.g. delivered before that turn's
   * first node_upsert, or tagged with a provisional turn_id the backend
   * later supersedes without ever re-announcing it). Applied the moment
   * that turn IS birthed instead of being dropped — see `birthTurn`. */
  private pendingLifecycle = new Map<
    TurnId,
    { phase: TurnPhaseWire; reason: TerminalReasonWire | null; usage: UsageWire | null }
  >();
  /** intent_id -> the provisional turn_id `sendPrompt` scaffolded for it,
   * for O(1) lookup when the backend's own confirmed `typed_prompt` node
   * (or a rejection) arrives. Cleared by `reconcileProvisionalSend` on
   * success; deliberately left in place on failure (see
   * `failProvisionalSend`) so a late-arriving real node can still
   * self-heal a false-negative error, and so `retrySend` can find it. */
  private pendingByIntentId = new Map<string, TurnId>();

  private readonly subscribers = new Set<() => void>();
  private cachedSnapshot: SurfaceStoreSnapshot | null = null;
  private readonly sessionId: string;

  constructor(sessionId: string) {
    this.sessionId = sessionId;
    this.socket = this.client.openSocket({
      onFrame: (frame) => this.handleFrame(frame),
      onResyncRequired: () => void this.hydrate(),
      onOpen: () => {
        this.socketConnected = true;
        this.notify();
      },
      onClose: () => {
        this.socketConnected = false;
        this.notify();
      },
    });
    void this.hydrate();
  }

  subscribe = (cb: () => void): (() => void) => {
    this.subscribers.add(cb);
    return () => this.subscribers.delete(cb);
  };

  getSnapshot = (): SurfaceStoreSnapshot => {
    if (this.cachedSnapshot) return this.cachedSnapshot;
    this.cachedSnapshot = {
      identity: this.identity,
      instructionWidget: this.instructionWidget,
      turnOrder: this.turnOrder,
      turnsById: this.turnsById,
      runsById: this.runsById,
      olderCursor: this.olderCursor,
      loadingOlder: this.loadingOlder,
      olderError: this.olderError,
      hydrated: this.hydrated,
      socketOpen: this.socketConnected,
    };
    return this.cachedSnapshot;
  };

  private notify(): void {
    if (this.cancelled) return;
    this.cachedSnapshot = null;
    for (const cb of this.subscribers) cb();
  }

  dispose(): void {
    this.cancelled = true;
    this.socket?.close();
    this.socket = null;
  }

  /** Returns the cached one-level children of `nodeId`, if already fetched
   * (or eagerly seeded), else undefined — never triggers a fetch (matches
   * "collapsed rendering never fetches a hidden subtree"). */
  getChildren(nodeId: NodeId): NodeWire[] | undefined {
    return this.childrenTables.get(nodeId)?.nodes;
  }

  /** Fetches (once) `children(nodeId)` at the current render_rev and caches
   * it — the on-demand-expansion fetch chat-panel.md's `lookupForRender`
   * describes ("fetch direct children only when that item is explicitly
   * expanded"). A subsequent collapse+reopen reuses the cache with no new
   * request; only invalidated by a live upsert changing that node's own
   * `child_manifest` (see `handleFrame`'s container-manifest-change path). */
  async ensureChildren(nodeId: NodeId): Promise<NodeWire[]> {
    const cached = this.childrenTables.get(nodeId);
    if (cached) return cached.nodes;
    const pending = this.pendingFetches.get(nodeId);
    if (pending) return pending;
    const atRenderRev = this.identity?.render_rev ?? 0;
    const promise = this.client
      .fetchChildren(this.sessionId, nodeId, atRenderRev)
      .then((envelope) => {
        this.pendingFetches.delete(nodeId);
        if (this.cancelled) return [];
        if (envelope.kind !== "ok") return [];
        this.setChildrenTable(nodeId, envelope.value);
        this.notify();
        return envelope.value;
      });
    this.pendingFetches.set(nodeId, promise);
    return promise;
  }

  /** A children fetch (`ensureChildren`) may resolve AFTER live frames have
   * already populated (or extended) this same container's table — e.g. a
   * cold-born turn's on-demand fetch racing its own first live
   * `node_upsert` (the fetch is issued the instant the turn mounts, before
   * the node it's fetching for necessarily exists server-side yet). Merge
   * into whatever's already cached by node identity instead of replacing
   * the map entry wholesale: a node the fetch response doesn't mention at
   * all (because it hadn't been created yet when the fetch was issued) is
   * left untouched, and a node present in both is only overwritten when
   * the fetched copy is not older (by `cv`) than what a live upsert has
   * already applied — so a late-resolving fetch can only add/refresh
   * content, never discard a newer live node (`hydrate()` always resets
   * `childrenTables` to empty first, so this is a no-op merge-into-nothing
   * there — behavior unchanged for cold hydrate/resync). */
  private setChildrenTable(nodeId: NodeId, nodes: readonly NodeWire[]): void {
    const table = this.childrenTables.get(nodeId) ?? createTurnNodeTable();
    const sorted = [...nodes].sort(byTsSeq);
    for (const n of sorted) {
      const current = getTurnNode(table, n.node_id);
      if (current && current.cv > n.cv) continue;
      upsertTurnNode(table, n);
      this.nodeLocation.set(n.node_id, { table });
    }
    this.childrenTables.set(nodeId, table);
  }

  private currentCursor() {
    if (!this.identity) return null;
    return {
      surface_id: this.sessionId,
      incarnation: this.identity.incarnation,
      render_rev: this.identity.render_rev,
    };
  }

  private syncSocketCursor(resend: boolean): void {
    const cursor = this.currentCursor();
    if (!this.socket || !cursor) return;
    if (!this.socketOpened) {
      this.socket.open([cursor], "opened");
      this.socketOpened = true;
    } else if (resend) {
      this.socket.updateCursors([cursor]);
    } else {
      this.socket.trackCursor([cursor]);
    }
  }

  async loadOlder(): Promise<void> {
    if (this.loadingOlder || !this.olderCursor) return;
    this.loadingOlder = true;
    this.olderError = null;
    this.notify();
    try {
      const envelope = await this.client.fetchOlder(this.sessionId, this.olderCursor);
      if (this.cancelled) return;
      if (envelope.kind === "stale_cursor") {
        await this.hydrate();
        return;
      }
      if (envelope.kind === "rebuilding") return;
      const olderIds: TurnId[] = [];
      for (const compact of envelope.turns) {
        const turnId = compact.turn.turn_id;
        if (this.turnsById.has(turnId)) continue;
        this.turnsById.set(turnId, this.newTurnEntry(compact.turn, compact.prompt, compact.results, compact.manifest, compact.runtime_change));
        olderIds.push(turnId);
      }
      this.turnOrder = [...olderIds, ...this.turnOrder];
      for (const run of envelope.runs) this.runsById.set(run.run_ref, run);
      this.olderCursor = envelope.older_cursor;
    } catch (err) {
      if (this.cancelled) return;
      // Leaves `olderCursor` untouched so the retry affordance below keeps
      // targeting the same page — a rejected fetch (network error, non-2xx
      // REST response) must not silently drop the ability to page further.
      this.olderError = err instanceof Error ? err.message : String(err);
    } finally {
      this.loadingOlder = false;
      this.notify();
    }
  }

  // ---- command plane: sending a prompt ---------------------------------

  /** Submits a `SendPrompt` intent over the live `/ws/v2/surface`
   * connection (backend/adapter_api.py `_parse_send_prompt`) and inserts a
   * provisional turn — one client-synthesized `turn`+`typed_prompt` node
   * pair, keyed by a fresh `intent_id` — at the tail of `turnOrder` so the
   * composer's submit renders instantly (visual parity with legacy's
   * optimistic bubble), before any backend round trip completes. Returns
   * the `intent_id`, which is both the provisional turn's `turnId` lookup
   * key (via `pendingByIntentId`) and `retrySend`'s argument.
   *
   * Reconciled in one of two ways once the backend responds:
   *   - success: a real `typed_prompt` node_upsert carrying this same
   *     `intent_id` in its payload arrives over the live plane and
   *     `reconcileProvisionalSend` (called from `handleFrame`) swaps the
   *     provisional turn for the real one, in place.
   *   - failure: the submit's own ack resolves `intent_rejected` (backend
   *     rejection, e.g. `unsupported_attachments`) OR the socket isn't
   *     OPEN at all (`SurfaceSocket.submit` returns `null` synchronously,
   *     same not-open contract as legacy's `sendMessage`) — either way
   *     `failProvisionalSend` flips the entry to an inline-retry error
   *     state. No client-side queue-until-open: matches legacy's
   *     behavior of surfacing failure immediately rather than buffering
   *     in this layer (a caller with a durable transport, e.g. the legacy
   *     offline queue, decides whether to even attempt this call while
   *     `snapshot.socketOpen` is false — see Chat.tsx's integration). */
  sendPrompt(
    text: string,
    attachments: readonly AttachmentInputWire[] = [],
    sendMode: SendModeWire = "queue",
  ): string {
    const intentId = uuidv4();
    const record: ProvisionalSendRecord = {
      intentId,
      text,
      attachments,
      sendMode,
      status: "sending",
      errorMessage: null,
      createdAtSeconds: Date.now() / 1000,
    };
    provisionalSendRegistry.set(this.sessionId, record);
    this.insertProvisionalEntry(record);
    this.notify();

    const intent = {
      kind: "send_prompt" as const,
      cv: CONTRACT_VERSION,
      intent_id: intentId,
      session_id: this.sessionId,
      text,
      attachments: [...attachments],
      send_mode: sendMode,
      target: { kind: "current" as const, fork_node_id: null },
    };
    const ack = this.socket?.submit(intent);
    if (!ack) {
      this.failProvisionalSend(intentId, "not connected");
    } else {
      ack.then((frame: TransportAckFrame) => {
        if (frame.type === "intent_rejected") this.failProvisionalSend(intentId, frame.message);
      });
    }
    return intentId;
  }

  /** Builds the provisional `turn`+`typed_prompt` node pair for a
   * `ProvisionalSendRecord` — the one place that shape is constructed,
   * shared by a fresh `sendPrompt` call and `reseedProvisionalSends`
   * reconstructing one from the durable registry after a session switch. */
  private buildProvisionalEntry(record: ProvisionalSendRecord): { turnId: TurnId; entry: TurnEntry } {
    const provisionalTurnId: TurnId = `pending:${record.intentId}`;
    const promptPayload: TypedPromptPayloadWire = {
      text: record.text,
      attachments: record.attachments.map((a) => ({ ...a, size: null })),
      send_mode: record.sendMode,
      origin: "user",
      source_session_ref: null,
      sent_text: null,
      intent_id: record.intentId,
    };
    const promptNode: NodeWire = {
      cv: 0,
      node_id: `pending-prompt:${record.intentId}`,
      parent_id: null,
      turn_id: provisionalTurnId,
      surface_id: this.sessionId,
      kind: "typed_prompt",
      ts: record.createdAtSeconds,
      seq: 0,
      status: null,
      payload: promptPayload,
      run_ref: null,
      sidecar_ref: null,
      target_ref: null,
      child_manifest: null,
    };
    const entry: TurnEntry = {
      ...this.newTurnEntry(scaffoldTurn(promptNode), promptNode, [], null, null),
      provisionalSend: { intentId: record.intentId, status: record.status, errorMessage: record.errorMessage },
    };
    return { turnId: provisionalTurnId, entry };
  }

  /** Inserts a provisional turn at the tail of `turnOrder`, registering it
   * for `pendingByIntentId` lookup — the store-local PROJECTION half of a
   * registry record (the registry write itself is the caller's job, since
   * `reseedProvisionalSends` writes nothing new to the registry, only
   * projects what's already there). */
  private insertProvisionalEntry(record: ProvisionalSendRecord): void {
    const { turnId, entry } = this.buildProvisionalEntry(record);
    this.turnsById.set(turnId, entry);
    this.turnOrder = [...this.turnOrder, turnId];
    this.pendingByIntentId.set(record.intentId, turnId);
  }

  /** Re-submits a failed provisional send under a FRESH `intent_id` —
   * parity with legacy's own retry semantics (client-id.test.ts: a retry
   * replaces the old failed optimistic entry with one new pending entry,
   * it never reuses the failed send's id). No-op (returns null) unless
   * `intentId` currently names a provisional entry in the "error" state. */
  retrySend(intentId: string): string | null {
    const turnId = this.pendingByIntentId.get(intentId);
    if (!turnId) return null;
    const entry = this.turnsById.get(turnId);
    if (!entry || !entry.provisionalSend || entry.provisionalSend.status !== "error" || !entry.prompt) {
      return null;
    }
    const payload = entry.prompt.payload as TypedPromptPayloadWire;
    this.turnsById.delete(turnId);
    this.turnOrder = this.turnOrder.filter((id) => id !== turnId);
    this.pendingByIntentId.delete(intentId);
    provisionalSendRegistry.delete(this.sessionId, intentId);
    this.notify();
    return this.sendPrompt(payload.text, payload.attachments, payload.send_mode);
  }

  private failProvisionalSend(intentId: string, message: string): void {
    const turnId = this.pendingByIntentId.get(intentId);
    if (!turnId) return; // already reconciled by a real node_upsert racing the rejection
    const entry = this.turnsById.get(turnId);
    if (!entry || !entry.provisionalSend) return;
    this.turnsById.set(turnId, {
      ...entry,
      provisionalSend: { ...entry.provisionalSend, status: "error", errorMessage: message },
    });
    const record = provisionalSendRegistry.get(this.sessionId, intentId);
    if (record) {
      provisionalSendRegistry.set(this.sessionId, { ...record, status: "error", errorMessage: message });
    }
    this.notify();
  }

  // ---- command plane: queued-prompt edit/delete --------------------------
  //
  // The native surface has no live projection of a queued (not-yet-running)
  // prompt in its own turn tree today (see wire.ts's `EditQueuedIntentWire`
  // docstring — `adapters/normalize.py` drops that row entirely) — the
  // queued-banner UI (InputArea.tsx, driven by Chat.tsx/App.tsx's legacy
  // `queuedPrompt`/`queuedBySession` projection) stays legacy-owned
  // regardless of `ba.surface_native`. These two methods only replace the
  // WIRE TRANSPORT an edit/delete action goes over when eligible — no
  // provisional/optimistic state of this store's own to manage, so unlike
  // `sendPrompt` there is nothing to reconcile later; the ack is returned
  // purely so a caller/test can observe accept/reject, fire-and-forget
  // otherwise (same as legacy's own `sendUpdateQueued`/`sendCancelQueued`).

  editQueued(queuedId: string, text: string): Promise<TransportAckFrame> | null {
    return (
      this.socket?.submit({
        kind: "edit_queued",
        cv: CONTRACT_VERSION,
        intent_id: uuidv4(),
        session_id: this.sessionId,
        node_id: queuedId,
        text,
      }) ?? null
    );
  }

  deleteQueued(queuedId: string): Promise<TransportAckFrame> | null {
    return (
      this.socket?.submit({
        kind: "delete_queued",
        cv: CONTRACT_VERSION,
        intent_id: uuidv4(),
        session_id: this.sessionId,
        node_id: queuedId,
      }) ?? null
    );
  }

  /** Replaces a provisional send's scaffolded turn with the backend's own
   * confirmed `typed_prompt` node, in place (same `turnOrder` slot) —
   * called from `handleFrame`'s `node_upsert` branch BEFORE the normal
   * `birthTurn`/typed_prompt-upsert path runs, so the real turn never
   * additionally gets birthed at the tail. Returns false (does nothing)
   * when `node` isn't a match for any currently-pending provisional send,
   * so the caller can fall through to normal handling unconditionally. */
  private reconcileProvisionalSend(node: NodeWire): boolean {
    if (node.kind !== "typed_prompt") return false;
    const intentId = (node.payload as TypedPromptPayloadWire | null)?.intent_id;
    if (!intentId) return false;
    const provisionalTurnId = this.pendingByIntentId.get(intentId);
    if (!provisionalTurnId) return false;
    this.pendingByIntentId.delete(intentId);
    provisionalSendRegistry.delete(this.sessionId, intentId);
    const idx = this.turnOrder.indexOf(provisionalTurnId);
    this.turnsById.delete(provisionalTurnId);

    // The real turn_id may already have a live entry (a non-prompt frame
    // for it arrived out of order, ahead of this typed_prompt — see
    // `birthTurn`'s own docstring) — merge onto it rather than clobbering
    // whatever it already accumulated (its `phase` in particular — already
    // "running"/live via `birthTurn`'s own default), and drop the now-
    // redundant provisional slot instead of duplicating turnOrder.
    const already = this.turnsById.get(node.turn_id);
    const turn = already && already.prompt !== null ? already.turn : scaffoldTurn(node);
    let entry: TurnEntry;
    if (already) {
      entry = { ...already, turn, prompt: node };
    } else {
      // A turn reconciled from a just-submitted send is, by construction,
      // currently in flight — same "live by construction, no lifecycle
      // frame to wait for" reasoning as `birthTurn`'s own default, and the
      // SAME pendingLifecycle buffer applies (a `turn_lifecycle` frame for
      // this real turn_id may have already arrived and be waiting, tagged
      // to a turn_id this store hadn't birthed yet).
      const pending = this.pendingLifecycle.get(node.turn_id);
      this.pendingLifecycle.delete(node.turn_id);
      entry = {
        ...this.newTurnEntry(turn, node, [], null, null),
        phase: pending?.phase ?? "running",
        reason: pending?.reason ?? null,
        usage: pending?.usage ?? null,
      };
    }
    this.turnsById.set(node.turn_id, entry);

    if (already) {
      if (idx >= 0) this.turnOrder = this.turnOrder.filter((_, i) => i !== idx);
    } else if (idx >= 0) {
      const next = [...this.turnOrder];
      next[idx] = node.turn_id;
      this.turnOrder = next;
    } else {
      this.turnOrder = [...this.turnOrder, node.turn_id];
    }
    return true;
  }

  private newTurnEntry(
    turn: NodeWire,
    prompt: NodeWire | null,
    results: readonly NodeWire[],
    manifest: ChildManifestWire | null,
    runtimeChange: NodeWire | null,
  ): TurnEntry {
    return {
      turnId: turn.turn_id,
      turn,
      prompt,
      results: [...results].sort(byTsSeq),
      manifest: manifest ?? EMPTY_MANIFEST,
      runtimeChange,
      phase: null,
      reason: null,
      usage: null,
      provisionalSend: null,
    };
  }

  private async hydrate(): Promise<void> {
    const envelope = await this.client.fetchSnapshot(this.sessionId);
    if (this.cancelled) return;
    if (envelope.kind === "stale_cursor") return;
    if (envelope.kind === "rebuilding") {
      setTimeout(() => {
        if (!this.cancelled) void this.hydrate();
      }, envelope.retry_after_ms ?? 500);
      return;
    }

    this.identity = envelope.snapshot_identity;
    this.instructionWidget = envelope.instruction_widget ?? null;
    this.turnsById = new Map();
    this.runsById = new Map();
    this.childrenTables = new Map();
    this.nodeLocation = new Map();
    this.pendingLifecycle = new Map();
    // Rebuilt below by `reseedProvisionalSends` — a stale entry pointing at
    // a provisional turn id from BEFORE this (re)hydrate would otherwise
    // survive pointing at a turn that no longer exists in the fresh
    // `turnsById` above.
    this.pendingByIntentId = new Map();
    for (const run of envelope.runs) this.runsById.set(run.run_ref, run);

    for (const compact of envelope.turns) {
      const turnId = compact.turn.turn_id;
      this.turnsById.set(
        turnId,
        this.newTurnEntry(compact.turn, compact.prompt, compact.results, compact.manifest, compact.runtime_change),
      );
    }
    this.turnOrder = envelope.turns.map((t) => t.turn.turn_id).sort((a, b) => {
      const ta = this.turnsById.get(a)!.turn;
      const tb = this.turnsById.get(b)!.turn;
      return byTsSeq(ta, tb);
    });
    this.olderCursor = envelope.older_cursor;

    // Eager-seed the trailing turn's fully-bounded node set (see
    // `_build_turn_view`'s `bounded_nodes`) so a live/last turn renders
    // instantly with zero extra round trips, mirroring the backend's own
    // "last window turn always arrives pre-expanded" design.
    this.seedLiveTurnNodes(envelope.live_turn_nodes);

    // Restore whatever provisional sends this session still has outstanding
    // in `ProvisionalSendRegistry` — the durable counterpart to a
    // `SurfaceStore` instance being disposed/recreated on every session
    // switch (see that class's docstring). Runs AFTER the real turns above
    // are in place so a send that resolved while this session was inactive
    // is recognized as already-present instead of re-scaffolded.
    this.reseedProvisionalSends();

    this.hydrated = true;
    this.notify();
    this.syncSocketCursor(true);
  }

  /** Counterpart to every `ProvisionalSendRegistry` write above — reads it
   * back for `this.sessionId` after a (re)hydrate and, for each record
   * still outstanding, either drops it (the fresh snapshot already proves
   * it resolved — some turn's `prompt.payload.intent_id` matches) or
   * re-scaffolds its provisional turn at the tail, exactly as `sendPrompt`
   * originally inserted it (same helper, `insertProvisionalEntry`) — this
   * is what makes a pending/"error, Retry" entry survive a session switch:
   * the OLD store instance (this session's previous mount) is long disposed,
   * but the registry it wrote through to is still there for the NEW
   * instance being built right now to read. */
  private reseedProvisionalSends(): void {
    for (const record of provisionalSendRegistry.list(this.sessionId)) {
      if (this.findTurnByIntentId(record.intentId)) {
        provisionalSendRegistry.delete(this.sessionId, record.intentId);
        continue;
      }
      this.insertProvisionalEntry(record);
    }
  }

  /** Scans the CURRENT (already-hydrated) `turnsById` for a turn whose real,
   * backend-confirmed prompt carries `intentId` — the snapshot-side mirror
   * of `reconcileProvisionalSend`'s live-frame match, used when the
   * confirming `typed_prompt` arrived while this session's socket was
   * closed (switched away) instead of over a live frame this store could
   * observe directly. */
  private findTurnByIntentId(intentId: string): TurnId | null {
    for (const entry of this.turnsById.values()) {
      if (entry.prompt === null) continue;
      const payload = entry.prompt.payload as TypedPromptPayloadWire | null;
      if (payload?.intent_id === intentId) return entry.turnId;
    }
    return null;
  }

  /** Buckets a flat NodeWire[] by `parent_id` into per-container tables —
   * the SAME operation `children()`'s response gets bucketed into via
   * `setChildrenTable`, just applied to every parent present in the batch
   * at once instead of one container at a time. */
  private seedLiveTurnNodes(nodes: readonly NodeWire[]): void {
    const byParent = new Map<NodeId, NodeWire[]>();
    for (const n of nodes) {
      // Turn/prompt/result/runtime_change nodes live on TurnEntry's own
      // fields, not a children table (see bodyItemsOf's docstring on why
      // Result still needs filtering out of a raw parent_id bucket too).
      if (n.kind === "turn" || n.kind === "typed_prompt" || n.kind === "result") continue;
      if (n.kind === "model_change" || n.kind === "harness_change") continue;
      if (n.parent_id === null) continue;
      const list = byParent.get(n.parent_id);
      if (list) list.push(n);
      else byParent.set(n.parent_id, [n]);
    }
    for (const [parentId, kids] of byParent) this.setChildrenTable(parentId, kids);
  }

  private upsertIntoContainer(containerId: NodeId, node: NodeWire): void {
    let table = this.childrenTables.get(containerId);
    if (!table) {
      // Container not yet materialized — either never extended (a
      // genuinely collapsed, historical subtree the user hasn't opened,
      // which never receives live upserts in the first place since only
      // the current live/trailing turn's own tree gets node_upserts), or
      // a container born entirely from live frames after hydrate (a
      // brand-new turn/explanation with no REST round trip to seed it —
      // see `birthTurn`). Either way there is no pre-existing cached
      // content this upsert could clobber, so create the table on demand
      // instead of dropping the node (mirrors `setChildrenTable`'s own
      // construction, just lazily one node at a time).
      table = createTurnNodeTable();
      this.childrenTables.set(containerId, table);
    }
    // Deliberately no cross-table relocation when `containerId` differs
    // from wherever this node_id last landed: a genuine structural
    // re-bracket (hist_rev-driven) is already handled by the manifest-
    // mismatch cache invalidation below (drop the WHOLE stale table, then
    // re-fetch via REST) — the one place this codebase treats container
    // membership as authoritative. Relocating individual nodes here on
    // any `parent_id` change would additionally have to trust that the
    // new `containerId` names a real, rendered container, which a raw
    // node id the render tree doesn't recognize as a container kind is
    // not — that would silently sink the node into a table nothing ever
    // walks instead of just leaving a harmless stale duplicate behind.
    upsertTurnNode(table, node);
    this.nodeLocation.set(node.node_id, { table });
  }

  /** Creates a TurnEntry from the first live frame this store has ever
   * seen for `node.turn_id` — a typed_prompt/turn-opening upsert births
   * the turn in the common case, but ANY node kind may arrive first (a
   * thinking/assistant_text delivered before its own typed_prompt is not
   * dropped, it births a promptless entry that the later typed_prompt
   * fills in). Applies any `turn_lifecycle` frame buffered for this
   * turn_id while it didn't exist yet; otherwise defaults to "running" —
   * a turn scaffolded from a LIVE frame (never present at hydrate) is, by
   * construction, currently in flight, and chat-panel.md's `isLive(turn)`
   * must not wait for an explicit lifecycle frame that the backend does
   * not always (re-)send once a provisional turn_id is superseded by its
   * final anchor. */
  private birthTurn(node: NodeWire): TurnEntry {
    const pending = this.pendingLifecycle.get(node.turn_id);
    this.pendingLifecycle.delete(node.turn_id);
    const entry: TurnEntry = {
      ...this.newTurnEntry(scaffoldTurn(node), node.kind === "typed_prompt" ? node : null, [], null, null),
      phase: pending?.phase ?? "running",
      reason: pending?.reason ?? null,
      usage: pending?.usage ?? null,
    };
    this.turnsById.set(node.turn_id, entry);
    this.turnOrder = [...this.turnOrder, node.turn_id];
    return entry;
  }

  private handleFrame(frame: ChatFrame): void {
    if (!this.identity) return; // pre-hydration frames: hydrate() catches up via resynthesized replay
    switch (frame.type) {
      case "node_upsert": {
        this.identity = frame.snapshot;
        this.syncSocketCursor(false);
        const node = frame.node;

        if (node.kind === "instruction_widget") {
          this.instructionWidget = node;
          this.notify();
          return;
        }

        // A confirmed typed_prompt carrying a pending send's intent_id
        // reconciles that provisional turn in place — checked BEFORE the
        // normal birthTurn lookup below, since `node.turn_id` here is the
        // backend's own real (not-yet-seen) turn id and would otherwise
        // just birth a second, duplicate turn at the tail.
        if (node.kind === "typed_prompt" && this.reconcileProvisionalSend(node)) {
          this.notify();
          return;
        }

        let entry = this.turnsById.get(node.turn_id);
        if (!entry) entry = this.birthTurn(node);

        if (node.kind === "typed_prompt") {
          // If this turn was born from a LATER node arriving first (the
          // out-of-order case), entry.prompt is still null and the turn
          // scaffold was derived from that later node's (ts, seq) — now
          // that the real prompt has arrived, re-derive the scaffold from
          // it so turn-level ordering reflects the turn's true start.
          const turn = entry.prompt === null ? scaffoldTurn(node) : entry.turn;
          this.turnsById.set(node.turn_id, { ...entry, turn, prompt: node });
          this.notify();
          return;
        }
        if (node.kind === "turn") {
          this.turnsById.set(node.turn_id, {
            ...entry,
            turn: node,
            manifest: node.child_manifest ?? entry.manifest,
          });
          this.notify();
          return;
        }
        if (node.kind === "result") {
          const results = [...entry.results.filter((n) => n.node_id !== node.node_id), node].sort(byTsSeq);
          this.turnsById.set(node.turn_id, { ...entry, results });
          this.notify();
          return;
        }
        if (node.kind === "model_change" || node.kind === "harness_change") {
          this.turnsById.set(node.turn_id, { ...entry, runtimeChange: node });
          this.notify();
          return;
        }

        // Everything else (explanation, assistant_text, thinking,
        // tool_interaction, steering_message, the SubAgentTurn family,
        // worker_interaction, compaction, continuation_session, failure,
        // diagnostic, user_interaction, lifecycle_notice, fact, unknown)
        // is a direct child of SOME container — `parent_id` when set (an
        // explanation for a partitioned member, a SubAgentTurn node for
        // its own members), else the turn's own container id: the live
        // wire sends `parent_id: null` for a node directly under the turn
        // (mirrored by TurnView's `useChildren(store, entry.turn.node_id,
        // ...)`, the same id REST-persisted top-level BodyItems carry as
        // their non-null `parent_id`).
        this.upsertIntoContainer(node.parent_id ?? entry.turn.node_id, node);
        // The node MAY itself be a container whose manifest changed (an
        // explanation/SubAgentTurn re-upserted with a fresh
        // child_manifest) — refresh its manifest-holding copy inside its
        // OWN parent's table too (already done above) and, if its cached
        // children count now disagrees with the fresh manifest, drop the
        // stale cache so the next expand re-fetches instead of under-
        // rendering (live members normally arrive as their own upserts
        // and self-append via the branch above; this only guards the
        // rarer structural-reconciliation case, e.g. hist_rev-driven
        // re-bracketing).
        const cachedOwn = this.childrenTables.get(node.node_id);
        const freshCount = node.child_manifest?.renderable_child_count;
        if (cachedOwn && freshCount !== undefined && freshCount !== cachedOwn.nodes.length) {
          this.childrenTables.delete(node.node_id);
        }
        this.notify();
        return;
      }
      case "text_delta": {
        const loc = this.nodeLocation.get(frame.node_id);
        if (!loc || !hasTurnNode(loc.table, frame.node_id)) return;
        const node = getTurnNode(loc.table, frame.node_id)!;
        // Per ADR 0006 §4: an optional optimization "between upserts for
        // assistant_text/thinking" — the only two kinds whose payload
        // `text` is a plain (non-nullable) string appended in place.
        if ((node.kind !== "assistant_text" && node.kind !== "thinking") || node.payload === null) return;
        // `node.kind` and `node.payload`'s concrete shape are correlated
        // by the wire contract but not linked in NodeWire's TS type (see
        // wire.ts's own docstring on `NodePayloadWire` having no
        // discriminant tag) — the kind check above is the runtime proof;
        // this cast is the one place that encodes it, same pattern
        // adapter/mapToRenderModel.ts uses.
        const textPayload = node.payload as AssistantTextPayloadWire | ThinkingPayloadWire;
        const patched: NodeWire = {
          ...node,
          payload: { ...textPayload, text: textPayload.text + frame.appended_text },
        };
        upsertTurnNode(loc.table, patched);
        this.identity = frame.snapshot;
        this.notify();
        return;
      }
      case "node_status": {
        const loc = this.nodeLocation.get(frame.node_id);
        if (!loc || !hasTurnNode(loc.table, frame.node_id)) return;
        const node = getTurnNode(loc.table, frame.node_id)!;
        upsertTurnNode(loc.table, { ...node, status: frame.status });
        this.identity = frame.snapshot;
        this.notify();
        return;
      }
      case "turn_lifecycle": {
        const entry = this.turnsById.get(frame.turn_id);
        if (!entry) {
          // Turn not born yet — arrived before its own opening node, or
          // tagged with a turn_id no node will ever carry (a provisional
          // anchor the backend later supersedes without re-announcing).
          // Buffer instead of dropping so `birthTurn` can still pick this
          // phase up if/when that turn_id IS born.
          this.pendingLifecycle.set(frame.turn_id, {
            phase: frame.phase,
            reason: frame.reason,
            usage: frame.usage ?? null,
          });
          this.identity = frame.snapshot;
          return;
        }
        this.turnsById.set(frame.turn_id, {
          ...entry,
          phase: frame.phase,
          reason: frame.reason,
          usage: frame.usage ?? entry.usage,
        });
        this.identity = frame.snapshot;
        this.notify();
        return;
      }
      case "run_upsert": {
        this.runsById.set(frame.run.run_ref, frame.run);
        this.notify();
        return;
      }
      // user_interaction_upsert / sidecar_upsert / session_state / notice:
      // not part of the chat content plane's render tree (SubAgentPanel
      // sidecar is fetched separately by the panel UI on demand, never a
      // grammar field per ADR 0006 §7; user_interaction_upsert is the
      // UserInteraction RESOURCE plane, consumed separately — see
      // hooks/usePendingUserInteractions.ts and
      // lib/interactionResolveSocket.ts).
      default:
        return;
    }
  }
}
