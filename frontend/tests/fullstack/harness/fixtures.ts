import { test as base, expect } from "@playwright/test";
import { startFullStackBackend, type FullStackBackend } from "./backend";
import { loginAndSkipFirstRunWizard } from "./login";

interface FullStackFixtures {
  /** A freshly spawned, isolated real backend (random headless credentials, real subprocess). */
  backend: FullStackBackend;
  /** `page` already navigated + logged in via the real Login.tsx form. */
  authedPage: import("@playwright/test").Page;
}

export const test = base.extend<FullStackFixtures>({
  backend: async ({}, use) => {
    const backend = await startFullStackBackend();
    try {
      await use(backend);
    } finally {
      await backend.stop();
    }
  },
  authedPage: async ({ page, backend }, use) => {
    await loginAndSkipFirstRunWizard(page, backend);
    await use(page);
  },
});

export { expect };
export type { FullStackBackend };
