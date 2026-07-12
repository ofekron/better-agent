import { isRetryableOfflineError } from "./offlineRequest";
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

export type FlushErrorKind = "transient" | "permanent";

/** Classify an error thrown while creating a session during the reconnect
 * drain.
 *
 * - `transient` — network/abort/timeout/5xx/429/etc. The backend never saw a
 *   well-formed, rejected request; retrying the SAME idempotent action
 *   (client_session_id is the backend id) will eventually succeed. The drain
 *   must PAUSE on these and retry the entire backlog later, so we never
 *   dispatch a later action ahead of an earlier one that is merely waiting on
 *   the network.
 * - `permanent` — the backend received the request and rejected it on its
 *   merits (4xx other than the retryable ones — bad shape, unknown provider,
 *   a feature that is genuinely unavailable). Retrying the identical bytes
 *   cannot fix it, so the drain must NOT block the rest of the backlog on it.
 *
 * Self-healing states that surface as a non-retryable status right after
 * reconnect (e.g. a 404 "team orchestration not ready yet" while the backend
 * finishes booting) are intentionally left to retry on the next backoff deadline
 * — the caller keeps the entry in the durable backlog either way, so a state
 * that later flips to ready recovers without losing the user's action. */
export function classifyFlushError(error: unknown): FlushErrorKind {
  return isRetryableOfflineError(error) ? "transient" : "permanent";
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
  /** The create failed for good. Mark the optimistic message visibly failed
   * (never silently drop it) and record the session id so dependent prompts in
   * this pass are skipped — but keep the durable backlog entry so the user's
   * intent is preserved and a later self-heal can still succeed. */
  permanentFailureSessionId?: string;
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
  if (classifyFlushError(error) === "transient") {
    return { stop: true };
  }
  return { stop: false, permanentFailureSessionId: sessionId };
}
