import { describe, expect, it } from "vitest";
import { filterStaleDraftPatch, nextDraftSeq } from "../src/utils/draftSeq";
import type { SessionMetadataPatch } from "../src/hooks/useSession";

describe("filterStaleDraftPatch", () => {
  it("drops a stale draft echo (incoming seq < stored clear seq) but keeps non-draft fields", () => {
    // Regression: after clearing on send (stored seq T2), a late WS echo of
    // the pre-send text (seq T1) used to resurrect the sent text.
    const patch: SessionMetadataPatch = {
      draft_input: "hello",
      draft_images: [],
      draft_input_seq: 100,
      model: "claude-opus-4-8",
    };
    const out = filterStaleDraftPatch(patch, 200, false);
    expect(out.draft_input).toBeUndefined();
    expect(out.draft_images).toBeUndefined();
    expect(out.draft_input_seq).toBeUndefined();
    expect(out.model).toBe("claude-opus-4-8");
  });

  it("applies a newer draft echo", () => {
    const patch: SessionMetadataPatch = { draft_input: "newer", draft_input_seq: 300 };
    const out = filterStaleDraftPatch(patch, 200, false);
    expect(out.draft_input).toBe("newer");
    expect(out.draft_input_seq).toBe(300);
  });

  it("drops an equal-seq echo (already applied)", () => {
    const patch: SessionMetadataPatch = { draft_input: "dup", draft_input_seq: 200 };
    const out = filterStaleDraftPatch(patch, 200, false);
    expect(out.draft_input).toBeUndefined();
  });

  it("drops draft fields while the user is actively typing (pending debounce)", () => {
    const patch: SessionMetadataPatch = { draft_input: "remote", draft_input_seq: 999 };
    const out = filterStaleDraftPatch(patch, 1, true);
    expect(out.draft_input).toBeUndefined();
  });

  it("applies when seqs are undefined (legacy snapshot / patch without seq)", () => {
    expect(filterStaleDraftPatch({ draft_input: "a", draft_input_seq: 5 }, undefined, false).draft_input).toBe("a");
    expect(filterStaleDraftPatch({ draft_input: "b" }, 5, false).draft_input).toBe("b");
  });

  it("passes patches with no draft fields through untouched", () => {
    const patch: SessionMetadataPatch = { model: "x" };
    expect(filterStaleDraftPatch(patch, 5, true)).toBe(patch);
  });
});

describe("nextDraftSeq", () => {
  it("returns the wall-clock time when it advances", () => {
    expect(nextDraftSeq(1000)).toBe(1000);
    expect(nextDraftSeq(2000)).toBe(2000);
  });

  it("forces strict increase on a same-ms (or backwards) call", () => {
    nextDraftSeq(5000);
    expect(nextDraftSeq(5000)).toBe(5001);
    expect(nextDraftSeq(4000)).toBe(5002);
  });
});
