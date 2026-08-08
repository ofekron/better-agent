// Native coverage for the compacted-prompt "expand to view the replayed
// transcript" gap (phase2-inventory.md, mb-family: "CompactionPayloadWire
// .replaced_node_ids is never read anywhere in src/"). Implemented via the
// SAME generic on-demand children fetch every other container uses
// (child_manifest + useChildren), not replaced_node_ids — see
// src/surface/nodes/Continuation.tsx's CompactedContent docstring for why:
// replaced_node_ids is never populated by any backend adapter today
// (grep-confirmed), so a real backend integration will show up as a
// non-zero child_manifest count instead — this suite proves the frontend
// side of that contract works today, gated so nothing renders while the
// backend gap remains open (verified below).

import { describe, it, expect } from "vitest";
import "../../src/i18n";
import { renderApp } from "../harness";
import { makeSession } from "../fixtures";
import { turnNode, compactTurn, promptNode, node, assistantTextNode } from "./fixtures";

describe("native surface — compaction replay expand", () => {
  it("a compaction with hidden pre-compaction content shows an expand control that reveals it on click", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-compact", messages: [] });
    const compactionId = "t1:compaction";
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-compact", {
          turns: [compactTurn(turnNode("t1", { renderable_child_count: 1, has_children: true }), promptNode("t1", "hi"), [])],
          childrenByNodeId: {
            "turn:t1": [
              node({
                node_id: compactionId,
                turn_id: "t1",
                kind: "compaction",
                parent_id: "turn:t1",
                child_manifest: { renderable_child_count: 1, has_children: true },
                payload: { origin: "native", summary: "conversation compacted", replaced_node_ids: [] },
              }),
            ],
            [compactionId]: [assistantTextNode("t1", "pre-compact-1", "the original pre-compaction reply", compactionId)],
          },
        });
      },
    });
    await h.selectSession("sess-compact");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);
    // The turn body (including the compaction boundary) is collapsed by
    // default — expand it first, same as multi-turn-collapse-independence.test.tsx.
    const turnEllipsis = Array.from(h.$$('button')).find((b) => /expand hidden content/i.test(b.getAttribute("aria-label") ?? ""));
    turnEllipsis?.click();
    await h.waitFor(() => h.$('[data-testid="surface-compaction"]') !== null);

    const compaction = h.$('[data-testid="surface-compaction"]')!;
    expect(compaction.textContent).toContain("conversation compacted");
    const expandControl = compaction.querySelector('[data-testid="surface-compaction-replay"]');
    expect(expandControl).not.toBeNull();
    expect(expandControl?.textContent).not.toContain("the original pre-compaction reply");

    const header = expandControl!.querySelector("button") as HTMLButtonElement;
    header.click();
    await h.waitFor(() => (expandControl?.textContent ?? "").includes("the original pre-compaction reply"));

    h.unmount();
  });

  it("a compaction with no hidden content (today's real backend shape) shows no expand control", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-compact-empty", messages: [] });
    const compactionId = "t1:compaction";
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-compact-empty", {
          turns: [compactTurn(turnNode("t1", { renderable_child_count: 1, has_children: true }), promptNode("t1", "hi"), [])],
          childrenByNodeId: {
            "turn:t1": [
              node({
                node_id: compactionId,
                turn_id: "t1",
                kind: "compaction",
                parent_id: "turn:t1",
                child_manifest: { renderable_child_count: 0, has_children: false },
                payload: { origin: "native", summary: "conversation compacted", replaced_node_ids: [] },
              }),
            ],
          },
        });
      },
    });
    await h.selectSession("sess-compact-empty");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);
    const turnEllipsis = Array.from(h.$$('button')).find((b) => /expand hidden content/i.test(b.getAttribute("aria-label") ?? ""));
    turnEllipsis?.click();
    await h.waitFor(() => h.$('[data-testid="surface-compaction"]') !== null);

    expect(h.$('[data-testid="surface-compaction-replay"]')).toBeNull();
    h.unmount();
  });
});
