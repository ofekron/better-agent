#!/usr/bin/env node
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  THROTTLE_WINDOW_MS,
  isThrottled,
  lastRunAt,
  recordRun,
} from "./artifact-throttle.mjs";

function fail(message) {
  throw new Error(message);
}

const dir = mkdtempSync(join(tmpdir(), "bc-artifact-throttle-"));
const stampPath = join(dir, "nested", "last-scheduled");

try {
  const t0 = 1_000_000_000_000;

  // No prior run → not throttled.
  if (lastRunAt(stampPath) !== 0) fail("expected lastRunAt=0 when no stamp exists");
  if (isThrottled(stampPath, t0)) fail("expected first run not to be throttled");

  // Record a run; a second attempt inside the window is throttled.
  recordRun(stampPath, t0);
  if (lastRunAt(stampPath) !== t0) fail("expected stamp to persist the recorded time");
  if (!isThrottled(stampPath, t0 + 1)) fail("expected throttling 1ms after a run");
  if (!isThrottled(stampPath, t0 + THROTTLE_WINDOW_MS - 1)) {
    fail("expected throttling just before the window closes");
  }

  // At/after the window boundary it is allowed again.
  if (isThrottled(stampPath, t0 + THROTTLE_WINDOW_MS)) {
    fail("expected no throttling exactly at the window boundary");
  }
  if (isThrottled(stampPath, t0 + THROTTLE_WINDOW_MS + 1)) {
    fail("expected no throttling after the window");
  }

  console.log("OK test_artifact_throttle");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
