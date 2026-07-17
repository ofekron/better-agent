import { useMemo, useRef, useCallback, useEffect, useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import type {
  AdvSyncOverlay,
  ChatMessage,
  FileFocus,
  RunInfo,
  Session,
} from "../types";
import { TurnGroup } from "./MessageBubble";
import { buildThreadColorMap } from "../threadColors";
import { useOpProgress } from "../progress/store";
import { useViewport } from "../hooks/useViewport";
import { mergeMessagesSorted } from "../utils/mergeMessages";
import { useScrollLoadOlder } from "../hooks/useScrollLoadOlder";
import { isUnanchoredRun } from "../utils/runTargets";
import { providerNameForId } from "../utils/providerCache";

function sessionModelMetaTitle(t: (key: string) => string, pane: Session): string {
  const providerName = providerNameForId(pane.provider_id);
  return [
    providerName ? `${t("message.provider")}: ${providerName}` : "",
    pane.model ? `${t("message.model")}: ${pane.model}` : "",
    pane.reasoning_effort ? `${t("message.effort")}: ${pane.reasoning_effort}` : "",
  ].filter(Boolean).join(" / ");
}

interface Props {
  /** Root tree to render. Its `forks` array drives the split columns. */
  tree: Session;
  /** Per-session optimistic pending bubbles, keyed by session id. */
  pendingBySession: Record<string, ChatMessage[]>;
  /** Per-session backend-owned run state, keyed by session id. Each
   * pane reads its own slice so multiple panes can show "running"
   * concurrently. */
  runStateBySession: Record<string, RunInfo[]>;
  /** Currently focused pane id (one of: tree.id, tree.forks[i].id). */
  focusedSessionId: string;
  onSetFocus: (sessionId: string) => void;
  onCloseFork: (sessionId: string) => void;
  onReopenFork: (sessionId: string) => void;
  /** Permanently delete a (closed) fork. Only enabled on closed panes
   * — open forks must be closed first. The deletion is irreversible:
   * the fork's session record + its descendants are dropped from the
   * root tree, and the messages are gone. */
  onDeleteFork?: (sessionId: string) => void;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (assistantMessage: ChatMessage) => void;
  userDisplayName?: string | null;
  /** Load older messages for a pane. Keyed by session id. */
  onLoadOlderMessages?: (sessionId: string) => Promise<void>;
  /** Click handler for adversarial-sync agreed-text spans. The
   * overlay carries the two fork ids; App navigates the focused
   * pane to the supportive fork. */
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
}

/** Side-by-side split view of a root session and its (possibly
 * nested) fork descendants.
 *
 * - The tree is FLATTENED for display: root + every fork (depth-first)
 *   becomes a column. Nested forks (a fork that has its own children)
 *   appear as siblings of their ancestors in the column row, since
 *   each fork carries the full history at fork time in its own
 *   `messages` array.
 * - The "shared" region above the split is the root's messages up to
 *   the EARLIEST fork point across the tree. After that point every
 *   pane has its own divergence — render per-pane below.
 * - Top-right of each pane: focus radio. The focused pane is the
 *   target of every Send/Fork action.
 * - Bottom-right of each pane: close (or reopen, if already closed)
 *   button. Closing a fork freezes it (no focus, no new prompts) but
 *   keeps the pane visible.
 *
 * Each pane uses `TurnGroup` (the same rich renderer Chat uses)
 * so tool calls, thinking blocks, run badges, etc. stay intact in
 * every pane regardless of nesting depth.
 */
export function ForkSplitView({
  tree,
  pendingBySession,
  runStateBySession,
  focusedSessionId,
  onSetFocus,
  onCloseFork,
  onReopenFork,
  onDeleteFork,
  onFileClick,
  onViewDiff,
  onRetry,
  onRetryStopped,
  userDisplayName,
  onLoadOlderMessages,
  onAdvSyncClick,
}: Props) {
  const { t } = useTranslation();
  const [focusedViewEnabled, setFocusedViewEnabled] = useState(true);

  // Flatten the tree depth-first so root → its forks → their forks ...
  // become an ordered list of panes. We render each as a column.
  // Internal-kind Better Agent sessions (delegate_fork, supervisor_worker,
  // adv_sync_fork) are skipped — adv-sync forks are accessed via the
  // dedicated AdvSyncWindow, not as inline panes in the main view.
  const flatPanes = useMemo(() => {
    const out: Session[] = [];
    const visit = (node: Session) => {
      if ((node.kind ?? "user") !== "user") return;
      out.push(node);
      for (const f of node.forks ?? []) visit(f);
    };
    visit(tree);
    return out;
  }, [tree]);

  // The earliest fork point across the whole tree. Messages strictly
  // before this seq are shared by every pane (no divergence yet);
  // messages from this seq onward render per-pane. Null when the tree
  // somehow has no forks (this component shouldn't render then).
  const forkPointSeq = useMemo(() => {
    let earliest: number | null = null;
    for (const p of flatPanes) {
      const fp = p.fork_point_seq;
      if (typeof fp === "number") {
        if (earliest === null || fp < earliest) earliest = fp;
      }
    }
    return earliest;
  }, [flatPanes]);

  // Shared = root messages with seq <= earliest fork_point_seq.
  const sharedMessages = useMemo(() => {
    const all = tree.messages ?? [];
    if (forkPointSeq === null) return all;
    return all.filter(
      (m) => typeof m.seq !== "number" || m.seq <= forkPointSeq
    );
  }, [tree.messages, forkPointSeq]);

  // Scroll-triggered load-older for the shared region (root messages
  // above the fork point). Uses tree.pagination from the root node.
  const sharedHasOlder = !!(tree.pagination?.has_older);
  const sharedLoadOlderFn = useCallback(async () => {
    if (!onLoadOlderMessages) return;
    await onLoadOlderMessages(tree.id);
  }, [onLoadOlderMessages, tree.id]);

  const sharedOlderOpId = `messages:loadOlder:shared:${tree.id}`;
  const { inflight: sharedLoadingOlder } = useOpProgress(sharedOlderOpId);
  const {
    scrollRef: sharedScrollRef,
    handleScroll: sharedScrollHandler,
    handleWheel: handleSharedLoadOlderWheel,
    handleTouchStart: handleSharedLoadOlderTouchStart,
    handleTouchMove: handleSharedLoadOlderTouchMove,
    handleTouchEnd: handleSharedLoadOlderTouchEnd,
    handleTouchCancel: handleSharedLoadOlderTouchCancel,
    handlePointerDown: handleSharedLoadOlderPointerDown,
    handlePointerUp: handleSharedLoadOlderPointerUp,
    handleKeyDown: handleSharedLoadOlderKeyDown,
    handleKeyUp: handleSharedLoadOlderKeyUp,
    handleScrollEnd: handleSharedLoadOlderScrollEnd,
    triggerLoadOlder: triggerSharedLoadOlder,
  } = useScrollLoadOlder(
    sharedOlderOpId,
    sharedHasOlder,
    onLoadOlderMessages ? sharedLoadOlderFn : undefined,
  );

  // Build a single thread-color map across the whole tree so worker
  // panel colors stay consistent between the shared region and panes.
  // The root carries the worker list (forks share the visible context, so
  // the same workers apply to every pane).
  const threadColorMap = useMemo(() => {
    const ids = (tree.workers ?? [])
      .map((w) => w.agent_session_id)
      .filter(Boolean) as string[];
    return buildThreadColorMap(ids);
  }, [tree.workers]);

  const panes = useMemo(() => {
    return flatPanes.map((pane) => {
      const msgs = (pane.messages ?? []).filter(
        (m) =>
          forkPointSeq === null ||
          (typeof m.seq === "number" && m.seq > forkPointSeq)
      );
      const pending = pendingBySession[pane.id] ?? [];
      const runs = runStateBySession[pane.id] ?? [];
      return { pane, msgs, pending, runs };
    });
  }, [flatPanes, forkPointSeq, pendingBySession, runStateBySession]);

  const viewport = useViewport();
  const isMobile = viewport.mode !== "desktop";

  const paneLabel = useCallback(
    (pane: Session, index: number) =>
      pane.id === tree.id ? t("fork.original") : pane.name || `${t("fork.fork")} ${index}`,
    [t, tree.id],
  );

  // Index of the focused pane; used by both the tab strip (active
  // marker) and the mobile swipe handler (prev/next neighbour).
  const focusedIdx = useMemo(() => {
    const i = panes.findIndex(({ pane }) => pane.id === focusedSessionId);
    return i < 0 ? 0 : i;
  }, [panes, focusedSessionId]);

  const focusedViewPane = useMemo(() => {
    if (!focusedViewEnabled) return null;
    return panes.find(({ pane }) => pane.id === focusedSessionId) ?? null;
  }, [focusedSessionId, focusedViewEnabled, panes]);

  useEffect(() => {
    if (focusedViewEnabled && !focusedViewPane) {
      setFocusedViewEnabled(false);
    }
  }, [focusedViewEnabled, focusedViewPane]);

  // Axis-locked horizontal swipe on the tab strip — switches the
  // focused pane to the previous / next neighbour. The swipe handler
  // intentionally lives on the strip (not pane bodies) so it can't
  // hijack Monaco / code-block horizontal scrolling inside a pane.
  const swipeStartRef = useRef<{ x: number; y: number } | null>(null);
  const swipeLockedRef = useRef<"none" | "horizontal" | "vertical">("none");
  const onStripTouchStart = useCallback((e: React.TouchEvent) => {
    if (e.touches.length !== 1) return;
    swipeStartRef.current = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    swipeLockedRef.current = "none";
  }, []);
  const onStripTouchMove = useCallback((e: React.TouchEvent) => {
    const start = swipeStartRef.current;
    if (!start) return;
    const dx = e.touches[0].clientX - start.x;
    const dy = e.touches[0].clientY - start.y;
    if (swipeLockedRef.current === "none") {
      const ax = Math.abs(dx);
      const ay = Math.abs(dy);
      // Lock axis once movement exceeds a small deadzone; ties go to
      // vertical (scroll wins) to keep page scroll responsive.
      if (ax > 10 || ay > 10) {
        swipeLockedRef.current = ax > ay ? "horizontal" : "vertical";
      }
    }
  }, []);
  const onStripTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      const start = swipeStartRef.current;
      swipeStartRef.current = null;
      if (!start || swipeLockedRef.current !== "horizontal") {
        swipeLockedRef.current = "none";
        return;
      }
      swipeLockedRef.current = "none";
      const dx = e.changedTouches[0].clientX - start.x;
      if (Math.abs(dx) < 50) return;
      // RTL: a left-swipe (dx < 0) means "go to next visual pane",
      // which is index + 1 in LTR but index - 1 in RTL. The pane
      // order in `panes` is LTR-natural, so flip in RTL.
      const rtl =
        typeof document !== "undefined" &&
        document.documentElement.getAttribute("dir") === "rtl";
      const direction = dx < 0 ? +1 : -1;
      const step = rtl ? -direction : direction;
      const next = focusedIdx + step;
      if (next >= 0 && next < panes.length) {
        onSetFocus(panes[next].pane.id);
      }
    },
    [focusedIdx, panes, onSetFocus]
  );

  // On mobile, only render the focused pane. Falling back to index 0
  // when focused id is stale (e.g. focused fork was deleted) — this
  // mirrors App.tsx's existing `setFocusedForkId(null)` fallback to
  // root.
  const focusedViewActive = !isMobile && !!focusedViewPane;
  const focusedViewPaneId = focusedViewPane?.pane.id ?? null;
  const renderedPanes = isMobile
    ? [panes[focusedIdx]].filter(Boolean)
    : panes;

  return (
    <div className="fork-split">
      <div className="fork-split-shared" data-testid="fork-shared" ref={sharedScrollRef} onScroll={sharedScrollHandler} onScrollEnd={handleSharedLoadOlderScrollEnd} onWheel={handleSharedLoadOlderWheel} onTouchStart={handleSharedLoadOlderTouchStart} onTouchMove={handleSharedLoadOlderTouchMove} onTouchEnd={handleSharedLoadOlderTouchEnd} onTouchCancel={handleSharedLoadOlderTouchCancel} onPointerDown={handleSharedLoadOlderPointerDown} onPointerUp={handleSharedLoadOlderPointerUp} onKeyDown={handleSharedLoadOlderKeyDown} onKeyUp={handleSharedLoadOlderKeyUp} tabIndex={0}>
        {sharedHasOlder && (
          <div className="load-older-sentinel">
            {sharedLoadingOlder ? (
              <div className="load-older-spinner">{t("fork.loading")}</div>
            ) : (
              <button className="load-older-link" onClick={triggerSharedLoadOlder}>
                {t("chat.loadOlderMessages")}
              </button>
            )}
          </div>
        )}
        <MessageList
          messages={sharedMessages}
          pending={[]}
          runs={[]}
          sessionId={tree.id}
          orchestrationMode={tree.orchestration_mode}
          threadColorMap={threadColorMap}
          userDisplayName={userDisplayName}
          onFileClick={onFileClick}
          onViewDiff={onViewDiff}
          onRetry={onRetry}
          onRetryStopped={onRetryStopped}
          advSyncOverlays={tree.adv_sync_overlays}
          onAdvSyncClick={onAdvSyncClick}
        />
      </div>
      {panes.length > 1 && (
        <div
          className="fork-tabs-strip"
          data-testid="fork-tabs-strip"
          role="tablist"
          onTouchStart={onStripTouchStart}
          onTouchMove={onStripTouchMove}
          onTouchEnd={onStripTouchEnd}
        >
          {panes.map(({ pane }, i) => {
            const active = i === focusedIdx;
            const closed = !!pane.fork_closed;
            const label = paneLabel(pane, i);
            return (
              <button
                key={pane.id}
                type="button"
                role="tab"
                aria-selected={active}
                className={
                  "fork-tab" +
                  (active ? " active" : "") +
                  (closed ? " closed" : "")
                }
                onClick={() => onSetFocus(pane.id)}
              >
                {label}
              </button>
            );
          })}
        </div>
      )}
      {focusedViewActive && focusedViewPane && (
        <div className="fork-focus-toolbar" data-testid="fork-focus-toolbar">
          <button
            type="button"
            className="fork-focus-back"
            onClick={() => setFocusedViewEnabled(false)}
            title={t("fork.backToSplitTitle")}
            data-testid="fork-back-to-split"
          >
            <Icon name="chevron-left" size={14} />
            {t("fork.backToSplit")}
          </button>
          <span className="fork-focus-title">
            {t("fork.focusedViewLabel", {
              name: paneLabel(focusedViewPane.pane, focusedIdx),
            })}
          </span>
        </div>
      )}
      <div
        className={"fork-split-grid" + (focusedViewActive ? " fork-split-grid-focused" : "")}
        style={
          isMobile
            ? { gridTemplateColumns: "1fr" }
            : focusedViewActive
              ? {
                  gridTemplateColumns: panes
                    .map(({ pane }) =>
                      pane.id === focusedViewPaneId
                        ? "minmax(220px, 1fr)"
                        : "minmax(0, 0fr)"
                    )
                    .join(" "),
                }
            : {
                gridTemplateColumns: `repeat(${panes.length}, minmax(220px, 1fr))`,
              }
        }
        data-testid="fork-grid"
        role="radiogroup"
        aria-label="Fork panes — exactly one is focused"
      >
        {renderedPanes.map(({ pane, msgs, pending, runs }) => {
          const isFocused = pane.id === focusedSessionId;
          const isFocusedViewHidden = focusedViewActive && pane.id !== focusedViewPaneId;
          const isClosed = !!pane.fork_closed;
          const isRoot = pane.id === tree.id;
          return (
            <ForkPane
              key={pane.id}
              pane={pane}
              messages={msgs}
              pending={pending}
              runs={runs}
              threadColorMap={threadColorMap}
              userDisplayName={userDisplayName}
              isFocused={isFocused}
              isFocusedViewHidden={isFocusedViewHidden}
              isClosed={isClosed}
              isRoot={isRoot}
              onSetFocus={() => onSetFocus(pane.id)}
              onOpenFocusedView={() => {
                if (!isClosed) onSetFocus(pane.id);
                setFocusedViewEnabled(true);
              }}
              onClose={() => onCloseFork(pane.id)}
              onReopen={() => onReopenFork(pane.id)}
              onDelete={onDeleteFork ? () => onDeleteFork(pane.id) : undefined}
              onFileClick={onFileClick}
              onViewDiff={onViewDiff}
              onRetry={onRetry}
              onRetryStopped={onRetryStopped}
              onLoadOlderMessages={
                onLoadOlderMessages
                  ? async () => {
                      await onLoadOlderMessages(pane.id);
                    }
                  : undefined
              }
              hasOlderMessages={pane.pagination?.has_older}
              advSyncOverlays={tree.adv_sync_overlays}
              onAdvSyncClick={onAdvSyncClick}
            />
          );
        })}
      </div>
    </div>
  );
}

interface PaneProps {
  pane: Session;
  messages: ChatMessage[];
  pending: ChatMessage[];
  runs: RunInfo[];
  threadColorMap: Map<string, string>;
  userDisplayName?: string | null;
  isFocused: boolean;
  isFocusedViewHidden: boolean;
  isClosed: boolean;
  isRoot: boolean;
  onSetFocus: () => void;
  onOpenFocusedView: () => void;
  onClose: () => void;
  onReopen: () => void;
  onDelete?: () => void;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (assistantMessage: ChatMessage) => void;
  onLoadOlderMessages?: () => Promise<void>;
  hasOlderMessages?: boolean;
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
}

function ForkPane({
  pane,
  messages,
  pending,
  runs,
  threadColorMap,
  userDisplayName,
  isFocused,
  isFocusedViewHidden,
  isClosed,
  isRoot,
  onSetFocus,
  onOpenFocusedView,
  onClose,
  onReopen,
  onDelete,
  onFileClick,
  onViewDiff,
  onRetry,
  onRetryStopped,
  onLoadOlderMessages: onLoadOlder,
  hasOlderMessages,
  advSyncOverlays,
  onAdvSyncClick,
}: PaneProps) {
  const { t } = useTranslation();
  const loadOlderOpId = `messages:loadOlder:${pane.id}`;
  const { inflight: loadingOlder } = useOpProgress(loadOlderOpId);

  const {
    scrollRef,
    handleScroll: handlePaneScroll,
    handleWheel: handlePaneLoadOlderWheel,
    handleTouchStart: handlePaneLoadOlderTouchStart,
    handleTouchMove: handlePaneLoadOlderTouchMove,
    handleTouchEnd: handlePaneLoadOlderTouchEnd,
    handleTouchCancel: handlePaneLoadOlderTouchCancel,
    handlePointerDown: handlePaneLoadOlderPointerDown,
    handlePointerUp: handlePaneLoadOlderPointerUp,
    handleKeyDown: handlePaneLoadOlderKeyDown,
    handleKeyUp: handlePaneLoadOlderKeyUp,
    handleScrollEnd: handlePaneLoadOlderScrollEnd,
    triggerLoadOlder: triggerPaneLoadOlder,
  } = useScrollLoadOlder(
    loadOlderOpId,
    !!hasOlderMessages,
    onLoadOlder,
  );

  // Keep the pane scrolled to the bottom as new messages arrive,
  // but only when already near the bottom (don't fight load-older
  // scroll-position restoration).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    if (nearBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, pending, runs]);

  const className = [
    "fork-pane",
    isFocused ? "fork-pane-focused" : "",
    isFocusedViewHidden ? "fork-pane-focus-hidden" : "",
    isClosed ? "fork-pane-closed" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const metaTitle = sessionModelMetaTitle(t, pane);
  const providerName = providerNameForId(pane.provider_id);

  return (
    <div
      className={className}
      data-testid="fork-pane"
      data-session-id={pane.id}
      aria-hidden={isFocusedViewHidden}
      inert={isFocusedViewHidden ? true : undefined}
    >
      <div className="fork-pane-header">
        <span className="fork-pane-label" title={pane.name}>
          {isRoot ? t("fork.original") : pane.name || t("fork.fork")}
        </span>
        {(providerName || pane.model || pane.reasoning_effort) && (
          <span className="fork-pane-run-meta" title={metaTitle}>
            {providerName && <span>{providerName}</span>}
            {pane.model && <span>{pane.model}</span>}
            {pane.reasoning_effort && <span>{pane.reasoning_effort}</span>}
          </span>
        )}
        {!isClosed && (
          <div className="fork-pane-actions">
            <button
              type="button"
              className="fork-pane-view-button"
              onClick={onOpenFocusedView}
              aria-label={t("fork.openFocusedAria")}
              title={t("fork.openFocusedTitle")}
            >
              <Icon name="expand" size={14} />
            </button>
            <button
              type="button"
              className="fork-pane-focus-radio"
              onClick={onSetFocus}
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
            <button
              type="button"
              className="fork-pane-reopen"
              onClick={onReopen}
              title={t("fork.reopenTitle")}
            >
              {t("fork.reopen")}
            </button>
            {onDelete && (
              <button
                type="button"
                className="fork-pane-delete"
                onClick={() => {
                  // Closed-pane gate is the soft prompt; this is the
                  // hard prompt. Two clicks (close, then delete) keep
                  // the destructive action behind a deliberate
                  // sequence without an extra dialog.
                  onDelete();
                }}
                title={t("fork.deleteTitle")}
              >
                {t("fork.delete")}
              </button>
            )}
          </>
        )}
        {isClosed && isRoot && (
          <span className="fork-pane-closed-tag">{t("fork.closed")}</span>
        )}
      </div>
      <div className="fork-pane-messages" ref={scrollRef} onScroll={handlePaneScroll} onScrollEnd={handlePaneLoadOlderScrollEnd} onWheel={handlePaneLoadOlderWheel} onTouchStart={handlePaneLoadOlderTouchStart} onTouchMove={handlePaneLoadOlderTouchMove} onTouchEnd={handlePaneLoadOlderTouchEnd} onTouchCancel={handlePaneLoadOlderTouchCancel} onPointerDown={handlePaneLoadOlderPointerDown} onPointerUp={handlePaneLoadOlderPointerUp} onKeyDown={handlePaneLoadOlderKeyDown} onKeyUp={handlePaneLoadOlderKeyUp} tabIndex={0}>
        {hasOlderMessages && (
          <div className="load-older-sentinel">
            {loadingOlder ? (
              <div className="load-older-spinner">{t("fork.loading")}</div>
            ) : (
              <button className="load-older-link" onClick={triggerPaneLoadOlder}>
                {t("chat.loadOlderMessages")}
              </button>
            )}
          </div>
        )}
        {messages.length === 0 && pending.length === 0 ? (
          <div className="fork-pane-empty">
            {isRoot ? t("fork.noNewTurns") : t("fork.forkNoMessages")}
          </div>
        ) : (
          <MessageList
            messages={messages}
            pending={pending}
            runs={runs}
            sessionId={pane.id}
            orchestrationMode={pane.orchestration_mode}
            threadColorMap={threadColorMap}
            userDisplayName={userDisplayName}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
            onRetry={onRetry}
            onRetryStopped={onRetryStopped}
            scrollEl={scrollRef.current}
            advSyncOverlays={advSyncOverlays}
            onAdvSyncClick={onAdvSyncClick}
          />
        )}
      </div>
      {!isRoot && !isClosed && (
        <button
          type="button"
          className="fork-pane-close"
          onClick={onClose}
          aria-label="Close this fork pane"
          title={t("fork.closeTitle")}
        >
          <Icon name="x" size={16} />
        </button>
      )}
    </div>
  );
}

/** Shared user/assistant message-pairing + per-group run-slice logic.
 * Mirrors what `Chat.tsx` does at the top level so both the shared
 * region and each pane render with identical fidelity. */
function MessageList({
  messages,
  pending,
  runs,
  sessionId,
  orchestrationMode,
  threadColorMap,
  userDisplayName,
  onFileClick,
  onViewDiff,
  onRetry,
  onRetryStopped,
  scrollEl,
  advSyncOverlays,
  onAdvSyncClick,
}: {
  messages: ChatMessage[];
  pending: ChatMessage[];
  runs: RunInfo[];
  sessionId: string;
  orchestrationMode?: Session["orchestration_mode"];
  threadColorMap: Map<string, string>;
  userDisplayName?: string | null;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (assistantMessage: ChatMessage) => void;
  scrollEl?: HTMLElement | null;
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
}) {
  const all = useMemo(() => mergeMessagesSorted(messages, pending), [messages, pending]);
  const groups = useMemo(() => {
    const out: { initiator: ChatMessage; response?: ChatMessage }[] = [];
    for (let i = 0; i < all.length; i++) {
      const msg = all[i];
      if (msg.role === "user") {
        const next = all[i + 1];
        if (next && next.role === "assistant") {
          out.push({ initiator: msg, response: next });
          i++;
        } else {
          out.push({ initiator: msg });
        }
      }
    }
    return out;
  }, [all]);
  const lastTurnGroupIdx = groups.length - 1;
  return (
    <>
      {groups.map((g, idx) => {
        const turnRuns = runs.filter((r) => {
          if (r.target_message_id === g.initiator.id) return true;
          if (g.response && r.target_message_id === g.response.id) return true;
          if (isUnanchoredRun(r) && !g.response && idx === lastTurnGroupIdx) {
            return true;
          }
          return false;
        });
        return (
          <TurnGroup
            key={g.initiator.id}
            initiatorMessage={g.initiator}
            responseMessage={g.response}
            sessionId={sessionId}
            userDisplayName={userDisplayName}
            onFileClick={onFileClick}
            onViewDiff={onViewDiff}
            onRetry={onRetry}
            onRetryStopped={onRetryStopped}
            threadColorMap={threadColorMap}
            defaultCollapsed={!!g.response && !g.response.isStreaming}
            orchestrationMode={orchestrationMode}
            runs={turnRuns}
            scrollEl={scrollEl}
            advSyncOverlays={advSyncOverlays}
            onAdvSyncClick={onAdvSyncClick}
          />
        );
      })}
    </>
  );
}
