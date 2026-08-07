// `result` node — chat-panel.md's Result (ProviderResult | DerivedResult).
// The backend already resolved WHICH nodes make up the result
// (resolveResult, ADR 0006 "Result resolution... computed backend-side")
// — a turn's `results` array (state.ts's TurnEntry.results) is rendered
// in order; each individual node here just needs its own leaf rendering,
// same as any other content node.

import type { NodeWire, ResultPayloadWire } from "../../adapter/wire";
import { Markdown } from "../leaf/Markdown";

export function ResultView({ node }: { node: NodeWire }) {
  const payload = node.payload as ResultPayloadWire | null;
  if (!payload) return null;
  if (!payload.text) return null;
  return (
    <div
      className={payload.is_error ? "event-error" : "surface-result"}
      data-testid="surface-result"
      data-result-kind={payload.result_kind}
    >
      <Markdown text={payload.text} />
    </div>
  );
}
