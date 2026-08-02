import { test, expect } from "./harness/fixtures";
import { startForeignOriginSpa } from "./harness/foreign-spa";

test("distinguishes browser-blocked backend access from failed reachability", async ({
  backend,
  page,
}) => {
  const spa = await startForeignOriginSpa(backend.baseURL, new Set([backend.port]));
  try {
    await page.goto(spa.baseURL);

    const browserBoundary = await page.evaluate(async (baseURL) => {
      let ordinaryFailure = "";
      try {
        await fetch(`${baseURL}/api/auth/me`, { credentials: "include" });
      } catch (error) {
        ordinaryFailure = error instanceof Error ? error.name : String(error);
      }
      const probe = await fetch(`${baseURL}/healthz`, {
        cache: "no-store",
        credentials: "omit",
        method: "GET",
        mode: "no-cors",
      });
      return { ordinaryFailure, probeType: probe.type };
    }, backend.baseURL);
    expect(browserBoundary).toEqual({ ordinaryFailure: "TypeError", probeType: "opaque" });
    await expect(page.getByText("The server responded, but this browser could not access it.")).toBeVisible();

    await backend.stop();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByText("Could not reach the backend from this browser.")).toBeVisible({
      timeout: 20_000,
    });
  } finally {
    await spa.stop();
  }
});
