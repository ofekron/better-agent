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
import type {
  AssistantTextPayloadWire,
  ChatFrame,
  ChildManifestWire,
  NodeId,
  NodeWire,
  ThinkingPayloadWire,
  RunWire,
  SnapshotIdentity,
  TerminalReasonWire,
  TurnId,
  TurnPhaseWire,
  UsageWire,
} from "../adapter/wire";

/** Turn phases chat-panel.md's `isLive(turn)` treats as "running" —
 * everything else (`completed | stopped | failed`, or no lifecycle frame
 * observed yet) is not-live. */
const LIVE_PHASES: ReadonlySet<TurnPhaseWire> = new Set([
  "queued",
  "starting",
  "running",
  "awaiting_approval",
  "reconnecting",
  "stopping",
]);

export function isLivePhase(phase: TurnPhaseWire | null): boolean {
  return phase !== null && LIVE_PHASES.has(phase);
}

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
  /** True once the first snapshot/resync completes; false while a fresh
   * session is still hydrating (initial REST fetch in flight). */
  hydrated: boolean;
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
  private hydrated = false;

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

  private readonly subscribers = new Set<() => void>();
  private cachedSnapshot: SurfaceStoreSnapshot | null = null;
  private readonly sessionId: string;

  constructor(sessionId: string) {
    this.sessionId = sessionId;
    this.socket = this.client.openSocket({
      onFrame: (frame) => this.handleFrame(frame),
      onResyncRequired: () => void this.hydrate(),
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
      hydrated: this.hydrated,
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

  private setChildrenTable(nodeId: NodeId, nodes: readonly NodeWire[]): void {
    const table = createTurnNodeTable();
    const sorted = [...nodes].sort(byTsSeq);
    for (const n of sorted) {
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
    } finally {
      this.loadingOlder = false;
      this.notify();
    }
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

    this.hydrated = true;
    this.notify();
    this.syncSocketCursor(true);
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
    const table = this.childrenTables.get(containerId);
    if (!table) {
      // Container not currently materialized (never extended, not on the
      // eagerly-seeded live path) — nothing rendered depends on this
      // node yet. Its container's own manifest upsert (handled generically
      // below, same as any other node) keeps the ellipsis-exists decision
      // correct for whenever it IS expanded.
      return;
    }
    upsertTurnNode(table, node);
    this.nodeLocation.set(node.node_id, { table });
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

        let entry = this.turnsById.get(node.turn_id);
        if (!entry) {
          if (node.kind !== "typed_prompt") return; // no anchoring turn yet
          entry = this.newTurnEntry(scaffoldTurn(node), node, [], null, null);
          this.turnsById.set(node.turn_id, entry);
          this.turnOrder = [...this.turnOrder, node.turn_id];
          this.notify();
          return;
        }

        if (node.kind === "typed_prompt") {
          this.turnsById.set(node.turn_id, { ...entry, prompt: node });
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
        // is a direct child of SOME container by `parent_id` — the turn
        // itself for a top-level BodyItem/Failure, an explanation for a
        // partitioned member, a SubAgentTurn node for its own members.
        if (node.parent_id) this.upsertIntoContainer(node.parent_id, node);
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
        if (!entry) return;
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
      // approval_upsert / sidecar_upsert / session_state / notice: not
      // part of the chat content plane's render tree (SubAgentPanel
      // sidecar is fetched separately by the panel UI on demand, never a
      // grammar field per ADR 0006 §7).
      default:
        return;
    }
  }
}
