// `tool_interaction` node — reuses components/ToolCall.tsx verbatim: it
// already takes {tool, args, result} as plain values with no ChatMessage/
// WSEvent coupling, so no extraction/adaptation was needed to make it
// native-safe.
//
// `derived_view: "todo_snapshot"` (TodoWrite/TaskCreate/TaskUpdate calls,
// per ADR 0006 §1): `deriveTodoSnapshotItems` (todoSnapshot.ts) parses
// this ONE call's own `args` into the checklist shape
// components/TodosPanel.tsx's `TodoItemRow` renders — reused verbatim
// (already plain-props, no ChatMessage coupling), wrapped in the SAME
// `.todos-snapshot` class components/MessageBubble.tsx's
// `TodosSnapshotEvent` uses, for visual parity. Falls through to the
// generic ToolCall args/result view when the parse can't produce any
// items (unrecognized tool, or malformed args).

import { lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import type { NodeWire, ToolInteractionPayloadWire } from "../../adapter/wire";
import { deriveTodoSnapshotItems } from "./todoSnapshot";

const ToolCall = lazy(() => import("../../components/ToolCall").then((m) => ({ default: m.ToolCall })));
const TodoItemRow = lazy(() => import("../../components/TodosPanel").then((m) => ({ default: m.TodoItemRow })));

export function ToolInteractionView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as ToolInteractionPayloadWire | null;
  if (!payload) return null;
  const resultText =
    payload.result && typeof payload.result.output === "string"
      ? payload.result.output
      : payload.result
        ? JSON.stringify(payload.result)
        : undefined;

  const todoItems =
    payload.derived_view === "todo_snapshot" ? deriveTodoSnapshotItems(payload.tool_name, payload.args) : null;

  return (
    <Suspense fallback={null}>
      <div data-testid="surface-tool-interaction" data-ui-kind={payload.ui_kind ?? undefined}>
        {payload.ui_kind && (
          <span className="surface-tool-ui-kind-badge" title={t("toolCall.subAgent") as string}>
            {payload.ui_kind}
          </span>
        )}
        {todoItems ? (
          <div className="todos-snapshot" data-testid="surface-todo-snapshot">
            {todoItems.map((item, i) => (
              <TodoItemRow key={item.source_id ?? `${i}-${item.content}`} item={item} />
            ))}
          </div>
        ) : (
          <ToolCall tool={payload.tool_name} args={payload.args} result={resultText} />
        )}
      </div>
    </Suspense>
  );
}
