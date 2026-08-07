import { describe, expect, it } from "vitest";
import { deriveTodoSnapshotItems } from "../../src/surface/nodes/todoSnapshot";

describe("deriveTodoSnapshotItems", () => {
  it("TodoWrite: maps args.todos verbatim (content/status/activeForm)", () => {
    const items = deriveTodoSnapshotItems("TodoWrite", {
      todos: [
        { content: "Write tests", status: "completed", activeForm: "Writing tests" },
        { content: "Ship it", status: "in_progress", activeForm: "Shipping it" },
        { content: "Celebrate", status: "pending" },
      ],
    });
    expect(items).toEqual([
      { content: "Write tests", status: "completed", activeForm: "Writing tests" },
      { content: "Ship it", status: "in_progress", activeForm: "Shipping it" },
      { content: "Celebrate", status: "pending", activeForm: undefined },
    ]);
  });

  it("TodoWrite: defaults an entry's missing/invalid status to pending", () => {
    const items = deriveTodoSnapshotItems("TodoWrite", { todos: [{ content: "X", status: "bogus" }] });
    expect(items).toEqual([{ content: "X", status: "pending", activeForm: undefined }]);
  });

  it("TodoWrite: null when args.todos is missing or not an array", () => {
    expect(deriveTodoSnapshotItems("TodoWrite", {})).toBeNull();
    expect(deriveTodoSnapshotItems("TodoWrite", { todos: "nope" })).toBeNull();
  });

  it("TodoWrite: null when every entry is malformed (empty result)", () => {
    expect(deriveTodoSnapshotItems("TodoWrite", { todos: [{}, { content: "" }] })).toBeNull();
  });

  it("TaskCreate: one pending item from subject/activeForm", () => {
    const items = deriveTodoSnapshotItems("TaskCreate", { subject: "  Fix the bug  ", activeForm: "Fixing the bug" });
    expect(items).toEqual([{ content: "Fix the bug", status: "pending", activeForm: "Fixing the bug" }]);
  });

  it("TaskCreate: null when subject is missing/blank", () => {
    expect(deriveTodoSnapshotItems("TaskCreate", {})).toBeNull();
    expect(deriveTodoSnapshotItems("TaskCreate", { subject: "   " })).toBeNull();
  });

  it("TaskUpdate: one item using subject when present, status defaults to in_progress", () => {
    const items = deriveTodoSnapshotItems("TaskUpdate", { taskId: "7", subject: "Renamed task" });
    expect(items).toEqual([{ content: "Renamed task", status: "in_progress", activeForm: undefined }]);
  });

  it("TaskUpdate: falls back to a taskId-derived label + explicit status when subject is absent", () => {
    const items = deriveTodoSnapshotItems("TaskUpdate", { taskId: 7, status: "completed" });
    expect(items).toEqual([{ content: "Task 7", status: "completed", activeForm: undefined }]);
  });

  it("TaskUpdate: null when neither subject nor taskId is present", () => {
    expect(deriveTodoSnapshotItems("TaskUpdate", { status: "completed" })).toBeNull();
  });

  it("unrecognized tool: null (falls through to the generic ToolCall view)", () => {
    expect(deriveTodoSnapshotItems("Read", { file_path: "/a" })).toBeNull();
  });
});
