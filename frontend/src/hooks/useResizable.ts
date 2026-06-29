import { useState, useRef, useCallback, useEffect, useLayoutEffect } from "react";

type Axis = "x" | "y";
/**
 * "forward"  — dragging right/down increases size (left-anchored / top-anchored panels)
 * "reverse"  — dragging left/up increases size (right-anchored / bottom-anchored panels)
 */
type Direction = "forward" | "reverse";

interface Options {
  storageKey: string;
  defaultSize: number;
  min: number;
  max: number;
  axis: Axis;
  direction?: Direction;
  /**
   * When false, `onMouseDown` is a no-op so resizers in the DOM are
   * inert. The `size` value (and its persistence) are unchanged so the
   * resizable width is restored automatically when the resizer is
   * re-enabled (e.g. resizing the window back from mobile to desktop).
   */
  enabled?: boolean;
}

function clampSize(size: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, size));
}

function readStoredSize(
  storageKey: string,
  defaultSize: number,
  min: number,
  max: number,
): number {
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored != null) {
      const n = Number(stored);
      if (Number.isFinite(n)) return clampSize(n, min, max);
    }
  } catch {
    // localStorage unavailable (private mode, SSR) — fall through
  }
  return clampSize(defaultSize, min, max);
}

/**
 * Pointer-drag resizer with localStorage persistence. Returns a `size`
 * (in px) and an `onMouseDown` to attach to a divider element. Reads
 * the stored size synchronously on first render so the layout renders
 * correctly on the first paint (no flash from default → persisted).
 */
export function useResizable({
  storageKey,
  defaultSize,
  min,
  max,
  axis,
  direction = "forward",
  enabled = true,
}: Options) {
  const [size, setSize] = useState<number>(() =>
    readStoredSize(storageKey, defaultSize, min, max)
  );

  // `size` read via a ref so `onMouseDown` doesn't re-create on every
  // drag tick (which was happening before because `size` was in the deps).
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const storageKeyRef = useRef(storageKey);

  useLayoutEffect(() => {
    if (storageKeyRef.current !== storageKey) {
      storageKeyRef.current = storageKey;
      setSize(readStoredSize(storageKey, defaultSize, min, max));
      return;
    }
    setSize((current) => clampSize(current, min, max));
  }, [defaultSize, min, max, storageKey]);

  useEffect(() => {
    try {
      localStorage.setItem(storageKey, String(size));
    } catch {
      // ignore quota/availability errors
    }
  }, [storageKey, size]);

  const draggingRef = useRef(false);
  const startPosRef = useRef(0);
  const startSizeRef = useRef(0);

  const beginDrag = useCallback(
    (
      clientX: number,
      clientY: number,
      preventDefault: () => void,
      addListeners: (onMove: (ev: { clientX: number; clientY: number }) => void, onUp: () => void) => void,
    ) => {
      if (!enabled) return;
      preventDefault();
      draggingRef.current = true;
      startPosRef.current = axis === "x" ? clientX : clientY;
      startSizeRef.current = sizeRef.current;
      document.body.style.cursor = axis === "x" ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";

      const onMove = (ev: { clientX: number; clientY: number }) => {
        if (!draggingRef.current) return;
        const pos = axis === "x" ? ev.clientX : ev.clientY;
        const rawDelta =
          direction === "forward"
            ? pos - startPosRef.current
            : startPosRef.current - pos;
        const next = clampSize(startSizeRef.current + rawDelta, min, max);
        setSize(next);
      };
      const onUp = () => {
        draggingRef.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };

      addListeners(onMove, onUp);
    },
    [axis, direction, max, min, enabled]
  );

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      beginDrag(e.clientX, e.clientY, () => e.preventDefault(), (onMove, onUp) => {
        const stop = () => {
          onUp();
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", stop);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", stop);
      });
    },
    [beginDrag]
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      beginDrag(e.clientX, e.clientY, () => e.preventDefault(), (onMove, onUp) => {
        const stop = () => {
          onUp();
          document.removeEventListener("pointermove", onMove);
          document.removeEventListener("pointerup", stop);
          document.removeEventListener("pointercancel", stop);
        };
        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", stop);
        document.addEventListener("pointercancel", stop);
      });
    },
    [beginDrag]
  );

  return { size, onMouseDown, onPointerDown };
}
