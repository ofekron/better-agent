// TurnView's turn-body-level virtualization (src/surface/TurnView.tsx's
// `TurnBodyRows`, switching to `VirtualizedEventList` above
// `VIRTUALIZE_EVENT_THRESHOLD`). Gap closure for
// tests/messageBubbleVirtualization.test.tsx (deleted): that file only
// asserted the legacy `ba.surface_v2`-flag-gated path (a different, now
// orthogonal flag — the native path virtualizes unconditionally, no flag
// gate at all); this is the first test of the native mechanism itself.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TurnView } from "../../src/surface/TurnView";
import { VIRTUALIZE_EVENT_THRESHOLD } from "../../src/components/VirtualizedEventList";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { NodeWire, RunWire } from "../../src/adapter/wire";
import { assistantTextNode, promptNode, resetSeq, turnNode } from "./fixtures";

resetSeq();

class FakeSurfaceStore {
  constructor(private children: NodeWire[]) {}
  getChildren(): NodeWire[] {
    return this.children;
  }
  async ensureChildren(): Promise<NodeWire[]> {
    return this.children;
  }
  subscribe(): () => void {
    return () => {};
  }
}

const NO_RUNS: ReadonlyMap<string, RunWire> = new Map();

function liveEntry(childCount: number): TurnEntry {
  return {
    turnId: "t1",
    turn: turnNode("t1", { renderable_child_count: childCount, has_children: childCount > 0 }),
    prompt: promptNode("t1", "hi"),
    results: [],
    manifest: { renderable_child_count: childCount, has_children: childCount > 0 },
    runtimeChange: null,
    phase: "running",
    reason: null,
    usage: null,
  };
}

function manyChildren(n: number): NodeWire[] {
  return Array.from({ length: n }, (_, i) => assistantTextNode("t1", `m${i}`, `line ${i}`, "turn:t1"));
}

describe("TurnView turn-body virtualization threshold", () => {
  it("below the threshold: plain DOM, no VirtualizedEventList", () => {
    const children = manyChildren(VIRTUALIZE_EVENT_THRESHOLD - 1);
    const store = new FakeSurfaceStore(children) as unknown as SurfaceStore;
    render(<TurnView entry={liveEntry(children.length)} store={store} runsById={NO_RUNS} />);
    expect(screen.queryByTestId(/^virtualized-event-list/)).toBeNull();
    expect(screen.getAllByTestId("surface-assistant-text")).toHaveLength(children.length);
  });

  it("above the threshold: switches to VirtualizedEventList unconditionally (no flag needed)", () => {
    const children = manyChildren(VIRTUALIZE_EVENT_THRESHOLD + 50);
    const store = new FakeSurfaceStore(children) as unknown as SurfaceStore;
    render(<TurnView entry={liveEntry(children.length)} store={store} runsById={NO_RUNS} />);
    const virtualized = document.querySelector('[data-testid^="virtualized-event-list"]');
    expect(virtualized).not.toBeNull();
  });
});
