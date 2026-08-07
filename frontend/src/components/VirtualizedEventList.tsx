// Windows a large, already-built row list (the output of
// MessageBubble.tsx's renderGroupedEvents/renderTreeLevel) onto the SAME
// scroll owner the rest of the chat uses, instead of mounting every row's
// DOM subtree at once. A "monster" live turn (thousands of nodes -> many
// hundreds of rendered rows) otherwise pays full mount/reconcile/paint
// cost for rows that are nowhere near the viewport.
//
// Design constraints (see chat-panel.md's "Scrolling and motion" section):
//   - "The chat has exactly one scroll owner" — this component never
//     introduces its own scrollable region. It walks up to the SAME
//     scrollable ancestor MessageBubble's collapse-anchor logic already
//     uses (findScrollParent, in utils/scrollParent.ts) and virtualizes
//     against THAT element, via react-virtual's `scrollMargin` (the
//     documented pattern for a virtualized sub-list nested inside a
//     larger scrollable page/panel).
//   - The spacer element is sized to the FULL (unvirtualized) content
//     height via `virtualizer.getTotalSize()`, so the outer container's
//     `scrollHeight` is unaffected by which rows are actually mounted —
//     Chat.tsx's existing bottom-follow / user-scroll-up-freedom logic
//     (which only ever reads the outer container's scrollHeight/scrollTop)
//     keeps working completely unchanged, virtualized or not.
//   - Below the threshold (see MessageBubble.tsx), this component is never
//     mounted at all — the non-virtualized DOM path is untouched.

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { findScrollParent } from "../utils/scrollParent";

/** Row-count threshold above which a turn's event list switches from
 * plain DOM rendering to windowed virtualization. Applied by the caller
 * (MessageBubble.tsx) against the SAME row array passed as `items` here —
 * see the module docstring there for why the check is on rendered rows,
 * not raw WSEvent count (grouping can collapse many events into one row). */
export const VIRTUALIZE_EVENT_THRESHOLD = 200;

interface VirtualizedEventListProps {
  /** Pre-built, already-keyed rows in display order. Building this array
   * stays exactly as it was before virtualization existed (unchanged
   * O(rows) grouping pass) — only how many of them actually mount into
   * the DOM changes. */
  items: ReactNode[];
  /** Estimated row height in px before a row is first measured. Refined
   * per-row automatically once mounted (react-virtual's dynamic-size
   * ResizeObserver-based measurement). */
  estimateSize?: number;
  overscan?: number;
}

export function VirtualizedEventList({
  items,
  estimateSize = 80,
  overscan = 8,
}: VirtualizedEventListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollElement, setScrollElement] = useState<HTMLElement | null>(null);
  const [scrollMargin, setScrollMargin] = useState(0);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const owner = findScrollParent(el);
    setScrollElement(owner);
    if (!owner) return;

    const measure = () => {
      const containerRect = el.getBoundingClientRect();
      const scrollRect = owner.getBoundingClientRect();
      // Offset of this list's top from the top of the scroll owner's
      // CONTENT (not its current viewport) — stable across scroll events
      // since scrollTop is added back in.
      setScrollMargin(containerRect.top - scrollRect.top + owner.scrollTop);
    };
    measure();

    // Re-measure whenever this list's offset from the scroll owner's top
    // could have shifted: the owner's own size changing (ResizeObserver),
    // or ANY DOM structural change within it — an earlier turn streaming
    // in new rows, a collapsible toggling, etc. (MutationObserver on the
    // owner's subtree) — coalesced to at most one remeasure per animation
    // frame so a burst of sibling mutations doesn't force layout on every
    // single one.
    let rafId: number | null = null;
    const scheduleMeasure = () => {
      if (rafId !== null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        measure();
      });
    };
    const resizeObserver = new ResizeObserver(scheduleMeasure);
    resizeObserver.observe(owner);
    const mutationObserver = new MutationObserver(scheduleMeasure);
    mutationObserver.observe(owner, { childList: true, subtree: true });
    return () => {
      resizeObserver.disconnect();
      mutationObserver.disconnect();
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, [items.length]);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollElement,
    estimateSize: () => estimateSize,
    overscan,
    scrollMargin,
  });

  if (!scrollElement) {
    // No scroll owner found yet (not mounted, or genuinely no scrollable
    // ancestor) — render everything so content is never silently dropped.
    return (
      <div ref={containerRef} data-testid="virtualized-event-list-fallback">
        {items}
      </div>
    );
  }

  const virtualItems = virtualizer.getVirtualItems();
  return (
    <div
      ref={containerRef}
      data-testid="virtualized-event-list"
      style={{ position: "relative", height: virtualizer.getTotalSize() }}
    >
      {virtualItems.map((virtualItem) => (
        <div
          key={virtualItem.key}
          data-index={virtualItem.index}
          ref={virtualizer.measureElement}
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            transform: `translateY(${virtualItem.start - scrollMargin}px)`,
          }}
        >
          {items[virtualItem.index]}
        </div>
      ))}
    </div>
  );
}
