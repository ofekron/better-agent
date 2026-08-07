/** Walk up the DOM tree from `el` and return the nearest ancestor whose
 * computed `overflow-y` makes it a scroll container. Single source of
 * truth for "find the chat's one scroll owner" — used by the collapse-
 * toggle anchor logic in MessageBubble.tsx (when the parent hasn't
 * threaded an explicit `scrollEl` prop) and by VirtualizedEventList.tsx
 * (to share that SAME scroll owner rather than introduce a second one —
 * see chat-panel.md's "the chat has exactly one scroll owner" rule).
 * Works for every container regardless of class name. Returns null if
 * nothing in the chain scrolls. */
export function findScrollParent(el: HTMLElement): HTMLElement | null {
  let parent = el.parentElement;
  while (parent) {
    const overflowY = getComputedStyle(parent).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return parent;
    parent = parent.parentElement;
  }
  return null;
}
