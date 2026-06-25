import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

// At most one artifact rebuild may be scheduled per this window.
export const THROTTLE_WINDOW_MS = 10 * 60 * 1000;

// Epoch-ms of the last scheduled rebuild, or 0 if none/unreadable-as-number.
export function lastRunAt(stampPath) {
  if (!existsSync(stampPath)) {
    return 0;
  }
  const ts = Number.parseInt(readFileSync(stampPath, "utf8").trim(), 10);
  return Number.isFinite(ts) ? ts : 0;
}

export function isThrottled(stampPath, now, windowMs = THROTTLE_WINDOW_MS) {
  return now - lastRunAt(stampPath) < windowMs;
}

export function recordRun(stampPath, now) {
  mkdirSync(dirname(stampPath), { recursive: true });
  writeFileSync(stampPath, String(now));
}
