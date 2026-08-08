// Top-level `render(turn)` (chat-panel.md) — the collapsed/extended/live
// dispatch over one turn, using TurnEntry (state.ts) directly: prompt and
// results are always available (no fetch); the body is fetched on demand
// via `useChildren` exactly when the turn is live or has been manually
// extended (chat-panel.md's on-demand-expansion rule).

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { RunWire } from "../adapter/wire";
import type { SurfaceStore, TurnEntry } from "./state";
import { bodyItemsOf, isLivePhase } from "./state";
import { TypedPromptView } from "./nodes/TypedPrompt";
import { NodeView } from "./NodeView";
import { buildNodeRows } from "./nodes/rows";
import { Ellipsis } from "./leaf/Collapsible";
import { CollapsedPreview } from "./nodes/Container";
import { useChildren } from "./useChildren";
import { isAlwaysInPlaceKind, lastPreviewCandidate } from "./nodes/kinds";
import { computeRunMarkers } from "./markers";
import { RunMarker } from "./leaf/RunMarker";
import { RunningIndicator } from "./leaf/RunningIndicator";
import { useRunSummaryByTurn } from "../hooks/useRunSummary";
import { StoppedIndicator } from "./leaf/StoppedIndicator";
import { UsageSummary } from "./leaf/UsageSummary";
import { ModelChangeView, HarnessChangeView } from "./nodes/RuntimeChanged";
import { VirtualizedEventList, VIRTUALIZE_EVENT_THRESHOLD } from "../components/VirtualizedEventList";

export function TurnView({
  entry,
  store,
  runsById,
  userDisplayName,
  isLatestTurn = false,
}: {
  entry: TurnEntry;
  store: SurfaceStore;
  runsById: ReadonlyMap<string, RunWire>;
  /** Threaded through to this turn's own TypedPromptView only — see that
   * component's docstring on why plain-user labeling doesn't apply to a
   * nested typed_prompt reached via NodeView.tsx's generic dispatch. */
  userDisplayName?: string | null;
  /** Gates the Alter (edit-and-resend) affordance to this turn's own
   * TypedPromptView, same rule legacy's Chat.tsx used for
   * `onAlterTurnMessage` (`g.isLatest && !pending`) — only the session's
   * LAST turn may be altered. Defaults false for every call site besides
   * ChatSurfaceView's own top-level turn list (a nested typed_prompt
   * reached via NodeView.tsx's generic dispatch is structurally never the
   * session's latest turn). */
  isLatestTurn?: boolean;
}) {
  const { t } = useTranslation();
  const [manuallyExtended, setManuallyExtended] = useState(false);
  const live = isLivePhase(entry.phase);
  const wantBody = live || manuallyExtended;
  const children = useChildren(store, entry.turn.node_id, wantBody);
  const canAlter = isLatestTurn && entry.provisionalSend === null;
  // B1/B3 parity: the session's own top-level turn is the ONE
  // RunningIndicator that needs a distinguishable manager/worker/native
  // label — a nested SubAgentTurn's own live indicator (Container.tsx)
  // sits right next to that panel's own kind-labeled chip already, so it
  // doesn't look this up (see RunningIndicator.tsx's own docstring).
  const runKind = useRunSummaryByTurn(live ? entry.turn.surface_id : undefined, live ? entry.turnId : undefined)?.kind;

  return (
    <div
      className="surface-turn"
      data-testid="surface-turn"
      data-turn-id={entry.turnId}
      data-phase={entry.phase ?? "unknown"}
      data-live={live ? "true" : undefined}
    >
      {/* Team-vs-native orchestration-mode chip — legacy `MessageBubble`'s
       * `primaryEntityLabel`/manager-scope chip (deleted `frontend/tests/
       * primary-entity-label.test.tsx` parity: `.role-label-manager
       * .role-chip` textContent "Team" for team turns; the element is
       * absent entirely for native/unset turns, never rendered empty).
       * Sourced from `entry.turn.orchestration_mode` — the backend's
       * per-turn, session-record-frozen fact (nodes.py's `Node.
       * orchestration_mode`), not live session state. */}
      {entry.turn.orchestration_mode === "team" && (
        <div className="manager-scope" data-testid="surface-turn-team-scope">
          <div className="role-label role-label-manager">
            <span className="role-chip">{t("message.teamModeLabel")}</span>
          </div>
        </div>
      )}
      {entry.runtimeChange && (
        entry.runtimeChange.kind === "model_change" ? (
          <ModelChangeView node={entry.runtimeChange} />
        ) : (
          <HarnessChangeView node={entry.runtimeChange} />
        )
      )}
      {entry.prompt && (
        <TypedPromptView
          node={entry.prompt}
          userDisplayName={userDisplayName}
          provisionalSend={entry.provisionalSend}
          onRetrySend={
            entry.provisionalSend?.status === "error"
              ? () => store.retrySend(entry.provisionalSend!.intentId)
              : undefined
          }
          onAlter={canAlter ? (text) => store.sendPrompt(text, [], "alter") : undefined}
        />
      )}
      {/* `.surface-turn-body` — chat-panel grammar's "the rest of the turn,
       * below its prompt": running indicator, body, stopped notice,
       * result, usage. Legacy's `.turn-group-children` analog (same
       * connector-line/spacing role, `globals.css`'s shared
       * `.turn-group-children`/`.surface-turn-body` rule) — one wrapper so
       * the connector line has a single owning box, same as legacy drew it
       * from the prompt down to the final result rather than per-item. */}
      <div className="surface-turn-body">
        {live && <RunningIndicator kind={runKind} />}

        {live ? (
          children && <TurnBodyRows nodes={children} store={store} runsById={runsById} mode="live" />
        ) : manuallyExtended ? (
          children && <TurnBodyRows nodes={children} store={store} runsById={runsById} mode="extended" />
        ) : (
          <>
            {/* lifecycle_notice/diagnostic "render at their occurrence"
             * per backend's derive.py (_NON_RENDERABLE_KINDS) — never
             * hidden behind the ellipsis, even fully collapsed. Reads
             * whatever `children` already has passively cached (the
             * `useChildren` call above returns cached content regardless
             * of `wantBody` — see useChildren.ts); never fetches on its
             * own. `failure` is excluded on purpose (see isAlwaysInPlaceKind). */}
            {children &&
              bodyItemsOf(children)
                .filter((n) => isAlwaysInPlaceKind(n.kind))
                .map((n) => <NodeView key={n.node_id} node={n} />)}
            {entry.manifest.renderable_child_count > 0 && (
              <Ellipsis count={entry.manifest.renderable_child_count} onExpand={() => setManuallyExtended(true)} />
            )}
            {/* Boundary-inline collapsed-content preview — the SAME
             * `CollapsedPreview`/`lastPreviewCandidate` mechanism
             * Container.tsx's SubAgentTurnView uses for its own collapse,
             * applied here to the turn's own body (chat-panel grammar +
             * legacy's `SubAgentBlock` boundary-inline preview, previously
             * wired only for nested containers). Reads only what `children`
             * already has cached; triggers no fetch of its own. */}
            <CollapsedPreview
              node={children ? lastPreviewCandidate(bodyItemsOf(children)) : null}
              store={store}
              runsById={runsById}
              testId="surface-turn-preview"
            />
          </>
        )}
        {entry.phase === "stopped" && <StoppedIndicator reason={entry.reason} />}

        <ResultRow results={entry.results} runsById={runsById} />
        <UsageSummary usage={entry.usage} />
      </div>
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
