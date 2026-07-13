import { useRef, useLayoutEffect, useEffect, useCallback, type KeyboardEvent, type PointerEvent, type TouchEvent, type WheelEvent } from "react";
import { startOp, completeOp } from "../progress/store";

export function scrollToLatest(el: Pick<HTMLDivElement, "scrollTop" | "scrollHeight">): void {
  el.scrollTop = el.scrollHeight;
}

type LoadGesture = "wheel" | "touch" | "pointer" | "key";

function isInteractiveDescendant(target: EventTarget | null, currentTarget: HTMLDivElement): boolean {
  if (!(target instanceof Element) || target === currentTarget) return false;
  return !!target.closest('a[href], button, input, select, textarea, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [tabindex]:not([tabindex="-1"])');
}

/**
 * Shared scroll-triggered "load older" logic. Returns a ref to attach to
 * the scrollable container and a `triggerLoadOlder` callback.
 *
 * When an upward wheel/touch/key gesture or scrollbar drag reaches the
 * top (≤40px) and `hasOlder` is true, calls `onLoadOlder()` once. Plain
 * scroll events never authorize paging. The caller should always render a manual
 * "load more" button when `hasOlder` is true — the scroll trigger
 * supplements it for cases where a scrollbar exists.
 * Preserves scroll position after prepending older content by tracking
 * scrollHeight deltas.
 */
export function useScrollLoadOlder(
  /** Unique id for the progress tracker (prevents double-fires). */
  opId: string,
  hasOlder: boolean,
  onLoadOlder: (() => Promise<void>) | undefined,
) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pendingScrollHeight = useRef<number | null>(null);
  const loadingRef = useRef(false);
  const loadIntentRef = useRef<LoadGesture | null>(null);
  const gestureScrollTopRef = useRef<number | null>(null);
  const touchYRef = useRef<number | null>(null);
  const pointerIdRef = useRef<number | null>(null);
  // Set true for the single render where older messages were just
  // prepended, so a consumer's stick-to-bottom effect can skip its
  // snap-to-end for that render (prepends must never scroll to end).
  const justPrepended = useRef(false);

  const triggerLoadOlder = useCallback(async () => {
    if (loadingRef.current || !onLoadOlder) return;
    if (scrollRef.current) {
      pendingScrollHeight.current = scrollRef.current.scrollHeight;
    }
    loadingRef.current = true;
    startOp(opId);
    try {
      await onLoadOlder();
    } finally {
      loadingRef.current = false;
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
    if (snap === null || !scrollRef.current) return;
    const el = scrollRef.current;
    if (el.scrollHeight <= snap) return; // older messages haven't rendered yet
    el.scrollTop += el.scrollHeight - snap;
    pendingScrollHeight.current = null;
    justPrepended.current = true;
  });

  const consumeIntentAtTop = useCallback(() => {
    const el = scrollRef.current;
    if (!el || !loadIntentRef.current || el.scrollTop > 40) return;
    loadIntentRef.current = null;
    gestureScrollTopRef.current = null;
    void triggerLoadOlder();
  }, [triggerLoadOlder]);

  const registerIntent = useCallback((intent: LoadGesture) => {
    if (!hasOlder || loadingRef.current || !onLoadOlder) return;
    loadIntentRef.current = intent;
    gestureScrollTopRef.current = scrollRef.current?.scrollTop ?? null;
    consumeIntentAtTop();
  }, [consumeIntentAtTop, hasOlder, onLoadOlder]);

  const handleScroll = useCallback(() => {
    const intent = loadIntentRef.current;
    if (!intent) return;
    const currentTop = scrollRef.current?.scrollTop ?? Number.POSITIVE_INFINITY;
    const previousTop = gestureScrollTopRef.current;
    gestureScrollTopRef.current = currentTop;
    if (previousTop !== null && currentTop > previousTop) {
      loadIntentRef.current = null;
      gestureScrollTopRef.current = null;
      return;
    }
    const atTop = currentTop <= 40;
    if (atTop) {
      consumeIntentAtTop();
      return;
    }
  }, [consumeIntentAtTop]);

  const handleWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) {
      registerIntent("wheel");
      return;
    }
    if (event.deltaY > 0 && loadIntentRef.current === "wheel") {
      loadIntentRef.current = null;
      gestureScrollTopRef.current = null;
    }
  }, [registerIntent]);

  const handleTouchStart = useCallback((event: TouchEvent<HTMLDivElement>) => {
    touchYRef.current = event.touches[0]?.clientY ?? null;
  }, []);

  const handleTouchMove = useCallback((event: TouchEvent<HTMLDivElement>) => {
    const nextY = event.touches[0]?.clientY;
    const previousY = touchYRef.current;
    touchYRef.current = nextY ?? null;
    if (nextY === undefined || previousY === null) return;
    if (nextY > previousY) {
      registerIntent("touch");
      return;
    }
    if (nextY < previousY && loadIntentRef.current === "touch") {
      loadIntentRef.current = null;
      gestureScrollTopRef.current = null;
    }
  }, [registerIntent]);

  const handleTouchEnd = useCallback(() => {
    touchYRef.current = null;
  }, []);

  const handleTouchCancel = useCallback(() => {
    touchYRef.current = null;
    if (loadIntentRef.current === "touch") {
      loadIntentRef.current = null;
      gestureScrollTopRef.current = null;
    }
  }, []);

  const handlePointerDown = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "mouse") return;
    const el = event.currentTarget;
    const bounds = el.getBoundingClientRect();
    const scrollbarWidth = Math.max(0, bounds.width - el.clientWidth);
    if (scrollbarWidth === 0) return;
    const rtl = getComputedStyle(el).direction === "rtl";
    const onVerticalScrollbar = rtl
      ? event.clientX <= bounds.left + scrollbarWidth
      : event.clientX >= bounds.right - scrollbarWidth;
    if (!onVerticalScrollbar) return;
    pointerIdRef.current = event.pointerId;
    el.setPointerCapture?.(event.pointerId);
    registerIntent("pointer");
  }, [registerIntent]);

  const clearPointerGesture = useCallback(() => {
    pointerIdRef.current = null;
    if (loadIntentRef.current === "pointer") {
      loadIntentRef.current = null;
      gestureScrollTopRef.current = null;
    }
  }, []);

  const handlePointerUp = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (pointerIdRef.current !== null && event.pointerId !== pointerIdRef.current) return;
    clearPointerGesture();
  }, [clearPointerGesture]);

  useEffect(() => {
    const handleWindowPointerEnd = (event: globalThis.PointerEvent) => {
      if (pointerIdRef.current !== null && event.pointerId !== pointerIdRef.current) return;
      clearPointerGesture();
    };
    window.addEventListener("pointerup", handleWindowPointerEnd);
    window.addEventListener("pointercancel", handleWindowPointerEnd);
    window.addEventListener("blur", clearPointerGesture);
    return () => {
      window.removeEventListener("pointerup", handleWindowPointerEnd);
      window.removeEventListener("pointercancel", handleWindowPointerEnd);
      window.removeEventListener("blur", clearPointerGesture);
    };
  }, [clearPointerGesture]);

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (isInteractiveDescendant(event.target, event.currentTarget)) return;
    if (event.key === "ArrowUp" || event.key === "PageUp" || event.key === "Home") registerIntent("key");
  }, [registerIntent]);

  const handleKeyUp = useCallback(() => {
    if (loadIntentRef.current === "key") {
      loadIntentRef.current = null;
      gestureScrollTopRef.current = null;
    }
  }, []);

  const handleScrollEnd = useCallback(() => {
    if (loadIntentRef.current !== "wheel" && loadIntentRef.current !== "touch") return;
    loadIntentRef.current = null;
    gestureScrollTopRef.current = null;
  }, []);

  return {
    scrollRef,
    handleScroll,
    handleWheel,
    handleTouchStart,
    handleTouchMove,
    handleTouchEnd,
    handleTouchCancel,
    handlePointerDown,
    handlePointerUp,
    handleKeyDown,
    handleKeyUp,
    handleScrollEnd,
    triggerLoadOlder,
    loadingRef,
    justPrepended,
  } as const;
}
