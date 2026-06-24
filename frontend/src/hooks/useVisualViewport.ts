import { useEffect } from "react";

/**
 * Writes a CSS variable `--vv-offset` on `document.documentElement` that
 * equals the distance from the bottom of the layout viewport to the
 * bottom of the visible (visualViewport-aware) area — i.e. how much
 * vertical space is occupied by an on-screen virtual keyboard.
 *
 * The composer / sticky bottom UI consumes `var(--vv-offset, 0px)` to
 * stay above the virtual keyboard on mobile browsers.
 *
 * Pass `enabled=false` on desktop so the listener isn't attached for
 * users that never see a virtual keyboard.
 */
export function useVisualViewport(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) {
      document.documentElement.style.removeProperty("--vv-offset");
      return;
    }
    const vv = window.visualViewport;
    if (!vv) return;

    const update = () => {
      const offset = Math.max(
        0,
        window.innerHeight - vv.height - vv.offsetTop
      );
      document.documentElement.style.setProperty(
        "--vv-offset",
        `${offset}px`
      );
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      document.documentElement.style.removeProperty("--vv-offset");
    };
  }, [enabled]);
}
