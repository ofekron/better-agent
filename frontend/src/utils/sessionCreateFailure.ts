import { isRetryableOfflineError } from "./offlineRequest";

/** How a failed session-create POST should be surfaced to the user. */
export type SessionCreateFailurePlan =
  | { kind: "queue-offline" }
  | { kind: "alert"; message: string };

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * Routes a session-create failure. File-edit sessions need the backend to
 * materialize a worktree, so they can never be queued locally — their
 * failures always alert with the real cause (a timeout gets the dedicated
 * message because the create is idempotent via `client_session_id`, so
 * retrying resolves to the same session). Non-file-edit creates keep the
 * local-first behavior: transient failures go to the offline queue.
 */
export function planSessionCreateFailure(
  error: unknown,
  opts: { fileEditEnabled: boolean; timeoutMessage: string },
): SessionCreateFailurePlan {
  if (!opts.fileEditEnabled && isRetryableOfflineError(error)) {
    return { kind: "queue-offline" };
  }
  if (isAbortError(error)) {
    return { kind: "alert", message: opts.timeoutMessage };
  }
  return {
    kind: "alert",
    message: error instanceof Error ? error.message : String(error),
  };
}
