import { describe, it, expect } from "vitest";
import "../src/i18n";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

describe("orchestration selector → /selectors PATCH", () => {
  it("an existing session has no orchestration-mode selector — mode is frozen at creation", async () => {
    // App.tsx's model-drift-sync effect explicitly does NOT patch
    // orchestration_mode: it's frozen at creation, and the selector shown
    // in NewSessionModal is a global preference for *new* sessions only.
    // No `.model-selector`-shaped control exists to change it for an
    // already-created session.
    const session = makeSession({ orchestration_mode: "manager" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const hasOrchestrationSelector = Array.from(
      h.raw.container.querySelectorAll<HTMLElement>(".model-selector"),
    ).some((w) =>
      w.querySelector("label")?.textContent?.toLowerCase().includes("orchestration"),
    );
    expect(hasOrchestrationSelector).toBe(false);
    h.unmount();
  });

  it("the workers panel is available via the Workers sidebar tab regardless of the session's orchestration mode", async () => {
    // workersTabAvailable (App.tsx) gates on the team extension + a cwd,
    // never on orchestration_mode — the panel mounts only once the Workers
    // tab is selected (defaults to the Sessions tab), independent of mode.
    // This file loads the real i18n bundle (see the project-suggestion
    // test below), so the tab's visible label is real copy, not a key.
    const session = makeSession({ orchestration_mode: "native" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();
    expect(h.toJSON().sidebar.workersPanelVisible).toBe(false);

    await h.clickByText("Workers");

    expect(h.toJSON().sidebar.workersPanelVisible).toBe(true);
    h.unmount();
  });

  it("session select with selectors matching localStorage default fires no PATCH", async () => {
    // App's localStorage default is orchestration_mode="manager" and cwd="".
    // A session that already matches those should NOT trigger a drift PATCH.
    const session = makeSession({ orchestration_mode: "manager", cwd: "" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const patches = h.backend.calls.filter(
      (c) =>
        c.method === "PATCH" && c.path === `/api/sessions/${session.id}/selectors`,
    );
    expect(patches).toHaveLength(0);
    h.unmount();
  });

  it("session select with selectors mismatching localStorage does NOT echo a stale PATCH", async () => {
    // Used to be a known-quirk drift PATCH closing over stale state
    // before the sync effect's setState committed. The split-fork
    // restructure (currentSession aliased through focusedForkId)
    // inserts an extra render between select and the drift gate,
    // letting the sync effect's values settle before the drift
    // comparison runs. Net: no spurious PATCH.
    const session = makeSession({ orchestration_mode: "native", cwd: "/tmp/proj" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const patches = h.backend.calls.filter(
      (c) =>
        c.method === "PATCH" && c.path === `/api/sessions/${session.id}/selectors`,
    );
    expect(patches.length).toBe(0);
    h.unmount();
  });

  it("moving a fresh session to a suggested project selects that project and session", async () => {
    const session = makeSession({
      id: "fresh",
      name: "Fresh",
      cwd: "/tmp/source",
      node_id: "node-a",
      messages: [],
    });
    const h = await renderApp({
      seed: {
        sessions: [session],
        projects: [
          {
            path: "/tmp/source",
            node_id: "node-a",
            name: "source",
            created_at: "2026-01-01T00:00:00.000Z",
            last_used: "2026-01-01T00:00:00.000Z",
          },
          {
            path: "/tmp/target",
            node_id: "node-a",
            name: "target",
            created_at: "2026-01-01T00:00:00.000Z",
            last_used: "2026-01-01T00:00:00.000Z",
          },
        ],
        projectSuggestion: {
          target_cwd: "/tmp/target",
          score: 0.91,
          margin: 0.4,
        },
      },
    });
    await h.selectSession(session.id);

    await h.typeAndSend("ship it");
    expect(h.raw.container.textContent).toContain("You sent this to: source");
    expect(h.raw.container.textContent).toContain("I think you meant to send it to: target");
    expect(h.raw.container.textContent).toContain("Send to source");
    expect(h.raw.container.textContent).toContain("Move to target");
    await h.clickByText("Move to target");

    const patches = h.backend.calls.filter(
      (c) => c.method === "PATCH" && c.path === "/api/sessions/fresh/selectors",
    );
    expect(patches.at(-1)?.body).toMatchObject({ cwd: "/tmp/target" });
    expect(localStorage.getItem("better-agent-selected-project")).toBe("/tmp/target");
    expect(localStorage.getItem("better-agent-selected-project-node")).toBe("node-a");
    expect(window.location.pathname).toBe("/s/fresh");
    expect(h.raw.container.querySelector('[data-session-id="fresh"]')?.getAttribute("data-active")).toBe("true");
    expect(h.outbound.find((f) => f.type === "send_message")).toMatchObject({
      app_session_id: "fresh",
      cwd: "/tmp/target",
    });
    h.unmount();
  });
});
