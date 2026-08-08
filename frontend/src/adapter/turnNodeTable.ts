// Mutable, node_id-keyed table of one turn's body nodes, kept sorted by
// (ts, seq) incrementally. Used by the native Contract-Node store
// (surface/state.ts) to avoid re-sorting/re-deriving a turn's entire body
// on every live frame — a naive rebuild-the-whole-array-then-resort
// approach is O(K log K) per frame (O(K^2 log K) total for a turn with K
// nodes receiving K live frames, a "monster" streaming turn).
//
// This table instead makes applying a node_upsert/text_delta/node_status
// frame an O(1) amortized operation:
//   - existing node_id, same (ts, seq) (the dominant live-update case —
//     payload/status patched on an already-placed node): O(1) in-place
//     array-slot replace.
//   - new node_id sorting at/after the current last node (the dominant
//     live-append case — nodes stream in increasing (ts, seq) order):
//     O(1) amortized push.
//   - anything out of order (new node arriving out of (ts, seq) order, or
//     an existing node_id whose (ts, seq) actually changed): O(K) splice +
//     reindex — correct but rare, never assumed away.
//
// `.nodes` is mutated in place. That is safe ONLY because each table is
// private per-turn/per-node state inside SurfaceStore (surface/state.ts's
// `childrenTables`), never exposed to React state/props/memo deps
// directly — every read that feeds React copies `.nodes` before use, so
// external immutability is preserved at the one boundary that needs it.

import type { NodeWire } from "./wire";

export interface TurnNodeTable {
  /** Sorted by (ts, seq). Mutated in place by upsertTurnNode. */
  nodes: NodeWire[];
  /** node_id -> current index into `nodes`, kept in sync with every
   * mutation below — turns "find existing node" from O(K) into O(1). */
  indexById: Map<string, number>;
}

export function createTurnNodeTable(): TurnNodeTable {
  return { nodes: [], indexById: new Map() };
}

function byTsSeq(a: NodeWire, b: NodeWire): number {
  return a.ts - b.ts || a.seq - b.seq;
}

/** Bulk constructor for hydration (snapshot/resync) — the one place a full
 * O(K log K) sort is expected and correct, matching the existing from-
 * scratch semantics of a fresh fetch. */
export function buildTurnNodeTable(nodes: NodeWire[]): TurnNodeTable {
  const sorted = [...nodes].sort(byTsSeq);
  const indexById = new Map<string, number>();
  sorted.forEach((n, i) => indexById.set(n.node_id, i));
  return { nodes: sorted, indexById };
}

function sortedInsertIndex(nodes: NodeWire[], node: NodeWire): number {
  let lo = 0;
  let hi = nodes.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (byTsSeq(nodes[mid], node) <= 0) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function removeTurnNodeAt(table: TurnNodeTable, idx: number): void {
  const { nodes, indexById } = table;
  indexById.delete(nodes[idx].node_id);
  nodes.splice(idx, 1);
  for (let i = idx; i < nodes.length; i++) indexById.set(nodes[i].node_id, i);
}

function insertSorted(table: TurnNodeTable, node: NodeWire): void {
  const { nodes, indexById } = table;
  const last = nodes[nodes.length - 1];
  if (!last || byTsSeq(last, node) <= 0) {
    indexById.set(node.node_id, nodes.length);
    nodes.push(node);
    return;
  }
  const idx = sortedInsertIndex(nodes, node);
  nodes.splice(idx, 0, node);
  for (let i = idx; i < nodes.length; i++) indexById.set(nodes[i].node_id, i);
}

/** Upserts one node into the table, keyed by node_id. Returns true when the
 * node's (ts, seq)-sorted position or content actually changed — always
 * true in practice (callers only invoke this when a frame carries a real
 * update), kept as a return value for callers that want it explicitly. */
export function upsertTurnNode(table: TurnNodeTable, node: NodeWire): void {
  const existingIdx = table.indexById.get(node.node_id);
  if (existingIdx !== undefined) {
    const existing = table.nodes[existingIdx];
    if (existing.ts === node.ts && existing.seq === node.seq) {
      table.nodes[existingIdx] = node;
      return;
    }
    // (ts, seq) changed for an already-placed node_id — not expected in
    // steady state (node identity is stamped once at creation), but
    // handled correctly rather than assumed away: reposition.
    removeTurnNodeAt(table, existingIdx);
  }
  insertSorted(table, node);
}

export function getTurnNode(table: TurnNodeTable, nodeId: string): NodeWire | undefined {
  const idx = table.indexById.get(nodeId);
  return idx === undefined ? undefined : table.nodes[idx];
}

export function hasTurnNode(table: TurnNodeTable, nodeId: string): boolean {
  return table.indexById.has(nodeId);
}
