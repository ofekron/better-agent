import { describe, it, expect } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

function findOrchestrationSelect(container: HTMLElement): HTMLSelectElement {
  // Both ModelSelector and OrchestrationSelector use class `.model-selector`
  // — disambiguate by scanning for the one whose label says "Orchestration".
  const wrappers = container.querySelectorAll<HTMLElement>(".model-selector");
  for (const w of Array.from(wrappers)) {
    const label = w.querySelector("label");
    if (label?.textContent?.toLowerCase().includes("orchestration")) {
      const select = w.querySelector("select");
      if (select) return select as HTMLSelectElement;
    }
  }
  throw new Error("orchestration select not found");
}

describe("orchestration selector → /selectors PATCH", () => {
  it("changing the selector PATCHes /api/sessions/:id/selectors with the new mode", async () => {
    const session = makeSession({ orchestration_mode: "manager" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const select = findOrchestrationSelect(h.raw.container as HTMLElement);
    expect(select.value).toBe("manager");

    fireEvent.change(select, { target: { value: "native" } });
    await h.flush();

    const patches = h.backend.calls.filter(
      (c) =>
        c.method === "PATCH" && c.path === `/api/sessions/${session.id}/selectors`,
    );
    expect(patches.length).toBeGreaterThan(0);
    // The user's selector change is the most recent PATCH and carries native.
    expect(patches[patches.length - 1].body).toMatchObject({
      orchestration_mode: "native",
    });
    h.unmount();
  });

  it("after switching to native the workers panel disappears", async () => {
    const session = makeSession({ orchestration_mode: "manager" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    expect(h.toJSON().sidebar.workersPanelVisible).toBe(true);

    fireEvent.change(findOrchestrationSelect(h.raw.container as HTMLElement), {
      target: { value: "native" },
    });
    await h.flush();

    expect(h.toJSON().sidebar.workersPanelVisible).toBe(false);
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
    await h.clickByText("Move & send");

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
