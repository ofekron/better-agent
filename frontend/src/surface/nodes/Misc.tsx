// `diagnostic`, `fact`, `unknown`, `worker_interaction`, `instruction_widget`
// — the remaining thin-dispatch leaf kinds. `user_interaction` has its own
// module (nodes/UserInteractionAction.tsx — a pending node renders a real
// action form, not just a chip).

import { useTranslation } from "react-i18next";
import type {
  DiagnosticPayloadWire,
  FactPayloadWire,
  InstructionWidgetPayloadWire,
  NodeWire,
  UnknownPayloadWire,
  WorkerInteractionPayloadWire,
} from "../../adapter/wire";
import { DiagnosticBox, FactChip } from "../leaf/Chips";
import { Markdown } from "../leaf/Markdown";

export function DiagnosticView({ node }: { node: NodeWire }) {
  const payload = node.payload as DiagnosticPayloadWire | null;
  if (!payload) return null;
  if (payload.code === "execution_continuation") {
    return (
      <div className="event-session" data-testid="surface-diagnostic-continuation">
        {payload.text}
      </div>
    );
  }
  return <DiagnosticBox kind={payload.code} raw={payload.data ?? payload.text} />;
}

export function FactView({ node }: { node: NodeWire }) {
  const payload = node.payload as FactPayloadWire | null;
  if (!payload) return null;
  return <FactChip payload={payload} />;
}

export function UnknownView({ node }: { node: NodeWire }) {
  const payload = node.payload as UnknownPayloadWire | null;
  if (!payload) return null;
  return <DiagnosticBox kind={payload.label} raw={payload.payload} />;
}

/** `worker_interaction` — one worker lifecycle fact carried verbatim
 * (worker_start | worker_event | worker_complete). No dedicated timeline
 * visualization yet in the native layer (legacy's rich
 * CollapsibleTimelineBlock/SubAgentBlock worker rendering is ChatMessage-
 * shaped) — renders as a compact one-line fact summary, a stage-1 gap. */
export function WorkerInteractionView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as WorkerInteractionPayloadWire | null;
  if (!payload) return null;
  const entries = Object.entries(payload.fact ?? {}).filter(([k]) => k !== "event");
  const summary = entries
    .slice(0, 4)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
  return (
    <div className="event-diagnostic" data-testid="surface-worker-interaction" style={{ fontSize: "0.8em", opacity: 0.75 }}>
      <span>{t("session.workers")}</span> <code>{payload.fact_kind}</code>
      {summary ? <span style={{ marginInlineStart: 6, opacity: 0.85 }}>{summary}</span> : null}
    </div>
  );
}

export function InstructionWidgetView({ payload }: { payload: InstructionWidgetPayloadWire }) {
  const { t } = useTranslation();
  return (
    <div className="event-session" data-testid="surface-instruction-widget">
      <strong>{t("message.instructionWidgetLabel")}</strong> <Markdown text={payload.text} />
    </div>
  );
}
