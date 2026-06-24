import { useLayoutEffect, useRef, type RefObject } from "react";

const MOVE_ANIMATION_MS = 180;
const MOVE_ANIMATION_EASING = "cubic-bezier(0.2, 0, 0, 1)";

export function useAnimatedTabMovement<T extends HTMLElement>(
  itemKeys: readonly string[],
): RefObject<T | null> {
  const containerRef = useRef<T>(null);
  const previousRectsRef = useRef<Map<string, DOMRect>>(new Map());
  const itemKeySignature = itemKeys.join("\u0000");

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const nextRects = new Map<string, DOMRect>();
    const elements = Array.from(
      container.querySelectorAll<HTMLElement>("[data-tab-movement-key]"),
    );

    const shouldAnimate =
      typeof window !== "undefined"
      && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    for (const element of elements) {
      const key = element.dataset.tabMovementKey;
      if (!key) continue;

      const nextRect = element.getBoundingClientRect();
      nextRects.set(key, nextRect);

      if (!shouldAnimate) continue;

      const previousRect = previousRectsRef.current.get(key);
      if (!previousRect) continue;

      const deltaX = previousRect.left - nextRect.left;
      const deltaY = previousRect.top - nextRect.top;
      if (deltaX === 0 && deltaY === 0) continue;

      element.animate(
        [
          { transform: `translate(${deltaX}px, ${deltaY}px)` },
          { transform: "translate(0, 0)" },
        ],
        {
          duration: MOVE_ANIMATION_MS,
          easing: MOVE_ANIMATION_EASING,
        },
      );
    }

    previousRectsRef.current = nextRects;
  }, [itemKeySignature]);

  return containerRef;
}
