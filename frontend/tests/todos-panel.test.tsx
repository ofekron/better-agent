import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  mergeTodoWorkItems,
  splitTodoPresentation,
  TodoItemRow,
  todoProgress,
  TodosPanel,
  visibleTodoCount,
} from "../src/components/TodosPanel";

describe("TodoItemRow", () => {
  it.each([
    ["pending", "Pending"],
    ["in_progress", "In progress"],
    ["completed", "Completed"],
  ] as const)("renders %s as a labeled checkbox indicator", (status, label) => {
    const { container } = render(
      <TodoItemRow item={{ content: "Review changes", status }} />,
    );

    expect(screen.getByLabelText(label).classList.contains("todo-marker")).toBe(true);
    expect(screen.getByText("Review changes")).toBeTruthy();
    expect(container.textContent).toBe("Review changes");
  });
});

describe("TodosPanel presentation", () => {
  it("merges todo and task state with completed duplicates winning", () => {
    const todos = [
      { content: "Shared item", status: "pending" as const },
      { content: "Todo only", status: "in_progress" as const },
    ];
    const tasks = [
      { content: "Shared item", status: "completed" as const },
      { content: "Task only", status: "pending" as const },
    ];

    expect(mergeTodoWorkItems(todos, tasks)).toEqual([
      { content: "Shared item", status: "completed" },
      { content: "Todo only", status: "in_progress" },
      { content: "Task only", status: "pending" },
    ]);
    expect(todoProgress(todos, tasks)).toEqual({ total: 3, done: 1, visible: 2 });
  });

  it("shows the current trailing plan and collapses older todo history", () => {
    const todos = [
      { content: "Old active", status: "in_progress" as const },
      { content: "Old pending", status: "pending" as const },
      { content: "Done boundary", status: "completed" as const },
      { content: "Current active", status: "in_progress" as const },
      { content: "Current pending", status: "pending" as const },
    ];

    expect(splitTodoPresentation(todos)).toEqual({
      current: todos.slice(3),
      previous: todos.slice(0, 3),
    });
    expect(visibleTodoCount(todos)).toBe(2);

    render(<TodosPanel todos={todos} tasks={[]} />);

    expect(screen.getByText("Current active")).toBeTruthy();
    expect(screen.getByText("Current pending")).toBeTruthy();
    expect(screen.getByText("Previous (3)")).toBeTruthy();
    expect(screen.getByText("Old active")).toBeTruthy();
  });

  it("keeps an all-completed list visible instead of hiding it as history", () => {
    const todos = [
      { content: "A", status: "completed" as const },
      { content: "B", status: "completed" as const },
    ];

    expect(splitTodoPresentation(todos)).toEqual({
      current: todos,
      previous: [],
    });
    expect(visibleTodoCount(todos)).toBe(2);
  });
});
