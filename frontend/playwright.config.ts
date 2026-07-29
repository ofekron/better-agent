import { defineConfig, devices } from "@playwright/test";

// Full-stack integration tests: each spec spawns its own real backend
// subprocess (frontend/tests/fullstack/harness/backend.ts) in its own
// isolated home on its own port, so specs are independent and safe to run
// concurrently. Each real provider-CLI turn is slow (tens of seconds), so
// parallelism matters for wall-clock time; capped well under core count to
// leave headroom for the provider subprocesses each worker's backend spawns.
export default defineConfig({
  testDir: "./tests/fullstack",
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  workers: 4,
  retries: 0,
  reporter: [["list"]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
