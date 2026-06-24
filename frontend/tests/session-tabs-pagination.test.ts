import { afterEach, describe, expect, it } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

async function waitFor(
  h: Awaited<ReturnType<typeof renderApp>>,
  predicate: () => boolean,
) {
  for (let i = 0; i < 20; i++) {
    if (predicate()) return true;
    await h.flush();
  }
  return false;
}

describe("session tabs with paged sessions", () => {
  afterEach(() => {
    localStorage.removeItem("better-agent-open-session-ids");
    window.history.pushState(null, "", "/");
  });

  it("hydrates saved tab sessions that are outside the first session page", async () => {
    const sessions = Array.from({ length: 60 }, (_, i) =>
      makeSession({
        id: `sess-${i + 1}`,
        name: `Session ${i + 1}`,
        cwd: i === 59 ? "/tmp/project-b" : "/tmp/project-a",
      }),
    );
    window.history.pushState(null, "", "/s/sess-1");
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(["sess-60", "sess-1"]),
    );
    const h = await renderApp({ seed: { sessions } });

    expect(
      await waitFor(h, () =>
        h.backend.calls.some(
          (call) => call.method === "GET" && call.path === "/api/sessions/sess-60",
        ),
      ),
    ).toBe(true);
    expect(
      await waitFor(
        h,
        () => h.$(".session-tabs")?.textContent?.includes("Session 60") === true,
      ),
    ).toBe(true);

    await h.clickByText(/Session 60/);

    expect(window.location.pathname).toBe("/s/sess-60");
    h.unmount();
  });
});
