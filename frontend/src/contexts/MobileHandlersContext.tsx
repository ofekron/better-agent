/**
 * Registry for mobile-specific action handlers.
 *
 * Problem: the mobile long-press handler lives in InvestigateContextMenu
 * (App-level), but the actual handlers (rewind, addTag, advSync) live in
 * Chat (a child). React context can't flow upward, so we use a module-level
 * ref instead.
 *
 * Chat calls registerMobileHandlers() once to stamp its handlers into the
 * shared ref. InvestigateContextMenu reads them at long-press time.
 * No re-renders involved — purely imperative.
 */

export interface MobileHandlers {
  /** Rewind a user message by ID. */
  rewind?: (messageId: string, pos: { x: number; y: number }) => void;
  /** Add an inline tag. */
  addTag?: (text: string, comment: string, messageId: string) => void;
  /** Run adversarial sync on selected text. */
  advSync?: (text: string, messageId: string) => void;
}

const handlers: MobileHandlers = {};

/** Stamp the latest handlers. Called from Chat on every render (cheap). */
export function registerMobileHandlers(next: MobileHandlers) {
  handlers.rewind = next.rewind;
  handlers.addTag = next.addTag;
  handlers.advSync = next.advSync;
}

/** Clear all handlers. Called on unmount so stale closures don't linger. */
export function clearMobileHandlers() {
  handlers.rewind = undefined;
  handlers.addTag = undefined;
  handlers.advSync = undefined;
}

/** Read the latest handlers. Called from InvestigateContextMenu at long-press time. */
export function getMobileHandlers(): MobileHandlers {
  return handlers;
}
