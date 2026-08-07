// Best-effort "is this SubAgentTurn-family child still actively
// producing" signal, used by the live-turn recursion (render.ts) to
// decide which NON-TRAILING sibling(s) also get forced-expanded per
// chat-panel.md's multi-live-path invariant ("A turn with parallel live
// subturns... therefore has multiple simultaneously expanded live
// paths").
//
// Known gap (stage 1): the contract's real signal is each SubAgentTurn's
// OWN `turn_lifecycle` frames scoped to ITS turn_id (ADR 0006 §4,
// "SubAgentTurn-family turns get their own lifecycle frames scoped to
// their turn_id") — for `worker_turn`/`sub_session_turn`/`session_turn`
// that means subscribing to the TARGET session's lifecycle, which this
// store does not do (out of frontend-only stage-1 scope; state.ts only
// tracks phase for top-level turns of the OPENED session). For
// `native_subagent_turn` (same-stream nesting, no separate session) this
// heuristic is a real, non-fabricated signal: a still-live subtree has a
// leaf whose `status` is non-terminal (queued/streaming) somewhere on its
// trailing edge — this walks ONLY already-cached children (never
// triggers a fetch, so it never expands a collapsed subtree just to
// check liveness).

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
