import { describe, it, expect } from "vitest";
import { toolApprovalArgRows } from "../src/components/Chat";

/**
 * The approval card must surface EVERY argument of a tool call so the user
 * knows exactly what they're approving — not just `command`/`files`. These
 * lock the normalization that drives the card's per-argument rows.
 */
describe("toolApprovalArgRows", () => {
  it("renders every argument of a Write call (file_path + content)", () => {
    const rows = toolApprovalArgRows({
      tool: "Write",
      input: { file_path: "/tmp/x.ts", content: "hello" },
    });
    expect(rows).toEqual([
      { key: "file_path", value: "/tmp/x.ts" },
      { key: "content", value: "hello" },
    ]);
  });

  it("renders every argument of an Edit call (old_string + new_string)", () => {
    const rows = toolApprovalArgRows({
      tool: "Edit",
      input: { file_path: "/a", old_string: "foo", new_string: "bar" },
    });
    expect(rows.map((r) => r.key)).toEqual(["file_path", "old_string", "new_string"]);
    expect(rows.find((r) => r.key === "old_string")?.value).toBe("foo");
  });

  it("still renders a Bash command (the only field the old card handled)", () => {
    const rows = toolApprovalArgRows({ tool: "Bash", input: { command: "ls -la" } });
    expect(rows).toEqual([{ key: "command", value: "ls -la" }]);
  });

  it("tolerates the legacy {args} shape (older / replayed records)", () => {
    const rows = toolApprovalArgRows({ tool: "Bash", args: { command: "pwd" } });
    expect(rows).toEqual([{ key: "command", value: "pwd" }]);
  });

  it("stringifies non-string values (arrays, numbers, objects, booleans)", () => {
    const rows = toolApprovalArgRows({
      tool: "X",
      input: { files: ["a", "b"], limit: 5, opts: { deep: true }, flag: false },
    });
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.value]));
    expect(byKey.files).toBe('["a","b"]');
    expect(byKey.limit).toBe("5");
    expect(byKey.opts).toBe('{"deep":true}');
    expect(byKey.flag).toBe("false");
  });

  it("renders null / undefined argument values without throwing", () => {
    const rows = toolApprovalArgRows({ tool: "X", input: { a: null, b: undefined } });
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.value]));
    expect(byKey.a).toBe("null");
    expect(byKey.b).toBe("undefined");
  });

  it("truncates very long values with an ellipsis (card stays bounded)", () => {
    const long = "z".repeat(5000);
    const rows = toolApprovalArgRows({ tool: "Write", input: { content: long } });
    const v = rows[0].value;
    expect(v.length).toBeLessThan(long.length);
    expect(v.endsWith("…")).toBe(true);
  });

  it("returns no rows for an empty input (card shows the no-args notice)", () => {
    expect(toolApprovalArgRows({ tool: "Read", input: {} })).toEqual([]);
  });

  it("returns no rows for a missing / malformed summary (never throws)", () => {
    expect(toolApprovalArgRows(undefined)).toEqual([]);
    expect(toolApprovalArgRows({})).toEqual([]);
    expect(toolApprovalArgRows({ tool: "X", input: null as unknown as undefined })).toEqual([]);
  });
});
