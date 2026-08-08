// A5 restoration: legacy `MessageBubble.tsx`'s collapsed-turn body showed
// a boundary-inline content preview (`buildTurnSummary` + last-item
// render), not just a bare "• • • (N)" count. Container.tsx's
// SubAgentTurnView already had this mechanism for NESTED containers
// (`collapsedExtra`) — this closes the gap specifically for TurnView.tsx's
// own TOP-LEVEL collapsed body, reusing the SAME `CollapsedPreview`
// component (Container.tsx), not a forked second implementation.
//
// The preview's `assistant_text` leaf lazy-loads legacy's `MessageBox` via
// a Suspense boundary (ContentLeaves.tsx) — the first dynamic import of
// that (large) module in a test run can outrun the default 1000ms
// `waitFor` timeout on a cold transform cache, same caveat
// tests/surface/render.test.tsx/workerTurn.test.tsx document; every
// assertion on that leaf's rendered text below raises its own wait window.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import "../../src/i18n";
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { ChildManifestWire, NodeWire, RunWire } from "../../src/adapter/wire";
import {
  assistantTextNode,
  node,
  promptNode,
  resetSeq,
  turnNode,
} from "./fixtures";

resetSeq();

const COLD_IMPORT_TIMEOUT = 15000;

function lifecycleNoticeNode(turnId: string, nodeId: string, parentId: string): NodeWire {
  return node({
    node_id: nodeId,
    turn_id: turnId,
    kind: "lifecycle_notice",
    parent_id: parentId,
    payload: { kind: "detached", data: {} },
  });
}

class FakeSurfaceStore {
  private tables = new Map<string, NodeWire[]>();
  fetchCalls = 0;
  seed(nodeId: string, children: NodeWire[]): void {
    this.tables.set(nodeId, children);
  }
  getChildren(nodeId: string): NodeWire[] | undefined {
    return this.tables.get(nodeId);
  }
  async ensureChildren(nodeId: string): Promise<NodeWire[]> {
    this.fetchCalls += 1;
    return this.tables.get(nodeId) ?? [];
  }
  subscribe(): () => void {
    return () => {};
  }
}

function asStore(fake: FakeSurfaceStore): SurfaceStore {
  return fake as unknown as SurfaceStore;
}

function entryWithBody(manifest: ChildManifestWire, overrides: Partial<TurnEntry> = {}): TurnEntry {
  return {
    turnId: "t1",
    turn: turnNode("t1", manifest),
    prompt: promptNode("t1", "hi"),
    results: [],
    manifest,
    runtimeChange: null,
    phase: null,
    reason: null,
    usage: null,
    provisionalSend: null,
    ...overrides,
  };
}

const NO_RUNS: ReadonlyMap<string, RunWire> = new Map();

describe("TurnView collapsed body — boundary-inline content preview", () => {
  it(
    "shows a cached-content preview of the last body item, never fetching",
    async () => {
      const fake = new FakeSurfaceStore();
      fake.seed("turn:t1", [assistantTextNode("t1", "m1", "leading text", "turn:t1")]);
      const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
      render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

      await waitFor(
        () => expect(screen.getByTestId("surface-turn-preview").textContent).toContain("leading text"),
        { timeout: COLD_IMPORT_TIMEOUT },
      );
      expect(screen.getByText("• • • (1)")).toBeTruthy();
      // Collapsed rendering never fetches hidden content on its own.
      expect(fake.fetchCalls).toBe(0);
    },
    20000,
  );

  it(
    "previews only the LAST cached body item, not earlier ones",
    async () => {
      const fake = new FakeSurfaceStore();
      fake.seed("turn:t1", [
        assistantTextNode("t1", "m1", "first message", "turn:t1"),
        assistantTextNode("t1", "m2", "second message", "turn:t1"),
      ]);
      const entry = entryWithBody({ renderable_child_count: 2, has_children: true });
      render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

      await waitFor(
        () => expect(screen.getByTestId("surface-turn-preview").textContent).toContain("second message"),
        { timeout: COLD_IMPORT_TIMEOUT },
      );
      expect(screen.queryByText("first message")).toBeNull();
    },
    20000,
  );

  it(
    "excludes always-in-place kinds (lifecycle_notice) from preview candidacy — already rendered unconditionally",
    async () => {
      const fake = new FakeSurfaceStore();
      fake.seed("turn:t1", [
        assistantTextNode("t1", "m1", "leading text", "turn:t1"),
        lifecycleNoticeNode("t1", "ln1", "turn:t1"),
      ]);
      const entry = entryWithBody({ renderable_child_count: 2, has_children: true });
      render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

      await waitFor(
        () => expect(screen.getByTestId("surface-turn-preview").textContent).toContain("leading text"),
        { timeout: COLD_IMPORT_TIMEOUT },
      );
    },
    20000,
  );

  it("no cached children yet: no preview, no fetch (collapsed rendering never fetches)", () => {
    const fake = new FakeSurfaceStore();
    const entry = entryWithBody({ renderable_child_count: 3, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    expect(screen.queryByTestId("surface-turn-preview")).toBeNull();
    expect(screen.getByText("• • • (3)")).toBeTruthy();
    expect(fake.fetchCalls).toBe(0);
  });

  it(
    "expanding the turn shows the full body and removes the preview",
    async () => {
      const fake = new FakeSurfaceStore();
      fake.seed("turn:t1", [assistantTextNode("t1", "m1", "leading text", "turn:t1")]);
      const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
      render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

      await waitFor(() => expect(screen.getByTestId("surface-turn-preview")).toBeTruthy(), {
        timeout: COLD_IMPORT_TIMEOUT,
      });
      await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));

      await waitFor(() => expect(screen.getByText("leading text")).toBeTruthy(), {
        timeout: COLD_IMPORT_TIMEOUT,
      });
      expect(screen.queryByTestId("surface-turn-preview")).toBeNull();
    },
    25000,
  );
});
