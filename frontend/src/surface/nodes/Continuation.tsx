// `compaction` / `continuation_session` — the Continuation family
// (chat-panel.md).

import { useTranslation } from "react-i18next";
import type { CompactionPayloadWire, ContinuationSessionPayloadWire, NodeWire } from "../../adapter/wire";
import { ContinuationPill } from "../leaf/Pills";

export function CompactionView({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const payload = node.payload as CompactionPayloadWire | null;
  if (!payload) return null;
  const originLabel = t(`message.compactionOrigin_${payload.origin}`, { defaultValue: payload.origin });
  return (
    <div className="event-session" data-testid="surface-compaction">
      <strong>{t("message.compactionNotice")}</strong>
      {originLabel ? <span> · {originLabel}</span> : null}
      {payload.summary ? <div style={{ marginTop: 2, opacity: 0.85 }}>{payload.summary}</div> : null}
    </div>
  );
}

/** Renders as a live notice until the new execution produces output
 * (chat-panel.md's ContinuationSession) — the store has no dedicated
 * "still the placeholder, no output yet" signal, so this always renders
 * the pill; once real content nodes land after it in the same turn body,
 * this notice simply stops being the last visible thing. */
export function ContinuationSessionView({ node }: { node: NodeWire }) {
  const payload = node.payload as ContinuationSessionPayloadWire | null;
  if (!payload) return null;
  return <ContinuationPill chainDepth={payload.chain_depth} />;
}
