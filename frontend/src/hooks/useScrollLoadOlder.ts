import { useRef, useLayoutEffect, useCallback, useState } from "react";
import { startOp, completeOp } from "../progress/store";

/**
 * Shared scroll-triggered "load older" logic. Returns a ref to attach to
 * the scrollable container and a `triggerLoadOlder` callback.
 *
 * When the user scrolls near the top (≤40px) and `hasOlder` is true,
 * calls `onLoadOlder()`. The caller should always render a manual
 * "load more" button when `hasOlder` is true — the scroll trigger
 * supplements it for cases where a scrollbar exists.
 * Preserves scroll position after prepending older content by tracking
 * scrollHeight deltas.
 */
export function useScrollLoadOlder(
  /** Unique id for the progress tracker (prevents double-fires). */
  opId: string,
  hasOlder: boolean,
  onLoadOlder: (() => Promise<boolean | void>) | undefined,
) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pendingScrollHeight = useRef<{ opId: string; height: number } | null>(null);
  const loadingRef = useRef(false);
  const activeRequestRef = useRef<{ opId: string; token: symbol } | null>(null);
  const currentOpIdRef = useRef(opId);
  const previousOpIdRef = useRef(opId);
  const [loadErrorOpId, setLoadErrorOpId] = useState<string | null>(null);
  // Set true for the single render where older messages were just
  // prepended, so a consumer's stick-to-bottom effect can skip its
  // snap-to-end for that render (prepends must never scroll to end).
  const justPrepended = useRef(false);
  currentOpIdRef.current = opId;
  if (previousOpIdRef.current !== opId) {
    previousOpIdRef.current = opId;
    loadingRef.current = false;
    pendingScrollHeight.current = null;
    justPrepended.current = false;
  }
  const triggerLoadOlder = useCallback(async () => {
    if (activeRequestRef.current?.opId === opId || !onLoadOlder) return;
    const token = Symbol(opId);
    if (scrollRef.current) {
      pendingScrollHeight.current = {
        opId,
        height: scrollRef.current.scrollHeight,
      };
    }
    activeRequestRef.current = { opId, token };
    loadingRef.current = true;
    setLoadErrorOpId(null);
    startOp(opId);
    try {
      const prepended = await onLoadOlder();
      if (prepended === false && activeRequestRef.current?.token === token) {
        pendingScrollHeight.current = null;
      }
    } catch {
      if (currentOpIdRef.current === opId && activeRequestRef.current?.token === token) {
        pendingScrollHeight.current = null;
        setLoadErrorOpId(opId);
      }
    } finally {
      if (activeRequestRef.current?.token === token) {
        activeRequestRef.current = null;
        if (currentOpIdRef.current === opId) loadingRef.current = false;
      }
      completeOp(opId);
    }
  }, [onLoadOlder, opId]);

  // Preserve scroll position after prepending older messages.
  // useLayoutEffect fires synchronously after DOM mutation but BEFORE
  // the browser paints — prevents the visible jump.
  // Runs on EVERY render (no dep array) and acts only once scrollHeight
  // has actually grown past the pre-prepend snapshot. This is required
  // because the DOM grows from the consumer's THROTTLED render data,
  // which can land in a later commit than the raw `messages` change that
  // triggered the load — a dep-gated effect would run on the pre-throttle
  // commit, see no growth, bail, and never re-run on the growth commit.
  // The null-snap early return keeps this cheap on unrelated renders.
  useLayoutEffect(() => {
    const snap = pendingScrollHeight.current;
    if (snap === null || snap.opId !== opId || !scrollRef.current) return;
    const el = scrollRef.current;
    if (el.scrollHeight <= snap.height) return; // older messages haven't rendered yet
    el.scrollTop += el.scrollHeight - snap.height;
    pendingScrollHeight.current = null;
    justPrepended.current = true;
  });

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (
      el.scrollTop <= 40
      && hasOlder
      && activeRequestRef.current?.opId !== opId
    ) {
      triggerLoadOlder();
    }
  }, [hasOlder, triggerLoadOlder]);

  return {
    scrollRef,
    handleScroll,
    triggerLoadOlder,
    loadingRef,
    justPrepended,
    loadError: loadErrorOpId === opId,
  } as const;
}
