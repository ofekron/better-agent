// ToolInteractionView's todo_snapshot dispatch: a recognized todo-tool
// call renders the checklist chrome (TodoItemRow, same `.todos-snapshot`
// class legacy's TodosSnapshotEvent uses); anything else falls through to
// the generic ToolCall view unchanged.

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolInteractionView } from "../../src/surface/nodes/ToolInteraction";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("ToolInteractionView — derived_view: todo_snapshot", () => {
  it("renders a checklist for a TodoWrite call, not the raw args/result view", async () => {
    const n = node({
      node_id: "tool1",
      turn_id: "t1",
      kind: "tool_interaction",
      payload: {
        tool_name: "TodoWrite",
        args: {
          todos: [
            { content: "Write tests", status: "completed", activeForm: "Writing tests" },
            { content: "Ship it", status: "in_progress", activeForm: "Shipping it" },
          ],
        },
        result: null,
        interaction_ref: null,
        ui_kind: null,
        derived_view: "todo_snapshot",
      },
    });
    render(<ToolInteractionView node={n} />);

    await waitFor(() => expect(screen.getByTestId("surface-todo-snapshot")).toBeTruthy());
    expect(screen.getByText("Write tests")).toBeTruthy();
    // In-progress item shows its activeForm, not its content (TodoItemRow's
    // own rule, reused verbatim).
    expect(screen.getByText("Shipping it")).toBeTruthy();
    expect(screen.queryByText(/"todos"/)).toBeNull();
  });

  it("falls through to the generic ToolCall view when the args can't be parsed as a todo snapshot", async () => {
    const n = node({
      node_id: "tool2",
      turn_id: "t1",
      kind: "tool_interaction",
      payload: {
        tool_name: "TodoWrite",
        args: { todos: "not-a-list" },
        result: null,
        interaction_ref: null,
        ui_kind: null,
        derived_view: "todo_snapshot",
      },
    });
    render(<ToolInteractionView node={n} />);

    await waitFor(() => expect(screen.getByTestId("surface-tool-interaction")).toBeTruthy());
    expect(screen.queryByTestId("surface-todo-snapshot")).toBeNull();
  });

  it("a non-todo tool call is unaffected (no derived_view)", async () => {
    const n = node({
      node_id: "tool3",
      turn_id: "t1",
      kind: "tool_interaction",
      payload: {
        tool_name: "Read",
        args: { file_path: "/a" },
        result: { output: "contents" },
        interaction_ref: null,
        ui_kind: null,
        derived_view: null,
      },
    });
    render(<ToolInteractionView node={n} />);

    await waitFor(() => expect(screen.getByTestId("surface-tool-interaction")).toBeTruthy());
    expect(screen.queryByTestId("surface-todo-snapshot")).toBeNull();
  });
});
