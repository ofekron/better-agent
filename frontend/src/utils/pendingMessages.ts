import type { ChatMessage } from "../types";

export interface PendingAckState {
  ackedClientIds: Set<string>;
  skipNextAppendBySession: Set<string>;
}

export function upsertPendingUnlessAcked(
  prev: ChatMessage[],
  sessionId: string,
  pendingMsg: ChatMessage,
  ackState: PendingAckState,
  replacedMessageId?: string,
): ChatMessage[] {
  const next = replacedMessageId
    ? prev.filter((message) => message.id !== replacedMessageId)
    : prev;
  if (ackState.ackedClientIds.has(pendingMsg.id)) return next;
  if (ackState.skipNextAppendBySession.has(sessionId)) {
    ackState.skipNextAppendBySession.delete(sessionId);
    return next;
  }
  return [...next, pendingMsg];
}
