import { eventBus } from "../lib/eventBus";

/** One message the app has been asked to scroll to, kept across an async
 * cross-session navigation: an event link may open a different session
 * whose messages are still loading, so the request outlives the click and
 * is consumed by the target Chat once its messages render. */
let pending: { sessionId: string; messageId: string } | null = null;

/** Play a short highlight on a freshly-focused message so the jump is
 * perceptible. Uses the Web Animations API so no stylesheet is required. */
function flashElement(el: HTMLElement): void {
  try {
    el.animate(
      [
        { boxShadow: "0 0 0 2px var(--accent, #4c8bf5)", backgroundColor: "rgba(76,139,245,0.14)" },
        { boxShadow: "0 0 0 2px var(--accent, #4c8bf5)", backgroundColor: "rgba(76,139,245,0.14)", offset: 0.6 },
        { boxShadow: "0 0 0 2px rgba(76,139,245,0)", backgroundColor: "rgba(76,139,245,0)" },
      ],
      { duration: 1500, easing: "ease-out" },
    );
  } catch {
    // animate() unsupported — the scroll alone still lands the user.
  }
}

/** Scroll the message with `messageId` into view inside `.chat-messages` and
 * flash it. Returns false if the container or message isn't in the DOM yet
 * (caller retries on the next frame). */
export function scrollMessageIntoView(messageId: string, documentRef: Document = document): boolean {
  const scrollEl = documentRef.querySelector(".chat-messages") as HTMLElement | null;
  if (!scrollEl) return false;
  const target = scrollEl.querySelector(
    `[data-message-id="${CSS.escape(messageId)}"]`,
  ) as HTMLElement | null;
  if (!target) return false;
  const scrollRect = scrollEl.getBoundingClientRect();
  const targetTop = target.getBoundingClientRect().top;
  scrollEl.scrollTo({
    top: scrollEl.scrollTop + targetTop - scrollRect.top - 24,
    behavior: "smooth",
  });
  flashElement(target);
  return true;
}

/** Ask the app to focus one message. Navigation to the owning session is the
 * caller's responsibility; this records the target and nudges an already-open
 * Chat via the bus. */
export function requestMessageFocus(sessionId: string, messageId: string): void {
  pending = { sessionId, messageId };
  eventBus.publish("focus_message", { session_id: sessionId, message_id: messageId });
}

/** The pending message id for `sessionId`, or null. Does not clear — the
 * consumer clears only once it successfully scrolled, so a not-yet-rendered
 * message stays queued. */
export function peekPendingMessageFocus(sessionId: string): string | null {
  return pending && pending.sessionId === sessionId ? pending.messageId : null;
}

export function clearPendingMessageFocus(messageId: string): void {
  if (pending && pending.messageId === messageId) pending = null;
}
