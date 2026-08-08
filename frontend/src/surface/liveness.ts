// Best-effort "is this SubAgentTurn-family child still actively
// producing" signal, used by the live-turn recursion (render.ts) to
// decide which NON-TRAILING sibling(s) also get forced-expanded per
// chat-panel.md's multi-live-path invariant ("A turn with parallel live
// subturns... therefore has multiple simultaneously expanded live
// paths").
//
// Confirmed backend gap (stage 2a investigation, not just "out of
// frontend scope"): `worker_turn`/`sub_session_turn`/`session_turn` are
// declared in the contract (backend/surface_contract/nodes.py's
// `NodeKind`/`SUBAGENT_TURN_KINDS`) and dispatched here per ADR 0006 §1's
// "one render contract, four sourcing modes" — but grep across
// backend/adapters/{normalize,derive,chat_adapter}.py (the only three
// modules that ever construct a Node) finds ZERO construction sites for
// any of the three. They are never emitted today, so there is no
// `turn_lifecycle` frame scoped to their turn_id (chat_adapter.py's
// `_on_lifecycle` only tracks the OPENED session's own top-level turns —
// no subscription to a worker/sub-session/cross-session target exists
// either) AND no sidecar/status data to fall back to, because the
// CONTAINER NODE ITSELF never arrives. This blocks the whole cross-
// session subturn feature at the node-construction level, not just its
// liveness signal — building a liveness mechanism on top of nodes that
// don't exist would be fabrication, so none is added here; the safe
// `false` default below is correct until the backend starts constructing
// these nodes. `native_subagent_turn` (same-stream nesting, no separate
// session — IS constructed today, by derive.py) is unaffected: its
// heuristic below is a real, non-fabricated signal — a still-live
// subtree has a leaf whose `status` is non-terminal (queued/streaming)
// somewhere on its trailing edge — walking ONLY already-cached children
// (never triggers a fetch, so it never expands a collapsed subtree just
// to check liveness).

import type { SurfaceStore } from "./state";
import type { ContentStatusWire, NodeWire } from "../adapter/wire";

const ACTIVE_STATUSES: ReadonlySet<ContentStatusWire> = new Set(["queued", "streaming"]);

export function hasLikelyLiveDescendant(node: NodeWire, store: SurfaceStore, depth = 0): boolean {
  if (depth > 32) return false; // defensive bound, never expected in practice
  if (node.status && ACTIVE_STATUSES.has(node.status)) return true;
  if (node.kind !== "explanation" && node.kind !== "native_subagent_turn") return false;
  const children = store.getChildren(node.node_id);
  if (!children || children.length === 0) return false;
  // Only the trailing edge matters — an active status earlier in an
  // already-completed container would be a contradiction (a finished
  // container's members are all terminal).
  return hasLikelyLiveDescendant(children[children.length - 1], store, depth + 1);
}
