// Shared "one container's direct children, fetched on demand and cached
// by the store" hook — the fetch wiring pattern useSurfaceSession.ts uses
// for subagent children, generalized to any container node id (explanation,
// any SubAgentTurn-family kind) and any component (ExplanationView,
// SubAgentTurnView).

import { useEffect, useState } from "react";
import type { SurfaceStore } from "./state";
import type { NodeWire } from "../adapter/wire";

/** Returns the container's cached children if present; triggers a fetch
 * (once, deduped by the store) when `enabled` and not yet cached.
 * `enabled=false` never fetches — the caller decides when a subtree is
 * "explicitly expanded" per chat-panel.md's on-demand rule. */
export function useChildren(store: SurfaceStore, nodeId: string, enabled: boolean): NodeWire[] | undefined {
  // Read synchronously on every render (a cheap Map lookup) instead of
  // mirroring it into state via an effect — correct from the very first
  // render with no extra render pass, and immediately reflects whatever
  // `fetched`/live-upsert state below has most recently written into the
  // store's own table.
  const cached = store.getChildren(nodeId);
  const [fetched, setFetched] = useState<NodeWire[] | undefined>(undefined);

  useEffect(() => {
    if (store.getChildren(nodeId) || !enabled) return;
    let cancelled = false;
    void store.ensureChildren(nodeId).then((result) => {
      if (!cancelled) setFetched(result);
    });
    return () => {
      cancelled = true;
    };
  }, [store, nodeId, enabled]);

  // Live upserts land directly in the store's cached table (mutated in
  // place per turnNodeTable.ts's contract) — resync on every store
  // notification so streaming content is reflected without waiting for a
  // re-fetch.
  useEffect(() => {
    return store.subscribe(() => {
      const latest = store.getChildren(nodeId);
      if (latest) setFetched([...latest]);
    });
  }, [store, nodeId]);

  return cached ?? fetched;
}
