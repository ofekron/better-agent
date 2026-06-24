import { describe, expect, it } from "vitest";

import {
  applyRowsToContent,
  buildAlignedDiffRows,
  buildDiffHunks,
  replaceLine,
} from "@better-agent/provider-config-sync-core/diff";

describe("providerConfigSyncDiff", () => {
  it("builds changed rows and hunks", () => {
    const rows = buildAlignedDiffRows("a\nb\nc\n", "a\nB\nc\n");
    const changed = rows.filter((row) => row.kind !== "same");
    expect(changed).toHaveLength(1);
    expect(changed[0]).toMatchObject({
      kind: "changed",
      unifiedLine: 2,
      specificLine: 2,
      unifiedText: "b",
      specificText: "B",
    });
    expect(buildDiffHunks(rows)).toHaveLength(1);
  });

  it("applies changed, added, and removed rows to either side", () => {
    const rows = buildAlignedDiffRows("a\nold\nremove\nz\n", "a\nnew\ninsert\nz\n");
    const diffRows = rows.filter((row) => row.kind !== "same");
    expect(applyRowsToContent("a\nold\nremove\nz\n", diffRows, "unified")).toBe("a\nnew\ninsert\nz\n");
    expect(applyRowsToContent("a\nnew\ninsert\nz\n", diffRows, "specific")).toBe("a\nold\nremove\nz\n");
  });

  it("applies a hunk insertion at its original position", () => {
    const rows = buildAlignedDiffRows("a\nz\n", "a\ninsert\nz\n");
    const [hunk] = buildDiffHunks(rows);

    expect(applyRowsToContent("a\nz\n", hunk.rows, "unified")).toBe("a\ninsert\nz\n");
  });

  it("applies a hunk insertion back to the specific side at its original position", () => {
    const rows = buildAlignedDiffRows("a\ninsert\nz\n", "a\nz\n");
    const [hunk] = buildDiffHunks(rows);

    expect(applyRowsToContent("a\nz\n", hunk.rows, "specific")).toBe("a\ninsert\nz\n");
  });

  it("replaces one editable line while preserving trailing newline style", () => {
    expect(replaceLine("a\nb\n", 2, 1, "B")).toBe("a\nB\n");
    expect(replaceLine("a\nb", null, 1, "insert")).toBe("a\ninsert\nb");
  });
});
