import { describe, expect, it } from "vitest";
import { emptyForestState, reduceChatForest } from "../src/chatForest/reducer";
import type { ForestSnapshot, PromptTree } from "../src/chatForest/types";

const tree = (id: string, text: string): PromptTree => ({
  id, prompt: { id: `${id}-p`, text, payload: {} }, turn_id: id,
  explanations: [], work: [], status: "complete", queued: false,
  partial: false, has_late_output: false, events_collapsed_by_default: true,
  prompt_text_collapsed_by_default: false, collapsed_preview: "",
});

const snapshot: ForestSnapshot = {
  found: true, kind: "snapshot", schema_version: 2, root_generation: 3,
  epoch: "epoch", revision: 1, canonical_through_seq: 4, checksum: "a",
  forest: { root_id: "root", root_generation: 3, canonical_through_seq: 4, trees: [tree("one", "first")] },
};

describe("reduceChatForest", () => {
  it("applies an exact delta without rebuilding unchanged trees", () => {
    const loading = reduceChatForest(emptyForestState, { type: "load", sessionId: "root" });
    const ready = reduceChatForest(loading, { type: "response", sessionId: "root", response: snapshot });
    const first = ready.forest!.trees[0];
    const updated = reduceChatForest(ready, { type: "response", sessionId: "root", response: {
      found: true, kind: "delta", schema_version: 2, root_generation: 3,
      epoch: "epoch", base_revision: 1, target_revision: 2,
      canonical_through_seq: 5, checksum: "b", upsert_trees: [tree("two", "second")], remove_tree_ids: [],
    }});
    expect(updated.revision).toBe(2);
    expect(updated.forest!.trees[0]).toBe(first);
    expect(updated.forest!.trees.map((value) => value.id)).toEqual(["one", "two"]);
  });

  it("requests a snapshot when epoch or base revision diverges", () => {
    const ready = reduceChatForest(
      reduceChatForest(emptyForestState, { type: "load", sessionId: "root" }),
      { type: "response", sessionId: "root", response: snapshot },
    );
    const diverged = reduceChatForest(ready, { type: "response", sessionId: "root", response: {
      found: true, kind: "delta", schema_version: 2, root_generation: 3,
      epoch: "other", base_revision: 1, target_revision: 2,
      canonical_through_seq: 5, checksum: "b", upsert_trees: [], remove_tree_ids: [],
    }});
    expect(diverged.status).toBe("loading");
  });
});
