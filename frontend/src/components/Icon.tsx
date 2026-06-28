/** Themed SVG icons — all use `currentColor` so they inherit CSS color. */

import { type CSSProperties } from "react";

export type IconName =
  | "clipboard"
  | "memo"
  | "pin"
  | "check-circle"
  | "search"
  | "paperclip"
  | "folder"
  | "folder-plus"
  | "tag"
  | "clock"
  | "sparkles"
  | "swords"
  | "rewind"
  | "palette"
  | "chat"
  | "chart"
  | "archive"
  | "trash"
  | "edit"
  | "expand"
  | "mic"
  | "chevron-right"
  | "chevron-down"
  | "chevron-up"
  | "chevron-left"
  | "menu"
  | "x"
  | "check"
  | "refresh"
  | "settings"
  | "warning"
  | "target"
  | "x-circle"
  | "balance"
  | "sliders"
  | "film"
  | "star"
  | "arrow-up"
  | "home"
  | "server"
  | "more-vertical"
  | "circle"
  | "info"
  | "testape";

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
  style?: CSSProperties;
}

const PATHS: Record<IconName, string> = {
  clipboard:
    "M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2M9 2h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z",
  memo: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8",
  pin: "M12 17v5M9 3h6l1 7a4 4 0 0 1-8 0zM7 10h10",
  "check-circle":
    "M22 11.08V12a10 10 0 1 1-5.93-9.14M22 4 12 14.01l-3-3",
  search:
    "M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM21 21l-4.35-4.35",
  paperclip:
    "M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48",
  folder:
    "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
  "folder-plus":
    "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2zM12 11v6M9 14h6",
  tag: "M20.59 13.41 13.41 20.59a2 2 0 0 1-2.82 0L3 13V3h10l7.59 7.59a2 2 0 0 1 0 2.82zM7 7h.01",
  clock: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 6v6l4 2",
  sparkles:
    "M12 3l1.912 5.813L20 10l-6.088 1.187L12 17l-1.912-5.813L4 10l6.088-1.187zM18 18l.94 2.06L21 21l-2.06.94L18 24l-.94-2.06L15 21l2.06-.94z",
  swords:
    "M14.5 17.5L3 6V3h3l11.5 11.5M13 19l6-6M16 16l4 4M19 21l-4-4M10 11L4.5 5.5M7 8L3.5 4.5",
  rewind: "M1 4v16l8-8zM9 4v16l8-8z",
  palette:
    "M12 2a10 10 0 0 0-1 19.95c.65.05 1.15-.48 1.15-1.14v-.01c0-.6-.47-1.09-1.05-1.19A7 7 0 0 1 5 13c0-1.9.74-3.62 1.95-4.9A7.03 7.03 0 0 1 12 6c3.87 0 7 3.13 7 7a7.03 7.03 0 0 1-1.95 4.9c-.33.34-.33.89.05 1.22.4.34 1 .28 1.34-.08A9 9 0 0 0 12 2z",
  chat: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  chart: "M3 21h18M6 21V12M12 21V5M18 21V15",
  archive:
    "M21 8v13H3V8M1 3h22v5H1zM10 12h4",
  trash:
    "M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
  edit: "M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z",
  expand:
    "M15 3h6v6M21 3l-7 7M9 21H3v-6M3 21l7-7M21 15v6h-6M21 21l-7-7M3 9V3h6M3 3l7 7",
  "chevron-right": "m9 18 6-6-6-6",
  "chevron-down": "m6 9 6 6 6-6",
  "chevron-up": "m18 15-6-6-6 6",
  "chevron-left": "m15 18-6-6 6-6",
  menu: "M4 6h16M4 12h16M4 18h16",
  x: "M18 6 6 18M6 6l12 12",
  check: "M20 6 9 17l-5-5",
  refresh:
    "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8M21 3v5h-5M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16M3 21v-5h5",
  settings:
    "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  warning:
    "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3zM12 9v4M12 17h.01",
  target:
    "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  "x-circle": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM15 9l-6 6M9 9l6 6",
  balance:
    "M12 3v18M5 7h14M7 7 3.5 14a3.5 3.5 0 0 0 7 0L7 7M17 7l-3.5 7a3.5 3.5 0 0 0 7 0L17 7M8 21h8",
  sliders:
    "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  film: "M3 3h18v18H3zM3 9h18M3 15h18M9 3v18M15 3v18",
  star:
    "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z",
  "arrow-up": "m5 12 7-7 7 7M12 19V5",
  home: "m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM9 22V12h6v10",
  server: "M2 3h20v6H2zM2 15h20v6H2zM6 6h.01M6 18h.01",
  "more-vertical": "M12 5v.01M12 12v.01M12 19v.01",
  circle: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
  mic: "M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2M12 19v3M8 22h8",
  testape:
    "M5 10a3 3 0 1 0 0 6 M19 10a3 3 0 1 1 0 6 M12 6a6 6 0 0 0-6 6c0 3.3 2.7 6 6 6s6-2.7 6-6a6 6 0 0 0-6-6z M10 11h.01 M14 11h.01 M11 14h2 M10 16a2 2 0 0 0 4 0",
};

/** Known icon names — the single source extensions validate their manifest
 *  `icon` string against (rendered as a letter fallback when unknown). */
export const ICON_NAMES: readonly string[] = Object.keys(PATHS);

export default function Icon({ name, size = 16, className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={style}
      aria-hidden
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
