import { useMemo, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import type {
  AdvSyncOverlay,
  ChatMessage,
  FileFocus,
  RunInfo,
  Session,
} from "../types";
import type { InlineTag } from "../types/inlineTag";
import { mergeMessagesSorted } from "../utils/mergeMessages";
import { isUnanchoredRun } from "../utils/runTargets";
import { MessageGroup } from "./MessageBubble";
import type { StreamingLoadPhase } from "../hooks/useWebSocket";
import { buildThreadColorMap } from "../threadColors";

type MessagePair = { user: ChatMessage; assistant?: ChatMessage };

interface TimelineRow {
  primary?: MessagePair;
  supervisor?: MessagePair;
}

interface Props {
  session: Session;
  pendingMessages: ChatMessage[];
  runs: RunInfo[];
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (assistantMessage: ChatMessage) => void;
  tags?: InlineTag[];
  onRemoveTag?: (id: string) => void;
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
  expandAllTrigger?: number;
  streamingLoadPhase?: StreamingLoadPhase;
}

/**
 * Chronological split view shown when the supervisor toggle is on.
 *
 * Both panes read from the SAME session record. Messages are
 * partitioned by `source`: supervisor-sourced messages render on the
 * right, everything else (primary agent + user prompts) on the left.
 * Interleaved by timestamp on a shared vertical axis.
 */
export function SupervisorSplitView({
  session,
  pendingMessages,
  runs,
  onFileClick,
  onViewDiff,
  onRetry,
  onRetryStopped,
  tags,
  onRemoveTag,
  advSyncOverlays,
  onAdvSyncClick,
  expandAllTrigger,
  streamingLoadPhase,
}: Props) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);

  const threadColorMap = useMemo(() => {
    const ids = (session.workers ?? [])
      .map((w) => w.agent_session_id)
      .filter(Boolean) as string[];
    return buildThreadColorMap(ids);
  }, [session.workers]);

  // Partition messages + pending by `source`. Supervisor-sourced ones
  // (both user prompts injected by the verdict loop and supervisor
  // assistant replies) land on the right pane; everything else on the
  // left pane.
  const primaryPairs = useMemo(
    () =>
      buildPairs(
        mergeMessagesSorted(session.messages ?? [], pendingMessages)
          .filter((m) => (m as { source?: string }).source !== "supervisor"),
      ),
    [session.messages, pendingMessages],
  );

  const supervisorPairs = useMemo(
    () =>
      buildPairs(
        mergeMessagesSorted(session.messages ?? [], pendingMessages)
          .filter((m) => (m as { source?: string }).source === "supervisor"),
      ),
    [session.messages, pendingMessages],
  );

  const rows = useMemo(() => {
    type Tagged = { pair: MessagePair; slot: "primary" | "supervisor"; ts: string };
    const items: Tagged[] = [
      ...primaryPairs.map((p) => ({ pair: p, slot: "primary" as const, ts: p.user.timestamp })),
      ...supervisorPairs.map((p) => ({ pair: p, slot: "supervisor" as const, ts: p.user.timestamp })),
    ];
    items.sort((a, b) => (a.ts ?? "").localeCompare(b.ts ?? ""));

    const out: TimelineRow[] = [];
    for (const { pair, slot } of items) {
      out.push({ [slot]: pair });
    }
    return out;
  }, [primaryPairs, supervisorPairs]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [rows]);

  const lastPrimaryIdx = useMemo(() => {
    for (let i = rows.length - 1; i >= 0; i--) {
      if (rows[i].primary) return i;
    }
    return -1;
  }, [rows]);

  const lastSupervisorIdx = useMemo(() => {
    for (let i = rows.length - 1; i >= 0; i--) {
      if (rows[i].supervisor) return i;
    }
    return -1;
  }, [rows]);

  const hasPrimary = rows.some((r) => r.primary);
  const hasSupervisor = rows.some((r) => r.supervisor);

  return (
    <div className="supervisor-split">
      <div className="supervisor-split-header">
        <span className="supervisor-split-label">{t("supervisor.primaryPaneLabel")}</span>
        <span className="supervisor-split-label">{t("supervisor.panelTitle")}</span>
      </div>
      <div className="supervisor-timeline" ref={scrollRef}>
        {rows.length === 0 ? (
          <div className="supervisor-pane-empty">
            {t("supervisor.noMessages", { label: "" })}
          </div>
        ) : (
          rows.map((row, idx) => {
            const isLastPrimary = idx === lastPrimaryIdx;
            const isLastSupervisor = idx === lastSupervisorIdx;
            return (
              <div className="supervisor-timeline-row" key={
                (row.primary?.user.id ?? "") + (row.supervisor?.user.id ?? "")
              }>
                <div className="supervisor-timeline-cell">
                  {row.primary && (
                    <CellGroup
                      pair={row.primary}
                      runs={runs}
                      isLast={isLastPrimary}
                      sessionId={session.id}
                      orchestrationMode={session.orchestration_mode}
                      threadColorMap={threadColorMap}

                      onFileClick={onFileClick}
                      onViewDiff={onViewDiff}
                      onRetry={onRetry}
                      onRetryStopped={onRetryStopped}
                      tags={tags}
                      onRemoveTag={onRemoveTag}
                      advSyncOverlays={advSyncOverlays}
                      onAdvSyncClick={onAdvSyncClick}
                      expandAllTrigger={expandAllTrigger}
                      streamingLoadPhase={
                        row.primary.assistant?.isStreaming && isLastPrimary
                          ? streamingLoadPhase
                          : undefined
                      }
                    />
                  )}
                  {!row.primary && hasPrimary && (
                    <div className="supervisor-timeline-spacer" />
                  )}
                </div>
                <div className="supervisor-timeline-cell">
                  {row.supervisor && (
                    <CellGroup
                      pair={row.supervisor}
                      runs={runs}
                      isLast={isLastSupervisor}
                      sessionId={session.id}
                      orchestrationMode={session.orchestration_mode}
                      threadColorMap={threadColorMap}

                      onFileClick={onFileClick}
                      onViewDiff={onViewDiff}
                      onRetry={onRetry}
                      onRetryStopped={onRetryStopped}
                      tags={tags}
                      onRemoveTag={onRemoveTag}
                      advSyncOverlays={advSyncOverlays}
                      onAdvSyncClick={onAdvSyncClick}
                      expandAllTrigger={expandAllTrigger}
                      streamingLoadPhase={
                        row.supervisor.assistant?.isStreaming && isLastSupervisor
                          ? streamingLoadPhase
                          : undefined
                      }
                    />
                  )}
                  {!row.supervisor && hasSupervisor && (
                    <div className="supervisor-timeline-spacer" />
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function buildPairs(messages: ChatMessage[]): MessagePair[] {
  const out: MessagePair[] = [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    if (msg.role !== "user") continue;
    const next = messages[i + 1];
    const pair: MessagePair =
      next && next.role === "assistant"
        ? { user: msg, assistant: next }
        : { user: msg };
    out.push(pair);
    if (pair.assistant) i++;
  }
  return out;
}

interface CellGroupProps {
  pair: MessagePair;
  runs: RunInfo[];
  isLast: boolean;
  sessionId: string;
  orchestrationMode?: Session["orchestration_mode"];
  threadColorMap: Map<string, string>;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
  onRetry?: (message: ChatMessage) => void;
  onRetryStopped?: (assistantMessage: ChatMessage) => void;
  tags?: InlineTag[];
  onRemoveTag?: (id: string) => void;
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
  expandAllTrigger?: number;
  streamingLoadPhase?: StreamingLoadPhase;
}

function CellGroup({
  pair,
  runs,
  isLast,
  sessionId,
  orchestrationMode,
  threadColorMap,
  onFileClick,
  onViewDiff,
  onRetry,
  onRetryStopped,
  tags,
  onRemoveTag,
  advSyncOverlays,
  onAdvSyncClick,
  expandAllTrigger,
  streamingLoadPhase,
}: CellGroupProps) {
  const groupRuns = runs.filter((r) => {
    if (r.target_message_id === pair.user.id) return true;
    if (pair.assistant && r.target_message_id === pair.assistant.id) return true;
    if (isUnanchoredRun(r) && !pair.assistant && isLast) return true;
    return false;
  });

  return (
    <MessageGroup
      key={pair.user.id}
      userMessage={pair.user}
      assistantMessage={pair.assistant}
      sessionId={sessionId}
      onFileClick={onFileClick}
      onViewDiff={onViewDiff}
      onRetry={onRetry}
      onRetryStopped={onRetryStopped}
      threadColorMap={threadColorMap}
      defaultCollapsed={!!pair.assistant && !pair.assistant.isStreaming}
      expandAllTrigger={expandAllTrigger}
      orchestrationMode={orchestrationMode}
      runs={groupRuns}
      tags={tags}
      onRemoveTag={onRemoveTag}
      advSyncOverlays={advSyncOverlays}
      onAdvSyncClick={onAdvSyncClick}
      loadPhase={streamingLoadPhase}
    />
  );
}
