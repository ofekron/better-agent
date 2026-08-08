// Coverage for the two backend-sourced turn/group chips this round adds:
//  - Explanation/group container count + time-range chip (backend
//    `nodes.py`'s `ChildManifest.started_ts`/`ended_ts`, `derive.py`
//    computes them at container build) — `ExplanationMetaChip` in
//    `nodes/Container.tsx`, reusing legacy `AutoActionGroup`'s
//    `.auto-action-group-count`/`-time` chip CSS.
//  - Team-vs-native turn chip (backend `Node.orchestration_mode`,
//    frozen at session creation) — `TurnView.tsx`, legacy parity with the
//    (still-present) `frontend/tests/primary-entity-label.test.tsx`
//    (`.role-label-manager .role-chip` textContent "Team").

import fs from "node:fs";
import path from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import "../../src/i18n"; // real translation resources (setup.ts's own i18n init carries none)
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { ChildManifestWire, NodeWire, RunWire } from "../../src/adapter/wire";
import { assistantTextNode, explanationNode, promptNode, resetSeq, toolInteractionNode, turnNode } from "./fixtures";

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

function entryWithBody(
  manifest: ChildManifestWire,
  overrides: Partial<TurnEntry> = {},
): TurnEntry {
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

describe("`.surface-explanation-meta` CSS chrome (reuses AutoActionGroup's chip look)", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "../../src/styles/globals.css"), "utf8");

  function ruleBody(selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
    expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
    return match![1];
  }

  it("the count chip reuses .auto-action-group-count's own rule (not a duplicated ruleset)", () => {
    expect(ruleBody(".auto-action-group-count")).toContain("color");
  });

  it("the time chip reuses .auto-action-group-time's own rule (not a duplicated ruleset)", () => {
    expect(ruleBody(".auto-action-group-time")).toContain("tabular-nums");
  });
});

describe("Explanation count + time-range chip (ChildManifest.started_ts/ended_ts)", () => {
  it("renders the renderable count and a start–end time range when they differ", async () => {
    const fake = new FakeSurfaceStore();
    const manifest: ChildManifestWire = {
      renderable_child_count: 2, has_children: true, started_ts: 1000, ended_ts: 1090,
    };
    const exp = explanationNode("t1", "exp1", "turn:t1", manifest);
    fake.seed("turn:t1", [exp]);
    fake.seed("exp1", [
      assistantTextNode("t1", "text1", "leading text", "exp1"),
      toolInteractionNode("t1", "tool1", "Read", "exp1"),
    ]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-explanation-meta")).toBeTruthy());

    const meta = screen.getByTestId("surface-explanation-meta");
    expect(meta.querySelector(".auto-action-group-count")).toBeTruthy();
    const timeEl = meta.querySelector(".auto-action-group-time");
    expect(timeEl).toBeTruthy();
    expect(timeEl!.textContent).toContain("–"); // en-dash: a real range, not a single lead time
  });

  it("renders no time chip and no meta row at all when the container has no children", async () => {
    const fake = new FakeSurfaceStore();
    const emptyManifest: ChildManifestWire = { renderable_child_count: 0, has_children: false };
    const exp = explanationNode("t1", "exp1", "turn:t1", emptyManifest);
    fake.seed("turn:t1", [exp]);
    fake.seed("exp1", [assistantTextNode("t1", "text1", "leading text", "exp1")]);
    const entry = entryWithBody({ renderable_child_count: 1, has_children: true });
    render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-explanation")).toBeTruthy());
    expect(screen.queryByTestId("surface-explanation-meta")).toBeNull();
  });
});

describe("Team-vs-native turn chip (Node.orchestration_mode)", () => {
  it("renders no `.role-label-manager` element at all for a native/unset turn", () => {
    const entry = entryWithBody({ renderable_child_count: 0, has_children: false });
    const { container } = render(<TurnView entry={entry} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);
    expect(container.querySelector(".role-label-manager")).toBeNull();
    expect(screen.queryByTestId("surface-turn-team-scope")).toBeNull();
  });

  it("labels a team-mode turn '.role-label-manager .role-chip' == 'Team' (legacy parity)", () => {
    const manifest: ChildManifestWire = { renderable_child_count: 0, has_children: false };
    const entry = entryWithBody(manifest, { turn: turnNode("t1", manifest, "s1", "team") });
    const { container } = render(<TurnView entry={entry} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />);
    expect(container.querySelector(".role-label-manager .role-chip")?.textContent).toBe("Team");
  });
});
