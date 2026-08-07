// Native `ba.surface_native` ForkSplit — chat-panel.md's `ChatView = Chat |
// ForkSplit` grammar, the ForkSplit arm. Purpose-built rather than
// extracted from components/ForkSplitView.tsx: that component's pane body
// (`MessageList`/`TurnGroup`) is deeply ChatMessage/RunInfo-shaped (not a
// shape-agnostic core to extract), so this reuses only its CHROME contract
// — the SAME CSS classes and i18n keys (`fork.*`) — with each pane's body
// swapped for a native `ChatSurfaceView` (panes = surfaces; the native
// store already keys one instance per session id via useSurfaceStore).
//
// Known gap: legacy's ForkSplitView renders the pre-fork history ONCE in
// a shared region above the split, using `Session.fork_point_seq` (a
// LEGACY ChatMessage.seq cutoff) to slice `messages` client-side. The
// Chat Surface Contract has no equivalent turn-level cutoff — NodeWire's
// own (ts, seq) is a different namespace, fork-minted sessions copy
// messages with brand-new ids (backend/session_store.py `fork_session`),
// and nothing in the wire (SnapshotIdentity, CompactSessionSnapshotWire)
// carries a "this turn predates the fork" marker. Computing a shared
// region here would mean either fabricating a ts-based heuristic with no
// contract backing, or duplicating legacy's message-seq bookkeeping into
// the native store — both rejected. Until the contract grows a
// fork-point marker, EVERY pane (including the root) renders its own full
// `ChatSurfaceView`, so pre-fork turns appear once per pane rather than
// once above the split. This is a backend/contract gap, not a frontend
// integration gap — tracked here rather than worked around.
//
// Also deferred (pure UI affordances, not data gaps, cut for this stage's
// priority order): the mobile tab-strip swipe gesture and the focused-
// single-pane toggle legacy's version has. Every pane always renders in
// the grid.

import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import Icon from "../components/Icon";
import type { Session } from "../types";
import { useViewport } from "../hooks/useViewport";
import { ChatSurfaceView } from "./ChatSurfaceView";

interface Props {
  tree: Session;
  focusedSessionId: string;
  onSetFocus: (sessionId: string) => void;
  onCloseFork: (sessionId: string) => void;
  onReopenFork: (sessionId: string) => void;
  onDeleteFork?: (sessionId: string) => void;
}

/** Root + every embedded user-facing fork, depth-first — the same
 * flattening components/ForkSplitView.tsx's `flatPanes` does. */
function flattenPanes(tree: Session): Session[] {
  const out: Session[] = [];
  const visit = (node: Session) => {
    if ((node.kind ?? "user") !== "user") return;
    out.push(node);
    for (const f of node.forks ?? []) visit(f);
  };
  visit(tree);
  return out;
}

export function ForkSplitView({
  tree,
  focusedSessionId,
  onSetFocus,
  onCloseFork,
  onReopenFork,
  onDeleteFork,
}: Props) {
  const { t } = useTranslation();
  const viewport = useViewport();
  const isMobile = viewport.mode !== "desktop";

  const panes = useMemo(() => flattenPanes(tree), [tree]);
  const focusedIdx = useMemo(() => {
    const i = panes.findIndex((p) => p.id === focusedSessionId);
    return i < 0 ? 0 : i;
  }, [panes, focusedSessionId]);

  const paneLabel = useCallback(
    (pane: Session, index: number) => (pane.id === tree.id ? t("fork.original") : pane.name || `${t("fork.fork")} ${index}`),
    [t, tree.id],
  );

  const renderedPanes = isMobile ? [panes[focusedIdx]].filter(Boolean) : panes;

  return (
    <div className="fork-split" data-testid="surface-fork-split">
      {panes.length > 1 && (
        <div className="fork-tabs-strip" data-testid="fork-tabs-strip" role="tablist">
          {panes.map((pane, i) => {
            const active = i === focusedIdx;
            const closed = !!pane.fork_closed;
            return (
              <button
                key={pane.id}
                type="button"
                role="tab"
                aria-selected={active}
                className={"fork-tab" + (active ? " active" : "") + (closed ? " closed" : "")}
                onClick={() => onSetFocus(pane.id)}
              >
                {paneLabel(pane, i)}
              </button>
            );
          })}
        </div>
      )}
      <div
        className="fork-split-grid"
        style={
          isMobile
            ? { gridTemplateColumns: "1fr" }
            : { gridTemplateColumns: `repeat(${panes.length}, minmax(220px, 1fr))` }
        }
        data-testid="fork-grid"
        role="radiogroup"
        aria-label="Fork panes — exactly one is focused"
      >
        {renderedPanes.map((pane) => {
          const isFocused = pane.id === focusedSessionId;
          const isClosed = !!pane.fork_closed;
          const isRoot = pane.id === tree.id;
          return (
            <div
              key={pane.id}
              className={"fork-pane" + (isFocused ? " fork-pane-focused" : "") + (isClosed ? " fork-pane-closed" : "")}
              data-testid="fork-pane"
              data-session-id={pane.id}
            >
              <div className="fork-pane-header">
                <span className="fork-pane-label" title={pane.name}>
                  {isRoot ? t("fork.original") : pane.name || t("fork.fork")}
                </span>
                {!isClosed && (
                  <div className="fork-pane-actions">
                    <button
                      type="button"
                      className="fork-pane-focus-radio"
                      onClick={() => onSetFocus(pane.id)}
                      aria-label={isFocused ? t("fork.focusedAria") : t("fork.focusTitle")}
                      title={isFocused ? t("fork.focusedTitle") : t("fork.focusTitle")}
                      role="radio"
                      aria-checked={isFocused}
                    >
                      {isFocused ? "●" : "◯"}
                    </button>
                  </div>
                )}
                {isClosed && !isRoot && (
                  <>
                    <button type="button" className="fork-pane-reopen" onClick={() => onReopenFork(pane.id)} title={t("fork.reopenTitle")}>
                      {t("fork.reopen")}
                    </button>
                    {onDeleteFork && (
                      <button type="button" className="fork-pane-delete" onClick={() => onDeleteFork(pane.id)} title={t("fork.deleteTitle")}>
                        {t("fork.delete")}
                      </button>
                    )}
                  </>
                )}
                {isClosed && isRoot && <span className="fork-pane-closed-tag">{t("fork.closed")}</span>}
              </div>
              <div className="fork-pane-messages">
                <ChatSurfaceView sessionId={pane.id} />
              </div>
              {!isRoot && !isClosed && (
                <button
                  type="button"
                  className="fork-pane-close"
                  onClick={() => onCloseFork(pane.id)}
                  aria-label="Close this fork pane"
                  title={t("fork.closeTitle")}
                >
                  <Icon name="x" size={16} />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
