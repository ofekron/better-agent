import type { OfflineQueueEntry } from "src/hooks/useOfflineQueue";
import type { ChatMessage } from "src/types";
import type { QueuedBannerState } from "src/utils/queuedPrompts";

export type PendingOfflineQueueDraft = QueuedBannerState & {
  clientId: string | null;
  offline?: boolean;
};

function compositeKey(sessionId: string, clientId: string): string {
  return `${sessionId}\u0000${clientId}`;
}

function entrySessionId(entry: OfflineQueueEntry): string {
  return entry.type === "create_session" ? entry.session.id : entry.sessionId;
}

export function reconcileOfflinePendingMessages(
  pendingBySession: Record<string, ChatMessage[]>,
  entries: OfflineQueueEntry[],
): Record<string, ChatMessage[]> {
  const authoritativeEntries = new Map(entries.map((entry) => [
    compositeKey(entrySessionId(entry), entry.clientId),
    entry,
  ]));
  let changed = false;
  const next: Record<string, ChatMessage[]> = {};
  for (const [sessionId, messages] of Object.entries(pendingBySession)) {
    const reconciled = messages.flatMap((message) => {
      const entry = authoritativeEntries.get(compositeKey(sessionId, message.id));
      if (!entry) {
        if (message.status === "offline" || message.status === "error") {
          changed = true;
          return [];
        }
        return [message];
      }
      if (entry.failure) {
        if (message.status === "error" && message.errorText === entry.failure.errorText) {
          return [message];
        }
        changed = true;
        return [{ ...message, status: "error" as const, errorText: entry.failure.errorText }];
      }
      if (message.status === "error") {
        changed = true;
        return [{ ...message, status: "offline" as const, errorText: undefined }];
      }
      return [message];
    });
    if (reconciled.length > 0) next[sessionId] = reconciled;
  }
  return changed ? next : pendingBySession;
}

export function reconcileOfflineQueueDrafts(
  draftsBySession: Record<string, PendingOfflineQueueDraft[]>,
  entries: OfflineQueueEntry[],
): Record<string, PendingOfflineQueueDraft[]> {
  const authoritativeKeys = new Set(entries.flatMap((entry) => (
    entry.type !== "create_session" && entry.sendMode === "queue"
      ? [compositeKey(entry.sessionId, entry.clientId)]
      : []
  )));
  let changed = false;
  const next: Record<string, PendingOfflineQueueDraft[]> = {};
  for (const [sessionId, drafts] of Object.entries(draftsBySession)) {
    const reconciled = drafts.flatMap((draft) => {
      if (draft.clientId === null) return [draft];
      const authoritative = authoritativeKeys.has(compositeKey(sessionId, draft.clientId));
      if (draft.offline && !authoritative) {
        changed = true;
        return [];
      }
      if (authoritative && !draft.offline) {
        changed = true;
        return [{ ...draft, offline: true }];
      }
      return [draft];
    });
    if (reconciled.length > 0) next[sessionId] = reconciled;
  }
  return changed ? next : draftsBySession;
}
