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

/**
 * Oldest numeric message seq in a list, ignoring live/streaming
 * placeholders (`live-*` ids, in-flight assistant turns) that carry no
 * `seq` yet. Returns `null` when no message has a seq — callers treat
 * that as "no cursor / nothing to page before".
 *
 * Without this filter, a single seq-less placeholder would reduce the
 * cursor to 0 via `m.seq ?? 0` and silently disable load-older.
 */
export function oldestNumericSeq(messages: ChatMessage[]): number | null {
  let min = Infinity;
  for (const m of messages) {
    if (typeof m.seq === "number") min = Math.min(min, m.seq);
  }
  return min === Infinity ? null : min;
}
