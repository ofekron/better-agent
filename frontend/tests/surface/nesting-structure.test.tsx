// Regression coverage for the native-rendering-migration "lost nesting"
// bug: chat-panel.md's Explanation container (Text? + Item*) had correct
// backend parent_id containment (derive.py's `derive_body`/
// `attach_body_items`) and correct frontend DOM nesting
// (nodes/Container.tsx's `ExplanationView` wraps its members in a real
// `.surface-explanation` div) — but that div carried NO CSS of its own,
// so a burst of grouped items rendered visually flush with its siblings,
// indistinguishable from an unstyled flat list. See globals.css's
// `.surface-explanation` rule (added alongside this file) for the fix.
//
// Two kinds of assertions on purpose:
//  - "chrome" describe: fails before the CSS fix, passes after — the
//    actual bug.
//  - "containment" describe: passes both before and after — it proves the
//    DOM structure itself was never broken, isolating the regression to
//    CSS rather than to backend derivation or React nesting.

import fs from "node:fs";
import path from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TurnView } from "../../src/surface/TurnView";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { ChildManifestWire, NodeWire, RunWire } from "../../src/adapter/wire";
import {
  explanationNode,
  failureNode,
  nativeSubagentTurnNode,
  node,
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

const NO_RUNS: ReadonlyMap<string, RunWire> = new Map();

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

describe("`.surface-explanation` CSS chrome (the actual regression)", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "../../src/styles/globals.css"), "utf8");

  function ruleBody(selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
    expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
    return match![1];
  }

  it("gives the Explanation container real box chrome (border + accent rail + radius + background), not zero-styling", () => {
    const rule = ruleBody(".surface-explanation");
    expect(rule).toContain("border");
    expect(rule).toContain("border-inline-start");
    expect(rule).toContain("border-radius");
    expect(rule).toContain("background");
  });
});

describe("Explanation/SubAgentTurn DOM containment (parent contains child, not sibling)", () => {
  it("nests thinking/tool/failure items INSIDE their owning .surface-explanation, and a subagent's own nested Explanation INSIDE .sub-agent-block, recursively", async () => {
    const fake = new FakeSurfaceStore();

    // Turn body: one Explanation (thinking + 2 tool calls + a failure) and
    // one native subagent turn (preserved in place, sibling of the
    // Explanation) whose OWN body is itself an Explanation wrapping one
    // more tool call — recursive containment to depth 2.
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 4, has_children: true });
    const subagent = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    fake.seed("turn:t1", [exp, subagent]);

    const thinking = node({
      kind: "thinking",
      node_id: "think1",
      turn_id: "t1",
      parent_id: "exp1",
      payload: { text: "planning the approach", redacted: false },
    });
    const tool1 = toolInteractionNode("t1", "tool1", "Read", "exp1");
    const tool2 = toolInteractionNode("t1", "tool2", "Write", "exp1");
    const failure = failureNode("t1", "fail1", "exp1");
    fake.seed("exp1", [thinking, tool1, tool2, failure]);

    const nestedExp = explanationNode("t1", "exp2", "sub1", { renderable_child_count: 1, has_children: true });
    fake.seed("sub1", [nestedExp]);
    const nestedTool = toolInteractionNode("t1", "tool3", "Bash", "exp2");
    fake.seed("exp2", [nestedTool]);

    const entry = entryWithBody(
      { renderable_child_count: 2, has_children: true },
      { results: [resultNode("t1", "done")] },
    );
    const { container } = render(<TurnView entry={entry} store={asStore(fake)} runsById={NO_RUNS} />);

    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));
    await waitFor(() => expect(screen.getByTestId("surface-explanation")).toBeTruthy());

    // The Explanation container has its OWN independent collapse state
    // (chat-panel.md: "Each level has its own independent collapse
    // state") — collapsed by default, showing only its ellipsis + last
    // item. Expand it too, to assert full containment of every member.
    await userEvent.click(screen.getByRole("button", { name: /expand hidden content/i }));

    const turnEl = container.querySelector('[data-testid="surface-turn"]')!;
    const explanationEls = turnEl.querySelectorAll('[data-testid="surface-explanation"]');
    expect(explanationEls.length).toBe(1); // the top-level one; the nested one is lazy behind the subagent's own collapse

    const topExplanation = explanationEls[0];
    // ThinkingBlock/ToolCall are lazy-loaded (Suspense, fallback={null}) —
    // wait for each dynamic import to resolve before querying its content;
    // under contended parallel test runs this can take longer than a
    // single microtask flush.
    await waitFor(() => expect(screen.getByText("planning the approach")).toBeTruthy());
    await waitFor(() =>
      expect(turnEl.querySelectorAll('[data-testid="surface-tool-interaction"]').length).toBeGreaterThanOrEqual(2),
    );
    const thinkingEl = screen.getByText("planning the approach");
    const toolEls = turnEl.querySelectorAll('[data-testid="surface-tool-interaction"]');
    const failureEl = screen.getByText("Something went wrong");

    // Every item from THIS partition is a DOM DESCENDANT of its owning
    // Explanation box — not a sibling dangling directly off .surface-turn.
    expect(topExplanation.contains(thinkingEl)).toBe(true);
    expect(topExplanation.contains(failureEl)).toBe(true);
    toolEls.forEach((el) => expect(topExplanation.contains(el)).toBe(true));

    // The subagent block is itself a direct structural child of the turn,
    // never flattened alongside the Explanation's own members.
    const subAgentEl = turnEl.querySelector('[data-testid="surface-subagent-turn"]')!;
    expect(subAgentEl).toBeTruthy();
    expect(topExplanation.contains(subAgentEl)).toBe(false);
    toolEls.forEach((el) => expect(subAgentEl.contains(el)).toBe(false));

    // Expand the subagent turn: its OWN nested Explanation/tool call must
    // land INSIDE .sub-agent-block, one level deeper — recursion, not
    // flattening back out to the turn root.
    const subAgentToggle = subAgentEl.querySelector("button.surface-collapsible-header") as HTMLElement;
    expect(subAgentToggle).toBeTruthy();
    await userEvent.click(subAgentToggle);
    await waitFor(() => expect(turnEl.querySelectorAll('[data-testid="surface-explanation"]').length).toBe(2));
    await waitFor(() =>
      expect(
        Array.from(turnEl.querySelectorAll('[data-testid="surface-tool-interaction"]')).some(
          (el) => !topExplanation.contains(el),
        ),
      ).toBe(true),
    );

    const nestedExplanationEl = Array.from(turnEl.querySelectorAll('[data-testid="surface-explanation"]')).find(
      (el) => el !== topExplanation,
    )!;
    expect(subAgentEl.contains(nestedExplanationEl)).toBe(true);
    const nestedToolEl = Array.from(turnEl.querySelectorAll('[data-testid="surface-tool-interaction"]')).find(
      (el) => !topExplanation.contains(el),
    )!;
    expect(nestedExplanationEl.contains(nestedToolEl)).toBe(true);
    expect(topExplanation.contains(nestedToolEl)).toBe(false);
  });
});
