import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { setSelectedProject } from "../src/utils/uiSelection";

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

function tabIds(h: Awaited<ReturnType<typeof renderApp>>): string[] {
  return h.$$(".session-tab-wrapper")
    .map((el) => el.getAttribute("data-tab-movement-key") ?? "")
    .filter(Boolean);
}

describe("session tabs with paged sessions", () => {
  afterEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("{}", { status: 200 }))),
    );
    setSelectedProject("", "primary");
    vi.unstubAllGlobals();
    localStorage.removeItem("better-agent-open-session-ids");
    localStorage.removeItem("better-agent-selected-project");
    localStorage.removeItem("better-agent-selected-project-node");
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

  it("retries restored tab summaries after a transient startup miss", async () => {
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
    const h = await renderApp({
      seed: {
        sessions,
        summaryMissOnceIds: ["sess-60"],
      },
    });

    h.emit({
      type: "session_created",
      data: {
        session: makeSession({
          id: "trigger-session",
          name: "Trigger",
          cwd: "/tmp/project-a",
        }),
      },
    });
    await h.flush();

    expect(
      await waitFor(
        h,
        () => h.$(".session-tabs")?.textContent?.includes("Session 60") === true,
      ),
    ).toBe(true);
    expect(
      h.restCalls.filter((c) => c.method === "GET" && c.path === "/api/sessions/summaries").length,
    ).toBeGreaterThanOrEqual(2);
    h.unmount();
  }, 15000);

  it("drops restored tab ids that keep missing from summaries", async () => {
    const session = makeSession({
      id: "existing-session",
      name: "Existing",
      cwd: "/tmp/project-a",
    });
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(["missing-session"]),
    );
    const h = await renderApp({ seed: { sessions: [session] } });

    expect(
      await waitFor(
        h,
        () => h.restCalls.some(
          (c) => c.method === "GET" && c.path === "/api/sessions/summaries",
        ),
      ),
    ).toBe(true);
    await h.flush();

    h.emit({
      type: "session_created",
      data: {
        session: makeSession({
          id: "trigger-session",
          name: "Trigger",
          cwd: "/tmp/project-a",
        }),
      },
    });
    await h.flush();

    expect(
      await waitFor(
        h,
        () => !JSON.parse(
          localStorage.getItem("better-agent-open-session-ids") || "[]",
        ).includes("missing-session"),
      ),
    ).toBe(true);
    const summaryCalls = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/sessions/summaries",
    ).length;

    h.emit({
      type: "session_created",
      data: {
        session: makeSession({
          id: "second-trigger-session",
          name: "Second Trigger",
          cwd: "/tmp/project-a",
        }),
      },
    });
    await h.flush();

    expect(
      h.restCalls.filter(
        (c) => c.method === "GET" && c.path === "/api/sessions/summaries",
      ),
    ).toHaveLength(summaryCalls);
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

  it("reselects inside the selected project when the route is on another project tab", async () => {
    const projectSession = makeSession({
      id: "project-session",
      name: "Project Session",
      cwd: "/tmp/project-a",
      updated_at: "2026-01-02T00:00:00.000Z",
    });
    const otherProjectTab = makeSession({
      id: "other-project-tab",
      name: "Other Project Tab",
      cwd: "/tmp/project-b",
      updated_at: "2026-01-03T00:00:00.000Z",
    });
    window.history.pushState(null, "", "/s/other-project-tab");
    localStorage.setItem("better-agent-selected-project", "/tmp/project-a");
    localStorage.setItem("better-agent-selected-project-node", "primary");

    const h = await renderApp({
      seed: {
        sessions: [otherProjectTab, projectSession],
        projects: [
          {
            path: "/tmp/project-a",
            name: "project-a",
            created_at: "2026-01-01T00:00:00.000Z",
            last_used: "2026-01-01T00:00:00.000Z",
          },
          {
            path: "/tmp/project-b",
            name: "project-b",
            created_at: "2026-01-01T00:00:00.000Z",
            last_used: "2026-01-01T00:00:00.000Z",
          },
        ],
      },
    });

    expect(await waitFor(h, () => window.location.pathname === "/s/project-session"))
      .toBe(true);
    expect(
      h.$('[data-session-id="project-session"]')?.getAttribute("data-active"),
    ).toBe("true");
    h.unmount();
  }, 10000);

  it("hides session tabs when the user preference is off", async () => {
    const session = makeSession({
      id: "work-session",
      name: "Work",
      cwd: "/tmp/project-a",
    });
    const h = await renderApp({ seed: { sessions: [session] } });

    await h.selectSession(session.id);
    expect(h.$(".session-tabs")?.textContent ?? "").toContain("Work");

    h.emit({
      type: "user_prefs_changed",
      data: {
        sessions_tabs_visible: false,
      },
    });
    await h.flush();

    expect(h.$(".session-tabs")).toBeNull();
    h.unmount();
  }, 10000);

  it("ignores the removed session-tabs status sort preference", async () => {
    const oldRunning = makeSession({
      id: "old-running",
      name: "Old Running",
      cwd: "/tmp/project-a",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    const newIdle = makeSession({
      id: "new-idle",
      name: "New Idle",
      cwd: "/tmp/project-a",
      updated_at: "2026-01-02T00:00:00.000Z",
    });
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify([oldRunning.id, newIdle.id]),
    );
    const h = await renderApp({ seed: { sessions: [oldRunning, newIdle] } });

    expect(
      await waitFor(
        h,
        () =>
          h.$$(".session-tab-wrapper").map((el) => el.dataset.tabMovementKey).join(",") ===
          "new-idle,old-running",
      ),
    ).toBe(true);

    h.emit({
      type: "user_prefs_changed",
      data: {
        sessions_tabs_status_sort: true,
      },
    });
    await h.flush();
    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: oldRunning.id,
        cwd: oldRunning.cwd,
        node_id: "primary",
        monitoring_state: "active",
      },
    });
    await h.flush();

    expect(h.$$(".session-tab-wrapper").map((el) => el.dataset.tabMovementKey)).toEqual([
      "new-idle",
      "old-running",
    ]);
    h.unmount();
  }, 10000);

  it("shows open session tabs when no session is selected", async () => {
    const sessions = ["One", "Two"].map((name, i) =>
      makeSession({
        id: `sess-${i + 1}`,
        name,
        cwd: "/tmp/project-a",
      }),
    );
    window.history.pushState(null, "", "/empty-project");
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(sessions.map((session) => session.id)),
    );
    const h = await renderApp({
      seed: {
        sessions,
        projects: [{
          path: "/tmp/project-a",
          name: "project-a",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });

    expect(h.$(".empty-project")).not.toBeNull();
    expect(
      await waitFor(h, () => {
        const tabsText = h.$(".session-tabs")?.textContent ?? "";
        return tabsText.includes("One") && tabsText.includes("Two");
      }),
    ).toBe(true);

    await h.clickByText(/One/);

    expect(window.location.pathname).toBe("/s/sess-1");
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

  it("moves a sidebar-selected open session to the left under last-opened tab sort", async () => {
    const older = makeSession({
      id: "older-session",
      name: "Older session",
      cwd: "/tmp/project-a",
      updated_at: "2020-01-01T00:00:00.000Z",
      last_opened_at: "2020-01-01T00:00:00.000Z",
    });
    const newer = makeSession({
      id: "newer-session",
      name: "Newer session",
      cwd: "/tmp/project-a",
      updated_at: "2020-01-02T00:00:00.000Z",
      last_opened_at: "2020-01-02T00:00:00.000Z",
    });
    window.history.pushState(null, "", "/s/newer-session");
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify([older.id, newer.id]),
    );
    const h = await renderApp({ seed: { sessions: [newer, older] } });

    expect(await waitFor(h, () => tabIds(h)[0] === newer.id)).toBe(true);

    await h.selectSession(older.id);

    expect(await waitFor(h, () => tabIds(h)[0] === older.id)).toBe(true);
    h.unmount();
  }, 10000);

  it("moves a cached sidebar-selected session to the left without waiting for REST", async () => {
    const first = makeSession({
      id: "cached-first-session",
      name: "Cached first session",
      cwd: "/tmp/project-a",
      updated_at: "2020-01-01T00:00:00.000Z",
      last_opened_at: "2020-01-01T00:00:00.000Z",
    });
    const second = makeSession({
      id: "cached-second-session",
      name: "Cached second session",
      cwd: "/tmp/project-a",
      updated_at: "2020-01-02T00:00:00.000Z",
      last_opened_at: "2020-01-02T00:00:00.000Z",
    });
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify([first.id, second.id]),
    );
    const h = await renderApp({ seed: { sessions: [second, first] } });

    await h.selectSession(first.id);
    await h.selectSession(second.id);
    expect(await waitFor(h, () => tabIds(h)[0] === second.id)).toBe(true);

    const restCallsBefore = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === `/api/sessions/${first.id}`,
    ).length;
    await h.selectSession(first.id);

    expect(await waitFor(h, () => tabIds(h)[0] === first.id)).toBe(true);
    expect(
      h.restCalls.filter(
        (c) => c.method === "GET" && c.path === `/api/sessions/${first.id}`,
      ),
    ).toHaveLength(restCallsBefore);
    h.unmount();
  }, 10000);

  it("applies last-opened metadata patches to open tab ordering", async () => {
    const first = makeSession({
      id: "ws-first-session",
      name: "WS first session",
      cwd: "/tmp/project-a",
      updated_at: "2020-01-01T00:00:00.000Z",
      last_opened_at: "2020-01-01T00:00:00.000Z",
    });
    const second = makeSession({
      id: "ws-second-session",
      name: "WS second session",
      cwd: "/tmp/project-a",
      updated_at: "2020-01-02T00:00:00.000Z",
      last_opened_at: "2020-01-02T00:00:00.000Z",
    });
    window.history.pushState(null, "", "/s/ws-second-session");
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify([first.id, second.id]),
    );
    const h = await renderApp({ seed: { sessions: [second, first] } });

    expect(await waitFor(h, () => tabIds(h)[0] === second.id)).toBe(true);
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: first.id,
        patch: { last_opened_at: "2030-01-01T00:00:00.000Z" },
        originated_by: null,
      },
    });
    await h.flush();

    expect(await waitFor(h, () => tabIds(h)[0] === first.id)).toBe(true);
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
