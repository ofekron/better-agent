// `compaction` / `continuation_session` — the Continuation family
// (chat-panel.md).

import { useTranslation } from "react-i18next";
import type { CompactionPayloadWire, ContinuationSessionPayloadWire, NodeWire } from "../../adapter/wire";
import { ContinuationPill } from "../leaf/Pills";
import { CollapsibleBlock } from "../leaf/Collapsible";
import { useChildren } from "../useChildren";
import type { SurfaceStore } from "../state";
import { bodyItemsOf } from "../state";
import { NodeView } from "../NodeView";

/** Legacy's "expand to view the pre-compaction transcript" affordance
 * (MessageBubble.tsx's `CollapsibleOutput label="Compacted prompt"`),
 * ported via the SAME generic on-demand children fetch every other
 * container uses (`useChildren`/`ensureChildren`/`/nodes/:id/children`) —
 * NOT `CompactionPayloadWire.replaced_node_ids` (that field exists on the
 * wire type but is never populated by any backend adapter today, grep-
 * confirmed across normalize.py/derive.py/chat_index.py; a hand-parsed
 * NodeId list would have nothing to fetch by design). A compaction node
 * behaves as a container over its pre-compaction content exactly like
 * `explanation`/the SubAgentTurn family (`isContainerKind` in
 * nodes/kinds.ts) — when the backend eventually parents the replaced
 * nodes under this node's id, `node.child_manifest.renderable_child_count`
 * goes non-zero and this expand control appears with no further frontend
 * change needed; today it is always 0, so nothing renders (real,
 * data-driven gating — never a dead/always-hidden control). */
function CompactedContent({ node, store }: { node: NodeWire; store: SurfaceStore }) {
  const { t } = useTranslation();
  const count = node.child_manifest?.renderable_child_count ?? 0;
  const children = useChildren(store, node.node_id, count > 0);
  if (count === 0) return null;
  const items = children ? bodyItemsOf(children) : [];
  return (
    <CollapsibleBlock
      header={<span>{t("message.compactionExpandLabel")}</span>}
      className="surface-compaction-replay"
      testId="surface-compaction-replay"
    >
      {items.map((n) => (
        <NodeView key={n.node_id} node={n} />
      ))}
    </CollapsibleBlock>
  );
}

export function CompactionView({ node, store }: { node: NodeWire; store?: SurfaceStore }) {
  const { t } = useTranslation();
  const payload = node.payload as CompactionPayloadWire | null;
  if (!payload) return null;
  const originLabel = t(`message.compactionOrigin_${payload.origin}`, { defaultValue: payload.origin });
  return (
    <div className="event-session" data-testid="surface-compaction">
      <strong>{t("message.compactionNotice")}</strong>
      {originLabel ? <span> · {originLabel}</span> : null}
      {payload.summary ? <div style={{ marginTop: 2, opacity: 0.85 }}>{payload.summary}</div> : null}
      {store && <CompactedContent node={node} store={store} />}
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
