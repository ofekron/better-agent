// `tool_interaction` node, `derived_view: "todo_snapshot"` — parses the
// ONE call's own `args` snapshot into the same TodoItem/TaskItem shape
// components/TodosPanel.tsx's `TodoItemRow` already renders
// (content/status/activeForm/source_id — no import needed here, this is
// structurally compatible by design, not a shared type).
//
// backend/todo_projection.py's `extract_todos_from_normalized` builds
// legacy's `todos_snapshot` WSEvent by UNION-MERGING every TodoWrite call
// across the WHOLE session (accumulates completed items from earlier
// phases the agent's latest call dropped) — that accumulator lives
// session-side in the legacy pipeline and has no equivalent in the
// Contract-Node wire (ToolInteractionPayloadWire carries only this one
// call's own args, no running snapshot). Replicating the accumulator here
// would mean inventing frontend-side cross-node state the contract
// doesn't model — out of scope for a per-node leaf renderer. What IS
// faithfully replicable per node:
//   - TodoWrite: `args.todos` is already a (near-)complete list snapshot
//     on every call (that's WHY the backend's union-merge works at all)
//     — rendered as-is, per-item field mapping identical to
//     `_normalize_claude_todo`.
//   - TaskCreate / TaskUpdate: each call only ever describes ONE item —
//     rendered as a single-item checklist reflecting that one add/update.
// Returns `null` when the tool isn't a recognized todo tool, or its args
// don't match the expected shape — the caller (ToolInteractionView) falls
// through to the generic ToolCall args/result view in that case.

export type TodoSnapshotStatus = "pending" | "in_progress" | "completed";

export interface TodoSnapshotItem {
  content: string;
  status: TodoSnapshotStatus;
  activeForm?: string;
  source_id?: string;
}

const VALID_STATUSES: ReadonlySet<string> = new Set(["pending", "in_progress", "completed"]);

function normalizeStatus(raw: unknown, fallback: TodoSnapshotStatus): TodoSnapshotStatus {
  return typeof raw === "string" && VALID_STATUSES.has(raw) ? (raw as TodoSnapshotStatus) : fallback;
}

function fromTodoWriteEntry(entry: unknown): TodoSnapshotItem | null {
  if (!entry || typeof entry !== "object") return null;
  const e = entry as Record<string, unknown>;
  const content = typeof e.content === "string" ? e.content : "";
  if (!content) return null;
  return {
    content,
    status: normalizeStatus(e.status, "pending"),
    activeForm: typeof e.activeForm === "string" ? e.activeForm : undefined,
  };
}

export function deriveTodoSnapshotItems(
  toolName: string,
  args: Record<string, unknown>,
): TodoSnapshotItem[] | null {
  if (toolName === "TodoWrite") {
    const todos = args.todos;
    if (!Array.isArray(todos)) return null;
    const items = todos.map(fromTodoWriteEntry).filter((t): t is TodoSnapshotItem => t !== null);
    return items.length > 0 ? items : null;
  }

  if (toolName === "TaskCreate") {
    const subject = typeof args.subject === "string" ? args.subject.trim() : "";
    if (!subject) return null;
    return [{
      content: subject,
      status: "pending",
      activeForm: typeof args.activeForm === "string" ? args.activeForm : undefined,
    }];
  }

  if (toolName === "TaskUpdate") {
    const subject = typeof args.subject === "string" ? args.subject.trim() : "";
    const taskId = args.taskId;
    const content = subject || (taskId !== undefined && taskId !== null ? `Task ${String(taskId)}` : "");
    if (!content) return null;
    return [{
      content,
      status: normalizeStatus(args.status, "in_progress"),
      activeForm: typeof args.activeForm === "string" ? args.activeForm : undefined,
    }];
  }

  return null;
}
