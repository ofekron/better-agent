// `buildNodeRows` — chat-panel.md `renderCompactEach` (mode="extended") /
// the live-path recursion (mode="live", per `renderLiveTurn`'s "except
// the last... renderLiveTurn(last(directChildren))" rule) over one
// container's direct children, with run markers attached per
// `computeRunMarkers`. Kept component-free (the row component itself
// lives in ItemRow.tsx) so react-refresh's only-export-components rule
// stays satisfied — this module exports a plain function returning
// ReactNode[], not a component.

import { Fragment, type ReactNode } from "react";
import type { NodeWire, RunWire } from "../../adapter/wire";
import type { SurfaceStore } from "../state";
import { bodyItemsOf } from "../state";
import { computeRunMarkers } from "../markers";
import { RunMarker } from "../leaf/RunMarker";
import { ItemRow } from "./ItemRow";

export interface ListProps {
  nodes: readonly NodeWire[];
  store: SurfaceStore;
  runsById: ReadonlyMap<string, RunWire>;
}

/** Returns one ReactNode per body item (never groups rows together) so a
 * caller that needs the raw row array for virtualization (TurnView.tsx,
 * at the turn-body level only) can build it once and either spread it
 * directly or hand it to VirtualizedEventList above a row-count
 * threshold. */
export function buildNodeRows({ nodes, store, runsById, mode }: ListProps & { mode: "extended" | "live" }): ReactNode[] {
  const items = bodyItemsOf(nodes);
  const markers = computeRunMarkers(items, runsById);
  return items.map((node, idx) => {
    const isLast = idx === items.length - 1;
    return (
      <Fragment key={node.node_id}>
        <ItemRow node={node} store={store} runsById={runsById} live={mode === "live"} isLast={isLast} />
        {markers.has(node.node_id) && <RunMarker run={markers.get(node.node_id)!} />}
      </Fragment>
    );
  });
}
