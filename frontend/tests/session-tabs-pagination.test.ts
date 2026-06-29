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
    expect(
      h.restCalls.filter((c) => c.method === "GET" && c.path === "/api/sessions"),
    ).toHaveLength(1);
    expect(
      h.restCalls.some(
        (c) => c.method === "GET" && c.path === "/api/sessions/sess-60",
      ),
    ).toBe(false);
    expect(
      h.restCalls.some(
        (c) => c.method === "GET" && c.path === "/api/sessions/summaries",
      ),
    ).toBe(true);

    await h.clickByText(/Session 60/);

    expect(window.location.pathname).toBe("/s/sess-60");
    expect(
      h.restCalls.filter(
        (c) => c.method === "GET" && c.path === "/api/sessions/sess-60",
      ),
    ).toHaveLength(1);
    h.unmount();
  }, 15000);

  it("keeps session tabs visible for active and Assistant sessions", async () => {
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
    expect(h.$(".session-tabs")?.textContent ?? "").toContain("Work");

    await h.selectSession(assistant.id);
    expect(h.$(".session-tabs")?.textContent ?? "").toContain("Assistant");

    h.unmount();
  }, 10000);

  it("does not cap saved open session tabs", async () => {
    const sessions = Array.from({ length: 20 }, (_, i) =>
      makeSession({
        id: `sess-${i + 1}`,
        name: `Session ${i + 1}`,
        cwd: "/tmp/project-a",
      }),
    );
    window.history.pushState(null, "", "/s/sess-1");
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(sessions.map((session) => session.id)),
    );
    const h = await renderApp({ seed: { sessions } });

    expect(
      await waitFor(h, () => h.$$(".session-tab-wrapper").length === 20),
    ).toBe(true);
    expect(h.$(".session-tabs")?.textContent ?? "").toContain("Session 20");
    h.unmount();
  }, 10000);

  it("fetches session stats only when the stats popover opens", async () => {
    const session = makeSession({
      id: "stats-session",
      name: "Stats",
      cwd: "/tmp/project-a",
      token_usage_total: {
        input_tokens: 1,
        output_tokens: 2,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
      },
    });
    const h = await renderApp({ seed: { sessions: [session] } });

    expect(
      h.restCalls.some(
        (c) => c.method === "GET" && c.path === "/api/sessions/stats-session/stats",
      ),
    ).toBe(false);

    await h.click('[data-testid="session-item"][data-session-id="stats-session"] .session-item-tag-control');
    await h.flush();

    expect(
      h.restCalls.some(
        (c) => c.method === "GET" && c.path === "/api/sessions/stats-session/stats",
      ),
    ).toBe(true);
    expect(
      await waitFor(
        h,
        () => h.restCalls.some(
          (c) => c.method === "GET" && c.path === "/api/sessions/stats-session",
        ),
      ),
    ).toBe(true);
    h.unmount();
  }, 10000);

  it("keeps selected and switched-away tab content live", async () => {
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
          tabsText.includes("Second live name")
        );
      }),
    ).toBe(true);
    h.unmount();
  }, 10000);

  it("closes other tabs relative to a tab", async () => {
    const sessions = ["One", "Two", "Three"].map((name, i) =>
      makeSession({
        id: `sess-${i + 1}`,
        name,
        cwd: "/tmp/project-a",
      }),
    );
    window.history.pushState(null, "", "/s/sess-1");
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(sessions.map((session) => session.id)),
    );
    const h = await renderApp({ seed: { sessions } });

    expect(await waitFor(h, () => h.$$(".session-tab-wrapper").length === 3)).toBe(true);
    await h.click('[data-tab-movement-key="sess-2"] .session-tab-close-others');

    expect(await waitFor(h, () => h.$$(".session-tab-wrapper").length === 1)).toBe(true);
    expect(h.$(".session-tabs")?.textContent ?? "").toContain("Two");
    expect(window.location.pathname).toBe("/s/sess-2");
    expect(JSON.parse(localStorage.getItem("better-agent-open-session-ids") || "[]"))
      .toEqual(["sess-2"]);
    h.unmount();
  }, 10000);

  it("registers a newly created session in open tabs immediately", async () => {
    const existing = makeSession({
      id: "existing-session",
      name: "Existing session",
      cwd: "/tmp/project-a",
    });
    const h = await renderApp({
      seed: {
        sessions: [existing],
        projects: [{
          path: "/tmp/project-a",
          name: "project-a",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });

    await h.selectSession(existing.id);
    await h.clickByText(/^(\+ New|session\.newButton)$/);
    await h.click(".modal-footer .btn-primary");

    expect(JSON.parse(localStorage.getItem("better-agent-open-session-ids") || "[]"))
      .toContain("sess-2");

    await h.selectSession(existing.id);

    expect(
      await waitFor(
        h,
        () => h.$(".session-tabs")?.textContent?.includes("New Session") === true,
      ),
    ).toBe(true);
    h.unmount();
  }, 10000);
});
