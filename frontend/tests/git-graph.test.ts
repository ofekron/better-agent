import { describe, expect, it } from "vitest";
import { buildGitGraphRows, graphLaneCount, type GitCommit } from "../src/utils/gitGraph";

function commit(hash: string, parents: string[]): GitCommit {
  return {
    hash,
    parents,
    refs: [],
    author: "Author",
    authored_at: "2026-07-21T10:00:00Z",
    subject: hash,
  };
}

describe("git graph layout", () => {
  it("keeps merge parents on stable lanes until their shared ancestor", () => {
    const rows = buildGitGraphRows([
      commit("merge", ["main", "feature"]),
      commit("main", ["root"]),
      commit("feature", ["root"]),
      commit("root", []),
    ]);

    expect(rows.map((row) => row.lane)).toEqual([0, 0, 1, 0]);
    expect(rows[0].lanesAfter).toEqual(["main", "feature"]);
    expect(rows[1].lanesAfter).toEqual(["root", "feature"]);
    expect(rows[2].lanesAfter).toEqual(["root"]);
    expect(graphLaneCount(rows)).toBe(2);
  });

  it("starts disconnected branch tips without inventing a parent edge", () => {
    const rows = buildGitGraphRows([
      commit("tip-a", ["root-a"]),
      commit("root-a", []),
      commit("tip-b", ["root-b"]),
      commit("root-b", []),
    ]);

    expect(rows[0].isNewTip).toBe(true);
    expect(rows[2].isNewTip).toBe(true);
  });
});
