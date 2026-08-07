// Grammar rule under test: chat-panel.md's `renderModelMarkers` —
// "attach one marker to the last visible event of each contiguous
// provider/account/model/effort run" (markers.ts).

import { describe, expect, it } from "vitest";
import { computeRunMarkers } from "../../src/surface/markers";
import { assistantTextNode, runWire, SESSION } from "./fixtures";
import type { NodeWire } from "../../src/adapter/wire";

function withRun(n: NodeWire, runRef: string | null): NodeWire {
  return { ...n, run_ref: runRef };
}

describe("computeRunMarkers", () => {
  it("attaches a marker only at the LAST node of a contiguous same-run stretch", () => {
    const a = withRun(assistantTextNode("t1", "n1", "a", "turn:t1"), "r1");
    const b = withRun(assistantTextNode("t1", "n2", "b", "turn:t1"), "r1");
    const c = withRun(assistantTextNode("t1", "n3", "c", "turn:t1"), "r1");
    const runs = new Map([["r1", runWire("r1")]]);
    const markers = computeRunMarkers([a, b, c], runs);
    expect(markers.has("n1")).toBe(false);
    expect(markers.has("n2")).toBe(false);
    expect(markers.has("n3")).toBe(true);
    expect(markers.get("n3")).toEqual(runWire("r1"));
  });

  it("marks a new boundary at the last node before the run actually changes", () => {
    const a = withRun(assistantTextNode("t1", "n1", "a", "turn:t1"), "r1");
    const b = withRun(assistantTextNode("t1", "n2", "b", "turn:t1"), "r1");
    const c = withRun(assistantTextNode("t1", "n3", "c", "turn:t1"), "r2");
    const runs = new Map([
      ["r1", runWire("r1", { model: "claude-a" })],
      ["r2", runWire("r2", { model: "claude-b" })],
    ]);
    const markers = computeRunMarkers([a, b, c], runs);
    expect(markers.get("n2")?.model).toBe("claude-a"); // boundary of the FIRST run
    expect(markers.get("n3")?.model).toBe("claude-b"); // trailing marker for the run continuing to the end
    expect(markers.has("n1")).toBe(false);
  });

  it("re-opens the SAME run as a new contiguous stretch if it reappears after a different run (no merge across the gap)", () => {
    const a = withRun(assistantTextNode("t1", "n1", "a", "turn:t1"), "r1");
    const b = withRun(assistantTextNode("t1", "n2", "b", "turn:t1"), "r2");
    const c = withRun(assistantTextNode("t1", "n3", "c", "turn:t1"), "r1");
    const runs = new Map([["r1", runWire("r1")], ["r2", runWire("r2", { model: "other" })]]);
    const markers = computeRunMarkers([a, b, c], runs);
    expect(markers.get("n1")?.run_ref).toBe("r1");
    expect(markers.get("n2")?.run_ref).toBe("r2");
    expect(markers.get("n3")?.run_ref).toBe("r1");
  });

  it("skips nodes with no run_ref without breaking an in-progress run's contiguity", () => {
    const a = withRun(assistantTextNode("t1", "n1", "a", "turn:t1"), "r1");
    const noRun = withRun(assistantTextNode("t1", "n2", "b", "turn:t1"), null);
    const b = withRun(assistantTextNode("t1", "n3", "c", "turn:t1"), "r1");
    const runs = new Map([["r1", runWire("r1")]]);
    const markers = computeRunMarkers([a, noRun, b], runs);
    expect(markers.size).toBe(1);
    expect(markers.has("n3")).toBe(true);
  });

  it("returns no markers for an empty or run-less node list", () => {
    expect(computeRunMarkers([], new Map()).size).toBe(0);
    const noRun = withRun(assistantTextNode("t1", "n1", "a", "turn:t1"), null);
    expect(computeRunMarkers([noRun], new Map()).size).toBe(0);
  });
});
