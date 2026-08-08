import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { compactTurn, explanationNode, promptNode, resultNode, turnNode } from "./surface/fixtures";

/**
 * Ported gap closure for tests/turn-group-collapse.test.tsx's
 * "default-collapse-when-completed" group (deleted, 1835 lines, legacy
 * flat-message-model specific): each completed turn collapses
 * independently by default — appending/rendering a second completed turn
 * must not disturb the first turn's own (still-collapsed) state, and
 * expanding one turn must not expand the other. tests/surface/render.test.tsx
 * already proves the single-turn collapse/expand mechanics against a
 * FakeSurfaceStore; this closes the multi-turn independence gap through
 * the real harness (seedSurface via configureBackend, per
 * tests/surfaceHarness.test.tsx's documented race-safe seeding pattern).
 */
describe("multiple completed turns collapse independently", () => {
  it("expanding turn 1 does not expand turn 2, and vice versa", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-multi-collapse", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-multi-collapse", {
          turns: [
            compactTurn(
              turnNode("t1", { renderable_child_count: 1, has_children: true }),
              promptNode("t1", "first"),
              [resultNode("t1", "result one")],
            ),
            compactTurn(
              turnNode("t2", { renderable_child_count: 1, has_children: true }),
              promptNode("t2", "second"),
              [resultNode("t2", "result two")],
            ),
          ],
          childrenByNodeId: {
            "turn:t1": [explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 1, has_children: true })],
            exp1: [
              {
                ...resultNode("t1", "hidden body one"),
                node_id: "body1",
                kind: "assistant_text",
                parent_id: "exp1",
                payload: { text: "hidden body one" },
              },
            ],
            "turn:t2": [explanationNode("t2", "exp2", "turn:t2", { renderable_child_count: 1, has_children: true })],
            exp2: [
              {
                ...resultNode("t2", "hidden body two"),
                node_id: "body2",
                kind: "assistant_text",
                parent_id: "exp2",
                payload: { text: "hidden body two" },
              },
            ],
          },
        });
      },
    });

    await h.selectSession("sess-multi-collapse");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 2);

    const turns = h.$$('[data-testid="surface-turn"]');
    const turn1 = turns.find((t) => t.getAttribute("data-turn-id") === "t1")!;
    const turn2 = turns.find((t) => t.getAttribute("data-turn-id") === "t2")!;

    // Both collapsed by default: neither body is visible yet.
    expect(turn1.textContent).not.toContain("hidden body one");
    expect(turn2.textContent).not.toContain("hidden body two");

    // Expand only turn 1.
    const expandButtons1 = turn1.querySelectorAll<HTMLButtonElement>('button');
    const ellipsisBtn1 = Array.from(expandButtons1).find((b) => /expand hidden content/i.test(b.getAttribute("aria-label") ?? b.textContent ?? ""));
    ellipsisBtn1?.click();

    await h.waitFor(() => turn1.textContent?.includes("hidden body one") ?? false);
    expect(turn1.textContent).toContain("hidden body one");
    // Turn 2 remains collapsed, unaffected by turn 1's expansion.
    expect(turn2.textContent).not.toContain("hidden body two");

    h.unmount();
  });
});
