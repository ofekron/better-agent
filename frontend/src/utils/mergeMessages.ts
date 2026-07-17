import type { ChatMessage } from "../types";

/**
 * Merge persisted and pending messages, sorted chronologically.
 * Handles missing/undefined timestamps by treating them as epoch 0
 * so they sort to the top instead of producing NaN ordering.
 */
export function mergeMessagesSorted(
  persisted: ChatMessage[],
  pending: ChatMessage[],
): ChatMessage[] {
  const persistedClientIds = new Set(
    persisted
      .map((message) => message.client_id)
      .filter((clientId): clientId is string => !!clientId),
  );
  const pendingWithoutAcked = pending.filter(
    (message) => !persistedClientIds.has(message.id),
  );
  const combined = [...persisted, ...pendingWithoutAcked];
  combined.sort(
    (a, b) =>
      (a.timestamp ? new Date(a.timestamp).getTime() : 0) -
      (b.timestamp ? new Date(b.timestamp).getTime() : 0),
  );
  return combined;
}

