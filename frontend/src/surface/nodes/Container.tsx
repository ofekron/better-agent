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

import { useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type {
  ChildManifestWire,
  NodeKindWire,
  NodeWire,
  RunWire,
  SubAgentTurnPayloadWire,
  TargetRefWire,
} from "../../adapter/wire";
import type { SurfaceStore } from "../state";
import { bodyItemsOf } from "../state";
import { CollapsibleBlock, Ellipsis } from "../leaf/Collapsible";
import { lastPreviewCandidate } from "./kinds";
import { useChildren } from "../useChildren";
import { NodeView } from "../NodeView";
import { buildNodeRows, type ListProps } from "./rows";
import { RunningIndicator } from "../leaf/RunningIndicator";
import { UsageSummary } from "../leaf/UsageSummary";
import { navigateRoute, sessionPath } from "../../hooks/useRoute";
import { fmtNodeTimestamp } from "../../utils/timestamp";

export type RenderMode = "collapsed" | "extended" | "live";

export function NodeList(props: ListProps & { mode: "extended" | "live" }) {
  return <>{buildNodeRows(props)}</>;
}

/** Boundary-inline collapse preview — chat-panel grammar's "last cached
 * item, dimmed" shape (legacy's `SubAgentBlock`/`renderLastEventPreview`,
 * `.collapse-ellipsis` CSS). The ONE shared mechanism for "a collapsed
 * container shows a preview of its last already-cached body item,
 * fetching nothing new" — used both by nested-container collapse
 * (`SubAgentTurnView` below, via `CollapsibleBlock`'s `collapsedExtra`
 * prop) and top-level turn collapse (`TurnView.tsx`, which has no
 * `CollapsibleBlock` wrapper of its own — the turn's prompt/header is
 * always visible, only the body collapses). Renders nothing when there's
 * no cached last item to preview; never triggers a fetch itself — the
 * caller decides what "cached" means (`useChildren`'s unconditional
 * `cached` read). */
export function CollapsedPreview({
  node,
  store,
  runsById,
  testId,
}: {
  node: NodeWire | null;
  store: SurfaceStore;
  runsById: ReadonlyMap<string, RunWire>;
  testId?: string;
}) {
  if (!node) return null;
  return (
    <>
      <div className="collapse-ellipsis">• • •</div>
      <span data-testid={testId}>
        <NodeView node={node} containerMode="collapsed" store={store} runsById={runsById} />
      </span>
    </>
  );
}

interface ContainerProps {
  node: NodeWire;
  store: SurfaceStore;
  containerMode: RenderMode;
  runsById: ReadonlyMap<string, RunWire>;
}

/** `started_ts`–`ended_ts` (backend `nodes.py`'s `ChildManifest` time
 * range, epoch seconds) formatted for the group/container chip — a single
 * time when the group spans one instant (or the two ends coincide), an
 * en-dash range otherwise. Legacy `AutoActionGroup`'s own time chip only
 * ever showed the LEAD action's timestamp (`fmtTime(lead.event._ts)`) —
 * this container has richer backend-sourced data (both ends of the span),
 * rendered with the same chip look. Null when the manifest carries no
 * timestamp (an empty container). */
function explanationTimeRangeLabel(manifest: ChildManifestWire | null | undefined): string | null {
  if (!manifest || manifest.started_ts == null) return null;
  const start = fmtNodeTimestamp(manifest.started_ts);
  if (!start) return null;
  if (manifest.ended_ts == null || manifest.ended_ts === manifest.started_ts) return start;
  const end = fmtNodeTimestamp(manifest.ended_ts);
  return end ? `${start}–${end}` : start;
}

/** Group count + time-range chip — legacy `AutoActionGroup`'s
 * `.auto-action-group-meta`/`-count`/`-time` chip look, reused verbatim
 * (same classes, same visual chrome) now driven by backend-computed data
 * (`node.child_manifest`) instead of a client-side grouping pass. */
function ExplanationMetaChip({ node }: { node: NodeWire }) {
  const { t } = useTranslation();
  const manifest = node.child_manifest;
  const count = manifest?.renderable_child_count ?? 0;
  if (count === 0) return null;
  const timeLabel = explanationTimeRangeLabel(manifest);
  return (
    <div className="surface-explanation-meta" data-testid="surface-explanation-meta">
      <span className="auto-action-group-count">{t("message.eventsCount", { count })}</span>
      {timeLabel && <span className="auto-action-group-time">{timeLabel}</span>}
    </div>
  );
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
        <ExplanationMetaChip node={node} />
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
      <ExplanationMetaChip node={node} />
      {textNode && <NodeView node={textNode} />}
      {hiddenCount > 0 && <Ellipsis count={hiddenCount} onExpand={() => setManuallyExtended(true)} />}
      {last && <NodeView node={last} containerMode="collapsed" store={store} runsById={runsById} />}
    </div>
  );
}

/** `native_subagent_turn` | `worker_turn` | `sub_session_turn` |
 * `session_turn` — collapsible block: collapsed shows a summary header,
 * plus a boundary-inline preview of the last body item WHEN one happens
 * to already be cached — chat-panel grammar's collapsed-preview
 * requirement, same "•••  + last item" shape legacy's `SubAgentBlock`
 * used (`.collapse-ellipsis` + `renderLastEventPreview`, both reused
 * here: the `Ellipsis` leaf for the former, a plain `NodeView` render for
 * the latter). Unlike Explanation's collapsed preview (which always
 * fetches), this container's fetch stays genuinely lazy: `useChildren`
 * below is gated by `wantChildren` (only true while live or manually
 * opened), so a collapsed render NEVER triggers a fetch of its own just
 * to build the preview — it only reads whatever `store.getChildren`
 * already has cached (`useChildren`'s own `cached` read is unconditional
 * — see useChildren.ts). The common case: a subagent turn nested inside
 * the trailing/live turn's already eager-seeded bounded-nodes window
 * (state.ts's `seedLiveTurnNodes`), or one the user expanded once and
 * re-collapsed (the store cache is never cleared on collapse). No cache
 * hit -> no preview, same as before (visibility-driven index rule:
 * collapsed fetches nothing). */
// Per-kind chip label FALLBACK — used only when the node carries no real
// `SubAgentTurnPayload.label` (e.g. a `_created`-variant container the
// backend can't yet describe, or the pre-producer default). Legacy's
// `panelKindLabel`, utils/mergeEvents.ts — mirrored here as the last
// resort, not the first choice, now that `payload.label` (the real
// `worker_description` legacy showed) exists on the wire.
const SUBAGENT_KIND_LABEL_KEY: Partial<Record<NodeKindWire, string>> = {
  native_subagent_turn: "toolCall.subAgent",
  worker_turn: "message.workerTurnLabel",
  sub_session_turn: "message.subSessionTurnLabel",
  session_turn: "message.sessionTurnLabel",
};

/** `target_ref` — a backend-issued, authorization-checked address of an
 * embedded turn in another session (legacy's "view fork" affordance).
 * Reuses the SAME standalone `navigateRoute`/`sessionPath` primitives
 * `src/utils/linkifyFilePaths.tsx`'s `SessionLink` already uses (not part
 * of the excluded state.ts/Chat.tsx/App.tsx/adapter/client.ts set) — a
 * fully working navigation, not a stub. Turn-level deep-scroll is not
 * attempted: no mechanism anywhere in the app jumps to a specific turn on
 * load, and legacy's worker panels didn't do this either. */
function SubAgentTargetLink({ targetRef }: { targetRef: TargetRefWire }) {
  const { t } = useTranslation();
  const activate = (e: ReactMouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    e.stopPropagation();
    navigateRoute(sessionPath(targetRef.session_id));
  };
  return (
    <a
      href={sessionPath(targetRef.session_id)}
      className="session-smart-link"
      data-testid="surface-subagent-target-link"
      title={targetRef.session_id}
      onClick={activate}
    >
      {t("userRequest.openSession")}
    </a>
  );
}

export function SubAgentTurnView({ node, store, containerMode, runsById }: ContainerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(containerMode === "live");
  const wantChildren = containerMode === "live" || open;
  const children = useChildren(store, node.node_id, wantChildren);

  const count = node.child_manifest?.renderable_child_count ?? 0;
  // Real delegated-task description (backend's `SubAgentTurnPayload.
  // label`, legacy's `worker_description`) when the backend has one;
  // falls back to the generic per-kind label otherwise (e.g. a
  // `_created`-variant container, or a kind/producer that never attaches
  // this payload — `native_subagent_turn` never does).
  const payload = node.payload as SubAgentTurnPayloadWire | null;
  const badgeText = payload?.label || t(SUBAGENT_KIND_LABEL_KEY[node.kind] ?? "toolCall.subAgent");
  const live = containerMode === "live";
  // Legacy parity: a `*_created` panel (target session already existed,
  // nothing ran — `CollapsibleTimelineBlock`'s `created` prop /
  // `.timeline-block-created` CSS modifier, `isCreationPanelKind` in
  // utils/mergeEvents.ts) gets the SAME dashed/muted visual treatment
  // here — reusing the existing class verbatim (zero new CSS) rather than
  // duplicating its ruleset under a `sub-agent-block`-scoped selector.
  const createdClass = payload?.created ? " timeline-block-created" : "";
  // Non-interactive header content only — this renders inside a <button>
  // in the collapsed/extended path below (CollapsibleBlock's `header`
  // prop), which the HTML content model forbids nesting interactive
  // elements into. The interactive target_ref link is kept separate
  // (`targetLink`) and rendered via `headerExtra` (a button sibling) or,
  // in the plain-div live branch, directly alongside it.
  //
  // No manual RunMarker here despite `node.run_ref` existing on this
  // kind: `markers.ts`'s `computeRunMarkers` already runs generically
  // over EVERY list this node appears in (rows.tsx's `buildNodeRows`, at
  // whatever nesting level owns it), by its own design ("called
  // identically at every nesting level... a SubAgentTurn's own members").
  // A SubAgentTurn node with a `run_ref` already gets its marker attached
  // as a sibling by that mechanism — adding a second one here would
  // double-render it (verified: caused a duplicate-testid failure in
  // tests/surface/workerTurn.test.tsx during development, removed).
  const header = (
    <span className="surface-subagent-header">
      <span className="toolCall-subAgent-badge" data-subagent-kind={node.kind}>
        {badgeText}
      </span>
      {live && <RunningIndicator />}
      {count > 0 && (
        <span className="sub-agent-collapsed-count">{t("message.eventsCount", { count })}</span>
      )}
    </span>
  );
  const targetLink = node.target_ref ? <SubAgentTargetLink targetRef={node.target_ref} /> : null;
  // "usage ... overlay over any SubAgentTurn" (chat-panel grammar) —
  // reuses the same UsageSummary leaf TurnView.tsx already uses for the
  // top-level turn. Populated only once the backend's delegation reaches
  // a terminal state (worker-be's producer note); `node.usage` is
  // `undefined` for every pre-existing fixture/producer that predates
  // this field, normalized to `null` (UsageSummary's own no-op case).
  const usageOverlay = node.usage ? <UsageSummary usage={node.usage} /> : null;
  const extraRow = (targetLink || usageOverlay) && (
    <>
      {targetLink}
      {usageOverlay}
    </>
  );

  if (live) {
    return (
      <div
        className={`sub-agent-block${createdClass}`}
        data-testid="surface-subagent-turn"
        data-mode="live"
        data-created={payload?.created ? "true" : undefined}
      >
        {header}
        {extraRow}
        {children && <NodeList nodes={children} store={store} runsById={runsById} mode="live" />}
      </div>
    );
  }

  const body: ReactNode =
    children && children.length > 0 ? <NodeList nodes={children} store={store} runsById={runsById} mode="extended" /> : null;

  const previewLast = !open && children ? lastPreviewCandidate(bodyItemsOf(children)) : null;

  return (
    <CollapsibleBlock
      header={header}
      headerExtra={extraRow}
      className={`sub-agent-block${createdClass}`}
      testId="surface-subagent-turn"
      open={open}
      onToggle={setOpen}
      collapsedExtra={
        <CollapsedPreview node={previewLast} store={store} runsById={runsById} testId="surface-subagent-preview" />
      }
    >
      {body}
    </CollapsibleBlock>
  );
}
