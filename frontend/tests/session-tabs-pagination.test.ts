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
      await waitFor(
        h,
        () => h.$(".session-tabs")?.textContent?.includes("Session 60") === true,
      ),
    ).toBe(true);

    await h.clickByText(/Session 60/);

    expect(window.location.pathname).toBe("/s/sess-60");
    h.unmount();
  }, 15000);

  it("hides open-session tabs for the Assistant session", async () => {
    const assistant = makeSession({
      id: "assistant-session",
      name: "Assistant",
      cwd: "/tmp/project-a",
    });
    const work = makeSession({
      id: "work-session",
      name: "Work",
      cwd: "/tmp/project-a",
    });
    const h = await renderApp({ seed: { sessions: [assistant, work] } });

    await h.selectSession(work.id);
    expect(h.$(".session-tabs")?.textContent).not.toContain("Work");

    await h.selectSession(assistant.id);
    expect(h.$(".session-tabs")).toBeNull();

    h.unmount();
  }, 10000);

  it("hides the selected session and keeps switched-away tab content live", async () => {
    const first = makeSession({
      id: "first-session",
      name: "First live name",
      model: "old-model",
      cwd: "/tmp/project-a",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    const second = makeSession({
      id: "second-session",
      name: "Second live name",
      cwd: "/tmp/project-a",
      updated_at: "2026-01-02T00:00:00.000Z",
    });
    const h = await renderApp({ seed: { sessions: [second, first] } });

    await h.selectSession(first.id);
    expect(
      await waitFor(h, () =>
        h.outbound.some(
          (frame) => frame.type === "subscribe" && frame.app_session_id === first.id,
        ),
      ),
    ).toBe(true);
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: first.id,
        patch: {
          model: "new-model",
          updated_at: "2026-01-03T00:00:00.000Z",
        },
        originated_by: "OTHER_TAB",
      },
    });
    await h.flush();

    await h.selectSession(second.id);

    expect(
      await waitFor(h, () => {
        const tabsText = h.$(".session-tabs")?.textContent ?? "";
        return (
          tabsText.includes("First live name") &&
          tabsText.includes("new-model") &&
          !tabsText.includes("Second live name")
        );
      }),
    ).toBe(true);
    h.unmount();
  }, 10000);
});
