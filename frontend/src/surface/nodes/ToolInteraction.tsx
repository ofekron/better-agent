// `tool_interaction` node — reuses components/ToolCall.tsx verbatim: it
// already takes {tool, args, result} as plain values with no ChatMessage/
// WSEvent coupling, so no extraction/adaptation was needed to make it
// native-safe.
//
// `derived_view: "todo_snapshot"` (TodoWrite/TaskCreate/TaskUpdate calls,
// per ADR 0006 §1) does not yet have a native-typed checklist renderer —
// components/MessageBubble.tsx's TodosSnapshotEvent takes a `TodoItem[]`
// parsed out of legacy WSEvent.data by the mapping layer; wiring the same
// parse here is a stage-1 gap (falls through to the generic ToolCall args/
// result view, which still shows the raw TodoWrite payload, just not the
// checklist chrome).

import { lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import type { NodeWire, ToolInteractionPayloadWire } from "../../adapter/wire";

const ToolCall = lazy(() => import("../../components/ToolCall").then((m) => ({ default: m.ToolCall })));

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
  return (
    <Suspense fallback={null}>
      <div data-testid="surface-tool-interaction" data-ui-kind={payload.ui_kind ?? undefined}>
        {payload.ui_kind && (
          <span className="surface-tool-ui-kind-badge" title={t("toolCall.subAgent") as string}>
            {payload.ui_kind}
          </span>
        )}
        <ToolCall tool={payload.tool_name} args={payload.args} result={resultText} />
      </div>
    </Suspense>
  );
}
