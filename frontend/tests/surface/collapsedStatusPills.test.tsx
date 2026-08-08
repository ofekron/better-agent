// Resolves the open question from phase2-inventory.md's
// turn-group-collapse.test.tsx deletion note: "status-pills-survive-
// collapse — UNVERIFIED". Backend's `derive.py` `_NON_RENDERABLE_KINDS`
// excludes ONLY `lifecycle_notice`/`diagnostic` from `renderable_child_count`
// specifically because they "render at their occurrence rather than as
// expandable children" — `failure` is NOT in that set, so it stays
// ordinary ellipsis-gated content. Answer, confirmed by this suite: yes
// for lifecycle_notice/diagnostic, no for failure (by backend design, not
// an oversight) — see src/surface/nodes/kinds.ts's `isAlwaysInPlaceKind`.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { ChildManifestWire, NodeWire, RunWire } from "../../src/adapter/wire";
import { failureNode, node, promptNode, resetSeq, turnNode } from "./fixtures";

resetSeq();

class FakeSurfaceStore {
  private tables = new Map<string, NodeWire[]>();
  seed(nodeId: string, children: NodeWire[]): void {
    this.tables.set(nodeId, children);
  }
  getChildren(nodeId: string): NodeWire[] | undefined {
    return this.tables.get(nodeId);
  }
  async ensureChildren(nodeId: string): Promise<NodeWire[]> {
    return this.tables.get(nodeId) ?? [];
  }
  subscribe(): () => void {
    return () => {};
  }
}

function asStore(fake: FakeSurfaceStore): SurfaceStore {
  return fake as unknown as SurfaceStore;
}

const NO_RUNS: ReadonlyMap<string, RunWire> = new Map();

function entryWithBody(manifest: ChildManifestWire): TurnEntry {
  return {
    turnId: "t1",
    turn: turnNode("t1", manifest),
    prompt: promptNode("t1", "hi"),
    results: [],
    manifest,
    runtimeChange: null,
    phase: "completed",
    reason: "ok",
    usage: null,
  };
}

describe("collapsed turn — lifecycle_notice/diagnostic render in place, failure does not", () => {
  it("a cached lifecycle_notice renders even while the turn is fully collapsed", () => {
    const fake = new FakeSurfaceStore();
    const notice = node({
      node_id: "n1",
      turn_id: "t1",
      kind: "lifecycle_notice",
      parent_id: "turn:t1",
      payload: { kind: "detached", data: null },
    });
    fake.seed("turn:t1", [notice]);
    // renderable_child_count is 0 — lifecycle_notice is excluded from it
    // (derive.py's _NON_RENDERABLE_KINDS) — so there is no ellipsis either.
    const entry = entryWithBody({ renderable_child_count: 0, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    expect(screen.getByText(/Reconnecting/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /expand hidden content/i })).toBeNull();
  });

  it("a cached diagnostic (execution_continuation) renders even while the turn is fully collapsed", () => {
    const fake = new FakeSurfaceStore();
    const diag = node({
      node_id: "d1",
      turn_id: "t1",
      kind: "diagnostic",
      parent_id: "turn:t1",
      payload: { severity: "info", code: "execution_continuation", text: "continuing execution", data: null },
    });
    fake.seed("turn:t1", [diag]);
    const entry = entryWithBody({ renderable_child_count: 0, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    expect(screen.getByTestId("surface-diagnostic-continuation").textContent).toContain("continuing execution");
  });

  it("a cached failure does NOT render while collapsed — stays behind the ellipsis, matching its own hidden count", async () => {
    const fake = new FakeSurfaceStore();
    const failure = failureNode("t1", "f1", "turn:t1");
    fake.seed("turn:t1", [failure]);
    // failure DOES count toward renderable_child_count (backend includes it).
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    expect(screen.queryByTestId("surface-failure-chip")).toBeNull();
    const ellipsis = screen.getByRole("button", { name: /expand hidden content/i });
    expect(ellipsis).toBeTruthy();

    await userEvent.click(ellipsis);
    await waitFor(() => expect(screen.getByTestId("surface-failure-chip")).toBeTruthy());
  });

  it("a lifecycle_notice alongside ordinary hidden content still shows the ellipsis for the ordinary content", () => {
    const fake = new FakeSurfaceStore();
    const notice = node({
      node_id: "n2",
      turn_id: "t1",
      kind: "lifecycle_notice",
      parent_id: "turn:t1",
      payload: { kind: "recovering", data: null },
    });
    const failure = failureNode("t1", "f2", "turn:t1");
    fake.seed("turn:t1", [notice, failure]);
    // Manifest count reflects only the failure (1) — lifecycle_notice is excluded.
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    expect(screen.getByTestId("surface-recovering-pill")).toBeTruthy();
    expect(screen.getByRole("button", { name: /expand hidden content/i })).toBeTruthy();
    expect(screen.queryByTestId("surface-failure-chip")).toBeNull();
  });
});
