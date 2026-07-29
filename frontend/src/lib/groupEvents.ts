import type { WSEvent } from "../types";

/** Normalize text for dedup comparison (strip leading emoji/whitespace) */
function normalizeForDedup(text: string): string {
  return text.replace(/^[\p{Emoji_Presentation}\p{Emoji}\uFE0F\u200D]+\s*/u, "").trim();
}

/**
 * Pre-process events: pair tool_call with following output, and deduplicate
 * output/thinking events that share the same text (CLI often emits both).
 *
 * `toolResultById` (optional): a map from `tool_use_id` to the tool's
 * rendered result text. Produced by `flattenClaudeMessages` when the
 * upstream event stream is claude's native shape — where tool_results
 * live in a separate `user` message and aren't adjacent to the matching
 * `tool_use`. We look up by id FIRST, and fall back to the legacy
 * "next event is an output" pairing for pre-refactor persisted sessions.
 */
const TODO_TOOLS = new Set(["TodoWrite", "TaskCreate", "TaskUpdate"]);
const STANDALONE_TOOL_CALLS = new Set(["WebSearch"]);
// An action group this large is a burst — collapse it by default into a
// single "N actions" header instead of rendering every tool card. Smaller
// groups stay open so normal multi-step turns read as before.
export const AUTO_ACTION_OPEN_MAX = 3;

function isTodoToolCall(ev: WSEvent): boolean {
  return ev.type === "tool_call" && TODO_TOOLS.has(ev.data?.tool as string);
}

function isStandaloneToolCall(ev: WSEvent): boolean {
  return ev.type === "tool_call" && STANDALONE_TOOL_CALLS.has(ev.data?.tool as string);
}

function todosKey(todos: unknown): string | null {
  if (!Array.isArray(todos)) return null;
  return JSON.stringify(todos.map((todo) => {
    if (!todo || typeof todo !== "object") return todo;
    const item = todo as Record<string, unknown>;
    return {
      content: item.content ?? "",
      status: item.status ?? "pending",
      activeForm: item.activeForm ?? null,
      source_id: item.source_id ?? null,
    };
  }));
}

export function groupEvents(
  events: WSEvent[],
  toolResultById?: Map<string, string>,
): Array<
  | { kind: "tool"; idx: number; event: WSEvent; result?: string }
  | { kind: "event"; idx: number; event: WSEvent }
> {
  const groups: ReturnType<typeof groupEvents> = [];
  let previousRenderedText: string | null = null;
  let i = 0;

  // When toolResultById has entries, events are in native Claude SDK
  // shape (tool_results in user messages). In this mode the positional
  // "next is output" fallback must NEVER fire — the next output after a
  // tool_call is assistant text, not a tool result. The fallback exists
  // only for legacy pre-refactor sessions that lacked tool_result blocks.
  const hasNativeResults = !!toolResultById && toolResultById.size > 0;

  // Track a run of consecutive todo tool_calls. When a non-todo event
  // breaks the run, flush the accumulated run as a single synthetic
  // todos_snapshot event. The last snapshot in the run carries the
  // final todo state (args contain the full list for TodoWrite, or the
  // individual item for TaskCreate/TaskUpdate).
  let todoRunStart = -1;
  let lastTodoArgs: Record<string, unknown> | null = null;
  let lastRenderedTodosKey: string | null = null;

  function pushTodosSnapshot(idx: number, event: WSEvent): void {
    const key = todosKey(event.data?.todos);
    if (key && key === lastRenderedTodosKey) return;
    groups.push({ kind: "event", idx, event });
    lastRenderedTodosKey = key;
  }

  function flushTodoRun() {
    if (todoRunStart === -1 || !lastTodoArgs) return;
    // For TodoWrite, args.todos is the full list. For TaskCreate/TaskUpdate,
    // we don't have the compiled list here — the todos_snapshot event
    // (injected by the backend) carries the compiled state. Fall back to
    // showing what we have.
    const todos = lastTodoArgs.todos as Array<Record<string, unknown>> | undefined;
    if (todos && Array.isArray(todos)) {
      // idx is used as a React key downstream — todoRunStart is unique
      // (the consumed todo tool_calls never pushed their own groups).
      const event = { type: "todos_snapshot", data: { todos } } as WSEvent;
      pushTodosSnapshot(todoRunStart, event);
    }
    todoRunStart = -1;
    lastTodoArgs = null;
  }

  while (i < events.length) {
    const ev = events[i];

    if (ev.type === "todos_snapshot") {
      previousRenderedText = null;
      const pendingKey = lastTodoArgs ? todosKey(lastTodoArgs.todos) : null;
      if (todoRunStart !== -1 && pendingKey && pendingKey === todosKey(ev.data?.todos)) {
        todoRunStart = -1;
        lastTodoArgs = null;
        pushTodosSnapshot(i, ev);
      } else {
        flushTodoRun();
        pushTodosSnapshot(i, ev);
      }
      i++;
      continue;
    }

    if (isTodoToolCall(ev)) {
      previousRenderedText = null;
      if (todoRunStart === -1) todoRunStart = i;
      lastTodoArgs = (ev.data?.args as Record<string, unknown>) ?? null;
      // Skip the tool_call + its paired result
      const tuid = ev.data?.tool_use_id as string | undefined;
      if (tuid && toolResultById?.has(tuid)) {
        i++;
      } else if (!hasNativeResults && i + 1 < events.length && events[i + 1].type === "output") {
        i += 2;
      } else {
        i++;
      }
      continue;
    }

    // Non-todo event — flush pending todo run before processing.
    flushTodoRun();

    if (ev.type === "tool_result" && ev.data?.paired_tool_result) {
      i++;
      continue;
    }

    if (ev.type === "tool_call") {
      previousRenderedText = null;
      // Prefer the id-based lookup (native claude shape); fall back to
      // the positional "next is output" pairing (legacy translator shape).
      let result: string | undefined;
      const tuid = ev.data?.tool_use_id as string | undefined;
      // idx is used as a React key downstream — stamp the group with the
      // tool_call's own index, not the post-consumption cursor (which
      // equals the NEXT group's index and collides).
      const startIdx = i;
      if (tuid && toolResultById?.has(tuid)) {
        result = toolResultById.get(tuid);
        i++;
      } else if (
        !hasNativeResults &&
        !isStandaloneToolCall(ev) &&
        i + 1 < events.length &&
        events[i + 1].type === "output"
      ) {
        result = events[i + 1].data.output as string;
        i += 2; // skip both
      } else {
        i++;
      }
      groups.push({ kind: "tool", idx: startIdx, event: ev, result });
    } else {
      // Deduplicate output/thinking events with identical text
      if (ev.type === "output" || ev.type === "thinking" || ev.type === "tool_result") {
        const raw = (
          ev.type === "output"
            ? ev.data.output
            : ev.type === "thinking"
              ? ev.data.thought
              : ev.data.output
        ) as string;
        const normalized = normalizeForDedup(raw || "");
        if (normalized && normalized === previousRenderedText) {
          i++;
          continue; // skip duplicate
        }
        previousRenderedText = normalized || null;
      } else {
        previousRenderedText = null;
      }
      groups.push({ kind: "event", idx: i, event: ev });
      i++;
    }
  }
  // Flush any trailing todo run.
  flushTodoRun();
  return groups;
}

type EventRenderGroup = ReturnType<typeof groupEvents>[number];
