// `worker_turn` / `sub_session_turn` — the two SubAgentTurn family kinds
// (nodes.py's `SUBAGENT_TURN_KINDS`) not yet produced by any backend
// adapter (phase2-inventory.md, "worker_turn / sub_session_turn native
// rendering" round). Fixture-driven ahead of the producer: proves the
// SHARED render contract (`SubAgentTurnView`, nodes/Container.tsx) that
// already renders `native_subagent_turn` also renders these two kinds at
// parity — one panel per kind-specific chrome (chip label, RunMarker,
// target_ref link, live overlay), same collapse/expand/preview mechanics.
//
// Legacy parity source: `CollapsibleTimelineBlock`
// (src/components/MessageBubble.tsx) + `panelKindLabel`
// (utils/mergeEvents.ts) — see the ledger's parity checklist for the full
// item-by-item mapping (this round's section, phase2-inventory.md).

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import "../../src/i18n";
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { ChildManifestWire, NodeWire, RunWire } from "../../src/adapter/wire";
import { renderApp } from "../harness";
import { makeSession } from "../fixtures";
import {
  assistantTextNode,
  compactTurn,
  nativeSubagentTurnNode,
  promptNode,
  resetSeq,
  runWire,
  subSessionTurnNode,
  turnNode,
  workerTurnNode,
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

const EMPTY: ChildManifestWire = { renderable_child_count: 0, has_children: false };

describe("worker_turn / sub_session_turn — kind-specific chip label", () => {
  it("worker_turn renders the 'Worker' chip (not the native_subagent_turn 'Sub-agent' label)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    expect(panel.getAttribute("data-subagent-kind")).toBeNull(); // attribute lives on the inner badge, not the panel root
    const badge = panel.querySelector('[data-subagent-kind="worker_turn"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toBe("Worker");
  });

  it("sub_session_turn renders the 'Sub Session' chip", async () => {
    const fake = new FakeSurfaceStore();
    const sst = subSessionTurnNode("t1", "ss1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [sst]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    const badge = panel.querySelector('[data-subagent-kind="sub_session_turn"]');
    expect(badge?.textContent).toBe("Sub Session");
  });

  it("native_subagent_turn keeps its existing 'Sub-agent' label (regression guard, unchanged behavior)", async () => {
    const fake = new FakeSurfaceStore();
    const nst = nativeSubagentTurnNode("t1", "n1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [nst]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    const badge = panel.querySelector('[data-subagent-kind="native_subagent_turn"]');
    expect(badge?.textContent).toBe("Sub-agent");
  });
});

describe("worker_turn / sub_session_turn — real label from SubAgentTurnPayload", () => {
  it("prefers payload.label (the real delegated-task description) over the generic kind label", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY, { label: "Fix the flaky CI job" });
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    const badge = panel.querySelector('[data-subagent-kind="worker_turn"]');
    expect(badge?.textContent).toBe("Fix the flaky CI job");
  });

  it("falls back to the generic 'Worker' kind label when payload.label is absent", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY); // no label override -> payload stays null
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    const badge = panel.querySelector('[data-subagent-kind="worker_turn"]');
    expect(badge?.textContent).toBe("Worker");
  });
});

describe("worker_turn / sub_session_turn — created-variant parity (SubAgentTurnPayload.created)", () => {
  it("applies the legacy `.timeline-block-created` modifier when payload.created is true (collapsed)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY, { created: true });
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    expect(panel.className).toContain("timeline-block-created");
  });

  it("does not apply the modifier when payload.created is false/absent (collapsed)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    expect(panel.className).not.toContain("timeline-block-created");
  });

  it("applies the modifier and data-created on the live-mode render path", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY, { created: true });
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false }, { phase: "running" });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    const panel = await screen.findByTestId("surface-subagent-turn");
    expect(panel.getAttribute("data-mode")).toBe("live");
    expect(panel.className).toContain("timeline-block-created");
    expect(panel.getAttribute("data-created")).toBe("true");
  });
});

describe("worker_turn / sub_session_turn — usage overlay (Node.usage)", () => {
  it("shows the UsageSummary leaf when the node carries a real usage payload", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY, {
      usage: { input_tokens: 120, output_tokens: 340, total_tokens: 460, cache_creation_input_tokens: null, cache_read_input_tokens: null },
    });
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    const usage = panel.querySelector('[data-testid="surface-usage-summary"]');
    expect(usage).not.toBeNull();
    expect(usage?.textContent).toContain("340");
  });

  it("shows no UsageSummary when the node carries no usage (undefined, the pre-producer default)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    expect(panel.querySelector('[data-testid="surface-usage-summary"]')).toBeNull();
  });
});

describe("worker_turn / sub_session_turn — collapse/expand + boundary preview parity", () => {
  it("worker_turn: collapsed shows a cached-content preview, expand reveals the full body (same mechanics as native_subagent_turn)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [wt]);
    fake.seed("w1", [assistantTextNode("t1", "a1", "worker output", "w1")]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-subagent-turn")).toBeTruthy());
    // The preview's assistant-text leaf reuses legacy's `MessageBox` via a
    // lazy()-loaded Suspense boundary (ContentLeaves.tsx). This is the
    // FIRST test in this file to trigger that dynamic import — its
    // cold-start compile of a large module can exceed even an 8s wait
    // under concurrent load (same mitigation family as
    // tests/surface/render.test.tsx's "extends on click" test, given more
    // headroom here since nothing earlier in this file has warmed it).
    await waitFor(() => expect(screen.getByTestId("surface-subagent-preview").textContent).toContain("worker output"), {
      timeout: 15000,
    });
    expect(screen.getByTestId("surface-subagent-turn").querySelector(".surface-collapsible-body")).toBeNull();

    await userEvent.click(screen.getByRole("button", { expanded: false }));
    await waitFor(() =>
      expect(screen.getByTestId("surface-subagent-turn").querySelector(".surface-collapsible-body")).not.toBeNull(),
    );
    expect(screen.getByText("worker output")).toBeTruthy();
    expect(screen.queryByTestId("surface-subagent-preview")).toBeNull();
  }, 18000);
});

describe("worker_turn / sub_session_turn — RunMarker (provider/model/effort/runner chip)", () => {
  it("shows the RunMarker when run_ref resolves against runsById", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY, { runRef: "run-worker-1" });
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    const runsById = new Map<string, RunWire>([["run-worker-1", runWire("run-worker-1", { provider_id: "anthropic", model: "claude-opus" })]]);
    render(<TurnView entry={entry} store={asStore(fake)} runsById={runsById} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const marker = await screen.findByTestId("surface-run-marker");
    expect(marker.textContent).toContain("claude-opus");
  });

  it("renders no RunMarker when run_ref is absent", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await screen.findByTestId("surface-subagent-turn");
    expect(screen.queryByTestId("surface-run-marker")).toBeNull();
  });
});

describe("worker_turn / sub_session_turn — target_ref link affordance", () => {
  it("renders a working session-open link when target_ref is present", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY, {
      targetRef: { session_id: "worker-session-42", turn_id: "wt-turn-1" },
    });
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const link = (await screen.findByTestId("surface-subagent-target-link")) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/s/worker-session-42");
    expect(link.textContent).toBe("Open session");
    // Must NOT be nested inside the collapse-toggle <button> — a <button>
    // cannot contain interactive content per the HTML content model.
    expect(link.closest("button")).toBeNull();
  });

  it("renders no link when target_ref is absent", async () => {
    const fake = new FakeSurfaceStore();
    const sst = subSessionTurnNode("t1", "ss1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [sst]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await screen.findByTestId("surface-subagent-turn");
    expect(screen.queryByTestId("surface-subagent-target-link")).toBeNull();
  });
});

describe("worker_turn / sub_session_turn — live status overlay", () => {
  it("shows the RunningIndicator while the panel is forced live (trailing item of a running turn)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [wt]);
    fake.seed("w1", [assistantTextNode("t1", "a1", "live worker output", "w1", "streaming")]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true }, { phase: "running" });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    // See the "collapse/expand" describe block above re: the lazy
    // MessageBox import's cold-start cost.
    await waitFor(() => expect(screen.getByText("live worker output")).toBeTruthy(), { timeout: 8000 });
    const panel = screen.getByTestId("surface-subagent-turn");
    expect(panel.getAttribute("data-mode")).toBe("live");
    expect(panel.querySelector('[data-testid="surface-run-badge"]')).not.toBeNull();
  }, 10000);

  it("shows no RunningIndicator when the panel is collapsed (not live)", async () => {
    const fake = new FakeSurfaceStore();
    const wt = workerTurnNode("t1", "w1", "turn:t1", EMPTY);
    fake.seed("turn:t1", [wt]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: false });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={new Map()} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    const panel = await screen.findByTestId("surface-subagent-turn");
    expect(panel.querySelector('[data-testid="surface-run-badge"]')).toBeNull();
  });
});

// Harness integration case: a worker_turn seeded as an ordinary REST-loaded
// turn child, through the real renderApp()/ChatSurfaceView/TurnView stack
// (not the FakeSurfaceStore double used above). Seeded via configureBackend
// (runs before the app mounts) rather than a post-render h.seedSurface() —
// see tests/surfaceHarness.test.tsx's header docstring on the
// autoSelectSession race a post-mount seed can lose against, and
// tests/surface/compactionExpand.test.tsx for the established pattern this
// mirrors.
describe("worker_turn — harness integration (renderApp + configureBackend)", () => {
  it("a worker_turn child of a REST-seeded turn renders its kind chip and target_ref link end to end", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-worker", messages: [] });
    const workerNodeId = "t1:worker";
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-worker", {
          turns: [compactTurn(turnNode("t1", { renderable_child_count: 1, has_children: true }), promptNode("t1", "delegate this"), [])],
          childrenByNodeId: {
            "turn:t1": [
              workerTurnNode("t1", workerNodeId, "turn:t1", { renderable_child_count: 0, has_children: false }, {
                targetRef: { session_id: "worker-child-session", turn_id: "wt1" },
              }),
            ],
          },
        });
      },
    });
    await h.selectSession("sess-worker");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);

    const turnEllipsis = Array.from(h.$$("button")).find((b) => /expand hidden content/i.test(b.getAttribute("aria-label") ?? ""));
    turnEllipsis?.click();
    await h.waitFor(() => h.$('[data-testid="surface-subagent-turn"]') !== null);

    const badge = h.$('[data-subagent-kind="worker_turn"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toBe("Worker");
    const link = h.$('[data-testid="surface-subagent-target-link"]') as HTMLAnchorElement | null;
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("/s/worker-child-session");

    h.unmount();
  });
});
