// The two structural container kinds with their own collapse/expand/live
// state: `explanation` (chat-panel.md's Explanation) and the SubAgentTurn
// family (`native_subagent_turn` | `worker_turn` | `sub_session_turn` |
// `session_turn` — "one render contract... regardless of which kind
// sourced it", ADR 0006 §1).
//
// `NodeList` is the shared "render an ordered list of body items" helper
// both this module and TurnView.tsx use — chat-panel.md's
// `renderCompactEach`/live-path recursion is ONE operation regardless of
// which container owns the list, so it lives once here instead of being
// re-implemented per container kind.

import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { NodeWire, RunWire } from "../../adapter/wire";
import type { SurfaceStore } from "../state";
import { bodyItemsOf } from "../state";
import { CollapsibleBlock, Ellipsis } from "../leaf/Collapsible";
import { useChildren } from "../useChildren";
import { NodeView } from "../NodeView";
import { buildNodeRows, type ListProps } from "./rows";

export type RenderMode = "collapsed" | "extended" | "live";

export function NodeList(props: ListProps & { mode: "extended" | "live" }) {
  return <>{buildNodeRows(props)}</>;
}

interface ContainerProps {
  node: NodeWire;
  store: SurfaceStore;
  containerMode: RenderMode;
  runsById: ReadonlyMap<string, RunWire>;
}

export function ExplanationView({ node, store, containerMode, runsById }: ContainerProps) {
  const [manuallyExtended, setManuallyExtended] = useState(false);
  const effectiveMode: RenderMode = containerMode === "live" ? "live" : manuallyExtended ? "extended" : "collapsed";

  // Explanation's collapsed preview (Text -> ... -> last item) is part of
  // its own row, not gated behind an extra click (see useChildren.ts) —
  // its members are always fetched once rendered, unlike SubAgentTurn's
  // deliberately lazy fetch below.
  const children = useChildren(store, node.node_id, true);
  if (!children) return null; // fetch in flight; nothing to show yet
  const items = bodyItemsOf(children);
  if (items.length === 0) return null;

  const textNode = items[0]?.kind === "assistant_text" ? items[0] : null;
  const rest = textNode ? items.slice(1) : items;

  if (effectiveMode !== "collapsed") {
    return (
      <div className="surface-explanation" data-testid="surface-explanation" data-mode={effectiveMode}>
        {textNode && <NodeView node={textNode} />}
        <NodeList nodes={rest} store={store} runsById={runsById} mode={effectiveMode === "live" ? "live" : "extended"} />
      </div>
    );
  }

  // renderCollapsedExplanation: Text -> ellipsis(hidden=count-1) if
  // count>1 -> compact(last item).
  const hiddenCount = rest.length > 1 ? rest.length - 1 : 0;
  const last = rest.length > 0 ? rest[rest.length - 1] : null;
  return (
    <div className="surface-explanation" data-testid="surface-explanation" data-mode="collapsed">
      {textNode && <NodeView node={textNode} />}
      {hiddenCount > 0 && <Ellipsis count={hiddenCount} onExpand={() => setManuallyExtended(true)} />}
      {last && <NodeView node={last} containerMode="collapsed" store={store} runsById={runsById} />}
    </div>
  );
}

/** `native_subagent_turn` | `worker_turn` | `sub_session_turn` |
 * `session_turn` — collapsible block per the task's explicit stage-1
 * guidance: collapsed shows a summary header only (no boundary-inline
 * preview, unlike Explanation — this fetch is genuinely lazy, gated
 * behind the user's own expand click or a detected live descendant). */
export function SubAgentTurnView({ node, store, containerMode, runsById }: ContainerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(containerMode === "live");
  const wantChildren = containerMode === "live" || open;
  const children = useChildren(store, node.node_id, wantChildren);

  const count = node.child_manifest?.renderable_child_count ?? 0;
  const header = (
    <span className="surface-subagent-header">
      <span className="toolCall-subAgent-badge">{t("toolCall.subAgent")}</span>
      {count > 0 && <span className="sub-agent-collapsed-count">{count}</span>}
    </span>
  );

  if (containerMode === "live") {
    return (
      <div className="sub-agent-block" data-testid="surface-subagent-turn" data-mode="live">
        {header}
        {children && <NodeList nodes={children} store={store} runsById={runsById} mode="live" />}
      </div>
    );
  }

  const body: ReactNode =
    children && children.length > 0 ? <NodeList nodes={children} store={store} runsById={runsById} mode="extended" /> : null;

  return (
    <CollapsibleBlock
      header={header}
      className="sub-agent-block"
      testId="surface-subagent-turn"
      open={open}
      onToggle={setOpen}
    >
      {body}
    </CollapsibleBlock>
  );
}
