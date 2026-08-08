// Regression coverage for the native-surface "chrome" gaps found in
// scratchpad/ui-before-after.md (bubble-card classes, turn running-state
// attribute, Collapsible caret glyph, pluralized collapsed-count copy) —
// the DOM/component half of each fix (see ../native-surface-chrome-css.test.ts
// for the CSS-source half).

import i18n from "i18next";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TurnView } from "../../src/surface/TurnView";
import { TypedPromptView } from "../../src/surface/nodes/TypedPrompt";
import { SubAgentTurnView } from "../../src/surface/nodes/Container";
import { CollapsibleBlock } from "../../src/surface/leaf/Collapsible";
import type { SurfaceStore, TurnEntry } from "../../src/surface/state";
import type { NodeWire, RunWire } from "../../src/adapter/wire";
import { nativeSubagentTurnNode, promptNode, resetSeq, resultNode, turnNode } from "./fixtures";

resetSeq();

// Honest {{count}} pluralization, same pattern as FileCommentBar.test.tsx —
// asserts the FORMATTED text, not a bare i18next missing-key fallback.
i18n.addResourceBundle(
  "en",
  "translation",
  { "message.eventsCount_one": "{{count}} event", "message.eventsCount_other": "{{count}} events" },
  true,
  true,
);

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

describe("TypedPromptView — bubble-card classes (legacy `.message-box.user-message-box`)", () => {
  it("carries message-box + user-message-box on the prompt wrapper", () => {
    const { getByTestId } = render(<TypedPromptView node={promptNode("t1", "hi")} />);
    const el = getByTestId("surface-typed-prompt");
    expect(el.classList.contains("message-box")).toBe(true);
    expect(el.classList.contains("user-message-box")).toBe(true);
  });
});

describe("TurnView — data-live reflects isLivePhase (turn running-state signal for CSS)", () => {
  function entry(phase: TurnEntry["phase"]): TurnEntry {
    return {
      turnId: "t1",
      turn: turnNode("t1", { renderable_child_count: 0, has_children: false }),
      prompt: promptNode("t1", "hi"),
      results: [resultNode("t1", "done")],
      manifest: { renderable_child_count: 0, has_children: false },
      runtimeChange: null,
      phase,
      reason: null,
      usage: null,
    };
  }

  it("sets data-live=true for every live phase (queued/starting/running/awaiting_interaction/reconnecting/stopping)", () => {
    for (const phase of ["queued", "starting", "running", "awaiting_interaction", "reconnecting", "stopping"] as const) {
      const { unmount, getByTestId } = render(
        <TurnView entry={entry(phase)} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />,
      );
      expect(getByTestId("surface-turn").getAttribute("data-live")).toBe("true");
      unmount();
    }
  });

  it("omits data-live for a not-live phase", () => {
    const { getByTestId } = render(
      <TurnView entry={entry("completed")} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />,
    );
    expect(getByTestId("surface-turn").getAttribute("data-live")).toBeNull();
  });

  it("wraps the body/result/usage region in .surface-turn-body, the connector-line owner", () => {
    const { getByTestId } = render(
      <TurnView entry={entry("completed")} store={asStore(new FakeSurfaceStore())} runsById={NO_RUNS} />,
    );
    const turnEl = getByTestId("surface-turn");
    const bodyEl = turnEl.querySelector(".surface-turn-body");
    expect(bodyEl).toBeTruthy();
    expect(bodyEl!.contains(getByTestId("surface-result"))).toBe(true);
  });
});

describe("CollapsibleBlock — app-wide caret glyph (legacy `.collapse-arrow` ▶/▼, not a distinct ▸/▾ family)", () => {
  it("uses the .collapse-arrow class and ▶/▼ glyphs, toggling on open/closed", () => {
    const { getByRole, container } = render(
      <CollapsibleBlock header={<span>label</span>} testId="cb1">
        body
      </CollapsibleBlock>,
    );
    const caret = container.querySelector(".collapse-arrow")!;
    expect(caret).toBeTruthy();
    expect(caret.textContent).toBe("▶");
    expect(container.querySelector(".surface-collapsible-caret")).toBeNull();

    fireEvent.click(getByRole("button"));
    expect(container.querySelector(".collapse-arrow")!.textContent).toBe("▼");
  });
});

describe("SubAgentTurnView — pluralized collapsed-count copy (legacy `{n} event(s)`)", () => {
  it("renders '1 event' for a single-item count, not a bare '1'", async () => {
    const fake = new FakeSurfaceStore();
    const n = nativeSubagentTurnNode("t1", "sub1", "turn:t1", { renderable_child_count: 1, has_children: true });
    render(<SubAgentTurnView node={n} store={asStore(fake)} containerMode="collapsed" runsById={NO_RUNS} />);
    await waitFor(() => expect(screen.getByText("1 event")).toBeTruthy());
  });

  it("renders '2 events' (plural) for a multi-item count", async () => {
    const fake = new FakeSurfaceStore();
    const n = nativeSubagentTurnNode("t1", "sub2", "turn:t1", { renderable_child_count: 2, has_children: true });
    render(<SubAgentTurnView node={n} store={asStore(fake)} containerMode="collapsed" runsById={NO_RUNS} />);
    await waitFor(() => expect(screen.getByText("2 events")).toBeTruthy());
  });
});
