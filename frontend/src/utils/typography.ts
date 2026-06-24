import { useEffect, useState } from "react";

export const DEFAULT_FONT_SCALE = 1;
export const DEFAULT_APP_FONT_SIZE = 14;

export function fontScaleForSize(fontSize: number): number {
  return fontSize / DEFAULT_APP_FONT_SIZE;
}

export function scaledFontSize(sizePx: number): string {
  return `calc(${sizePx}px * var(--app-font-scale, ${DEFAULT_FONT_SCALE}))`;
}

export function currentFontScale(): number {
  if (typeof window === "undefined") return DEFAULT_FONT_SCALE;
  const raw = window
    .getComputedStyle(document.documentElement)
    .getPropertyValue("--app-font-scale")
    .trim();
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_FONT_SCALE;
}

export function useScaledMonacoFontSize(basePx: number): number {
  const [scale, setScale] = useState(currentFontScale);

  useEffect(() => {
    const update = () => setScale(currentFontScale());
    update();
    window.addEventListener("appearance_prefs_changed", update);
    return () => window.removeEventListener("appearance_prefs_changed", update);
  }, []);

  return Math.round(basePx * scale * 100) / 100;
}
