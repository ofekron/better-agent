import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { TodosSnapshotEvent } from "../src/components/MessageBubble";
import type { TodoItem } from "../src/types";

// TodosSnapshotEvent renders a `.todos-snapshot` list of TodoItemRow children.
// Branch coverage targets: the `!todos` and `length === 0` early-return branches
// (both null), the per-item map, and delegation to TodoItemRow for status class
// and in_progress activeForm text selection.

describe("TodosSnapshotEvent empty guard", () => {
  it("renders nothing when todos is undefined", () => {
    const { container } = render(<TodosSnapshotEvent todos={undefined as unknown as TodoItem[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when todos is empty", () => {
    const { container } = render(<TodosSnapshotEvent todos={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("TodosSnapshotEvent rendering", () => {
  it("renders a snapshot wrapper with one row per item", () => {
    const todos: TodoItem[] = [
      { content: "Write tests", status: "pending" },
      { content: "Ship it", status: "completed" },
    ];
    const { container } = render(<TodosSnapshotEvent todos={todos} />);

    const snapshot = container.querySelector(".todos-snapshot");
    expect(snapshot).not.toBeNull();
    const rows = container.querySelectorAll(".todo-item");
    expect(rows).toHaveLength(2);
  });

  it("applies the status class per row", () => {
    const todos: TodoItem[] = [
      { content: "a", status: "pending" },
      { content: "b", status: "in_progress" },
      { content: "c", status: "completed" },
    ];
    const { container } = render(<TodosSnapshotEvent todos={todos} />);

    expect(container.querySelector(".todo-pending")).not.toBeNull();
    expect(container.querySelector(".todo-in_progress")).not.toBeNull();
    expect(container.querySelector(".todo-completed")).not.toBeNull();
  });

  it("shows activeForm text for an in_progress row", () => {
    const todos: TodoItem[] = [
      { content: "Write tests", status: "in_progress", activeForm: "Writing tests" },
    ];
    const { container } = render(<TodosSnapshotEvent todos={todos} />);

    const text = container.querySelector(".todo-text");
    expect(text?.textContent).toBe("Writing tests");
  });

  it("shows content text when no activeForm is set", () => {
    const todos: TodoItem[] = [{ content: "Write tests", status: "pending" }];
    const { container } = render(<TodosSnapshotEvent todos={todos} />);

    const text = container.querySelector(".todo-text");
    expect(text?.textContent).toBe("Write tests");
  });
});
