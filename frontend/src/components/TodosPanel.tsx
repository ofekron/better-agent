import { useTranslation } from "react-i18next";
import type { TodoItem, TaskItem } from "../types";

interface Props {
  todos: TodoItem[];
  tasks: TaskItem[];
}

interface TodoPresentation<T extends TodoItem | TaskItem> {
  current: T[];
  previous: T[];
}

export function splitTodoPresentation<T extends TodoItem | TaskItem>(
  items: T[],
): TodoPresentation<T> {
  const completedIndexes = items
    .map((item, index) => item.status === "completed" ? index : -1)
    .filter((index) => index !== -1);
  if (completedIndexes.length === 0) {
    return { current: items, previous: [] };
  }

  const lastCompletedIndex = completedIndexes[completedIndexes.length - 1];
  const tail = items.slice(lastCompletedIndex + 1);
  if (tail.some((item) => item.status !== "completed")) {
    return { current: tail, previous: items.slice(0, lastCompletedIndex + 1) };
  }

  const openItems = items.filter((item) => item.status !== "completed");
  if (openItems.length > 0) {
    return {
      current: openItems,
      previous: items.filter((item) => item.status === "completed"),
    };
  }

  return { current: items, previous: [] };
}

export function visibleTodoCount(items: Array<TodoItem | TaskItem>): number {
  return splitTodoPresentation(items).current.length;
}

export function TodoItemRow({
  item,
  className,
}: {
  item: TodoItem | TaskItem;
  className?: string;
}) {
  const statusLabel = item.status === "completed" ? "Completed"
    : item.status === "in_progress" ? "In progress" : "Pending";
  const text = item.status === "in_progress" && item.activeForm
    ? item.activeForm
    : item.content;
  return (
    <div className={`todo-item todo-${item.status} ${className ?? ""}`}>
      <span
        className="todo-marker"
        role="img"
        aria-label={statusLabel}
        title={statusLabel}
      />
      <span className="todo-text">{text}</span>
    </div>
  );
}

function ItemList({ items }: { items: TodoItem[] | TaskItem[] }) {
  return (
    <>
      {items.map((item, idx) => (
        <TodoItemRow
          key={item.source_id ?? `${idx}-${item.content}`}
          item={item}
        />
      ))}
    </>
  );
}

function SmartItemList<T extends TodoItem | TaskItem>({
  items,
  previousLabel,
}: {
  items: T[];
  previousLabel: string;
}) {
  const { current, previous } = splitTodoPresentation(items);
  return (
    <>
      <ItemList items={current} />
      {previous.length > 0 && (
        <details className="todos-history">
          <summary>{previousLabel}</summary>
          <ItemList items={previous} />
        </details>
      )}
    </>
  );
}

/** Cross-provider TODO + Tasks list for the right panel.
 *
 * Read-only by design: both lists are backend-owned, derived from
 * the event stream. Todos come from TodoWrite (Claude snapshot,
 * Gemini delta); Tasks come from TaskCreate / TaskUpdate.
 */
export function TodosPanel({ todos, tasks }: Props) {
  const { t } = useTranslation();
  const hasTodos = todos && todos.length > 0;
  const hasTasks = tasks && tasks.length > 0;

  if (!hasTodos && !hasTasks) {
    return (
      <div className="todos-panel-empty">
        <p>{t("todos.empty", "No todos yet")}</p>
        <p className="todos-panel-hint">
          {t(
            "todos.hint",
            "Appears when the agent calls TodoWrite (Claude) or update_topic (Gemini).",
          )}
        </p>
      </div>
    );
  }

  return (
    <div className="todos-panel">
      {hasTodos && (
        <div className="todos-section">
          {hasTasks && (
            <div className="todos-section-header">{t("todos.sectionTodos", "Todos")}</div>
          )}
          <SmartItemList
            items={todos}
            previousLabel={`${t("todos.previous", "Previous")} (${splitTodoPresentation(todos).previous.length})`}
          />
        </div>
      )}
      {hasTasks && (
        <div className="todos-section">
          {hasTodos && (
            <div className="todos-section-header">{t("todos.sectionTasks", "Tasks")}</div>
          )}
          <SmartItemList
            items={tasks}
            previousLabel={`${t("todos.previous", "Previous")} (${splitTodoPresentation(tasks).previous.length})`}
          />
        </div>
      )}
    </div>
  );
}
