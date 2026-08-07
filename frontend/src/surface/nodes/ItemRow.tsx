// One BodyItem inside a list currently rendering in "extended" or "live"
// mode — split into its own module (react-refresh's only-export-components
// rule wants component-only modules) from nodes/rows.tsx, which exports
// the non-component `buildNodeRows`/`isContainerKind`.

import type { NodeWire, RunWire } from "../../adapter/wire";
import type { SurfaceStore } from "../state";
import { hasLikelyLiveDescendant } from "../liveness";
import { NodeView } from "../NodeView";
import { isContainerKind } from "./kinds";

/** Leaf kinds render as-is; container kinds (`explanation`, the
 * SubAgentTurn family) get a mode of their own:
 *   - the list's trailing item, when the list itself is live -> "live"
 *     (forced fully expanded, chat-panel.md's trailing-path rule)
 *   - a non-trailing item in a live list, WITH a detected-live trailing
 *     descendant of its own -> "live" too (multi-live-path — see
 *     liveness.ts for the same-stream-only detection caveat)
 *   - anything else -> "collapsed" (renderCompact's default; the item
 *     still has its own independent expand control) */
export function ItemRow({
  node,
  store,
  runsById,
  live,
  isLast,
}: {
  node: NodeWire;
  store: SurfaceStore;
  runsById: ReadonlyMap<string, RunWire>;
  live: boolean;
  isLast: boolean;
}) {
  if (!isContainerKind(node.kind)) return <NodeView node={node} />;
  const forceLive = live && (isLast || hasLikelyLiveDescendant(node, store));
  return <NodeView node={node} containerMode={forceLive ? "live" : "collapsed"} store={store} runsById={runsById} />;
}
