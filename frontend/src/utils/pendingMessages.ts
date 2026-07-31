import type { ChatMessage } from "../types";

export interface PendingAckState {
  ackedClientIds: Set<string>;
  skipNextAppendBySession: Set<string>;
}

/** True while any pending entry is still WAITING on a backend ack — i.e.
 * would resolve to either a `user_message_persisted` ack or a terminal
 * failure. A message already at "error" status has its definitive
 * outcome; it must not count here, or Chat.tsx's `showPendingPromptAsRunning`
 * (the only caller) keeps forcing the turn group's `isRunning` true after
 * the session itself has stopped, which suppresses the failed message's
 * own Retry button (MessageBubble.tsx TurnGroupImpl only renders it when
 * `!isRunning`). */
export function hasPendingPromptAck(messages: ChatMessage[]): boolean {
  return messages.some(
    (message) => !message.file_discussion_id && message.status !== "error",
  );
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
