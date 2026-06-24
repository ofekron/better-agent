import type { ChatMessage } from "../types";

export interface PendingAckState {
  ackedClientIds: Set<string>;
  skipNextAppendBySession: Set<string>;
}

export function appendPendingUnlessAcked(
  prev: ChatMessage[],
  sessionId: string,
  pendingMsg: ChatMessage,
  ackState: PendingAckState,
): ChatMessage[] {
  if (ackState.ackedClientIds.has(pendingMsg.id)) return prev;
  if (ackState.skipNextAppendBySession.has(sessionId)) {
    ackState.skipNextAppendBySession.delete(sessionId);
    return prev;
  }
  return [...prev, pendingMsg];
}
