// liveness.ts's `hasLikelyLiveDescendant` — the multi-live-path signal
// ItemRow.tsx uses to force-expand a non-trailing live sibling.

import { describe, expect, it } from "vitest";
import { hasLikelyLiveDescendant } from "../../src/surface/liveness";
import type { SurfaceStore } from "../../src/surface/state";
import {
  assistantTextNode,
  nativeSubagentTurnNode,
  resetSeq,
} from "./fixtures";
import type { NodeWire } from "../../src/adapter/wire";

resetSeq();

function fakeStore(tables: Record<string, NodeWire[]>): SurfaceStore {
  return {
    getChildren: (nodeId: string) => tables[nodeId],
  } as unknown as SurfaceStore;
}

describe("hasLikelyLiveDescendant", () => {
  it("true for a leaf node whose own status is non-terminal (queued/streaming)", () => {
    const leaf = assistantTextNode("t1", "a1", "x", "p1", "streaming");
    expect(hasLikelyLiveDescendant(leaf, fakeStore({}))).toBe(true);
  });

  it("false for a leaf node whose status is terminal", () => {
    const leaf = assistantTextNode("t1", "a1", "x", "p1", "complete");
    expect(hasLikelyLiveDescendant(leaf, fakeStore({}))).toBe(false);
  });

  it("native_subagent_turn: true when its trailing cached child is still streaming", () => {
    const sub = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    const store = fakeStore({
      sub1: [assistantTextNode("t1", "a1", "x", "sub1", "streaming")],
    });
    expect(hasLikelyLiveDescendant(sub, store)).toBe(true);
  });

  it("native_subagent_turn: false when its trailing cached child is terminal", () => {
    const sub = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    const store = fakeStore({
      sub1: [assistantTextNode("t1", "a1", "x", "sub1", "complete")],
    });
    expect(hasLikelyLiveDescendant(sub, store)).toBe(false);
  });

  it("native_subagent_turn: false when its children aren't cached yet (never triggers a fetch)", () => {
    const sub = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    expect(hasLikelyLiveDescendant(sub, fakeStore({}))).toBe(false);
  });

  // Confirmed backend gap (see liveness.ts's module comment): worker_turn/
  // sub_session_turn/session_turn are never constructed by any backend
  // adapter today, so there is no live signal to detect for them — the
  // recursive descent intentionally does NOT cover these three kinds,
  // even when a child table happens to be seeded (defensive: a future
  // backend change must not silently start "detecting" liveness from
  // data this function was never designed to trust).
  it.each(["worker_turn", "sub_session_turn", "session_turn"] as const)(
    "%s: always false, even with a cached streaming child (no construction site backs this kind yet)",
    (kind) => {
      const node: NodeWire = {
        cv: 1,
        node_id: "cross1",
        parent_id: "turn:t1",
        turn_id: "t1",
        surface_id: "s1",
        kind,
        ts: 100,
        seq: 0,
        status: null,
        payload: null,
        run_ref: null,
        sidecar_ref: null,
        target_ref: null,
        child_manifest: { renderable_child_count: 1, has_children: true },
      };
      const store = fakeStore({
        cross1: [assistantTextNode("t1", "a1", "x", "cross1", "streaming")],
      });
      expect(hasLikelyLiveDescendant(node, store)).toBe(false);
    },
  );
});
