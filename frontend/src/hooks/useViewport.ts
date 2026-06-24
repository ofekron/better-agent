import { useEffect, useState } from "react";

/**
 * Breakpoints. Width-only — no `pointer:` clause, because iPad Pro with
 * Magic Keyboard reports `pointer: fine` and would be misclassed as
 * desktop. Hover/touch concerns live in CSS via
 * `@media (hover: hover) and (pointer: fine)` for show-on-hover rules.
 */
export const BP_MOBILE = 480;
export const BP_TABLET = 1024;

export type ViewportMode = "mobile" | "tablet" | "desktop";

interface Viewport {
  width: number;
  height: number;
  mode: ViewportMode;
}

function read(): Viewport {
  const w = window.innerWidth;
  const h = window.innerHeight;
  const mode: ViewportMode =
    w <= BP_MOBILE ? "mobile" : w <= BP_TABLET ? "tablet" : "desktop";
  return { width: w, height: h, mode };
}

/**
 * Single source of truth for the responsive layout mode. The mode is
 * width-driven so the same device gets the same shell regardless of
 * pointer attachment.
 *
 * Mounts a single `resize` listener at the window level — cheap and
 * sufficient because layout decisions are not per-component.
 */
export function useViewport(): Viewport {
  const [vp, setVp] = useState<Viewport>(() => read());

  useEffect(() => {
    const onResize = () => setVp(read());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return vp;
}
