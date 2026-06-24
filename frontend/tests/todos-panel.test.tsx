import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  splitTodoPresentation,
  TodoItemRow,
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
