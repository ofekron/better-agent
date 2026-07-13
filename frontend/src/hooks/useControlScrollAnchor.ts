import { useCallback, useEffect, useRef, type RefObject } from "react";

type Anchor = {
  control: HTMLElement;
  controlTop: number;
  scrollEl: HTMLElement;
  contentCommitted: boolean;
  layoutActive: boolean;
  layoutCompleted: boolean;
};

const clampScrollTop = (scrollEl: HTMLElement, value: number) =>
  Math.min(Math.max(value, 0), Math.max(scrollEl.scrollHeight - scrollEl.clientHeight, 0));

export function useControlScrollAnchor(
  scrollElProp: HTMLElement | null | undefined,
  ownerRef: RefObject<HTMLElement | null>,
  findScrollParent: (element: HTMLElement) => HTMLElement | null,
) {
  const anchorRef = useRef<Anchor | null>(null);
  const observerRef = useRef<ResizeObserver | null>(null);

  const stop = useCallback(() => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    anchorRef.current = null;
  }, []);

  const correct = useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor || !anchor.control.isConnected || !anchor.scrollEl.isConnected) {
      stop();
      return;
    }
    const nextControlTop = anchor.control.getBoundingClientRect().top;
    const desired = anchor.scrollEl.scrollTop + nextControlTop - anchor.controlTop;
    anchor.scrollEl.scrollTop = clampScrollTop(anchor.scrollEl, desired);
  }, [stop]);

  const finishIfSettled = useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor?.contentCommitted || anchor.layoutActive || !anchor.layoutCompleted) return;
    correct();
    stop();
  }, [correct, stop]);

  const contentCommitted = useCallback(() => {
    if (!anchorRef.current) return;
    anchorRef.current.contentCommitted = true;
    // A completion from the loading/previous layout cannot settle the
    // terminal content. Only the owning animation after this commit can.
    anchorRef.current.layoutCompleted = false;
    correct();
  }, [correct]);

  const layoutAnimationStarted = useCallback(() => {
    if (!anchorRef.current) return;
    anchorRef.current.layoutActive = true;
    anchorRef.current.layoutCompleted = false;
  }, []);

  const layoutAnimationCompleted = useCallback(() => {
    if (!anchorRef.current) return;
    anchorRef.current.layoutActive = false;
    anchorRef.current.layoutCompleted = true;
    correct();
    finishIfSettled();
  }, [correct, finishIfSettled]);

  const capture = useCallback((control: HTMLElement) => {
    stop();
    const owner = ownerRef.current;
    const scrollEl = scrollElProp ?? (owner ? findScrollParent(owner) : null);
    if (!scrollEl) return;
    anchorRef.current = {
      control,
      controlTop: control.getBoundingClientRect().top,
      scrollEl,
      contentCommitted: false,
      layoutActive: false,
      layoutCompleted: false,
    };
  }, [findScrollParent, ownerRef, scrollElProp, stop]);

  const stabilize = useCallback((region: HTMLElement | null) => {
    if (!anchorRef.current) return;
    correct();
    if (!region || typeof ResizeObserver === "undefined") {
      return;
    }
    observerRef.current?.disconnect();
    const observer = new ResizeObserver(() => {
      correct();
    });
    observerRef.current = observer;
    observer.observe(region);
  }, [correct]);

  useEffect(() => stop, [stop]);

  return { capture, contentCommitted, layoutAnimationCompleted, layoutAnimationStarted, stabilize };
}
