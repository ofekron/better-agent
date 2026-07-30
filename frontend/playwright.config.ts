import { defineConfig, devices } from "@playwright/test";

// Full-stack integration tests: each spec spawns its own real backend
// subprocess (frontend/tests/fullstack/harness/backend.ts) in its own
// isolated home on its own port, so specs are independent and safe to run
// concurrently. Each real provider-CLI turn is slow (tens of seconds), so
// parallelism matters for wall-clock time; capped well under core count to
// leave headroom for the provider subprocesses each worker's backend spawns.
export default defineConfig({
  testDir: "./tests/fullstack",
  timeout: 240_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  workers: 3,
  // This machine runs other concurrent agent sessions outside this suite's
  // control, so a single flaky-looking failure can be real contention
  // (backend health-check timeout) rather than a logic bug — one retry
  // absorbs that without masking a genuinely deterministic failure (which
  // will fail the retry too).
  retries: 1,
  reporter: [["list"]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
