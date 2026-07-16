import { HttpStatusError, isRetryableOfflineError } from "./offlineRequest";
import type { OfflineQueueEntry } from "../hooks/useOfflineQueue";

// Policy helpers for draining the durable offline-action backlog on reconnect.
//
// These are deliberately PURE so the head-of-line-blocking contract can be
// unit-tested without mounting the whole App or a real WebSocket. The
// imperative flush loop in App.tsx owns the side effects (createSession /
// sendMessage / pending-message updates); this module owns the decisions.
//
// The invariant they protect (AGENTS.md "Offline-first usability"):
//   "Sync must be idempotent and preserve user action order so reconnects,
//    reloads, duplicate attempts, and partial syncs cannot lose or duplicate
//    work."
// A single poison entry must never strand the unrelated actions queued behind
// it, and a merely-transient failure must pause the whole drain (so nothing is
// dispatched out of order) rather than skip ahead.

export type FlushErrorKind = "transient" | "actionable" | "terminal";

/** Classify an error thrown while creating a session during the reconnect
 * drain.
 *
 * The backend's 410 tombstone is terminal. Network-class failures retry in
 * order; deterministic client/conflict responses become durable actionable
 * holds so they cannot hot-loop but can be retried after user correction. */
export function classifyFlushError(error: unknown): FlushErrorKind {
  if (error instanceof HttpStatusError && error.status === 410) return "terminal";
  if (isRetryableOfflineError(error)) return "transient";
  return "actionable";
}

/** A queued prompt targets a session. If that session's queued `create_session`
 * permanently failed earlier in THIS drain pass, dispatching the prompt would
 * race a session that does not exist yet (and may never, this pass). Skip it so
 * it stays buffered in the durable backlog — never dropped, retried on the next
 * tick — instead of being flipped to a hard error the user can't recover. */
export function shouldSkipDependentSend(
  entry: OfflineQueueEntry,
  failedSessionIds: ReadonlySet<string>,
): boolean {
  if (entry.type === "create_session") return false;
  return failedSessionIds.has(entry.sessionId);
}

export interface FlushOutcome {
  /** Stop the whole drain pass now and retry the entire backlog on the next
   * tick. Set ONLY for transient failures, so action order is preserved and no
   * later action is dispatched ahead of an earlier one still waiting on the
   * network. */
  stop: boolean;
  scheduleRetry: boolean;
  hold?: { sessionId: string; kind: "actionable" | "terminal" };
}

export interface OfflineRetryDeadline {
  attempt: number;
  dueAt: number;
}

export function nextOfflineRetryDeadline(
  previousAttempt: number,
  now: number,
  randomFraction: number,
): OfflineRetryDeadline {
  const attempt = previousAttempt + 1;
  const baseMs = Math.min(60_000, 2_000 * (2 ** Math.min(attempt - 1, 5)));
  const boundedRandom = Math.max(0, Math.min(1, randomFraction));
  return { attempt, dueAt: now + baseMs + Math.floor(baseMs * 0.2 * boundedRandom) };
}

/** Decide what the drain loop does after `createSession` throws for a queued
 * `create_session` entry. Pure so the policy is locked by a regression test. */
export function outcomeForCreateError(
  error: unknown,
  sessionId: string,
): FlushOutcome {
  const kind = classifyFlushError(error);
  if (kind === "transient") {
    return { stop: true, scheduleRetry: true };
  }
  return { stop: false, scheduleRetry: false, hold: { sessionId, kind } };
}
