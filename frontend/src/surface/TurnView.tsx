// Top-level `render(turn)` (chat-panel.md) — the collapsed/extended/live
// dispatch over one turn, using TurnEntry (state.ts) directly: prompt and
// results are always available (no fetch); the body is fetched on demand
// via `useChildren` exactly when the turn is live or has been manually
// extended (chat-panel.md's on-demand-expansion rule).

import { useState } from "react";
import type { RunWire } from "../adapter/wire";
import type { SurfaceStore, TurnEntry } from "./state";
import { isLivePhase } from "./state";
import { TypedPromptView } from "./nodes/TypedPrompt";
import { NodeView } from "./NodeView";
import { buildNodeRows } from "./nodes/rows";
import { Ellipsis } from "./leaf/Collapsible";
import { useChildren } from "./useChildren";
import { computeRunMarkers } from "./markers";
import { RunMarker } from "./leaf/RunMarker";
import { ModelChangeView, HarnessChangeView } from "./nodes/RuntimeChanged";
import { VirtualizedEventList, VIRTUALIZE_EVENT_THRESHOLD } from "../components/VirtualizedEventList";

export function TurnView({
  entry,
  store,
  runsById,
}: {
  entry: TurnEntry;
  store: SurfaceStore;
  runsById: ReadonlyMap<string, RunWire>;
}) {
  const [manuallyExtended, setManuallyExtended] = useState(false);
  const live = isLivePhase(entry.phase);
  const wantBody = live || manuallyExtended;
  const children = useChildren(store, entry.turn.node_id, wantBody);

  return (
    <div className="surface-turn" data-testid="surface-turn" data-turn-id={entry.turnId} data-phase={entry.phase ?? "unknown"}>
      {entry.runtimeChange && (
        entry.runtimeChange.kind === "model_change" ? (
          <ModelChangeView node={entry.runtimeChange} />
        ) : (
          <HarnessChangeView node={entry.runtimeChange} />
        )
      )}
      {entry.prompt && <TypedPromptView node={entry.prompt} />}

      {live ? (
        children && <TurnBodyRows nodes={children} store={store} runsById={runsById} mode="live" />
      ) : manuallyExtended ? (
        children && <TurnBodyRows nodes={children} store={store} runsById={runsById} mode="extended" />
      ) : (
        entry.manifest.renderable_child_count > 0 && (
          <Ellipsis count={entry.manifest.renderable_child_count} onExpand={() => setManuallyExtended(true)} />
        )
      )}

      <ResultRow results={entry.results} runsById={runsById} />
    </div>
  );
}

/** Turn-body-level virtualization: builds the row array once and, above
 * the shared threshold (VirtualizedEventList's own module constant), hands
 * it to the windowed renderer instead of spreading every row into the DOM
 * — the "monster turn" case (chat-panel.md "Large and complex sessions
 * render completely" + Phase H's existing virtualization precedent).
 * Scoped to the turn-body level only, per this stage's explicit guidance
 * — nested Explanation/SubAgentTurn bodies (typically far smaller) render
 * un-virtualized via nodes/Container.tsx's plain `NodeList`. */
function TurnBodyRows({
  nodes,
  store,
  runsById,
  mode,
}: {
  nodes: readonly import("../adapter/wire").NodeWire[];
  store: SurfaceStore;
  runsById: ReadonlyMap<string, RunWire>;
  mode: "extended" | "live";
}) {
  const rows = buildNodeRows({ nodes, store, runsById, mode });
  if (rows.length > VIRTUALIZE_EVENT_THRESHOLD) {
    return <VirtualizedEventList items={rows} />;
  }
  return <>{rows}</>;
}

function ResultRow({ results, runsById }: { results: readonly import("../adapter/wire").NodeWire[]; runsById: ReadonlyMap<string, RunWire> }) {
  if (results.length === 0) return null;
  const markers = computeRunMarkers(results, runsById);
  return (
    <>
      {results.map((n) => (
        <span key={n.node_id}>
          <NodeView node={n} />
          {markers.has(n.node_id) && <RunMarker run={markers.get(n.node_id)!} />}
        </span>
      ))}
    </>
  );
}
