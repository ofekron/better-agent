// TurnView's `data-phase` attribute (src/surface/TurnView.tsx) — the native
// replacement for legacy TurnGroup's `.turn-group.is-running` class. Ported
// gap closure for tests/turn-group-running-visual-state.test.tsx (deleted):
// the attribute mechanism existed but no test asserted it reflects the
// turn's live/not-live state. NOT ported: whether any CSS actually keys
// off `[data-phase]` to produce a visible running treatment — grep of
// src/styles found no such rule (only the legacy `.turn-group.is-running`
// selector exists), so the *visual* running indicator may be unported;
// flagged as a possible gap in the migration ledger, not asserted here
// since it's a stylesheet concern, not this component's contract.

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { RunWire } from "../../src/adapter/wire";
import { promptNode, resetSeq, resultNode, turnNode } from "./fixtures";

resetSeq();

class FakeSurfaceStore {
  getChildren(): undefined {
    return undefined;
  }
  async ensureChildren() {
    return [];
  }
  subscribe(): () => void {
    return () => {};
  }
}

const NO_RUNS: ReadonlyMap<string, RunWire> = new Map();
const EMPTY = { renderable_child_count: 0, has_children: false };

function entry(phase: TurnEntry["phase"]): TurnEntry {
  return {
    turnId: "t1",
    turn: turnNode("t1", EMPTY),
    prompt: promptNode("t1", "hi"),
    results: [resultNode("t1", "done")],
    manifest: EMPTY,
    runtimeChange: null,
    phase,
    reason: null,
    usage: null,
  };
}

describe("TurnView data-phase reflects TurnEntry.phase", () => {
  it("completed turn: data-phase=completed (not a live phase)", () => {
    render(<TurnView entry={entry("completed")} store={new FakeSurfaceStore() as unknown as SurfaceStore} runsById={NO_RUNS} />);
    expect(screen.getByTestId("surface-turn").getAttribute("data-phase")).toBe("completed");
  });

  it("running turn: data-phase=running (a live phase)", () => {
    render(<TurnView entry={entry("running")} store={new FakeSurfaceStore() as unknown as SurfaceStore} runsById={NO_RUNS} />);
    expect(screen.getByTestId("surface-turn").getAttribute("data-phase")).toBe("running");
  });

  it("no lifecycle frame observed yet: data-phase=unknown", () => {
    render(<TurnView entry={entry(null)} store={new FakeSurfaceStore() as unknown as SurfaceStore} runsById={NO_RUNS} />);
    expect(screen.getByTestId("surface-turn").getAttribute("data-phase")).toBe("unknown");
  });

  // Regression for A0-1: the backend renamed its "awaiting approval" phase
  // to "awaiting_interaction" (surface_contract/frames.py's TurnPhase enum
  // — AWAITING_APPROVAL no longer exists). LIVE_PHASES (state.ts) must key
  // off the CURRENT wire value, or a turn paused on any UserInteraction
  // (approval, choice, or input) silently loses both its live badge and
  // its live body fetch.
  it("awaiting_interaction turn stays live: badge shows and body is fetched", async () => {
    const fetchedNodeIds: string[] = [];
    class SpyStore extends FakeSurfaceStore {
      async ensureChildren(nodeId: string) {
        fetchedNodeIds.push(nodeId);
        return [];
      }
    }
    render(
      <TurnView
        entry={entry("awaiting_interaction")}
        store={new SpyStore() as unknown as SurfaceStore}
        runsById={NO_RUNS}
      />,
    );
    expect(screen.getByTestId("surface-turn").getAttribute("data-phase")).toBe("awaiting_interaction");
    expect(screen.getByTestId("surface-run-badge")).toBeTruthy();
    await waitFor(() => expect(fetchedNodeIds).toContain("turn:t1"));
  });
});
