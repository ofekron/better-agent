import type { CSSProperties } from "react";

export function mobileRightPanelSizingStyle(size: number): CSSProperties {
  return {
    height: size,
    minHeight: size,
    maxHeight: size,
    flex: `0 0 ${size}px`,
  };
}
