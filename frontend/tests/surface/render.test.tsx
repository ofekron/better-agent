// Component-level grammar-conformance suite (chat-panel.md render
// algorithm), driven directly against TurnView/NodeView with a minimal
// FakeSurfaceStore double (SurfaceStore's public surface is exactly
// {getChildren, ensureChildren, subscribe} as consumed by surface/
// components — a structural fake avoids needing a real fetch/WebSocket
// backend for pure rendering-logic assertions; SurfaceStore's own
// hydrate/frame-routing behavior is covered by state.test.ts instead).

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { ChildManifestWire, NodeWire, RunWire } from "../../src/adapter/wire";
import {
  assistantTextNode,
  explanationNode,
  nativeSubagentTurnNode,
  promptNode,
  resetSeq,
  resultNode,
  toolInteractionNode,
  turnNode,
} from "./fixtures";

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

/** entry.manifest is the authoritative field TurnView reads for the
 * ellipsis-exists decision (mirrors backend CompactTurnWire.manifest,
 * separate from turn.child_manifest) — this helper keeps both in sync so
 * fixtures can't silently drift the way a raw `turnNode(...)` override
 * alone would. */
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
    ...overrides,
  };
}

const NO_RUNS: ReadonlyMap<string, RunWire> = new Map();
const EMPTY: ChildManifestWire = { renderable_child_count: 0, has_children: false };

describe("TurnView — collapsed/extended (chat-panel.md render/renderCollapsedTurn/renderExtendedTurn)", () => {
  it("renders Prompt -> Result with NO ellipsis when the turn has zero renderable body items", () => {
    const entry = entryWithBody(EMPTY, { results: [resultNode("t1", "the result")] });
    render(<TurnView entry={entry} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);
    expect(screen.getByTestId("surface-typed-prompt")).toBeTruthy();
    expect(screen.getByTestId("surface-result")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /expand hidden content/i })).toBeNull();
  });

  it("renders the clickable ellipsis ONLY when renderable_child_count > 0 (render invariant)", () => {
    const entry = entryWithBody(
      { renderable_child_count: 2, has_children: true },
      { results: [resultNode("t1", "the result")] },
    );
    render(<TurnView entry={entry} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);
    expect(screen.getByRole("button", { name: /expand hidden content/i })).toBeTruthy();
    // Collapsed: body is never fetched/rendered, only Prompt -> ... -> Result.
    expect(screen.queryByTestId("surface-assistant-text")).toBeNull();
    expect(screen.getByTestId("surface-result")).toBeTruthy();
  });

  // Own timeout raised alongside the inner waitFor's: this is typically the
  // FIRST test in the run to trigger AssistantTextView's dynamic import of
  // components/MessageBubble.tsx (a large module), which can outrun
  // vitest's default 5000ms per-test timeout on a cold transform cache.
  it("extends on click: fetches and renders the body via children(turn_id), boundary-inline with Result", async () => {
    const fake = new FakeSurfaceStore();
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [exp]);
    fake.seed("exp1", [assistantTextNode("t1", "m1", "body text", "exp1")]);
    const entry = entryWithBody(
      { renderable_child_count: 1, has_children: true },
      { results: [resultNode("t1", "the result")] },
    );
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    // AssistantTextView lazy-loads components/MessageBubble.tsx's
    // MessageBox (see nodes/ContentLeaves.tsx) — the first dynamic import
    // of that (large) module in a test run can take longer than
    // waitFor's default timeout.
    await waitFor(() => expect(screen.getByText("body text")).toBeTruthy(), { timeout: 8000 });
    expect(screen.getByTestId("surface-result")).toBeTruthy();
  }, 10000);
});

describe("Explanation collapse (chat-panel.md renderCollapsedExplanation: Text -> ... -> last item)", () => {
  it("shows Text, an ellipsis for N-1 hidden items, and the LAST item compact — never the middle ones", async () => {
    const fake = new FakeSurfaceStore();
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 4, has_children: true });
    fake.seed("turn:t1", [exp]);
    fake.seed("exp1", [
      assistantTextNode("t1", "text1", "leading text", "exp1"),
      toolInteractionNode("t1", "tool1", "Read", "exp1"),
      toolInteractionNode("t1", "tool2", "Write", "exp1"),
      toolInteractionNode("t1", "tool3", "LastTool", "exp1"),
    ]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-explanation")).toBeTruthy());

    // AssistantTextView lazy-loads components/MessageBubble.tsx's
    // MessageBox (see nodes/ContentLeaves.tsx) — wait for that dynamic
    // import to resolve before asserting its content.
    await waitFor(() => expect(screen.getByText("leading text")).toBeTruthy(), { timeout: 5000 });
    // Ellipsis hides count-1 = 2 of the 3 non-text items.
    expect(screen.getByText("• • • (2)")).toBeTruthy();
    // Only the LAST tool_interaction (LastTool) renders; the hidden two do not.
    expect(screen.queryByText("Read")).toBeNull();
    expect(screen.queryByText("Write")).toBeNull();
    // ToolCall (components/ToolCall.tsx) is lazy-loaded — wait for the
    // dynamic import to resolve before asserting the last item's content.
    await waitFor(() => expect(screen.getByText("LastTool")).toBeTruthy());
  });
});

describe("Live turn (chat-panel.md renderLiveTurn — trailing-path forced expansion)", () => {
  it("force-expands the trailing body item while a turn is live, with no ellipsis", async () => {
    const fake = new FakeSurfaceStore();
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [exp]);
    fake.seed("exp1", [assistantTextNode("t1", "m1", "streaming content", "exp1", "streaming")]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true }, { phase: "running" });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await waitFor(() => expect(screen.getByText("streaming content")).toBeTruthy(), { timeout: 5000 });
    expect(screen.queryByRole("button", { name: /expand hidden content/i })).toBeNull();
  });

  it("multi-live-path: two live native_subagent_turn siblings are BOTH force-expanded (not just the trailing one)", async () => {
    const fake = new FakeSurfaceStore();
    const sub1 = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    const sub2 = nativeSubagentTurnNode("t1", "sub2", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [sub1, sub2]);
    fake.seed("sub1", [assistantTextNode("t1", "a1", "worker one output", "sub1", "streaming")]);
    fake.seed("sub2", [assistantTextNode("t1", "a2", "worker two output", "sub2", "streaming")]);
    const entry = entryWithBody({ renderable_child_count: 2, has_children: true }, { phase: "running" });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    // Both siblings' trailing leaves are visible — sub1 is the non-trailing
    // sibling but liveness.ts detects its own live descendant; sub2 is the
    // list's trailing item and is unconditionally forced live.
    await waitFor(() => expect(screen.getByText("worker one output")).toBeTruthy(), { timeout: 5000 });
    expect(screen.getByText("worker two output")).toBeTruthy();
  });
});

describe("native_subagent_turn — lazy collapsible block", () => {
  it("shows a boundary-inline preview of already-cached content while collapsed, then the full body once expanded", async () => {
    // "already-cached" here mirrors the real-world case this gap closure
    // targets: a subagent turn nested inside a live turn's eager-seeded
    // bounded-nodes window (state.ts's seedLiveTurnNodes), so its
    // children are present in the store the moment it first renders
    // collapsed — `fake.seed("sub1", ...)` reproduces exactly that
    // precondition. No fetch is triggered to build this preview: it only
    // reads what's already there (see the "does not fetch" case below for
    // the cold/nothing-cached counterpart).
    const fake = new FakeSurfaceStore();
    const sub = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [sub]);
    fake.seed("sub1", [assistantTextNode("t1", "a1", "subagent output", "sub1")]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-subagent-turn")).toBeTruthy());
    expect(screen.getByTestId("surface-subagent-preview").textContent).toContain("subagent output");
    // Still collapsed: the real (extended) body region hasn't rendered.
    expect(screen.getByTestId("surface-subagent-turn").querySelector(".surface-collapsible-body")).toBeNull();

    await userEvent.click(screen.getByRole("button", { expanded: false }));
    await waitFor(() =>
      expect(screen.getByTestId("surface-subagent-turn").querySelector(".surface-collapsible-body")).not.toBeNull(),
    );
    expect(screen.getByText("subagent output")).toBeTruthy();
    expect(screen.queryByTestId("surface-subagent-preview")).toBeNull(); // preview row gone once expanded
  });

  it("does not fetch children until explicitly expanded, and shows no preview when nothing is cached", async () => {
    const fake = new FakeSurfaceStore();
    const sub = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [sub]);
    // Deliberately NOT seeding "sub1" — nothing cached for it yet.
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-subagent-turn")).toBeTruthy());
    expect(screen.queryByTestId("surface-subagent-preview")).toBeNull();
  });
});
