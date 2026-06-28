import { describe, it, expect, vi } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession, makeWorker } from "./fixtures";

vi.mock("../src/components/extensionModuleLoader", async () => {
  const module = await import("../../better-agent-private/extensions/team-orchestration/ui/team-sidebar.entry.js");
  return { loadExtensionModule: async () => module };
});

async function openWorkersTab(h: Awaited<ReturnType<typeof renderApp>>) {
  let tab: HTMLElement | undefined;
  for (let i = 0; i < 1; i += 1) {
    tab = h.$$('button[role="tab"]').find((button) => button.textContent === "Workers");
    if (tab) break;
    await h.flush();
  }
  if (!tab) throw new Error(`Workers tab not found; calls=${h.restCalls.map((call) => call.path).join(",")}`);
  fireEvent.click(tab);
  await h.flush();
}

describe("workers panel actions", () => {
  it("workers panel shows the seeded worker count", async () => {
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        workers: [
          makeWorker({ agent_session_id: "w1", name: "Indexer" }),
          makeWorker({ agent_session_id: "w2", name: "Reviewer" }),
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    expect(h.toJSON().sidebar.workerCount).toBe(2);
    const panel = h.$('[data-testid="workers-panel"]');
    expect(panel?.textContent).toContain("Indexer");
    expect(panel?.textContent).toContain("Reviewer");
    h.unmount();
  });

  it("'+ New' opens the create form, submitting POSTs /api/workers", async () => {
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await openWorkersTab(h);
    await h.flush();

    const panel = h.$('[data-testid="workers-panel"]') as HTMLElement;
    const newBtn = Array.from(panel.querySelectorAll("button")).find(
      (b) => b.textContent === "+ New" || b.textContent === "workers.newButton",
    ) as HTMLButtonElement;
    fireEvent.click(newBtn);
    await h.flush();

    // Modal description input + Create button.
    const descInput = panel.querySelector(
      "input[type='text']",
    ) as HTMLInputElement;
    fireEvent.change(descInput, { target: { value: "Codebase explorer" } });
    const createBtn = Array.from(panel.querySelectorAll("button")).find(
      (b) => b.textContent === "Create" || b.textContent === "workers.createButton",
    ) as HTMLButtonElement;
    fireEvent.click(createBtn);
    await h.flush();

    const post = h.backend.calls.find(
      (c) => c.method === "POST" && c.path === "/api/workers",
    );
    expect(post).toBeDefined();
    expect(post!.body).toMatchObject({
      cwd: session.cwd,
      description: "Codebase explorer",
      orchestration_mode: "native",
    });
    h.unmount();
  });

  it("'Mark existing' is disabled when no eligible Better Agent sessions exist", async () => {
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        workers: [makeWorker({ agent_session_id: session.id })],
      },
    });
    await h.selectSession(session.id);
    await openWorkersTab(h);
    await h.flush();

    const panel = h.$('[data-testid="workers-panel"]') as HTMLElement;
    const markBtn = Array.from(panel.querySelectorAll("button")).find(
      (b) => b.textContent === "Mark existing" || b.textContent === "workers.markExisting",
    ) as HTMLButtonElement;
    expect(markBtn.disabled).toBe(true);
    h.unmount();
  });

  it("workers panel hidden in native-mode sessions", async () => {
    const session = makeSession({ orchestration_mode: "native" });
    const h = await renderApp({
      seed: { sessions: [session], workers: [makeWorker()] },
    });
    await h.selectSession(session.id);
    await openWorkersTab(h);
    await h.flush();

    expect(h.toJSON().sidebar.workersPanelVisible).toBe(false);
    h.unmount();
  });

  it("workers list shows delegation count badge per row", async () => {
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        workers: [
          makeWorker({ agent_session_id: "w1", name: "A", delegation_count: 7 }),
        ],
      },
    });
    await h.selectSession(session.id);
    await openWorkersTab(h);
    await h.flush();

    const row = h.$(".worker-row") as HTMLElement;
    fireEvent.click(row.querySelector(".worker-row-header") as HTMLElement);
    await h.flush();
    expect(row.textContent).toContain("7");
    expect(row.textContent).toMatch(/delegations|workers\.delegations/);
    h.unmount();
  });

  it("workers panel groups tagged workers into collapsible pools", async () => {
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({
      seed: {
        sessions: [session],
        workers: [
          makeWorker({ agent_session_id: "w1", name: "Reviewer A", tags: ["review"] }),
          makeWorker({ agent_session_id: "w2", name: "Reviewer B", tags: ["review"] }),
          makeWorker({ agent_session_id: "w3", name: "Builder", tags: ["build"] }),
        ],
      },
    });
    await h.selectSession(session.id);
    await openWorkersTab(h);
    await h.flush();

    const panel = h.$('[data-testid="workers-panel"]') as HTMLElement;
    expect(panel.textContent).toContain("Pool: review");
    expect(panel.textContent).toContain("Pool: build");

    const reviewHeader = Array.from(panel.querySelectorAll(".worker-group-header")).find(
      (el) => el.textContent?.includes("Pool: review"),
    ) as HTMLElement;
    fireEvent.click(reviewHeader);
    await h.flush();
    expect(panel.textContent).toContain("Reviewer A");
    expect(panel.textContent).toContain("Reviewer B");
    h.unmount();
  });
});
